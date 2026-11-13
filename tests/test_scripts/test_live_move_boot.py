"""The boot step: genesis-server's ExecStartPre runs the installed kit (#3492 D2a-2).

The kit judges the update state before the server starts and heals a `live`
checkout a killed switch left half-written. A tree it judged half-written and
could not heal keeps the server down (exit 75); everything else starts, with
one page for each state the start goes past. Every run is bounded by one
absolute deadline inside systemd's TimeoutStartSec. Real git, scratch HOME, the
kit installed into it (World.kit), as in test_live_move_repair.py.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from tests.test_scripts._live_repair_world import (
    GUARD,
    KIT_FILES,
    LIB,
    MODULE,
    World,
    _installer,
    killed_rollback,
    killed_tag_side,
)
from tests.test_scripts.test_live_move_repair import (
    _CANDIDATE_HOOK,
    FakeClock,
    _hold,
    _install,
    _real_engine_world,
    _symlink_world,
)

REFUSE = 75


def _kit(w: World, **constants: float) -> Path:
    """The installed kit, with live_move_boot.py's ``constants`` set (when the
    kit has that file) and KIT.json written again over the result."""
    kit = w.kit()
    boot = kit / "live_move_boot.py"
    if constants and boot.exists():
        text = boot.read_text()
        for name, value in constants.items():
            text, n = re.subn(rf"^{name} = .*$", f"{name} = {value!r}", text, flags=re.M)
            assert n == 1, name
        boot.write_text(text)
    names = [n for n in KIT_FILES if (kit / n).exists()]
    (kit / "KIT.json").write_text(json.dumps(_installer().describe(str(kit), names)))
    return kit


def _run_kit(w: World, kit: Path, timeout: float = 120, **env: str):
    return subprocess.run(
        ["python3", "-I", "-S", "-B", str(kit / MODULE.name), "--boot", str(w.state), str(w.root)],
        capture_output=True, text=True, env=w.env(**env), timeout=timeout,
    )  # fmt: skip


def _main(w: World, mod, monkeypatch) -> int:
    """main --boot in-process, the module's directory pointed at the kit."""
    monkeypatch.setattr(mod, "_LIB", str(w.kit()))
    return mod.main(["--boot", str(w.state), str(w.root)])


def _keys(w: World) -> list[str]:
    return [a["dedupe_key"] for a in w.alerts()]


def _lines_ok(stderr: str) -> None:
    """Every boot line is the guardian's: prefixed, and under its 400-char cut."""
    for line in filter(None, stderr.splitlines()):
        if not line.startswith(" "):
            assert line.startswith("live-move repair:"), line
            assert len(line) <= 400, (len(line), line)


def _gone(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    return False


# ── T-D31: the boot self-check of the installed kit ──────────────────────────


@pytest.mark.parametrize("name", [n for n in KIT_FILES if n != MODULE.name])
def test_td31_a_kit_file_missing_skips_the_boot_repair_with_exit_3(w, name):
    """A partial kit never runs: a missing shell lib would hold the server down
    on every start (C4), a missing guard would skip silently. Exit 3 is the
    unit shell's "start + page"; nothing is judged or written."""
    killed_tag_side(w, "Z3")
    kit = w.kit()
    (kit / name).unlink()
    before, state = w.snapshot(), w.state.read_bytes()
    r = _run_kit(w, kit)
    assert r.returncode == 3, (r.returncode, r.stderr)
    assert "the installed kit is incomplete or altered" in r.stderr, r.stderr
    assert "live_repair_kit.py install" in r.stderr, r.stderr
    assert w.backups() == [] and w.snapshot() == before and w.state.read_bytes() == state
    _lines_ok(r.stderr)


@pytest.mark.parametrize(
    "change", ["byte", "kit_json_missing", "older_guard", "kit_json_list", "kit_json_null"]
)
def test_td31_an_altered_kit_skips_the_boot_repair_with_exit_3(w, change):
    killed_tag_side(w, "Z3")
    kit = w.kit()
    if change == "byte":
        data = bytearray((kit / "deploy_marker.sh").read_bytes())
        data[-2] ^= 0x01
        (kit / "deploy_marker.sh").write_bytes(bytes(data))
    elif change == "kit_json_missing":
        (kit / "KIT.json").unlink()
    elif change.startswith("kit_json_"):  # valid JSON, not an object (#4501 review)
        (kit / "KIT.json").write_text("[]" if change == "kit_json_list" else "null")
    else:
        # A guard from before #3987 (no bounded read_state): a version mix.
        text = GUARD.read_text()
        (kit / GUARD.name).write_text(text.replace("def read_state(", "def _read_state_gone(", 1))
    r = _run_kit(w, kit)
    assert r.returncode == 3, (r.returncode, r.stderr)
    assert "incomplete or altered" in r.stderr and w.backups() == [], r.stderr


def test_td31_a_whole_kit_passes_the_self_check(w):
    killed_tag_side(w, "Z3")
    r = w.repair(mode="--boot")
    assert r.returncode == 0 and not w.state.exists(), r.stderr
    assert "incomplete or altered" not in r.stderr


def test_the_boot_run_holds_its_kit_against_the_installers_prune(w, mod, monkeypatch):
    """Section 13 / F9: the installer prunes a versioned kit only when it can
    take KIT.json exclusively; the boot run holds it shared while it runs."""
    kit = w.kit()
    monkeypatch.setattr(mod, "_LIB", str(kit))
    assert mod._kit_hold() is None and mod._kit_check() is None
    fd = os.open(kit / "KIT.json", os.O_RDONLY)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


def test_a_command_run_holds_its_kit_from_before_the_guard_until_it_exits(w, mod, monkeypatch):
    """Review SF-A: repair-move (command mode) loads kit siblings lazily and may
    wait on update.lock for hours; its kit dir must survive the installer's
    prune all that time, so the shared hold is taken before the guard loads,
    in both modes (the hash self-check stays boot-only)."""
    killed_tag_side(w, "Z3")
    kit = w.kit()
    monkeypatch.setattr(mod, "_LIB", str(kit))
    seen = []

    def exclusive_refused() -> bool:
        fd = os.open(kit / "KIT.json", os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)
        return False

    real_guard, real_finish = mod._load_guard, mod.Repair._finish

    def guard():
        seen.append(("guard", exclusive_refused()))
        return real_guard()

    def finish(self, *a, **kw):
        seen.append(("finish", exclusive_refused()))
        _installer()._remove(str(kit))  # the prune of a held kit keeps it
        return real_finish(self, *a, **kw)

    monkeypatch.setattr(mod, "_load_guard", guard)
    monkeypatch.setattr(mod.Repair, "_finish", finish)
    assert mod.main(["--command", str(w.state), str(w.root)]) == 0
    assert seen == [("guard", True), ("finish", True)], seen
    assert (kit / "KIT.json").exists() and not w.state.exists()
    assert exclusive_refused(), "the hold ended before the process did"


def test_a_command_run_from_a_checkout_needs_no_kit_json(w):
    """scripts/lib has no KIT.json: by command the repair still runs there."""
    killed_tag_side(w, "Z3")
    assert not (LIB / "KIT.json").exists()
    r = w.repair()
    assert r.returncode == 0 and not w.state.exists(), r.stdout + r.stderr


# ── T-D36: Q2 meets the pre-change rule (W6a) ────────────────────────────────


def test_td36_head_moved_before_the_heal_stays_down(w, mod, monkeypatch):
    killed_tag_side(w, "Z3")

    def moved(self, backup):
        raise mod.RepairError("HEAD moved while the repair judged it")

    monkeypatch.setattr(mod.Repair, "_keep_index", moved)
    assert _main(w, mod, monkeypatch) == REFUSE
    assert w.state.exists() and [a["severity"] for a in w.alerts()] == ["critical"]


def test_td36_a_target_lookup_failing_after_a_mixed_verdict_stays_down(w, mod, monkeypatch):
    """#4501 review (Codex P1): the guard judged the tree half-written, then
    resolving the move's target raised (the boot deadline in its git call).
    That is a failure after judging: the service stays down, it never starts."""
    killed_tag_side(w, "Z3")

    def lookup(self, state):
        raise mod.RepairError("the boot deadline passed (git rev-parse)")

    monkeypatch.setattr(mod.Repair, "_live_target", lookup)
    assert _main(w, mod, monkeypatch) == REFUSE
    assert [a["severity"] for a in w.alerts()] == ["critical"]


def _crash_partway(mod, monkeypatch) -> None:
    def partway(self, *a, **kw):  # fails as it starts to change the checkout
        mod._changed = True
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(mod.Repair, "_apply", partway)


def _next_start(mod, monkeypatch) -> None:
    monkeypatch.setattr(mod, "_changed", False)
    monkeypatch.setattr(mod, "_healing", False)
    time.sleep(1.1)  # the next heal's report dir is named by the second and this pid


def test_td36_a_heal_that_crashed_partway_twice_is_not_healed_a_third_time(w, mod, monkeypatch):
    """Review SF-2 + owner ruling: a heal that raised after the checkout began to
    change ends unresolved in its REPAIR.json with one retry owed. The next start
    retries once; a second crash refuses at every later start, with no new
    backup, until the tree or the kit changes."""
    killed_tag_side(w, "Z3")
    _crash_partway(mod, monkeypatch)
    assert _main(w, mod, monkeypatch) == REFUSE
    assert len(w.backups()) == 1
    report = w.report()
    assert report["result"].startswith("unresolved: the boot repair failed partway (OSError")
    _next_start(mod, monkeypatch)
    assert _main(w, mod, monkeypatch) == REFUSE
    assert len(w.backups()) == 2, "the one retry a crashed heal is owed did not run"
    keys = _keys(w)
    _next_start(mod, monkeypatch)
    assert _main(w, mod, monkeypatch) == REFUSE
    assert len(w.backups()) == 2, "a heal that crashed twice was tried a third time"
    assert all(":unresolved:" in k for k in _keys(w)[len(keys) :]), _keys(w)


@pytest.mark.parametrize("how", ["unknown_mark", "killed_no_result"])
def test_td36_the_one_retry_holds_when_the_crash_left_no_readable_mark(w, mod, monkeypatch, how):
    """#4501 round 3 (Codex P2): a heal that ran out of time records its end mark
    as "unknown" (no git past the deadline), and a heal killed outright records
    no result at all. Attempts are counted from the reports, and an unreadable
    mark proves no change: two backups, then every start refuses."""
    killed_tag_side(w, "Z3")
    if how == "unknown_mark":
        _crash_partway(mod, monkeypatch)
        real_mark = mod._end_mark
        # "unknown" only while the crash is recorded (past the deadline); the
        # next start reads a real mark, as Codex's reproduction does.
        monkeypatch.setattr(
            mod, "_end_mark", lambda repo: "unknown" if mod._changed else real_mark(repo)
        )
    else:

        def killed(self, *a, **kw):  # the run dies before any result is written
            mod._changed = True
            raise SystemExit(137)

        monkeypatch.setattr(mod.Repair, "_apply", killed)
    for _ in range(3):
        with contextlib.suppress(SystemExit):
            _main(w, mod, monkeypatch)
        _next_start(mod, monkeypatch)
    assert len(w.backups()) == 2, f"{len(w.backups())} backups: the one retry did not hold"


def test_td36_a_crash_after_a_proven_checkout_change_gets_its_own_retry(w, mod, monkeypatch):
    """#4501 round 4 (CodeRabbit): two crashes refuse; then the checkout changes
    from outside, the heal runs, and crashes again. That changed tree is owed its
    one retry: the count stops where a crash's end mark differs from the next
    heal's start mark."""
    killed_tag_side(w, "Z3")
    _crash_partway(mod, monkeypatch)
    for _ in range(3):
        _main(w, mod, monkeypatch)
        _next_start(mod, monkeypatch)
    assert len(w.backups()) == 2
    (w.root / "outside-edit.txt").write_text("changed while the server was down\n")
    _main(w, mod, monkeypatch)  # the changed tree: a new heal, which crashes
    _next_start(mod, monkeypatch)
    assert len(w.backups()) == 3
    _main(w, mod, monkeypatch)  # its one retry
    _next_start(mod, monkeypatch)
    assert len(w.backups()) == 4, "a crash on a changed checkout got no retry"
    _main(w, mod, monkeypatch)
    assert len(w.backups()) == 4, "the retry after a checkout change did not hold"


def test_td36_crashes_that_each_changed_the_tree_still_stop_after_one_retry(w, mod, monkeypatch):
    """A heal that crashes partway changes the checkout itself. Its own change is
    no change from outside: each crash leaves a different tree, and still only
    one retry runs (an end-to-end-mark boundary would retry forever)."""
    killed_tag_side(w, "Z3")
    runs = []

    def partway(self, *a, **kw):
        mod._changed = True
        runs.append(1)
        (w.root / f"half-written-{len(runs)}.txt").write_text("left by a crashed heal\n")
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(mod.Repair, "_apply", partway)
    for _ in range(4):
        _main(w, mod, monkeypatch)
        _next_start(mod, monkeypatch)
    assert len(w.backups()) == 2, f"{len(w.backups())} backups: a heal's own change reset the count"


def test_td25_an_unresolved_heal_with_an_unknown_end_mark_is_not_healed_again(
    tmp_path, monkeypatch
):
    """#4501 round 3 class audit: an F4 end whose end mark the deadline left
    unknown still refuses the next start (an unknown mark is no proof of a change)."""
    w = _symlink_world(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x").write_text("not the checkout's\n")
    w.write_state(w.row("partial"))
    assert w.repair(mode="--boot").returncode == REFUSE
    report_path = w.backups()[-1] / "REPAIR.json"
    data = json.loads(report_path.read_text())
    data["end_mark"] = "unknown"
    report_path.write_text(json.dumps(data, indent=2))
    again = w.repair(mode="--boot")
    assert again.returncode == REFUSE, again.stderr
    assert len(w.backups()) == 1, "an unknown end mark reopened the heal"


def test_td36_a_crashed_heal_retried_once_heals(w, mod, monkeypatch):
    """The one retry is what heals a one-time failure (a disk briefly full)."""
    killed_tag_side(w, "Z3")
    real = mod.Repair._apply
    _crash_partway(mod, monkeypatch)
    assert _main(w, mod, monkeypatch) == REFUSE
    monkeypatch.setattr(mod.Repair, "_apply", real)
    _next_start(mod, monkeypatch)
    assert _main(w, mod, monkeypatch) == 0
    assert not w.state.exists(), "the retry did not heal"


def test_td36_a_failure_before_judging_starts_with_a_page(w, mod, monkeypatch):
    """V15: the guard cannot load after the self-check passed (nothing judged):
    start, and page because a state file exists."""
    killed_tag_side(w, "Z3")

    def broken():
        raise ImportError("the guard raised at import")

    monkeypatch.setattr(mod, "_load_guard", broken)
    assert _main(w, mod, monkeypatch) == 0
    assert w.backups() == [] and w.state.exists()
    keys = _keys(w)
    assert len(keys) == 1 and keys[0].startswith("live_move_repair:boot:skipped:"), keys


def test_td36_no_state_and_a_failure_pages_nothing(w, mod, monkeypatch):
    def broken():
        raise ImportError("x")

    monkeypatch.setattr(mod, "_load_guard", broken)
    assert _main(w, mod, monkeypatch) == 0
    assert w.alerts() == []


@pytest.mark.parametrize("unit", ["genesis-server", "genesis-bridge"])
def test_a_boot_page_names_the_unit_that_runs_it(w, unit):
    """The same kit runs in genesis-server's and genesis-bridge's start step:
    each page names its own unit (the templates pass it as GENESIS_BOOT_UNIT)."""
    killed_tag_side(w, "Z3")
    root = w.gen / "deploy-backups"
    root.mkdir()
    root.chmod(0o500)
    try:
        r = w.repair(mode="--boot", GENESIS_BOOT_UNIT=unit)
    finally:
        root.chmod(0o755)
    assert r.returncode == REFUSE, r.stderr
    [alert] = w.alerts()
    other = {"genesis-server", "genesis-bridge"} - {unit}
    for text in (alert["title"], alert["body"], r.stderr):
        assert unit in text and not any(o in text for o in other), text
    for name in ("genesis-server", "genesis-bridge"):
        tpl = (LIB.parent / "systemd" / f"{name}.service.template").read_text()
        assert "\nEnvironment=GENESIS_BOOT_UNIT=%N\n" in tpl, name


# ── T-D26: the held-down arms page, and their lines fit the guardian's cut ───


def test_td26_neither_at_the_tag_nor_on_live_at_the_target_stays_down(w, mod, monkeypatch, capfd):
    """V10: reached only by a race (HEAD moved between the guard and the repair)."""
    killed_tag_side(w, "Z3")
    real = mod._out

    def elsewhere(repo, *args, **kw):
        if args == ("rev-parse", "--verify", "-q", "HEAD^{commit}"):
            return "0" * 40
        return real(repo, *args, **kw)

    monkeypatch.setattr(mod, "_out", elsewhere)
    assert _main(w, mod, monkeypatch) == REFUSE
    keys = _keys(w)
    assert len(keys) == 1 and keys[0].startswith("live_move_repair:boot:refused:"), keys
    _lines_ok(capfd.readouterr().err)


# ── T-D21: every state the start goes past pages once ────────────────────────


def _guard_message(w: World, state_path: Path) -> str:
    code = (
        "import importlib.util, sys; s = importlib.util.spec_from_file_location('g', sys.argv[1]); "
        "g = importlib.util.module_from_spec(s); s.loader.exec_module(g); "
        "v, r = g.verdict(sys.argv[2], sys.argv[3]); print(g.message(sys.argv[2], v, r))"
    )
    return subprocess.run(
        ["python3", "-I", "-S", "-c", code, str(GUARD), str(state_path), str(w.root)],
        capture_output=True, text=True, env=w.env(), check=True,
    ).stdout.strip()  # fmt: skip


def test_td21_a_hold_at_boot_starts_and_pages_the_guards_remedy(w):
    w.write_state(w.row())
    (w.root / "K").write_text("my edit\n")
    assert w.guard_rc() == 6, "control: HOLD"
    expected = _guard_message(w, w.state)
    r = w.repair(mode="--boot")
    assert r.returncode == 0, r.stderr
    alerts = w.alerts()
    assert len(alerts) == 1, alerts
    assert alerts[0]["severity"] == "warning"
    assert alerts[0]["dedupe_key"].startswith("live_move_repair:boot:kept:")
    assert expected in alerts[0]["body"] and "started anyway" in alerts[0]["body"]
    again = w.repair(mode="--boot")
    assert again.returncode == 0 and len(w.alerts()) == 1, "the same state paged twice"


def test_td21_the_3722_shape_names_live_and_never_advises_post_merge(w):
    """A `live` rollback killed after `merging` (here: phase bootstrap, HEAD at
    own_head on `live`, the tree part-restored): the repair does not heal that
    phase yet. Start (Q5) with a critical page in `live`'s words."""
    killed_rollback(w, "P")
    state = {k: v for k, v in w.row().items() if k != "live_move"}
    w.write_state({**state, "phase": "bootstrap", "own_head": w.to})
    assert w.guard_rc(json.loads(w.state.read_text())) == 4, "control: half-written"
    r = w.repair(mode="--boot")
    assert r.returncode == 0, r.stderr
    alerts = w.alerts()
    assert len(alerts) == 1 and alerts[0]["severity"] == "critical", alerts
    body = alerts[0]["body"]
    assert "`live`" in body and "#3722" in body and "--post-merge" not in body, body
    assert alerts[0]["dedupe_key"].startswith("live_move_repair:boot:mixed:")
    assert "on main" not in r.stderr, r.stderr


def test_td21_a_main_mixed_tree_starts_with_a_critical_page(w):
    (w.root / "F").write_text("f\n")
    w.write_state(
        {"phase": "merging", "rollback_tag": "pre", "deploy_head": w.to, "pid": 2_147_483_647}
    )
    r = w.repair(mode="--boot")
    assert r.returncode == 0, r.stderr
    alerts = w.alerts()
    assert [a["severity"] for a in alerts] == ["critical"], alerts
    assert alerts[0]["dedupe_key"].startswith("live_move_repair:boot:mixed:")


def _held_index_lock(w: World):
    lock = w.root / ".git" / "index.lock"
    lock.touch()
    return lock, subprocess.Popen(["bash", "-c", 'exec 3<"$1"; sleep 600', "hold", str(lock)])


@pytest.mark.parametrize(
    ("kind", "lock", "rc", "page"),
    [
        ("holds", "index", 0, ("warning", "kept")),
        ("holds", "checkout", 0, ("warning", "kept")),
        ("mixed", "index", 1, ("critical", "refused")),
        ("safe", "index", 0, None),
        ("main_mixed", "checkout", 0, ("critical", "mixed")),
    ],
)
def test_td21_a_lock_held_past_the_budget_is_judged_read_only(
    w, mod, monkeypatch, kind, lock, rc, page
):
    """F2 + SF5: a lock still held after the wait. The tree is judged read-only:
    a `live` row judged half-written stays down; a main checkout starts (V9);
    another guarding verdict starts with a page; SAFE starts silently."""
    if kind == "mixed":
        killed_tag_side(w, "Z3")
    elif kind == "main_mixed":
        (w.root / "F").write_text("f\n")
        w.write_state({"phase": "merging", "rollback_tag": "pre", "deploy_head": w.to, "pid": 1})
    else:
        w.write_state(w.row())
        if kind == "holds":
            (w.root / "K").write_text("my edit\n")
    if kind == "safe":
        assert w.guard_rc() == 10
    holder, fd = None, None
    if lock == "index":
        path, holder = _held_index_lock(w)
        deadline = time.monotonic() + 30
        while not mod._held_open(str(path)):
            assert time.monotonic() < deadline, "the holder never opened the lock"
            time.sleep(0.05)
    else:
        fd = _hold(w.root / ".git" / "genesis-checkout.lock")
    clock = FakeClock()
    monkeypatch.setattr(mod, "_now", clock.now)
    monkeypatch.setattr(mod, "_sleep", clock.sleep)
    try:
        assert mod.Repair(str(w.state), str(w.root), True, 0).run() == rc
    finally:
        if holder:
            holder.kill()
            holder.wait()
        if fd is not None:
            os.close(fd)
    assert w.backups() == []
    alerts = w.alerts()
    if page is None:
        assert alerts == []
    else:
        assert [(a["severity"], a["dedupe_key"].split(":")[2]) for a in alerts] == [page], alerts


def test_td21_a_held_lock_target_lookup_failing_after_a_mixed_verdict_stays_down(
    w, mod, monkeypatch
):
    """#4501 round 2 (Codex P1): the held-lock path judged a `live` row
    half-written, then resolving its target raised (git under the deadline).
    The judgment is latched from the state before that lookup: exit 75."""
    killed_tag_side(w, "Z3")
    fd = _hold(w.root / ".git" / "genesis-checkout.lock")
    clock = FakeClock()
    monkeypatch.setattr(mod, "_now", clock.now)
    monkeypatch.setattr(mod, "_sleep", clock.sleep)

    def lookup(self, state):
        raise mod.RepairError("the boot deadline passed (git rev-parse)")

    monkeypatch.setattr(mod.Repair, "_live_target", lookup)
    try:
        assert _main(w, mod, monkeypatch) == REFUSE
    finally:
        os.close(fd)
    assert w.backups() == [] and w.state.exists()


def test_a_failure_after_a_successful_heal_starts(w, mod, monkeypatch):
    """#4501 round 2 class audit: once the heal returned 0 the tree is healed,
    so a failure in the hooks retry after it starts (with a page), never 75."""
    killed_tag_side(w, "Z3")

    def broken(self, owed):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(mod.Repair, "_retry_pending", broken)
    assert _main(w, mod, monkeypatch) == 0
    assert not w.state.exists(), "the heal did not run first"


def test_a_checkout_lock_past_the_wait_by_command_refuses_without_judging(w, mod, monkeypatch):
    """By command a held checkout lock refuses (1) whatever the tree, as before
    D2a-2: the read-only judging of the lock arms is the boot step's."""
    w.write_state(w.row())
    calls = []
    real = mod.Repair.__init__

    def init(self, *a, **kw):
        real(self, *a, **kw)
        verdict = self.guard.verdict
        self.guard.verdict = lambda *x, **k: calls.append(1) or verdict(*x, **k)

    monkeypatch.setattr(mod.Repair, "__init__", init)
    fd = _hold(w.root / ".git" / "genesis-checkout.lock")
    clock = FakeClock()
    monkeypatch.setattr(mod, "_now", clock.now)
    monkeypatch.setattr(mod, "_sleep", clock.sleep)
    try:
        assert mod.Repair(str(w.state), str(w.root), False, 10).run() == 1
    finally:
        os.close(fd)
    assert calls == [], "the command judged the tree on a held checkout lock"


# ── T-D37: one page per event (F6) ───────────────────────────────────────────


def test_td37_a_heal_that_keeps_the_state_and_the_next_boot_page_once(w):
    killed_tag_side(w, "Z3")
    (w.root / "K").write_text("my edit\n")
    first = w.repair(mode="--boot")
    assert first.returncode == 0, first.stderr
    assert w.report()["result"].startswith("healed, state kept"), w.report()["result"]
    keys = _keys(w)
    assert len(keys) == 1 and keys[0].startswith("live_move_repair:boot:kept:"), keys
    second = w.repair(mode="--boot")
    assert second.returncode == 0 and "nothing to repair" in second.stderr, second.stderr
    assert _keys(w) == keys, "the HOLD the heal left paged a second time"
    (w.root / "K").write_text("keep\n")
    w.state.unlink()  # a Dismiss
    assert w.repair(mode="--boot").returncode == 0
    assert _keys(w) == keys


# ── T-D25: an unresolved heal is not redone while nothing changed ────────────


def test_td25_an_unresolved_heal_repeats_without_a_new_backup(tmp_path):
    w = _symlink_world(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x").write_text("not the checkout's\n")
    w.write_state(w.row("partial"))
    first = w.repair(mode="--boot")
    assert first.returncode == REFUSE, (first.returncode, first.stderr)
    assert len(w.backups()) == 1 and w.report()["result"].startswith("unresolved")
    keys = _keys(w)
    assert len(keys) == 1 and keys[0].startswith("live_move_repair:boot:unresolved:"), keys
    second = w.repair(mode="--boot")
    assert second.returncode == REFUSE, second.stderr
    assert len(w.backups()) == 1, "nothing changed, yet a new backup was made"
    assert _keys(w) == keys
    _lines_ok(second.stderr)
    # Review SF-B: a newly installed kit (another kit_id) tries again, the
    # checkout unchanged: it may be the fix.
    kit = w.kit()
    with (kit / "alert_queue.sh").open("a") as fh:
        fh.write("# a newer kit\n")
    names = [n for n in KIT_FILES if (kit / n).exists()]
    (kit / "KIT.json").write_text(json.dumps(_installer().describe(str(kit), names)))
    newer = _run_kit(w, kit)
    assert newer.returncode == REFUSE, newer.stderr
    assert len(w.backups()) == 2, "a new kit did not try the heal again"
    assert w.report()["kit_id"] == json.loads((kit / "KIT.json").read_text())["kit_id"]
    again = _run_kit(w, kit)
    assert again.returncode == REFUSE and len(w.backups()) == 2, "the same new kit healed twice"
    (w.root / "new-untracked").write_text("a change\n")
    third = _run_kit(w, kit)
    assert len(w.backups()) == 3, "the tree changed, and the heal was not tried again"
    assert third.returncode in (0, REFUSE)


def test_td25_a_residue_gone_outside_the_checkout_is_healed_again(tmp_path):
    """#4501 review (Codex P2): the residue that kept a heal unresolved can sit
    outside the checkout (reached through a tracked symlink), where HEAD, status
    and index.lock never see it go. The repeat check also compares what the guard
    judges now with what it judged when the heal ended, so removing it heals."""
    w = _symlink_world(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x").write_text("not the checkout's\n")
    w.write_state(w.row("partial"))
    first = w.repair(mode="--boot")
    assert first.returncode == REFUSE, (first.returncode, first.stderr)
    assert w.report()["result"].startswith("unresolved")
    (outside / "x").unlink()
    second = w.repair(mode="--boot")
    assert second.returncode == 0, second.stderr
    assert not w.state.exists(), "the residue is gone, yet the heal was not tried again"
    assert len(w.backups()) == 2


# ── T-D22 / T-D32: the hooks step is bounded ─────────────────────────────────

# AFTER_S above the guard's own reap reserve (5 s): the healed tree is judged in time.
_FAST = {"BOOT_DEADLINE_S": 14, "KILL_GRACE_S": 1, "HOOKS_MIN_S": 1, "AFTER_S": 7}


@pytest.mark.parametrize("fork", [False, True])
def test_td22_restore_hooks_is_killed_at_the_deadline_and_left_pending(w, tmp_path, fork):
    """A forward heal's restore-hooks that outruns the deadline is killed with
    its whole group (a grandchild holding the pipe included); the tree is
    healed (time is kept for `_after`), the hooks are pending for the next run."""
    killed_rollback(w, "P")
    kit = _kit(w, **_FAST)
    pids = tmp_path / "spy.pids"
    env = {"SPY_SLEEP": "30", "SPY_PIDFILE": str(pids)}
    if fork:
        env["SPY_FORK"] = "60"
    start = time.monotonic()
    r = _run_kit(w, kit, **env)
    took = time.monotonic() - start
    assert r.returncode == 0, (r.returncode, r.stderr)
    assert took <= 14 + 3 + 1.5, took
    assert w.report()["hooks"] == "pending" and not w.state.exists()
    assert all(_gone(int(p)) for p in pids.read_text().split()), "a spy process survived"


def test_td32_the_reinstall_never_starts_its_unlink_without_time_for_it(tmp_path, monkeypatch):
    """C8: the reinstall removes hooks, then runs the engine. Under the deadline
    it starts only with 2 x HOOKS_MIN_S left; otherwise no hook is moved and the
    hooks stay pending; the next run with time reinstalls them."""
    w = _real_engine_world(tmp_path)
    killed_rollback(w, "P")
    _install(w, "pre-commit", _CANDIDATE_HOOK)
    import importlib.util

    spec = importlib.util.spec_from_file_location("lmr_td32", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("HOME", str(w.home))
    for k, v in w.env().items():
        if k.startswith("GIT_"):
            monkeypatch.setenv(k, v)

    from tests.test_scripts._live_repair_world import Killed

    def die(self, *a):
        raise Killed

    real = mod.Repair._restore_hooks
    monkeypatch.setattr(mod.Repair, "_restore_hooks", die)
    with pytest.raises(Killed):
        mod.Repair(str(w.state), str(w.root), True, 0).run()
    oid = w.to
    (w.root / ".git" / "objects" / oid[:2] / oid[2:]).unlink()
    first = w.backups()[0]
    time.sleep(1.1)  # the next heal's report dir is named by the second and this pid

    def then_short(self, before, after):
        ok = real(self, before, after)
        boot = mod._BOOT
        # The deadline is close when the reinstall would begin.
        mod._DEADLINE = time.monotonic() + boot.AFTER_S + boot.KILL_GRACE_S + boot.HOOKS_MIN_S
        self.guard._DEADLINE = None
        return ok

    monkeypatch.setattr(mod.Repair, "_restore_hooks", then_short)
    assert _main(w, mod, monkeypatch) == 0
    report = json.loads((first / "REPAIR.json").read_text())
    assert report["hooks"] == "pending", report["hooks"]
    assert (w.root / ".git" / "hooks" / "pre-commit").read_text() == _CANDIDATE_HOOK
    assert not (first / "hooks-before").exists(), "a hook was moved aside"
    monkeypatch.setattr(mod.Repair, "_restore_hooks", real)
    r = w.repair(mode="--boot")
    assert r.returncode == 0, r.stderr
    assert json.loads((first / "REPAIR.json").read_text())["hooks"] == "reinstalled"


def test_td32_the_budget_spent_while_copying_hooks_aside_moves_none(tmp_path, monkeypatch):
    """#4501 review: the entry check passed, then the per-hook git calls and the
    copies spent the budget. Checked again before any hook is removed or
    recorded: the installed hook stays, REPAIR.json names no moved hook."""
    w = _real_engine_world(tmp_path)
    killed_rollback(w, "P")
    _install(w, "pre-commit", _CANDIDATE_HOOK)
    import importlib.util

    spec = importlib.util.spec_from_file_location("lmr_td32b", MODULE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("HOME", str(w.home))
    for k, v in w.env().items():
        if k.startswith("GIT_"):
            monkeypatch.setenv(k, v)

    from tests.test_scripts._live_repair_world import Killed

    def die(self, *a):
        raise Killed

    real = mod.Repair._restore_hooks
    monkeypatch.setattr(mod.Repair, "_restore_hooks", die)
    with pytest.raises(Killed):
        mod.Repair(str(w.state), str(w.root), True, 0).run()
    oid = w.to
    (w.root / ".git" / "objects" / oid[:2] / oid[2:]).unlink()
    first = w.backups()[0]
    time.sleep(1.1)
    monkeypatch.setattr(mod.Repair, "_restore_hooks", real)
    copy = mod._copy_private

    def slow_copy(src, folder, name):
        out = copy(src, folder, name)
        boot = mod._BOOT  # the copies spent the budget: one HOOKS_MIN_S is left
        mod._DEADLINE = time.monotonic() + boot.AFTER_S + boot.KILL_GRACE_S + boot.HOOKS_MIN_S
        return out

    monkeypatch.setattr(mod, "_copy_private", slow_copy)
    _main(w, mod, monkeypatch)
    assert (w.root / ".git" / "hooks" / "pre-commit").read_text() == _CANDIDATE_HOOK
    report = json.loads((first / "REPAIR.json").read_text())
    assert not report.get("hooks_before"), report.get("hooks_before")
    assert report["hooks"] == "pending", report["hooks"]


# ── T-D24: git is bounded at boot ────────────────────────────────────────────

_SHIM = """#!/bin/bash
if [[ " $* " == *" core.hooksPath=/dev/null "* && " $* " == *" diff -z --no-renames --name-only "* ]]; then
    trap '' TERM
    echo $$ >> "$SHIM_PIDS"
    sleep 120 &
    sleep 120
    exit 0
fi
exec {git} "$@"
"""


def _git_shim(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "shim"
    bin_dir.mkdir()
    shim = bin_dir / "git"
    shim.write_text(_SHIM.format(git=shutil.which("git")))
    shim.chmod(0o755)
    return bin_dir, tmp_path / "shim.pids"


def test_td24_a_git_that_ignores_term_is_killed_at_the_deadline(w, tmp_path):
    """The repair's own `git diff` hangs, ignores SIGTERM and leaves a
    grandchild holding the pipe: killed at the deadline. The tree was judged
    half-written and is not healed, so the server stays down (Q2)."""
    killed_tag_side(w, "Z3")
    kit = _kit(w, BOOT_DEADLINE_S=9, KILL_GRACE_S=1)  # the guard keeps 5 s for its own reap
    bin_dir, pids = _git_shim(tmp_path)
    path = f"{bin_dir}:{os.environ['PATH']}"
    start = time.monotonic()
    r = _run_kit(w, kit, timeout=60, PATH=path, SHIM_PIDS=str(pids))
    took = time.monotonic() - start
    assert r.returncode == REFUSE, (r.returncode, r.stderr)
    assert took <= 9 + 3 + 2, took
    assert "deadline" in r.stderr, r.stderr
    assert all(_gone(int(p)) for p in pids.read_text().split()), "a git process survived"
    assert w.state.exists()


# ── T-D34: the tail after the deadline ends inside TimeoutStartSec ───────────


def test_td34_the_pin_fits_timeout_start_sec(mod):
    boot = mod._load_kit("live_move_boot.py")
    tail = boot.PRE_MAIN_S + boot.BOOT_DEADLINE_S + boot.ALERT_TIMEOUT_S + boot.REPORT_S
    assert tail + boot.SHELL_ALERT_S <= boot.TIMEOUT_START_S == 90
    assert boot.BOOT_DEADLINE_S == 75
    # The healed tree is judged inside the deadline: _after's guard keeps its reap time.
    guard = mod._load_kit("update_state_guard.py")
    assert boot.GUARD_REAP_S == guard._REAP_S and boot.AFTER_S >= boot.GUARD_REAP_S + 2
    for name in ("genesis-server", "genesis-bridge"):
        text = (LIB.parent / "systemd" / f"{name}.service.template").read_text()
        assert re.search(r"^TimeoutStartSec=90$", text, re.M), name


def _user_bus() -> dict[str, str]:
    run = f"/run/user/{os.getuid()}"
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", run)
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={run}/bus")
    return env


@pytest.mark.user_manager  # runs the real systemd-run, for its capped transient unit
def test_td34_a_boot_run_past_its_deadline_ends_as_exit_code_not_timeout(w, tmp_path):
    """M5 as a test: a git that ignores TERM with a grandchild holding stdout,
    and an alert that hangs, under a 6 s deadline and TimeoutStartSec=12. The
    unit fails `exit-code` (75), never `timeout`, within 9 + 3 + 1 s of main."""
    if shutil.which("systemd-run") is None:
        pytest.skip("no systemd-run (a CI runner): T-D24 bounds the same run in-process")
    probe = ["systemd-run", "--user", "-p", "MemoryMax=1G", "--wait", "--collect", "-q", "true"]
    if subprocess.run(probe, capture_output=True, timeout=60, env=_user_bus()).returncode:
        pytest.skip("no user systemd manager to run a transient unit")
    killed_tag_side(w, "Z3")
    kit = _kit(w, BOOT_DEADLINE_S=9, KILL_GRACE_S=2)
    (kit / "alert_queue.sh").write_text("queue_alert() { sleep 60; }\n")
    names = [n for n in KIT_FILES if (kit / n).exists()]
    (kit / "KIT.json").write_text(json.dumps(_installer().describe(str(kit), names)))
    bin_dir, pids = _git_shim(tmp_path)
    unit = f"d2a2-probe-{uuid.uuid4().hex[:12]}"
    env = w.env(PATH=f"{bin_dir}:{os.environ['PATH']}", SHIM_PIDS=str(pids))
    props = ["-p", "Type=oneshot", "-p", "TimeoutStartSec=15", "-p", "MemoryMax=1G"]
    sets = [f"--setenv={k}={env[k]}" for k in ("HOME", "PATH", "SHIM_PIDS")]
    argv = [
        "python3",
        "-I",
        "-S",
        "-B",
        str(kit / MODULE.name),
        "--boot",
        str(w.state),
        str(w.root),
    ]
    start = time.monotonic()
    r = subprocess.run(
        ["systemd-run", "--user", "--wait", "--collect", f"--unit={unit}", *props, *sets, *argv],
        capture_output=True, text=True, timeout=60, env=_user_bus(),
    )  # fmt: skip
    out = r.stdout + r.stderr
    took = time.monotonic() - start
    # systemd 255: "failed because the control process exited with error code"
    # (result exit-code); a TimeoutStartSec kill reads "a timeout was exceeded".
    # Newer systemd (the CI runner's) prints the result as "Finished with
    # result: exit-code" instead.
    assert r.returncode != 0, out
    assert "exited with error code" in out or "result: exit-code" in out, out
    assert "timeout was exceeded" not in out and "result: timeout" not in out, out
    assert took <= 9 + 3 + 1 + 1.5, took  # + 1.5: systemd-run and the interpreter start


# ── Section 13: the kit may be newer than the code that wrote the state ─────


def _state_by_update_sh(w: World, phase: str, **env: str) -> None:
    """The state, written by the `_write_state` of the scripts/update.sh this
    tree carries: the writer the previous release ran (D2a-2 does not change it)."""
    text = (LIB.parent / "update.sh").read_text()
    start = text.index("_write_state() {")
    end = text.index("\n}\n", start) + 3
    script = (
        'STATE_FILE="$HOME/.genesis/update_state.json"; STARTED_AT=t; WERE_RUNNING=(genesis-server)\n'
        + text[start:end] + f'_write_state "{phase}"\n'
    )  # fmt: skip
    subprocess.run(["bash", "-c", script], check=True, env=w.env(**env))


def test_the_boot_step_reads_the_state_an_older_update_sh_wrote(w):
    """An update killed in bootstrap on `live` with HEAD at its tag: the newer
    kit reads that writer's state and judges it (start + the guard's page),
    and a row from before `post_merge` and `services_stopped` existed reads the
    same; neither is a crash or a silent skip."""
    _state_by_update_sh(
        w, "bootstrap", ROLLBACK_TAG="pre", ORIGINAL_BRANCH="live",
        UPDATE_OWN_HEAD=w.tag, DEPLOY_HEAD=w.tag,
    )  # fmt: skip
    written = json.loads(w.state.read_text())
    assert written["phase"] == "bootstrap" and written["services_stopped"] == ["genesis-server"]
    expected = _guard_message(w, w.state)
    r = w.repair(mode="--boot")
    assert r.returncode == 0, r.stderr
    assert "skipped" not in r.stderr, r.stderr
    assert len(w.alerts()) == 1 and expected in w.alerts()[0]["body"], w.alerts()
    older = {k: v for k, v in written.items() if k not in ("post_merge", "services_stopped")}
    w.write_state(older)
    shutil.rmtree(w.gen / "alerts")
    again = w.repair(mode="--boot")
    assert again.returncode == 0 and "skipped" not in again.stderr, again.stderr
    assert len(w.alerts()) == 1, w.alerts()
