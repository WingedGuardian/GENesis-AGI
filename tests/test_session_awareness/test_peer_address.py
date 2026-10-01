"""Tests for the session-id -> peer-name resolver.

Hermetic: every test builds its own registry directory, its own /proc tree and
its own machine id, so nothing here reads the real ~/.claude/sessions or /proc.
Values are synthetic throughout.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from genesis.session_awareness import peer_address as pa

MACHINE = "0123456789abcdef0123456789abcdef"
NS = "pid:[4026530001]"
OWN = f"linux:{MACHINE}:{NS}"
SID = "11111111-2222-3333-4444-555555555555"
OTHER_SID = "99999999-8888-7777-6666-555555555555"


class World:
    """A fake registry, /proc and machine id."""

    def __init__(self, root: Path) -> None:
        self.reg = root / "sessions"
        self.reg.mkdir()
        self.proc = root / "proc"
        (self.proc / "self" / "ns").mkdir(parents=True)
        os.symlink(NS, self.proc / "self" / "ns" / "pid")
        self.machine = root / "machine-id"
        self.machine.write_text(MACHINE + "\n")

    def proc_entry(self, pid: int, start: str, *, state: str = "S", comm: str = "claude") -> None:
        d = self.proc / str(pid)
        d.mkdir()
        fields = [state] + ["0"] * 18 + [start] + ["0"] * 5
        (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields) + "\n")

    def entry(self, pid: int, /, sid: str = SID, start: str = "1000", **extra) -> dict:
        obj = {
            "pid": pid,
            "sessionId": sid,
            "procStart": start,
            "pidDomain": OWN,
            "name": f"peer-{pid}",
            "tmux": f"cc-1:@{pid % 100}.%{pid % 100}",
            "messagingSocketPath": f"/run/sock-{pid}",
        }
        obj.update(extra)
        obj = {k: v for k, v in obj.items() if v is not _DROP}
        (self.reg / f"{pid}.json").write_text(json.dumps(obj))
        return obj

    def live(self, pid: int, /, sid: str = SID, start: str = "1000", **extra) -> dict:
        self.proc_entry(pid, start)
        return self.entry(pid, sid, start, **extra)

    def resolve(self, *sids: str) -> dict[str, pa.Resolution]:
        return pa.resolve_many(
            list(sids) or [SID],
            directory=self.reg,
            proc_root=self.proc,
            machine_id_path=self.machine,
        )

    def one(self, sid: str = SID) -> pa.Resolution:
        return self.resolve(sid)[sid]


_DROP = object()


@pytest.fixture
def w(tmp_path: Path) -> World:
    return World(tmp_path)


# -- the address ------------------------------------------------------------


def test_a_live_entry_resolves_to_its_name_and_pane(w: World) -> None:
    w.live(4242, name="genesis-7f", tmux="cc-2:@1.%1")
    r = w.one()
    assert (r.status, r.name, r.pane, r.pid) == ("ok", "genesis-7f", "cc-2:@1.%1", 4242)
    assert pa.render(r) == "-> genesis-7f (cc-2:@1.%1)"


def test_a_dead_pid_is_not_reachable_and_says_why(w: World) -> None:
    w.entry(4242)  # no /proc entry: the process is gone
    r = w.one()
    assert r.status == "not-reachable"
    assert r.detail == ("pid 4242: dead",)
    assert pa.render(r) == "-> (not reachable)"


def test_a_recycled_pid_is_not_mistaken_for_the_session(w: World) -> None:
    """Same pid, different start time: another process now owns the number."""
    w.proc_entry(4242, "2000")
    w.entry(4242, start="1000")
    r = w.one()
    assert r.status == "not-reachable"
    assert r.detail == ("pid 4242: recycled",)


@pytest.mark.parametrize(
    "domain",
    [f"linux:{MACHINE}:pid:[4026539999]", f"linux:{'f' * 32}:{NS}"],
    ids=["other-pid-namespace", "other-machine"],
)
def test_an_entry_from_another_pid_domain_is_never_trusted(w: World, domain: str) -> None:
    """Even with a live pid and a matching start time in THIS namespace."""
    w.live(4242, pidDomain=domain)
    r = w.one()
    assert r.status == "not-reachable"
    assert r.detail == ("pid 4242: other-domain",)


def test_a_different_session_on_a_live_pid_is_not_this_session(w: World) -> None:
    w.live(4242, sid=OTHER_SID)
    r = w.one()
    assert r.status == "not-reachable"
    assert r.detail == ("no registry entry",)


def test_a_resumed_session_resolves_to_its_current_pid(w: World) -> None:
    """The registry keeps the old, dead entry beside the new one."""
    w.entry(1111, name="old-name")
    w.live(2222, start="5000", name="new-name")
    r = w.one()
    assert (r.status, r.pid, r.name) == ("ok", 2222, "new-name")


def test_one_name_on_dead_entries_does_not_count_as_shared(w: World) -> None:
    w.entry(1111, sid=OTHER_SID, name="genesis-x")
    w.entry(1112, sid=OTHER_SID, name="genesis-x")
    w.live(2222, name="genesis-x")
    r = w.one()
    assert r.status == "ok" and r.shared_name is False


def test_a_name_two_live_sessions_share_is_flagged(w: World) -> None:
    w.live(2222, name="genesis-x")
    w.live(3333, sid=OTHER_SID, name="genesis-x")
    res = w.resolve(SID, OTHER_SID)
    assert all(r.shared_name for r in res.values())
    assert pa.render(res[SID]).endswith("(shared name)")


def test_two_live_entries_for_one_session_are_ambiguous_never_a_pick(w: World) -> None:
    w.live(2222, name="a")
    w.live(3333, name="b")
    r = w.one()
    assert (r.status, r.names) == ("ambiguous", ("a", "b"))
    assert r.name is None
    assert pa.render(r) == "-> (ambiguous)"


def test_a_live_entry_without_a_name_is_unnamed(w: World) -> None:
    w.live(2222, name=_DROP)
    r = w.one()
    assert (r.status, r.pid) == ("unnamed", 2222)
    assert pa.render(r) == "-> (no peer name)"


def test_a_zombie_is_dead(w: World) -> None:
    w.proc_entry(2222, "1000", state="Z")
    w.entry(2222)
    assert w.one().detail == ("pid 2222: dead",)


def test_a_process_name_containing_a_paren_does_not_shift_the_fields(w: World) -> None:
    w.proc_entry(2222, "1000", comm="evil) S 1 2 3")
    w.entry(2222)
    assert w.one().status == "ok"


# -- registry shape ---------------------------------------------------------


def test_a_torn_file_is_counted_not_silently_skipped(w: World) -> None:
    (w.reg / "3333.json").write_text('{"pid": 33')
    r = w.one()
    assert r.status == "not-reachable"
    assert "1 registry files unreadable" in r.detail


def test_an_oversized_file_is_not_parsed(w: World) -> None:
    (w.reg / "3333.json").write_text(" " * (pa._MAX_ENTRY_BYTES + 1) + "{}")
    assert "1 registry files unreadable" in w.one().detail


@pytest.mark.parametrize(
    "change",
    [
        {"procStart": 1000},
        {"procStart": "12a"},
        {"procStart": "1000\n"},
        {"procStart": "\u0661\u0662"},
        {"pidDomain": 3},
        {"pid": True},
        {"name": 7},
        {"tmux": ["x"]},
        {"spare": "yes"},
        {"parkedJobId": 5},
        {"messagingSocketPath": {"path": "/x"}},
    ],
    ids=[
        "int-procStart",
        "nondigit-procStart",
        "newline-procStart",
        "arabic-digit-procStart",
        "int-pidDomain",
        "bool-pid",
        "int-name",
        "list-tmux",
        "string-spare",
        "int-parkedJobId",
        "object-socket",
    ],
)
def test_a_matching_entry_in_an_unknown_shape_is_a_format_change(w: World, change: dict) -> None:
    """Loud, because a silent miss would read exactly like a peer that left."""
    w.proc_entry(2222, "1000")
    w.entry(2222, **change)
    r = w.one()
    assert r.status == "registry-format-changed"
    assert pa.render(r) == "-> (registry format changed)"


def test_a_live_entry_with_a_mistyped_session_id_poisons_every_miss(w: World) -> None:
    """It might be the entry asked for, so a miss cannot be called a miss."""
    w.proc_entry(2222, "1000")
    w.entry(2222, sessionId=12345)
    assert w.one().status == "registry-format-changed"


@pytest.mark.parametrize("missing", ["procStart", "pidDomain"])
def test_a_field_claude_code_treats_as_optional_is_unverifiable_not_an_alarm(
    w: World, missing: str
) -> None:
    """Claude Code's own reader treats both as optional, so their absence is a
    legitimate shape. Without them liveness cannot be checked, so it is not an
    address, but it is not a format change either."""
    w.live(2222, **{missing: _DROP})
    r = w.one()
    assert (r.status, r.detail) == ("not-reachable", ("pid 2222: unverifiable",))


def test_an_entry_without_a_session_id_is_simply_not_this_session(w: World) -> None:
    w.live(2222, sessionId=_DROP)
    assert w.one().detail == ("no registry entry",)


@pytest.mark.parametrize(
    ("extra", "why"),
    [
        ({"spare": True}, "spare"),
        ({"parkedJobId": "job1"}, "parked"),
        ({"messagingSocketPath": _DROP}, "no-socket"),
        ({"messagingSocketPath": ""}, "no-socket"),
    ],
    ids=["spare", "parked", "no-socket", "empty-socket"],
)
def test_entries_claude_code_will_not_message_are_not_addresses(
    w: World, extra: dict, why: str
) -> None:
    """Claude Code's lookup skips these even when the process is live."""
    w.live(2222, **extra)
    assert w.one().detail == (f"pid 2222: {why}",)


def test_a_session_moved_to_a_background_job_resolves_to_the_job(w: World) -> None:
    """The window's entry is parked; the job's entry, under its own session id,
    carries the jobId that the window names. That job is where messages go."""
    w.live(2222, name="window-name", parkedJobId="job1")
    w.live(3333, sid=OTHER_SID, start="3000", name="job-name", jobId="job1")
    r = w.one()
    assert (r.status, r.name, r.pid) == ("ok", "job-name", 3333)
    assert r.detail == ("moved to background job job1",)


def test_a_job_entry_is_only_followed_from_a_parked_entry(w: World) -> None:
    w.live(2222, name="window-name")
    w.live(3333, sid=OTHER_SID, start="3000", name="job-name", jobId="job1")
    assert w.one().name == "window-name"


def test_a_dead_entry_in_an_old_shape_is_ignored(w: World) -> None:
    """Dead entries are never cleaned up, so an old one must not poison forever."""
    w.entry(2222, procStart=5)  # mistyped, but no /proc entry: dead
    w.entry(3333, sessionId=7, pidDomain=f"linux:{MACHINE}:pid:[1]")  # another domain
    r = w.one()
    assert r.status == "not-reachable"


def test_credential_files_are_never_opened(w: World) -> None:
    """A directory named like a key file raises if anything opens it."""
    (w.reg / f"2222.{'a' * 64}.key").mkdir()
    (w.reg / "notes.json").mkdir()
    # A MISS, because only a miss reports unreadable files: an opened key
    # file would show up here as "registry files unreadable".
    r = w.one()
    assert r.status == "not-reachable"
    assert r.detail == ("no registry entry",)


def test_a_missing_registry_is_its_own_state_and_renders_nothing(tmp_path: Path) -> None:
    r = pa.resolve(SID, directory=tmp_path / "absent", proc_root=tmp_path)
    assert r.status == "no-registry"
    assert pa.render(r) == ""


def test_a_real_shape_entry_parses(w: World) -> None:
    """Pins every field this reader uses against the shape Claude Code 2.1.280
    writes (values synthetic; the extra keys are there to show they are
    tolerated)."""
    w.proc_entry(2222, "123456789")
    (w.reg / "2222.json").write_text(
        json.dumps(
            {
                "pid": 2222,
                "sessionId": SID,
                "cwd": "/work",
                "startedAt": 1700000000000,
                "procStart": "123456789",
                "version": "2.1.280",
                "peerProtocol": 1,
                "peerFeatures": ["notify_idle"],
                "kind": "interactive",
                "entrypoint": "cli",
                "pidDomain": OWN,
                "tmux": "work:@7.%9",
                "messagingSocketPath": "/tmp/sock",
                "name": "genesis-7f",
                "nameSource": "derived",
                "nameSince": 1700000000001,
                "status": "busy",
                "updatedAt": 1700000000002,
                "statusUpdatedAt": 1700000000002,
            }
        )
    )
    assert pa.render(w.one()) == "-> genesis-7f (work:@7.%9)"


# -- environment ------------------------------------------------------------


def test_the_registry_follows_claude_config_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert pa.registry_dir() == tmp_path / "cfg" / "sessions"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert pa.registry_dir() == Path.home() / ".claude" / "sessions"


def test_own_domain_is_spelled_as_claude_code_spells_it(w: World, tmp_path: Path) -> None:
    assert pa.own_pid_domain(w.proc, w.machine) == OWN
    # Claude Code substitutes "" for a part it cannot read; so must this.
    assert pa.own_pid_domain(tmp_path / "none", tmp_path / "none") == "linux::"


# -- rendering --------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["a|b", "a]b", "a\n[Concurrent | x] y", "A-upper", "x" * 49, "-leading", "genesis-7f\n"],
)
def test_a_name_outside_the_allowlist_is_omitted_whole(name: str) -> None:
    r = pa.Resolution(SID, "ok", name=name, pane="cc-1:@1.%1")
    out = pa.render(r)
    assert out == "-> (name not shown; see session_address)"
    assert name not in out


def test_the_longest_allowed_name_is_rendered_whole() -> None:
    name = "x" * 48
    assert pa.render(pa.Resolution(SID, "ok", name=name)) == f"-> {name}"


@pytest.mark.parametrize(
    "pane",
    [
        "cc 1:@1.%1",
        "a|b:@1.%1",
        "cc-1:@1.%1]",
        "x" * 33 + ":@1.%1",
        "cc-1:@1.%1\n",
        "cc-1:@\u0661.%1",
    ],
)
def test_a_pane_outside_the_allowlist_is_dropped_and_the_name_kept(pane: str) -> None:
    r = pa.Resolution(SID, "ok", name="genesis-7f", pane=pane)
    assert pa.render(r) == "-> genesis-7f"


def test_as_dict_carries_the_rendered_form() -> None:
    d = pa.Resolution(SID, "not-reachable", detail=("no registry entry",)).as_dict()
    assert d["display"] == "-> (not reachable)" and d["detail"] == ["no registry entry"]


# -- hostile registry contents ---------------------------------------------


def test_a_fifo_named_like_an_entry_cannot_hang_the_scan(w: World) -> None:
    """Opening a FIFO for reading blocks until a writer appears. The scan must
    not wait for one: the hook calls this on every prompt."""
    import threading

    os.mkfifo(w.reg / "7777.json")
    w.live(2222)
    box: dict = {}
    t = threading.Thread(target=lambda: box.update(r=w.one()), daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "resolve blocked on a FIFO"
    assert box["r"].status == "ok"


def test_a_symlinked_entry_is_not_followed(w: World, tmp_path: Path) -> None:
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps({"pid": 2222, "sessionId": SID}))
    os.symlink(real, w.reg / "2222.json")
    os.symlink("/dev/zero", w.reg / "3333.json")
    r = w.one()
    assert r.status == "not-reachable"
    assert "2 registry files unreadable" in r.detail


def test_a_scan_the_deadline_cut_short_answers_nothing(w: World) -> None:
    """A partial scan must not call a peer it never read 'not reachable'."""
    w.live(2222)
    res = pa.resolve_many(
        [SID], directory=w.reg, proc_root=w.proc, machine_id_path=w.machine, deadline=0.0
    )
    assert res[SID].status == "no-registry"
    assert res[SID].detail == ("registry scan ran out of time",)
    assert pa.render(res[SID]) == ""


def test_no_ids_reads_nothing(w: World, monkeypatch) -> None:
    monkeypatch.setattr(pa, "_load", lambda *a, **k: pytest.fail("read the registry"))
    assert pa.resolve_many([], directory=w.reg) == {}


def test_as_dict_releases_peer_text_only_in_its_allowlisted_form() -> None:
    """The MCP tool and --json return this dict to a model, so it carries the
    same allowlist the hook line does; the raw value never leaves."""
    bad = pa.Resolution(SID, "ok", name="x | y]\n[Concurrent | z]", pane="a|b:@1.%1")
    d = bad.as_dict()
    assert (d["name"], d["name_withheld"], d["pane"]) == (None, True, None)
    assert "Concurrent" not in json.dumps(d)
    good = pa.Resolution(SID, "ok", name="genesis-7f", pane="cc-1:@1.%1").as_dict()
    assert (good["name"], good["name_withheld"], good["pane"]) == (
        "genesis-7f",
        False,
        "cc-1:@1.%1",
    )
    amb = pa.Resolution(SID, "ambiguous", names=("a", "b|c")).as_dict()
    assert amb["names"] == ["a", None]


def test_an_unreadable_file_for_a_running_pid_makes_any_answer_ambiguous(w: World) -> None:
    """That file could be a second live entry for the same session."""
    w.live(2222, name="genesis-7f")
    w.proc_entry(3333, "3000")
    (w.reg / "3333.json").write_text('{"pid": 33')  # torn, but pid 3333 runs
    r = w.one()
    assert r.status == "ambiguous"
    assert r.detail == ("pid 3333: registry file unreadable, process running",)


def test_an_unreadable_file_for_a_dead_pid_does_not_block_the_answer(w: World) -> None:
    w.live(2222)
    (w.reg / "3333.json").write_text('{"pid": 33')  # no /proc/3333
    assert w.one().status == "ok"


def test_a_job_id_outside_the_allowlist_is_not_echoed(w: World) -> None:
    w.live(2222, name="window-name", parkedJobId="job\n[Concurrent | x]")
    w.live(3333, sid=OTHER_SID, start="3000", name="job-name", jobId="job\n[Concurrent | x]")
    r = w.one()
    assert (r.status, r.name) == ("ok", "job-name")
    assert r.detail == ("moved to a background job",)


@pytest.mark.parametrize("calls_before_expiry", [0, 1, 2])
def test_the_deadline_is_honoured_inside_the_proc_loops(
    w: World, monkeypatch, calls_before_expiry: int
) -> None:
    """Expiry part-way through the /proc work must not yield a half-made answer."""
    w.live(2222)
    w.proc_entry(3333, "3000")
    (w.reg / "3333.json").write_text('{"pid": 33')
    w.entry(4444, procStart=5)  # malformed, so the suspect loop runs
    calls = {"n": 0}

    def expired(_deadline):
        calls["n"] += 1
        return calls["n"] > calls_before_expiry

    monkeypatch.setattr(pa, "_expired", expired)
    r = pa.resolve_many(
        [SID], directory=w.reg, proc_root=w.proc, machine_id_path=w.machine, deadline=1e18
    )[SID]
    assert (r.status, r.detail) == ("no-registry", ("registry scan ran out of time",))


def test_a_torn_read_of_the_only_entry_is_not_called_not_reachable(w: World) -> None:
    """Claude Code rewrites a session's own file in place, so a torn read most
    likely hides the very entry asked for: undecided, not absent."""
    w.proc_entry(2222, "1000")
    (w.reg / "2222.json").write_text('{"pid": 22')
    r = w.one()
    assert r.status == "ambiguous"
    assert r.detail == ("pid 2222: registry file unreadable, process running",)


@pytest.mark.parametrize(
    ("mtime", "status"),
    [(1_000_000_000.0, "ok"), (1_000_000_100.0, "ambiguous")],
    ids=["written-before-process-started", "written-after"],
)
def test_an_unreadable_file_older_than_its_pid_cannot_hide_a_peer(
    w: World, mtime: float, status: str
) -> None:
    """A file last written before the process now holding that pid number was
    born belongs to an earlier process, so it must not poison every answer."""
    (w.proc / "stat").write_text("cpu 1 2 3\nbtime 1000000000\n")
    w.live(2222)
    w.proc_entry(3333, "1000")  # born at btime + 1000 ticks
    torn = w.reg / "3333.json"
    torn.write_text('{"pid": 33')
    os.utime(torn, (mtime, mtime))
    assert w.one().status == status


@pytest.mark.parametrize(
    ("running", "miss_status"), [(False, "not-reachable"), (True, "registry-format-changed")]
)
def test_a_non_object_entry_is_judged_by_the_pid_in_its_file_name(
    w: World, running: bool, miss_status: str
) -> None:
    """`[]` has no pid field, so the file name's pid decides whether it could be
    a live peer: dead, it is ignored; running, every miss stays loud."""
    w.live(2222)
    if running:
        w.proc_entry(7777, "7000")
    (w.reg / "7777.json").write_text("[]")
    res = w.resolve(SID, OTHER_SID)
    assert res[SID].status == "ok"
    assert res[OTHER_SID].status == miss_status
