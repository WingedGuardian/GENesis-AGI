"""The live-move repair's READERS (#3492 PR D1, readers split out of #3648):
deploy_health.collect_live_repairs / live_repair_findings, the guardian
collector's report pick (#3627), and the derive_findings wiring.

The readers REPORT and never judge: per report its ts, its backup folder, how
many paths the update did not write were saved there (a count of the report's
foreign rows, no other row field read) and a result enum. They generate no
shell step and walk no path, so a crafted report costs a bounded read. The
scratch world is tests/test_scripts/_live_repair_world.py; the `w` and `mod`
fixtures come from this directory's conftest.py.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.test_scripts._live_repair_world import (
    MODULE,
    REPO,
    Killed,
    _dir_becomes_file,
    _foreign_edit,
    _foreign_fifo_inside_dir,
    _foreign_inside_dir,
    _foreign_skip_worktree,
    _foreign_staged,
    _foreign_untracked,
    _git,
    _killed_mid_repair,
    _lock_aside,
    _retarget,
    killed_rollback,
    killed_tag_side,
    restore,
    tag_snapshot,
)

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="git + bash")

LATER = 2 * 3600  # past REPAIR_UNFINISHED_S: a report with no result is a killed run
SUFFIX = "-live-move-repair"


def _dh():
    return importlib.import_module("genesis.observability.snapshots.deploy_health")


def _found(w, **kw) -> dict:
    return _dh().collect_live_repairs(w.gen, w.root, **kw)


def _dismiss(w, ts: str) -> None:
    d = subprocess.run(
        ["python3", "-I", "-S", str(MODULE), "--dismiss", ts, str(w.state), str(w.root)],
        capture_output=True,
        text=True,
        env=w.env(),
    )
    assert d.returncode == 0, d.stderr


# ── what a real repair's report reads as ────────────────────────────────────


@pytest.mark.parametrize(
    "make",
    [
        _foreign_edit,
        _foreign_untracked,
        _foreign_inside_dir,
        _foreign_staged,
        _foreign_skip_worktree,
        _foreign_fifo_inside_dir,
    ],
)
def test_a_real_repair_is_reported_by_count_until_dismissed(w, make):
    """The reader half of the writer's T-D3: every foreign row the writer saved
    is counted in its report's entry, with its ts and backup folder, until
    --dismiss."""
    killed_tag_side(w, "Z3")
    _lock_aside(w, lambda: make(w))
    assert w.repair().returncode == 0
    foreign = sum(1 for r in w.report()["paths"] if r["class"] == "foreign")
    assert foreign, "control: the repair found nothing foreign"
    d = w.backups()[-1]
    found = _found(w)
    [entry] = found["reports"]
    assert entry == {
        "ts": d.name[: -len(SUFFIX)],
        "backup_dir": str(d),
        "saved": foreign,
        "restore_refused": 0,
        "result": entry["result"],
        "dismissed": False,
        "unresolved": False,
        "hooks_saved": False,
    }, entry
    assert entry["result"] in ("healed", "healed-kept"), entry
    assert (found["saved"], found["refused"], found["unread_older"]) == (foreign, 0, 0), found
    assert _dh().live_repair_findings(found) == [f"live_repair_saved:{foreign}"]
    _dismiss(w, entry["ts"])
    found = _found(w)
    assert found["saved"] == 0 and found["reports"][0]["dismissed"] is True, found
    assert _dh().live_repair_findings(found) == []


@pytest.mark.parametrize("damage", ["not-json", "other-checkout"])
def test_dismissing_a_report_no_reader_can_read_clears_it(w, damage):
    """#4331 review: an unreadable report, or one recorded for another checkout,
    is kept by the deploy-backups prune and counted refused by deploy health,
    so it needs a way out through Genesis. --dismiss TS writes DISMISSED.json
    beside it (REPAIR.json is not edited); every reader then skips it."""
    killed_tag_side(w, "Z3")
    _lock_aside(w, lambda: _foreign_edit(w))
    assert w.repair().returncode == 0
    d = w.backups()[-1]
    report = d / "REPAIR.json"
    if damage == "not-json":
        report.write_text("{not json")
    else:
        report.write_text(json.dumps({**json.loads(report.read_text()), "repo": "/elsewhere"}))
    before = report.read_bytes()
    assert _found(w)["refused"] == 1, "control: the report is refused"
    _dismiss(w, d.name[: -len(SUFFIX)])
    assert report.read_bytes() == before, "REPAIR.json itself is not edited"
    mark = json.loads((d / "DISMISSED.json").read_text())
    assert isinstance(mark["dismissed_at"], str) and mark["unread"], mark
    found = _found(w)
    assert (found["refused"], found["reports"]) == (0, []), found
    assert _dh().live_repair_findings(found) == []
    assert _guardian_pick(w.home) is None


def _guardian_pick(home: Path) -> dict | None:
    """The host guardian's container-side report pick (#3627), run against this HOME."""
    from genesis.guardian import collector as gc

    out = subprocess.run(
        [sys.executable, "-I", "-S", "-"],
        input=gc._REPAIR_PICK,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        timeout=60,
    ).stdout
    if not out:
        return None
    ts, doc = out.split(gc._REPAIR_MARK.encode(), 1)[1].split(b"\n", 1)
    return {**json.loads(doc), "dir_ts": ts.decode()}


def test_the_guardian_picks_what_this_writer_left_eligible(w, mod, monkeypatch):
    """#3627: the guardian's pick reads the fields THIS writer writes. A run
    killed partway (result unset) is eligible until the next run marks it
    superseded; the healed report is then the one shown, until --dismiss. The
    pick compares the report's repo with ~/genesis, here a link to the world's
    checkout."""
    os.symlink(w.root, w.home / "genesis")
    killed_tag_side(w, "Z3")
    real = mod._git

    def die_at_restore(repo, *args, **kw):
        if args[:1] == ("restore",):
            raise Killed
        return real(repo, *args, **kw)

    monkeypatch.setattr(mod, "_git", die_at_restore)
    with pytest.raises(Killed):
        mod.Repair(str(w.state), str(w.root), True, 0).run()
    killed = _guardian_pick(w.home)
    assert killed is not None and killed["result"] is None, killed
    assert w.repair(mode="--boot").returncode == 0
    healed = _guardian_pick(w.home)
    assert healed is not None and healed["result"] == "healed", healed
    _dismiss(w, healed["ts"])
    assert _guardian_pick(w.home) is None  # one superseded, one dismissed: nothing to show


def test_the_writers_layout_is_what_the_guardians_over_cap_read_expects(w):
    """The pick reads an over-cap report's repo and result from its head and its
    dismissed_at from its tail, by the writer's indent-2 layout. Pinned on a
    real report, dismissed by the real --dismiss: that layout, and that key
    order (repo and result before paths, dismissed_at after)."""
    os.symlink(w.root, w.home / "genesis")
    killed_tag_side(w, "Z3")
    _lock_aside(w, lambda: _foreign_edit(w))
    assert w.repair().returncode == 0
    path = w.backups()[-1] / "REPAIR.json"
    _dismiss(w, w.backups()[-1].name[: -len(SUFFIX)])
    raw = path.read_bytes()
    report = json.loads(raw)
    assert raw == json.dumps(report, indent=2).encode(), "not the indent-2 layout"
    keys = list(report)
    assert max(keys.index("repo"), keys.index("result")) < keys.index("paths")
    assert keys.index("paths") < keys.index("dismissed_at")
    pad = [{"path": f"pad/{n}" + "p" * 200, "class": "engine"} for n in range(25_000)]
    big = {**report, "paths": report["paths"] + pad}  # the same keys, in the same order
    for over, shown in (({}, False), ({"dismissed_at": None}, True), ({"result": "superseded"}, False)):
        path.write_bytes(json.dumps({**big, "dismissed_at": None, **over} if over else big, indent=2).encode())
        picked = _guardian_pick(w.home)
        assert (picked is not None and "too large" in picked["result"]) == shown, (over, picked)


def test_the_guardian_pick_shows_the_dir_names_ts(w):
    """The ts shown is the dir name's, which the pick validated; the stored one
    is user-writable."""
    os.symlink(w.root, w.home / "genesis")
    killed_tag_side(w, "Z3")
    assert w.repair().returncode == 0
    path = w.backups()[-1] / "REPAIR.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "ts": "x\n--- END ---"}))
    assert _guardian_pick(w.home)["dir_ts"] == w.backups()[-1].name[: -len(SUFFIX)]


def test_the_guardian_pick_skips_a_report_for_another_checkout(w):
    """CodeRabbit c4461224321: a report whose repo is not ~/genesis is never
    shown as this checkout's newest repair (the loader refuses it the same way)."""
    (w.home / "genesis").mkdir()
    killed_tag_side(w, "Z3")
    assert w.repair().returncode == 0
    assert _guardian_pick(w.home) is None
    (w.home / "genesis").rmdir()
    os.symlink(w.root, w.home / "genesis")
    assert _guardian_pick(w.home) is not None, "control: the same report, this checkout"


# ── unresolved: the report's word, checked against the guard ───────────────


def test_td19_an_unfinished_repair_is_unresolved_while_the_tree_is_mixed(w, mod, monkeypatch):
    """The reader derives "unresolved" from the guard at read time: a report
    that never finished, an hour old, counts only while the shared guard still
    calls the checkout half-written; a later heal clears it."""
    dh = _dh()
    killed_tag_side(w, "Z3")
    _killed_mid_repair(w, mod, monkeypatch, "at_apply")
    later = time.time() + LATER
    assert dh.live_repair_findings(_found(w, now=time.time())) == []
    found = _found(w, now=later)
    assert dh.live_repair_findings(found) == ["live_repair_unresolved"]
    assert found["reports"][0]["result"] == "unfinished", found
    findings = dh.derive_findings(
        missing_units=[], tier2_pending=[], host_gateway={}, commits_behind=0, live_repairs=found
    )
    assert findings == ["live_repair_unresolved"]
    assert w.repair().returncode == 0
    assert "live_repair_unresolved" not in dh.live_repair_findings(_found(w, now=later))


@pytest.mark.parametrize("window", ["before_clear", "before_finish"])
def test_r1_an_interrupted_report_is_not_unresolved_once_healed(w, mod, monkeypatch, window):
    """The reader half of the writer's r1 test: killed after its restore, before
    its result was written, the next run heals; nothing reads unresolved."""
    killed_tag_side(w, "Z3")

    def die(self, *a, **kw):
        raise Killed

    monkeypatch.setattr(mod.Repair, "_after" if window == "before_clear" else "_finish", die)
    with pytest.raises(Killed):
        mod.Repair(str(w.state), str(w.root), True, 0).run()
    assert w.repair().returncode == 0 and not w.state.exists()
    assert _found(w, now=time.time() + LATER)["unresolved"] is False


def test_r2_a_killed_repair_on_a_tag_directory_reads_resolved(w, mod, monkeypatch):
    """Audit S2, the reader half: killed after its restore on a path that is a
    directory in the tag, then healed by the next run: nothing unresolved."""
    _retarget(w, _dir_becomes_file)
    killed_tag_side(w, "prepared")
    _killed_mid_repair(w, mod, monkeypatch, "after_apply")
    assert w.snapshot() == tag_snapshot() and w.state.exists()
    assert w.repair().returncode == 0 and not w.state.exists()
    assert _found(w, now=time.time() + LATER)["unresolved"] is False


# ── loading only what a report needs ────────────────────────────────────────


def _report_at(home: Path, name: str, **report) -> Path:
    d = home / "deploy-backups" / f"{name}{SUFFIX}"
    d.mkdir(parents=True)
    (d / "REPAIR.json").write_text(json.dumps(report))
    return d


def _spy_scripts(monkeypatch) -> list[str]:
    dh = _dh()
    loaded: list[str] = []
    real = dh._script

    def script(path):
        loaded.append(Path(path).name)
        return real(path)

    monkeypatch.setattr(dh, "_script", script)
    return loaded


def _foreign(path: str = "Z", **over) -> dict:
    return {"path": path, "class": "foreign", **over}


def test_the_repair_module_loads_once_and_only_when_a_report_exists(tmp_path, monkeypatch):
    """Every report is read through the repair's load_report, so its module
    loads once per call when any report dir exists, and never when none does."""
    loaded = _spy_scripts(monkeypatch)
    (tmp_path / "deploy-backups").mkdir()
    _dh().collect_live_repairs(tmp_path, tmp_path)
    assert loaded == [], "a module loaded with no report"
    engine = {"path": "A", "class": "engine"}
    _report_at(tmp_path, "20260101T000000Z-1", result="healed", repo=str(tmp_path), paths=[engine])
    _report_at(tmp_path, "20260101T000001Z-2", result="healed", repo=str(tmp_path),
               paths=[_foreign()], dismissed_at="2026-01-01T00:00:00Z")  # fmt: skip
    found = _dh().collect_live_repairs(tmp_path, tmp_path)
    assert (found["saved"], found["unresolved"], found["refused"]) == (0, False, 0), found
    assert loaded == ["live_move_repair.py"], loaded


def test_the_guard_runs_only_when_a_report_reads_unresolved(tmp_path, monkeypatch):
    loaded = _spy_scripts(monkeypatch)
    _report_at(tmp_path, "20260101T000000Z-1", result="healed", repo=str(tmp_path), paths=[])
    assert _dh().collect_live_repairs(tmp_path, tmp_path)["unresolved"] is False
    assert "update_state_guard.py" not in loaded, loaded
    _report_at(tmp_path, "20260101T000001Z-2", result="unresolved: x", repo=str(tmp_path),
               paths=[])  # fmt: skip
    _dh().collect_live_repairs(tmp_path, tmp_path)
    assert loaded.count("update_state_guard.py") == 1, loaded


def test_saved_counts_every_foreign_row_and_reads_no_other_field(tmp_path):
    """A2: a staged deletion, a flag-only row and a FIFO hold no saved bytes but
    are saved state; every dict row of class "foreign" counts, whatever its
    other fields hold. Engine rows and non-dict rows do not."""
    rows = [
        _foreign("staged-deletion", disk="absent", index=[], saved_as=None),
        _foreign("flag-only", flag="S", disk_changed=False),
        _foreign("fifo", special={"type": "fifo", "mode": "0640"}),
        _foreign("../outside", saved_as="/etc/passwd", blobs="x", index="nonsense"),
        _foreign(["not", "a", "path"]),
        {"path": "e", "class": "engine"},
        {"path": "c", "class": "Foreign"},
        "a string row",
        7,
    ]
    _report_at(tmp_path, "20260101T000000Z-1", result="healed", repo=str(tmp_path), paths=rows)
    found = _dh().collect_live_repairs(tmp_path, tmp_path)
    assert found["saved"] == 5 and found["reports"][0]["saved"] == 5, found
    assert "/etc/passwd" not in json.dumps(found)


@pytest.mark.parametrize(
    ("raw", "enum"),
    [
        ("healed", "healed"),
        ("healed (the target was unreadable)", "healed"),
        ("healed, state kept: the update is still in progress", "healed-kept"),
        ("unresolved: mixed_tree: two paths differ", "unresolved"),
        ("superseded", "superseded"),
        (None, "unfinished"),
        ("x" * (1024 * 1024), "unknown"),
        ({"healed": True}, "unknown"),
        (["healed"], "unknown"),
        (7, "unknown"),
        ("HEALED", "unknown"),
    ],
    ids=lambda v: str(v)[:24],
)
def test_the_result_is_an_enum_never_the_reports_string(tmp_path, raw, enum):
    """A1: the result is derived from the stored value by prefix, and nothing
    of the stored string leaves the reader."""
    _report_at(tmp_path, "20260101T000000Z-1", result=raw, repo=str(tmp_path), paths=[])
    found = _dh().collect_live_repairs(tmp_path, tmp_path)
    assert found["reports"][0]["result"] == enum, found
    if isinstance(raw, str) and enum == "unknown":
        assert raw[:12] not in json.dumps(found)


# ── #3492 B2b: a restore's outcome (review SF-4, N-4) ───────────────────────


@pytest.mark.parametrize(
    ("block", "cleared", "refused"),
    [
        ({"inflight": None, "refused": 0, "restored_at": "2026-11-04T00:00:00Z"}, True, 0),
        ({"inflight": None, "refused": 1, "restored_at": None}, False, 1),
        ({"inflight": None, "refused": False, "restored_at": "2026-11-04T00:00:00Z"}, False, 0),
        ({"inflight": None, "refused": 0, "restored_at": 5}, False, 0),
        ({"inflight": None, "refused": -1, "restored_at": "2026-11-04T00:00:00Z"}, False, 0),
        ({"inflight": None, "refused": "0", "restored_at": "2026-11-04T00:00:00Z"}, False, 0),
        ("restored", False, 0),
    ],
    ids=["cleared", "one-refused", "bool-refused", "restored-at-not-str", "negative", "str",
         "not-a-block"],
)  # fmt: skip
def test_a_report_clears_only_once_a_restore_refused_nothing(tmp_path, block, cleared, refused):
    """Its saved paths drop out only when `restored_at` is a str and `refused`
    an int (never a bool) of 0; a refused count is shown either way."""
    _report_at(tmp_path, "20260101T000000Z-1", result="healed", repo=str(tmp_path),
               paths=[_foreign(), _foreign("Y")], restore=block)  # fmt: skip
    found = _dh().collect_live_repairs(tmp_path, tmp_path)
    [entry] = found["reports"]
    assert (found["saved"], entry["saved"]) == ((0, 0) if cleared else (2, 2)), found
    assert entry["restore_refused"] == refused, entry


def test_a_real_restore_clears_the_report(w):
    """End to end: a real repair is counted, a real restore of it clears it."""
    killed_tag_side(w, "Z3")
    _lock_aside(w, lambda: _foreign_edit(w))
    assert w.repair().returncode == 0
    assert _found(w)["saved"] == 1
    r = subprocess.run(
        ["python3", "-I", "-S", str(MODULE), "--restore", w.report()["ts"], str(w.state),
         str(w.root)],
        capture_output=True, text=True, env=w.env(),
    )  # fmt: skip
    assert r.returncode == 0, r.stdout + r.stderr
    found = _found(w)
    assert found["saved"] == 0 and _dh().live_repair_findings(found) == [], found


# ── which reports are read: the newest 8, by the writer's name ──────────────


def _reports(home: Path, n: int, *, start: int = 0) -> list[str]:
    """``n`` valid reports, one minute apart, oldest first; their ts."""
    out = []
    for i in range(start, start + n):
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(1_767_225_600 + 60 * i)) + f"-{i}"
        _report_at(home, ts, result="healed", repo=str(home), paths=[_foreign()])
        out.append(ts)
    return out


def test_only_the_newest_eight_reports_are_read(tmp_path, monkeypatch):
    """A3: a ninth valid report is counted in unread_older, never refused and
    never read; the eight read are the newest, newest first."""
    dh = _dh()
    names = _reports(tmp_path, 9)
    real, read = dh._script, []

    def script(path):
        module = real(path)
        if path.name == "live_move_repair.py":
            loader = module.load_report
            module.load_report = lambda p, r: read.append(os.path.basename(p)) or loader(p, r)
        return module

    monkeypatch.setattr(dh, "_script", script)
    found = dh.collect_live_repairs(tmp_path, tmp_path)
    assert (found["unread_older"], found["refused"], found["saved"]) == (1, 0, 8), found
    assert [r["ts"] for r in found["reports"]] == names[1:][::-1], found["reports"]
    assert f"{names[0]}{SUFFIX}" not in read and len(read) == 8, read
    assert dh.live_repair_findings(found) == ["live_repair_saved:8", "live_repair_unread_older:1"]


def test_reports_are_ordered_by_their_parsed_ts_not_by_name(tmp_path):
    """A6: pid 10 sorts before pid 9 as text; by the parsed ts it is newer. A
    name the writer's pattern accepts but whose ts is no date is refused."""
    for name in ("20260101T000000Z-9", "20260101T000000Z-10"):
        _report_at(tmp_path, name, result="healed", repo=str(tmp_path), paths=[])
    _report_at(tmp_path, "20261399T000000Z-1", result="healed", repo=str(tmp_path), paths=[])
    found = _dh().collect_live_repairs(tmp_path, tmp_path)
    assert [r["ts"] for r in found["reports"]] == ["20260101T000000Z-10", "20260101T000000Z-9"]
    assert found["refused"] == 1, found


def test_the_report_order_is_the_loaders_seconds_then_pid(mod):
    """R2 (Codex c4464516280): one order for every reader, defined in the
    loader: the parsed seconds, then the pid as a number."""
    import calendar

    secs = calendar.timegm(time.strptime("20260101T000000Z", "%Y%m%dT%H%M%SZ"))
    assert mod.report_order("20260101T000000Z-10000") == (secs, 10000)
    assert mod.report_order("20260101T000000Z-10000") > mod.report_order("20260101T000000Z-9999")
    for bad in ("20261399T000000Z-1", "20260101T000000Z", "20260101T000000Z-x"):
        with pytest.raises(ValueError):
            mod.report_order(bad)
    assert not hasattr(_dh(), "_report_order"), "deploy health keeps a private copy of the order"


def test_a_name_far_in_the_future_is_refused_and_never_the_newest(tmp_path):
    """R2 premise P4: a report dir named more than REPORT_FUTURE_S ahead of the
    reader's clock (a write while the clock ran ahead) is not a report: it is
    refused, takes no read slot, and never governs ``unresolved``."""
    now = time.time()
    current = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)) + "-2"
    _report_at(tmp_path, "29991231T000000Z-1", result="healed", repo=str(tmp_path), paths=[])
    _report_at(tmp_path, current, result="unresolved: x", repo=str(tmp_path), paths=[])
    found = _dh().collect_live_repairs(tmp_path, tmp_path, now=now)
    assert [r["ts"] for r in found["reports"]] == [current], found
    assert found["refused"] == 1 and found["reports"][0]["unresolved"] is True, found
    # Inside the skew allowance it is a report like any other, and the newest.
    near = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now + 200)) + "-3"
    _report_at(tmp_path, near, result="healed", repo=str(tmp_path), paths=[])
    found = _dh().collect_live_repairs(tmp_path, tmp_path, now=now)
    assert [r["ts"] for r in found["reports"]] == [near, current], found


def test_both_readers_agree_on_the_newest_report(tmp_path, mod):
    """R2 premise SHOULD-FIX-1: one crafted backups dir set, both readers.
    They share the order (seconds, then pid) and the future-skew rule, so they
    agree on which report is newest. The one intended difference: deploy
    health's ``unresolved`` reads the newest READ report (dismissed included),
    while the guardian pick shows the newest ELIGIBLE one, and shows a newest
    report it cannot parse as unreadable rather than an older one."""
    home = tmp_path
    gen = home / ".genesis"
    checkout = home / "genesis"
    checkout.mkdir()
    now = time.time()

    def at(seconds_ago: float, pid: int) -> str:
        return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now - seconds_ago)) + f"-{pid}"

    def put(ts: str, **over) -> Path:
        report = {"ts": ts, "repo": str(checkout), "paths": [], "result": "healed", **over}
        return _report_at(gen, ts, **report)

    def both() -> tuple[str | None, str | None, bool]:
        found = _dh().collect_live_repairs(gen, checkout, now=now)
        picked = _guardian_pick(home)
        first = found["reports"][0] if found["reports"] else {}
        return first.get("ts"), (picked or {}).get("ts"), first.get("unresolved")

    # 1. Same second, pids 9999 and 10000: 10000 is the newer in both.
    put(at(600, 9999), result="healed")
    put(at(600, 10000), result="unresolved: x")
    assert both() == (at(600, 10000), at(600, 10000), True)
    # 2. A far-future name next to a current unresolved report: neither reader
    # takes it as a report.
    put("29991231T000000Z-1", result="healed")
    assert both() == (at(600, 10000), at(600, 10000), True)
    # 3. A dismissed newest: deploy health reads it as the newest, so its
    # ``unresolved`` reads that (dismissed) report; the pick shows the newest
    # eligible one. Intended.
    put(at(300, 5), result="unresolved: y", dismissed_at="2026-01-01T00:00:00Z")
    assert both() == (at(300, 5), at(600, 10000), False)
    # 4. A malformed newest: deploy health refuses it (its newest read report
    # stays the dismissed one); the pick shows it as unreadable, ts from its dir.
    bad = at(60, 7)
    (gen / "deploy-backups" / f"{bad}{SUFFIX}").mkdir()
    (gen / "deploy-backups" / f"{bad}{SUFFIX}" / "REPAIR.json").write_text("{not json")
    found = _dh().collect_live_repairs(gen, checkout, now=now)
    assert found["reports"][0]["ts"] == at(300, 5) and found["refused"] == 2, found
    picked = _guardian_pick(home)
    assert picked == {"ts": bad, "result": "report unreadable; not read", "dir_ts": bad}, picked


def test_a_name_the_writer_never_makes_takes_no_slot(tmp_path):
    """A name that is not the writer's sorts newest as text, yet the eight read
    are still the eight newest valid reports; it counts as refused, unread."""
    _reports(tmp_path, 8)
    _report_at(tmp_path, "99999999T999999Z-1; touch MARKER; #", result="healed",
               repo=str(tmp_path), paths=[_foreign()])  # fmt: skip
    _report_at(tmp_path, "zzz", result="healed", repo=str(tmp_path), paths=[_foreign()])
    found = _dh().collect_live_repairs(tmp_path, tmp_path)
    assert (len(found["reports"]), found["refused"], found["unread_older"]) == (8, 2, 0), found
    assert "MARKER" not in json.dumps(found), found


# ── a malformed report never blanks the snapshot ────────────────────────────


@pytest.mark.parametrize(
    "paths",
    [
        ["oops"],
        "x",
        [{"path": ["a"], "class": "foreign"}],
        [{"path": "p", "class": "foreign"}, 7],
        [{"path": "p", "class": "foreign", "disk_changed": True, "saved_as": 5, "index": "x"}],
    ],
)
def test_sf3_a_malformed_report_never_blanks_the_snapshot(tmp_path, paths):
    """SF3: a REPAIR.json of the wrong shape costs that report, never the
    snapshot (an exception there blanks the whole deploy-health snapshot)."""
    report = {"result": "healed", "paths": paths, "repo": str(tmp_path)}
    _report_at(tmp_path, "20260101T000000Z-1", **report)
    found = _dh().collect_live_repairs(tmp_path, tmp_path)
    assert all(isinstance(found[k], int) for k in ("saved", "refused", "unread_older")), found


@pytest.mark.parametrize(
    "repo", ["/x/\ud800", "/x\0y", "/" + "a" * 4096, 7], ids=["surrogate", "nul", "4097B", "int"]
)
def test_one_bad_report_costs_that_report_and_keeps_the_next(tmp_path, repo):
    """#3741: each of these raised out of the reader and blanked deploy health.
    The report is refused; a later good report is still read."""
    _report_at(tmp_path, "20260101T000000Z-1", result="healed", repo=repo, paths=[_foreign()])
    _report_at(tmp_path, "20260102T000000Z-2", result="healed", repo=str(tmp_path),
               paths=[_foreign(), _foreign("Y")])  # fmt: skip
    found = _collect_bounded(tmp_path, tmp_path)
    assert (found["refused"], found["saved"]) == (1, 2), found
    assert [r["ts"] for r in found["reports"]] == ["20260102T000000Z-2"], found


def test_an_unforeseen_failure_costs_only_that_report(tmp_path, monkeypatch):
    """The boundary holds for a fault no check foresaw."""
    dh = _dh()
    for ts in ("20260101T000000Z-1", "20260102T000000Z-2"):
        _report_at(tmp_path, ts, result="healed", repo=str(tmp_path), paths=[_foreign()])
    real = dh._script

    def script(path):
        module = real(path)
        if path.name == "live_move_repair.py":
            loader = module.load_report

            def flaky(p, r):
                if "20260101" in p:
                    raise RuntimeError("boom")
                return loader(p, r)

            module.load_report = flaky
        return module

    monkeypatch.setattr(dh, "_script", script)
    found = dh.collect_live_repairs(tmp_path, tmp_path)
    assert (found["refused"], found["saved"]) == (1, 1), found


_REPAIR_API = ("report_ts", "report_order", "REPORT_FUTURE_S", "BadReport", "load_report", "dismiss_marked")


@pytest.mark.parametrize("missing", _REPAIR_API)
def test_a_repair_module_missing_its_api_keeps_every_other_finding(tmp_path, monkeypatch, missing):
    """Architect SHOULD-FIX-1: a repair module on disk that loads but lacks a
    name the reader uses (an API renamed by a later pull, a server not yet
    restarted) reads every report as refused; deploy_health keeps its other
    findings instead of blanking the whole section."""
    import asyncio
    import types

    import genesis.env

    dh = _dh()
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "gh"
    _report_at(home, "20260101T000000Z-1", result="healed", repo=str(repo), paths=[_foreign()])
    real = dh._script

    def script(path):
        module = real(path)
        if path.name != "live_move_repair.py":
            return module
        stub = types.ModuleType("stub_repair")
        for name in _REPAIR_API:
            if name != missing:
                setattr(stub, name, getattr(module, name))
        if missing != "load_report":
            stub.load_report = lambda p, r: (_ for _ in ()).throw(RuntimeError("boom"))
        return stub

    monkeypatch.setattr(dh, "_script", script)
    found = dh.collect_live_repairs(home, repo)
    assert (found["saved"], found["refused"]) == (0, 1), found
    monkeypatch.setattr(genesis.env, "genesis_home", lambda: home)
    monkeypatch.setattr(genesis.env, "repo_root", lambda: repo)
    monkeypatch.setattr(
        dh, "collect_main_checkout_dirty", lambda r: {"status": "clean", "count": 0, "paths": []}
    )
    monkeypatch.setattr(dh, "collect_missing_units", lambda a, b: ["genesis-x.service"])
    snap = asyncio.run(dh.deploy_health(None))
    assert snap["status"] != "error", snap
    assert "missing_units:genesis-x.service" in snap["findings"], snap["findings"]
    assert "live_repair_refused:1" in snap["findings"], snap["findings"]


def test_a_checkout_whose_path_is_not_utf8_reads_its_own_reports(tmp_path):
    """Architect SHOULD-FIX-2: the writer stores repo = abspath(checkout); for
    a path whose bytes are not UTF-8 that is a surrogate-escaped str, which
    JSON round-trips. Its length is measured in its real bytes (os.fsencode),
    so the report is read. What stays refused: a NUL, a lone surrogate that
    no file name decodes to (fsencode cannot encode it), over 4096 bytes."""
    mod = _writer()
    repo = Path(os.fsdecode(os.path.join(os.fsencode(tmp_path), b"r\xff")))
    repo.mkdir()
    d = _report_at(tmp_path, "20260101T000000Z-1", result="healed", repo=str(repo),
                   paths=[_foreign()])  # fmt: skip
    assert mod.load_report(str(d), str(repo)) is not None
    found = _collect_bounded(tmp_path, repo)
    assert (found["saved"], found["refused"]) == (1, 0), found
    for bad in (str(repo) + "\0", "/x/\ud800", str(repo) + "/" * 4096):
        assert not mod.same_repo(bad, str(repo)), repr(bad[:40])


def test_an_unloadable_repair_module_keeps_every_other_finding(tmp_path, monkeypatch):
    """With the repair's module unreadable, no report can be read through its
    loader: each counts as refused (a finding of its own, never a blank
    snapshot) and deploy_health keeps its other findings."""
    import asyncio

    import genesis.env

    dh = _dh()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    home = tmp_path / "gh"
    _report_at(home, "20260101T000000Z-1", result="healed", repo=str(repo), paths=[_foreign()])

    def unreadable(path):
        raise PermissionError(f"{path}: denied")

    monkeypatch.setattr(dh, "_script", unreadable)
    found = dh.collect_live_repairs(home, repo)
    assert (found["saved"], found["refused"], found["reports"]) == (0, 1, []), found
    monkeypatch.setattr(genesis.env, "genesis_home", lambda: home)
    monkeypatch.setattr(genesis.env, "repo_root", lambda: repo)
    monkeypatch.setattr(
        dh, "collect_main_checkout_dirty", lambda r: {"status": "clean", "count": 0, "paths": []}
    )
    snap = asyncio.run(dh.deploy_health(None))
    assert snap["status"] != "error", snap
    assert "live_repair_refused:1" in snap["findings"], snap["findings"]


def test_a_linked_worktree_reads_no_repair_reports(tmp_path, monkeypatch):
    """A linked worktree (a dev tree) is not the checkout the repair heals: no
    report is read and no live_repair finding is raised there, as collect_live."""
    dh = _dh()
    _report_at(tmp_path, "20260101T000000Z-1", result="healed", repo=str(tmp_path),
               paths=[_foreign()])  # fmt: skip
    status = {"status": "not_deploy_root", "count": 0, "paths": []}
    monkeypatch.setattr(dh, "collect_main_checkout_dirty", lambda r: dict(status))
    monkeypatch.setattr(dh, "collect_live", lambda r: None)
    monkeypatch.setattr(dh, "collect_git_facts", lambda r, on_live: {})
    monkeypatch.setattr(dh, "collect_missing_units", lambda a, b: [])
    monkeypatch.setattr(dh, "collect_host_gateway", lambda *a, **k: {})
    assert dh._collect_sync(tmp_path, tmp_path)["live_repairs"] is None
    assert dh.live_repair_findings(None) == []
    status["status"] = "clean"
    assert dh._collect_sync(tmp_path, tmp_path)["live_repairs"]["saved"] == 1, "control"


# ── one loader: every reader reads a report through load_report ─────────────


def _writer():
    """The repair module, loaded fresh (no scratch World, unlike `mod`)."""
    spec = importlib.util.spec_from_file_location("live_move_repair_caps", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _collect_bounded(home: Path, repo: Path | None = None, seconds: float = 10.0) -> dict:
    """collect_live_repairs on a thread, failing (not hanging) past ``seconds``.
    The deploy checkout is ``repo``, by default ``home``/repo."""
    out: dict = {}
    repo = home / "repo" if repo is None else repo
    t = threading.Thread(
        target=lambda: out.update(_dh().collect_live_repairs(home, repo)), daemon=True
    )
    t.start()
    t.join(seconds)
    assert not t.is_alive(), f"collect_live_repairs still running after {seconds}s"
    return out


def _valid_report(tmp_path: Path, name: str = "20260101T000000Z-1") -> tuple[Path, Path]:
    """A report the writer could have left: one foreign row. (repo, dir)."""
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    d = _report_at(tmp_path, name, result="healed", repo=str(repo), paths=[_foreign()])
    return repo, d


def test_control_the_valid_report_is_read(tmp_path):
    repo, d = _valid_report(tmp_path)
    found = _collect_bounded(tmp_path, repo)
    assert (found["saved"], found["refused"]) == (1, 0), found
    assert found["reports"][0]["backup_dir"] == str(d), found


@pytest.mark.parametrize(
    "named", ["/x\0y", "/x/\ud800", "/" + "a" * 4096], ids=["nul", "surrogate", "4097B"]
)
def test_a_repo_the_loader_cannot_compare_is_refused_before_realpath(tmp_path, monkeypatch, named):
    """#3741 at its source: a repo with a NUL, a lone surrogate or over 4096
    bytes raises BadReport from load_report, and realpath never sees it."""
    mod = _writer()
    d = _report_at(tmp_path, "20260101T000000Z-1", result="healed", repo=named, paths=[])
    seen: list[str] = []
    real = os.path.realpath
    monkeypatch.setattr(os.path, "realpath", lambda p, **kw: seen.append(p) or real(p, **kw))
    with pytest.raises(mod.BadReport):
        mod.load_report(str(d), str(tmp_path))
    assert named not in seen, "realpath ran on the untrusted repo"
    assert len(("/" + "a" * 4095).encode()) == mod.REPO_BYTES  # the boundary named


def test_a_realpath_failure_is_a_refusal(tmp_path, monkeypatch):
    mod = _writer()
    d = _report_at(tmp_path, "20260101T000000Z-1", result="healed", repo=str(tmp_path), paths=[])
    assert mod.load_report(str(d), str(tmp_path)) is not None, "control"

    def broken(p, **kw):
        raise OSError("ELOOP")

    monkeypatch.setattr(os.path, "realpath", broken)
    with pytest.raises(mod.BadReport):
        mod.load_report(str(d), str(tmp_path))


def test_the_report_name_is_the_writers_backup_dir(mod):
    """The loader's name rule is derived from the writer's own _backup_dir:
    every name it makes matches, and the ts is that name less the suffix."""
    from types import SimpleNamespace

    name = os.path.basename(mod.Repair._backup_dir(SimpleNamespace(backups="/b")))
    assert mod.report_ts(name) == name[: -len(mod.SUFFIX)], name
    for bad in (
        "x-live-move-repair",
        "20260101T000000Z-1; touch MARKER; #-live-move-repair",
        "20260101T000000Z-1\n-live-move-repair",
        "20260101T000000Z-\udcff-live-move-repair",
        "2026010١T000000Z-1-live-move-repair",  # a non-ASCII digit
        "20260101T000000Z-1-live-move-repair.old",
    ):
        assert mod.report_ts(bad) is None, bad


def _release(fifo: Path) -> None:
    """Open the FIFO's write end once, so a reader blocked on it (the RED run)
    returns; no reader, nothing to release."""
    with contextlib.suppress(OSError):
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))


def test_a_fifo_report_is_refused_promptly(tmp_path):
    """Codex c4401481304: a FIFO REPAIR.json is never opened for a blocking
    read (that wedged the whole health snapshot); it is refused and counted."""
    repo, d = _valid_report(tmp_path)
    (d / "REPAIR.json").unlink()
    os.mkfifo(d / "REPAIR.json")
    try:
        found = _collect_bounded(tmp_path, repo)
    finally:
        _release(d / "REPAIR.json")
    assert (found["saved"], found["refused"], found["reports"]) == (0, 1, []), found


def test_a_report_linked_to_an_endless_device_is_refused_promptly(tmp_path):
    repo, d = _valid_report(tmp_path)
    (d / "REPAIR.json").unlink()
    os.symlink("/dev/zero", d / "REPAIR.json")
    found = _collect_bounded(tmp_path, repo)
    assert (found["saved"], found["refused"]) == (0, 1), found


def test_a_report_over_the_byte_cap_is_refused(tmp_path):
    mod = _writer()
    repo, d = _valid_report(tmp_path)
    body = (d / "REPAIR.json").read_text()
    (d / "REPAIR.json").write_text(body + " " * mod.REPORT_BYTES)  # still valid JSON
    found = _collect_bounded(tmp_path, repo)
    assert (found["saved"], found["refused"]) == (0, 1), found


def test_a_report_over_the_row_cap_is_refused(tmp_path):
    mod = _writer()
    repo = tmp_path / "repo"
    repo.mkdir()
    rows = [{"path": f"r{n}"} for n in range(mod.REPORT_ROWS + 1)]
    _report_at(tmp_path, "20260101T000000Z-1", result="healed", repo=str(repo), paths=rows)
    found = _collect_bounded(tmp_path, repo)
    assert (found["saved"], found["refused"]) == (0, 1), found


def test_a_symlinked_report_dir_is_refused(tmp_path):
    repo, d = _valid_report(tmp_path, "20260101T000000Z-1")
    elsewhere = tmp_path / "elsewhere"
    d.rename(elsewhere)
    os.symlink(elsewhere, d)
    found = _collect_bounded(tmp_path, repo)
    assert (found["saved"], found["refused"]) == (0, 1), found


def test_a_report_dir_name_the_writer_never_makes_is_refused(tmp_path):
    """Codex c4401481317: the ts comes from the dir name, so a name that is not
    the writer's format is refused whole and never reaches a shown command."""
    repo, _ = _valid_report(tmp_path, "20260101T000000Z-1; touch MARKER; #")
    found = _collect_bounded(tmp_path, repo)
    assert (found["saved"], found["refused"], found["reports"]) == (0, 1, []), found
    assert "MARKER" not in json.dumps(found), found


def test_a_report_for_another_checkout_runs_no_git_there(tmp_path):
    """The report's `repo` must be the deploy checkout the reader was given:
    no report of another checkout is counted, and no git runs there (git runs
    that checkout's core.fsmonitor)."""
    deploy = tmp_path / "repo"
    deploy.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    marker = tmp_path / "fsmonitor-ran"
    _git(other, "config", "core.fsmonitor", f"touch {marker}; false")
    _report_at(tmp_path, "20260101T000000Z-1", result="unresolved: x", repo=str(other),
               paths=[_foreign()])  # fmt: skip
    marker.unlink(missing_ok=True)  # the config command itself may have run it
    found = _collect_bounded(tmp_path, deploy)
    assert not marker.exists(), "git ran in the checkout the report named"
    assert (found["saved"], found["refused"]) == (0, 1), found


def test_the_same_checkout_by_another_spelling_is_accepted(tmp_path):
    repo, _ = _valid_report(tmp_path)
    link = tmp_path / "link-to-repo"
    os.symlink(repo, link)
    found = _collect_bounded(tmp_path, link)
    assert (found["saved"], found["refused"]) == (1, 0), found


# ── crafted reports cost a bounded read, never a per-path walk ──────────────


def _crafted(case: str, home: Path, repo: Path) -> None:
    """The architect's crafted reports (#3721 round-3 audit), each at most the
    loader's byte cap, so each is READ (not refused) where it can be."""
    r = str(repo)
    if case == "200k-parts":
        rows = [_foreign("a/" * 199_999 + "a")]
    elif case == "900-deep-prefixes":
        rows = [_foreign(f"{n}/" + "x/" * 1_800 + "f") for n in range(900)]
    elif case == "600-nested-chain":
        rows = [_foreign("/".join(["d"] * n)) for n in range(1, 601)]
    elif case == "100k-blobs":
        rows = [_foreign("Z", blobs=[f"b{n}" for n in range(100_000)])]
    elif case == "2MB-repo":
        _report_at(home, "20260101T000000Z-1", result="healed", repo="/" + "r" * 2_000_000,
                   paths=[_foreign()])  # fmt: skip
        return
    elif case == "200-dirs":
        _reports(home, 200)
        for d in (home / "deploy-backups").iterdir():
            data = json.loads((d / "REPAIR.json").read_text())
            (d / "REPAIR.json").write_text(json.dumps({**data, "repo": r}))
        return
    else:
        raise AssertionError(case)
    _report_at(home, "20260101T000000Z-1", result="unresolved: x", repo=r, paths=rows)


_CRAFTED = {
    "200k-parts": (1, 0, 0),  # (saved, refused, unread_older)
    "900-deep-prefixes": (900, 0, 0),
    "600-nested-chain": (600, 0, 0),
    "100k-blobs": (1, 0, 0),
    "2MB-repo": (0, 1, 0),
    "200-dirs": (8, 0, 192),
}

#: Read inside the capped unit: its own cgroup's memory.peak, since systemd-run's
#: "Memory peak" summary under --pipe MEASURED 556K for a 200 MB allocation.
_CHILD = """
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
import importlib
dh = importlib.import_module("genesis.observability.snapshots.deploy_health")
from pathlib import Path
t = time.monotonic()
found = dh.collect_live_repairs(Path(sys.argv[2]), sys.argv[3])
secs = time.monotonic() - t
cg = open("/proc/self/cgroup").read().strip().split("::", 1)[1]
peak = int(open("/sys/fs/cgroup" + cg + "/memory.peak").read())
print(json.dumps({"secs": secs, "peak": peak, "found": found}))
"""


def _user_bus() -> dict[str, str]:
    """The environment with the user manager's bus, which `systemd-run --user`
    needs; a session without XDG_RUNTIME_DIR gets "No medium found". Only a
    test marked user_manager keeps the real bus variables at all."""
    run = f"/run/user/{os.getuid()}"
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", run)
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={run}/bus")
    return env


def _cap_available() -> str | None:
    """None when a capped user unit can run here, else why it cannot."""
    if shutil.which("systemd-run") is None:
        return "no systemd-run (a CI runner): the in-process bound test still runs"
    probe = subprocess.run(
        ["systemd-run", "--user", "--slice=app-capped.slice", "-p", "MemoryMax=1G",
         "--wait", "--collect", "-q", "true"],
        capture_output=True, text=True, timeout=60, env=_user_bus(),
    )  # fmt: skip
    if probe.returncode == 0:
        return None
    return f"no user systemd manager to run a capped unit: {probe.stderr.strip()[:200]}"


@pytest.mark.user_manager  # runs the real systemd-run, for its capped transient unit
@pytest.mark.parametrize("case", sorted(_CRAFTED))
def test_a_crafted_report_returns_promptly_in_bounded_memory(tmp_path, case):
    """Under a 1 GB, no-swap cap (an uncapped repro of the old per-path walk
    took shared services down): each crafted report returns in under 2 s with
    a memory peak under 1 GB, and counts as expected."""
    why = _cap_available()
    if why:
        pytest.skip(why)
    home, repo = tmp_path / "home", tmp_path / "repo"
    home.mkdir()
    repo.mkdir()
    _crafted(case, home, repo)
    script = tmp_path / "child.py"
    script.write_text(_CHILD)
    done = subprocess.run(
        ["systemd-run", "--user", "--slice=app-capped.slice", "-p", "MemoryMax=1G",
         "-p", "MemorySwapMax=0", "-p", "RuntimeMaxSec=60", "--wait", "--collect", "--pipe",
         "-q", sys.executable, "-I", str(script), str(REPO / "src"), str(home), str(repo)],
        capture_output=True, text=True, timeout=120, env=_user_bus(),
    )  # fmt: skip
    assert done.returncode == 0, done.stderr[-2000:]
    out = json.loads(done.stdout.strip().splitlines()[-1])
    found = out["found"]
    print(f"{case}: {out['secs']:.3f}s, peak {out['peak'] / 2**20:.1f} MiB")
    assert out["secs"] < 2.0, out["secs"]
    assert out["peak"] < 2**30, out["peak"]
    assert (found["saved"], found["refused"], found["unread_older"]) == _CRAFTED[case], found


@pytest.mark.parametrize("case", sorted(_CRAFTED))
def test_a_crafted_report_calls_no_per_path_function(tmp_path, monkeypatch, case):
    """In process and deterministic: for every crafted report, the reader calls
    none of the repair's per-path or git functions (the old walk built every
    parent prefix through _parents)."""
    dh = _dh()
    called: list[str] = []
    real = dh._script

    def script(path):
        module = real(path)
        if path.name == "live_move_repair.py":
            for name in ("_parents", "_link_parent", "_index_state", "_tree", "_out", "_git",
                         "reapply_step", "_as_index"):  # fmt: skip
                monkeypatch.setattr(
                    module, name, lambda *a, _n=name, **k: called.append(_n), raising=False
                )
        return module

    monkeypatch.setattr(dh, "_script", script)
    monkeypatch.setattr(dh, "_mixed", lambda home, repo: False)
    home, repo = tmp_path / "home", tmp_path / "repo"
    home.mkdir()
    repo.mkdir()
    _crafted(case, home, repo)
    found = dh.collect_live_repairs(home, repo)
    assert called == [], called
    assert (found["saved"], found["refused"], found["unread_older"]) == _CRAFTED[case], found


# -- the backup folder reaches the observation table, whatever its bytes --


async def test_a_non_utf8_backup_folder_reaches_the_observation_table(tmp_path, monkeypatch):
    """The whole path to SQLite: a home whose name is not UTF-8, its report's
    paragraph, a real observations table. A surrogate there made the insert
    raise and the deploy alert was lost."""
    import aiosqlite

    from genesis.awareness import loop
    from genesis.db.schema import create_all_tables
    from tests.test_awareness.test_deploy_staleness_check import _patch_snapshot, _rows, _snap

    for name, value in (("_last_deploy_alert_at", 0.0), ("_last_deploy_alert_key", "")):
        monkeypatch.setattr(loop, name, value)
    home = Path(os.fsdecode(os.path.join(os.fsencode(tmp_path), b"h\xff")))
    home.mkdir()
    # Only the home (the backup folder) is not UTF-8 here; the checkout case has its own test.
    repo = tmp_path / "repo"
    repo.mkdir()
    _report_at(home, "20260101T000000Z-1", result="healed", repo=str(repo), paths=[_foreign()])
    found = _dh().collect_live_repairs(home, repo)
    assert found["saved"] == 1, found
    snap = _snap(_dh().live_repair_findings(found))
    snap["live_repairs"] = found
    _patch_snapshot(monkeypatch, snap)
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    try:
        await create_all_tables(db)
        await loop._check_deploy_staleness(db)
        [row] = await _rows(db)
    finally:
        await db.close()
    assert "h\\udcff" in row["content"] and "20260101T000000Z-1" in row["content"], row


# -- the guardian's pick applies the loader's rules (it cannot import it) --


def test_the_guardian_pick_reads_a_checkout_whose_path_is_not_utf8(tmp_path):
    """SHOULD-FIX-2 in the pick's copy of same_repo: a HOME whose bytes are not
    UTF-8 still has its own report picked."""
    home = Path(os.fsdecode(os.path.join(os.fsencode(tmp_path), b"h\xff")))
    (home / "genesis").mkdir(parents=True)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-1"
    report = {"ts": ts, "result": "healed", "repo": str(home / "genesis"), "paths": []}
    _report_at(home / ".genesis", ts, **report)
    picked = _guardian_pick(home)
    assert picked is not None and picked["result"] == "healed", picked


def test_the_guardian_pick_shares_the_loaders_name_rule_and_caps(mod):
    """The pick runs as `python3 -I -S -` inside the container, so it carries
    its own copy of the loader's rules: the same name pattern, the same caps."""
    from genesis.guardian import collector as gc

    assert mod.REPORT_NAME.pattern == gc._REPAIR_NAME
    assert gc._REPAIR_REPORT_BYTES == mod.REPORT_BYTES
    assert gc._REPAIR_REPO_BYTES == mod.REPO_BYTES
    assert gc._REPAIR_FUTURE_S == mod.REPORT_FUTURE_S
    assert gc._REPAIR_READS == _dh().REPAIR_REPORTS_READ
    assert gc._REPAIR_DISMISSED == mod.DISMISS_MARK
    keep = importlib.util.spec_from_file_location("_keep", Path(mod.__file__).parent / "repair_report_keep.py")
    kept = importlib.util.module_from_spec(keep)
    keep.loader.exec_module(kept)
    assert kept.DISMISS_MARK == mod.DISMISS_MARK
    names = ["20260101T000000Z-42-live-move-repair", "20260101T000000Z-42-live-move-restore",
             "20260101T000000Z-live-move-repair", "x20260101T000000Z-42-live-move-repair",
             "20260101T000000Z-42-live-move-repair.bak", "2026010T000000Z-42-live-move-repair"]  # fmt: skip
    for name in names:
        assert bool(kept.REPORT_NAME.fullmatch(name)) == bool(mod.REPORT_NAME.fullmatch(name)), name


@pytest.mark.parametrize("case", ["bad-name", "linked-dir", "nul-repo", "long-repo"])
def test_the_guardian_pick_refuses_what_the_loader_refuses(tmp_path, mod, case):
    """A name the writer never makes, a symlinked report dir and a repo the
    loader cannot compare are skipped by the pick as by load_report (the
    control: the same report as a plain dir with the writer's name is picked)."""
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-1"
    gen = tmp_path / ".genesis"
    checkout = tmp_path / "genesis"
    checkout.mkdir()
    report = {"ts": ts, "result": "healed", "repo": str(checkout), "paths": []}
    d = _report_at(gen, ts, **report)
    assert _guardian_pick(tmp_path) is not None, "control: the plain report is picked"
    if case == "bad-name":
        d.rename(d.with_name(f"{ts}; x{mod.SUFFIX}"))
    elif case == "linked-dir":
        elsewhere = tmp_path / "elsewhere"
        d.rename(elsewhere)
        os.symlink(elsewhere, d)
    else:
        bad = str(checkout) + ("\0" if case == "nul-repo" else "/" * 4096)
        (d / "REPAIR.json").write_text(json.dumps({**report, "repo": bad}))
    with pytest.raises(mod.BadReport):
        mod.load_report(str(next((gen / "deploy-backups").iterdir())), str(checkout))
    assert _guardian_pick(tmp_path) is None


def test_the_refusal_log_set_is_bounded(monkeypatch, caplog):
    """Refused report names come from a user-writable directory, so the set that
    keeps each refusal to one log line has a cap: past it a refusal is still
    counted by the reader, only no longer logged or remembered."""
    dh = _dh()
    monkeypatch.setattr(dh, "_REPAIR_REFUSALS_LOGGED", set())
    with caplog.at_level("WARNING", logger=dh.logger.name):
        for i in range(dh._REPAIR_REFUSALS_LOG_CAP + 50):
            dh._refused(f"crafted-{i}", "not a name the repair writes")
    assert len(dh._REPAIR_REFUSALS_LOGGED) == dh._REPAIR_REFUSALS_LOG_CAP
    logged = [r for r in caplog.records if "crafted-" in r.getMessage()]
    assert len(logged) == dh._REPAIR_REFUSALS_LOG_CAP


def _guard_spy(monkeypatch, verdict):
    """The real guard module, loaded the way the reader loads it, with verdict replaced."""
    dh = _dh()
    real_script = dh._script
    seen = []

    def spying(path):
        module = real_script(path)
        if path == dh._GUARD_SCRIPT:
            seen.append(module)
            module.verdict = verdict(module)
        return module

    monkeypatch.setattr(dh, "_script", spying)
    return dh, seen


def test_the_guard_runs_on_the_health_routes_budget(tmp_path, monkeypatch):
    budgets = []

    def verdict(module):
        def run(*_args):
            budgets.append(module._TIMEOUT_S)
            return module.SAFE, ""

        return run

    dh, seen = _guard_spy(monkeypatch, verdict)
    assert dh._mixed(tmp_path, str(tmp_path)) is False
    assert budgets == [dh._GUARD_GIT_TIMEOUT_S]
    assert dh._GUARD_GIT_TIMEOUT_S < 15  # /api/genesis/health gives up at 15 s


def test_a_git_that_stalls_keeps_the_reports_word(tmp_path, monkeypatch):
    def verdict(_module):
        def run(*_args):
            raise subprocess.TimeoutExpired(cmd="git", timeout=5)

        return run

    dh, _ = _guard_spy(monkeypatch, verdict)
    assert dh._mixed(tmp_path, str(tmp_path)) is True


def test_a_backups_folder_that_cannot_be_read_is_refused_not_empty(tmp_path):
    (tmp_path / "deploy-backups").write_text("a file where the folder should be")
    found = _dh().collect_live_repairs(tmp_path, tmp_path)
    assert found["refused"] == 1
    assert found["reports"] == []


def test_no_backups_folder_is_simply_no_reports(tmp_path):
    found = _dh().collect_live_repairs(tmp_path, tmp_path)
    assert found["refused"] == 0


def test_a_name_rule_that_raises_anything_is_a_refusal(tmp_path, monkeypatch):
    backups = tmp_path / "deploy-backups"
    (backups / f"20260101T000000Z-1{SUFFIX}").mkdir(parents=True)
    dh = _dh()
    real_script = dh._script

    def changed(path):
        module = real_script(path)
        if path == dh._REPAIR_SCRIPT:

            def bad_order(_ts):
                raise TypeError("a later module changed this signature")

            module.report_order = bad_order
        return module

    monkeypatch.setattr(dh, "_script", changed)
    found = dh.collect_live_repairs(tmp_path, tmp_path)
    assert found["refused"] == 1


# ── D1f: the forward repair's reports ───────────────────────────────────────


def _restore_parts(r: subprocess.CompletedProcess) -> dict[tuple[str, str], str]:
    """(path, part) -> verdict from `--restore TS --check`'s per-part lines."""
    out = {}
    for line in r.stdout.splitlines():
        if line.startswith('"'):
            path, end = json.JSONDecoder().raw_decode(line)
            part, verdict = line[end:].split()[:2]
            out[(path, part)] = verdict
    return out


def test_d1f_a_forward_repairs_foreign_path_is_restorable(w):
    """After the forward repair HEAD is the report's tag on `live`, the
    restore's precondition: --restore TS --check reads its foreign part
    as-left, the restore puts it back, and the reader's count then clears."""
    killed_rollback(w, "P")
    (w.root / "sub" / "s1").write_text("mine\n")  # the rollback had not reached it
    r = w.repair()
    assert r.returncode == 0, r.stdout + r.stderr
    assert _git(w.root, "rev-parse", "HEAD") == w.tag == w.report()["tag"]
    assert _git(w.root, "rev-parse", "live") == w.tag
    ts = w.report()["ts"]
    assert _found(w)["saved"] == 1, "control: the reader counts the saved path"
    check = restore(w, ts, "--check")
    parts = _restore_parts(check)
    assert check.returncode == 0, check.stdout + check.stderr
    assert parts and set(parts.values()) == {"as-left"}, parts
    assert any(path == "sub/s1" for path, _ in parts), parts
    put = restore(w, ts)
    assert put.returncode == 0, put.stdout + put.stderr
    assert (w.root / "sub" / "s1").read_text() == "mine\n"
    assert _found(w)["saved"] == 0


def test_td19_hooks_pending_are_reported_until_the_retry_puts_them_back(w):
    dh = _dh()
    killed_rollback(w, "P")
    assert w.repair(SPY_RC="1").returncode == 0
    found = _found(w)
    assert found["hooks_pending"] == 1, found
    assert dh.live_repair_findings(found) == ["live_repair_hooks_pending:1"]
    assert w.repair(mode="--boot").returncode == 0  # the retry puts them back
    found = _found(w)
    assert found["hooks_pending"] == 0 and dh.live_repair_findings(found) == [], found


def test_td19_hooks_pending_after_live_moved_or_dismissed_are_not_reported(w):
    """Once `live` left the tag the writer supersedes the entry on its next
    run; the reader does not wait for that run. A dismissed report is not
    reported."""
    killed_rollback(w, "P")
    assert w.repair(SPY_RC="1").returncode == 0
    _git(w.root, "commit", "-q", "--allow-empty", "-m", "the next update")
    assert _found(w)["hooks_pending"] == 0
    _git(w.root, "reset", "-q", "--hard", w.tag)
    assert _found(w)["hooks_pending"] == 1, "control: `live` is back at the tag"
    _dismiss(w, w.report()["ts"])
    assert _found(w)["hooks_pending"] == 0


def test_td19_hooks_pending_follow_the_live_ref_not_head(w):
    """SF-2: the writer supersedes an entry only once `refs/heads/live` left
    the tag, and waits while HEAD is merely elsewhere; the reader counts the
    same: HEAD detached elsewhere with `live` at the tag still counts, `live`
    moved with HEAD detached at the tag does not."""
    killed_rollback(w, "P")
    assert w.repair(SPY_RC="1").returncode == 0
    _git(w.root, "checkout", "-q", "--detach", w.to)
    assert _found(w)["hooks_pending"] == 1
    _git(w.root, "checkout", "-q", "--detach", w.tag)
    other = _git(w.root, "commit-tree", "-m", "the next update", f"{w.tag}^{{tree}}")
    _git(w.root, "update-ref", "refs/heads/live", other)
    assert _found(w)["hooks_pending"] == 0


def _live_repo(tmp_path: Path) -> tuple[Path, str]:
    """A checkout with one commit and `live` at it: (repo, that commit)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "one")
    _git(repo, "branch", "live")
    return repo, _git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    "hooks", ["pending", "pending ", "PENDING", 1, True, ["pending"], {"x": 1}, None, "ok",
              "superseded", "reinstalled"],
)  # fmt: skip
def test_td19_only_an_exact_pending_counts(tmp_path, hooks):
    """REPAIR.json is user-writable: only the writer's exact "pending", with
    `live` at the report's tag, counts; any other value counts 0 and nothing
    raises (the reader validates nothing else about the field)."""
    repo, head = _live_repo(tmp_path)
    _report_at(tmp_path, "20260101T000000Z-1", result="healed", repo=str(repo), tag=head,
               paths=[], hooks=hooks)  # fmt: skip
    found = _collect_bounded(tmp_path, repo)
    assert found["refused"] == 0, found
    assert found["hooks_pending"] == int(hooks == "pending"), found


@pytest.mark.parametrize("tag", ["0" * 40, 5, None, ["x"]])
def test_td19_a_pending_report_whose_tag_is_not_live_counts_0(tmp_path, tag):
    repo, _ = _live_repo(tmp_path)
    _report_at(tmp_path, "20260101T000000Z-1", result="healed", repo=str(repo), tag=tag,
               paths=[], hooks="pending")  # fmt: skip
    assert _collect_bounded(tmp_path, repo)["hooks_pending"] == 0


def _spy_git(monkeypatch, answer=None) -> list[tuple[str, ...]]:
    """Every _run_git call deploy health makes (``answer``: replace its result)."""
    dh = _dh()
    calls: list[tuple[str, ...]] = []
    real = dh._run_git

    def spy(repo, *args, timeout):
        calls.append(args)
        return answer if answer is not None else real(repo, *args, timeout=timeout)

    monkeypatch.setattr(dh, "_run_git", spy)
    return calls


def test_td19_the_live_ref_is_read_only_when_a_pending_report_exists(tmp_path, monkeypatch):
    """One bounded rev-parse per snapshot, and none at all unless a read,
    undismissed report has hooks pending."""
    calls = _spy_git(monkeypatch)
    repo, head = _live_repo(tmp_path)
    _report_at(tmp_path, "20260101T000000Z-1", result="healed", repo=str(repo), tag=head,
               paths=[], hooks="ok")  # fmt: skip
    _report_at(tmp_path, "20260101T000001Z-2", result="healed", repo=str(repo), tag=head,
               paths=[], hooks="pending", dismissed_at="2026-01-02T00:00:00Z")  # fmt: skip
    assert _dh().collect_live_repairs(tmp_path, repo)["hooks_pending"] == 0
    assert calls == [], calls
    for i in (3, 4):
        _report_at(tmp_path, f"20260101T00000{i}Z-{i}", result="healed", repo=str(repo),
                   tag=head, paths=[], hooks="pending")  # fmt: skip
    assert _dh().collect_live_repairs(tmp_path, repo)["hooks_pending"] == 2
    assert calls == [("rev-parse", "--verify", "-q", "refs/heads/live^{commit}")], calls


@pytest.mark.parametrize(("answer", "want"), [((-1, "", "timeout"), 1), ((-2, "", "gone"), 1),
                                              ((128, "", "fatal"), 1), ((1, "", ""), 0)])  # fmt: skip
def test_td19_doubt_keeps_the_reports_word(tmp_path, monkeypatch, answer, want):
    """A git that stalls or fails keeps the count (as _mixed keeps
    "unresolved"); exit 1 (no `live` branch: the writer supersedes) is 0."""
    _spy_git(monkeypatch, answer)
    repo, head = _live_repo(tmp_path)
    _report_at(tmp_path, "20260101T000000Z-1", result="healed", repo=str(repo), tag=head,
               paths=[], hooks="pending")  # fmt: skip
    assert _dh().collect_live_repairs(tmp_path, repo)["hooks_pending"] == want


def test_td19_no_live_branch_counts_0(tmp_path):
    repo, head = _live_repo(tmp_path)
    _git(repo, "branch", "-D", "live")
    _report_at(tmp_path, "20260101T000000Z-1", result="healed", repo=str(repo), tag=head,
               paths=[], hooks="pending")  # fmt: skip
    assert _collect_bounded(tmp_path, repo)["hooks_pending"] == 0


@pytest.mark.parametrize("before", [["pre-push"], [], None])
def test_sf_a_a_report_holding_replaced_hooks_is_counted_until_dismissed(tmp_path, before):
    """SF-A: a reinstall that moved hooks aside (``hooks_before``) holds their
    only copy: counted in ``hooks_saved`` (its own finding) while not
    dismissed, the report's entry saying so, as the prune keeps it."""
    dh = _dh()
    repo, head = _live_repo(tmp_path)
    d = _report_at(tmp_path, "20260101T000000Z-1", result="healed", repo=str(repo), tag=head,
                   paths=[], hooks="reinstalled", hooks_before=before)  # fmt: skip
    found = _collect_bounded(tmp_path, repo)
    want = int(bool(before))
    assert found["hooks_saved"] == want and found["reports"][0]["hooks_saved"] is bool(want)
    assert ("live_repair_hooks_saved:1" in dh.live_repair_findings(found)) is bool(want)
    report = json.loads((d / "REPAIR.json").read_text())
    (d / "REPAIR.json").write_text(json.dumps({**report, "dismissed_at": "2026-01-02T00:00:00Z"}))
    assert _collect_bounded(tmp_path, repo)["hooks_saved"] == 0
