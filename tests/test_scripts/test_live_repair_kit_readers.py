"""#3492 D2b: every reader of a repair report or kit uses the kit that writes
them (closes #3892).

Deploy health judges the installed kit (live_repair_kit.status) as a
``live_repair_kit:<state>`` finding and, with the kit ``ok``, reads the reports
with the kit's own loader. The guardian's report pick is a stub that runs the
kit's ``newest_eligible``, else HEAD's (a kit older than the function included).
Every kit here is made by the installer itself, in a scratch HOME.
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_scripts._live_repair_world import KIT_FILES, LIB, MODULE, _git_env, _installer

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="git + posix")

SUFFIX = "-live-move-repair"


def _dh():
    return importlib.import_module("genesis.observability.snapshots.deploy_health")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=_git_env()
    ).stdout.strip()


def _checkout(root: Path) -> Path:
    """A checkout on main holding the kit sources at HEAD."""
    lib = root / "scripts" / "lib"
    lib.mkdir(parents=True)
    for name in KIT_FILES:
        shutil.copy2(LIB / name, lib / name)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "kit")
    return root


def _install(repo: Path, home: Path) -> Path:
    assert _installer().install(str(repo), str(home)) == 0
    return _current(home).resolve()


def _current(home: Path) -> Path:
    return home / ".genesis" / "lib" / "live-repair" / "current"


def _now_ts(pid: int = 1) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{pid}"


def _report(home: Path, ts: str, repo: Path, **over) -> Path:
    d = home / ".genesis" / "deploy-backups" / f"{ts}{SUFFIX}"
    d.mkdir(parents=True)
    report = {
        "ts": ts,
        "repo": str(repo),
        "result": "healed",
        "paths": [{"path": "Z", "class": "foreign"}],
    }
    (d / "REPAIR.json").write_text(json.dumps({**report, **over}))
    return d


def _make(state: str, repo: Path, home: Path) -> None:
    """The kit in ``home`` left in ``state``, as live_repair_kit.status names it."""
    if state == "missing":
        return
    if state == "foreign":
        _current(home).mkdir(parents=True)
        shutil.copy2(MODULE, _current(home) / MODULE.name)
        return
    kit = _install(repo, home)
    if state == "partial":
        (kit / "alert_queue.sh").unlink()
    elif state == "corrupt":
        (kit / "KIT.json").write_text("{not json")
    elif state == "stale":
        lib = repo / "scripts" / "lib" / "alert_queue.sh"
        lib.write_text(lib.read_text() + "# a later commit\n")
        _git(repo, "commit", "-qam", "bump")


@pytest.fixture
def world(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))  # the kit lives under $HOME/.genesis/lib
    return _checkout(tmp_path / "repo"), home


# -- deploy health: the kit's state is a finding --------------------------------


@pytest.mark.parametrize("reports", [False, True], ids=["no-report", "a-report"])
@pytest.mark.parametrize("state", ["ok", "missing", "partial", "stale", "corrupt", "foreign"])
def test_the_kit_state_is_a_finding_unless_ok(world, state, reports):
    """Each kit state but ``ok`` is its own finding, with or without a report:
    a kit that cannot run is news before any repair needs it."""
    repo, home = world
    _make(state, repo, home)
    assert _installer().status(str(repo), str(home))["state"] == state, "control: the fixture"
    if reports:
        _report(home, "20260101T000000Z-1", repo)
    found = _dh().collect_live_repairs(home / ".genesis", repo)
    assert found["kit"] == state, found
    want = [] if state == "ok" else [f"live_repair_kit:{state}"]
    saved = ["live_repair_saved:1"] if reports else []
    assert _dh().live_repair_findings(found) == saved + want, found


def test_the_kit_is_read_under_home_whatever_genesis_home_names(world, tmp_path):
    """Review SF-1: the installer, repair-move and the unit use $HOME/.genesis/lib;
    a GENESIS_HOME elsewhere (its deploy-backups read from there) never moves it."""
    repo, home = world
    _install(repo, home)
    elsewhere = tmp_path / "srv" / "gdata"
    elsewhere.mkdir(parents=True)
    assert _dh().collect_live_repairs(elsewhere, repo)["kit"] == "ok"


def test_a_head_that_cannot_be_read_is_an_unknown_kit_state_not_stale(world, tmp_path):
    """Review SF-3: status calls a HEAD it cannot read (a git failure or timeout)
    stale too, with nothing named as differing: unknown, never a finding."""
    repo, home = world
    _install(repo, home)
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    assert _installer().status(str(not_a_repo), str(home))["state"] == "stale", "control"
    found = _dh().collect_live_repairs(home / ".genesis", not_a_repo)
    assert found["kit"] is None and _dh().live_repair_findings(found) == [], found


@pytest.mark.parametrize("state", ["ok", "stale", "missing"])
def test_reports_are_read_with_the_kits_loader_only_while_it_is_ok(world, monkeypatch, state):
    """The kit is the writer that runs: with it ``ok`` its own live_move_repair.py
    reads the reports (resolved physically, as the unit runs it), else the
    checkout's."""
    repo, home = world
    _make(state, repo, home)
    _report(home, "20260101T000000Z-1", repo)
    dh, loaded = _dh(), []
    real = dh._script
    monkeypatch.setattr(dh, "_script", lambda path: loaded.append(Path(path)) or real(path))
    found = dh.collect_live_repairs(home / ".genesis", repo)
    assert found["saved"] == 1, found
    used = [p for p in loaded if p.name == "live_move_repair.py"]
    want = _current(home).resolve() / "live_move_repair.py" if state == "ok" else dh._REPAIR_SCRIPT
    assert used == [want], loaded


# -- the guardian's pick: the kit's newest_eligible, else HEAD's -------------------


def _pick(home: Path) -> bytes:
    from genesis.guardian import collector as gc

    return subprocess.run(
        [sys.executable, "-I", "-S", "-"],
        input=gc._REPAIR_PICK,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        timeout=60,
    ).stdout


def test_the_pick_runs_the_installed_kits_newest_eligible(world):
    """No copy of the loader's rules: what the kit's newest_eligible answers is
    what the pick shows."""
    repo, home = world
    os.symlink(repo, home / "genesis")
    kit = _install(repo, home)
    ts = _now_ts()
    _report(home, ts, repo)
    shown = _pick(home)
    assert shown.endswith(
        (home / ".genesis" / "deploy-backups" / f"{ts}{SUFFIX}" / "REPAIR.json").read_bytes()
    )
    with open(kit / "live_move_repair.py", "a") as fh:
        fh.write(
            f'\n\ndef newest_eligible(root, checkout, now):\n    return {ts!r}, b\'{{"result": "the kit"}}\'\n'
        )
    assert _pick(home).endswith(b'\n{"result": "the kit"}'), (
        "the kit's function was not the one run"
    )


@pytest.mark.parametrize("kit_module", ["older", "unloadable"])
def test_a_kit_without_newest_eligible_falls_back_to_heads_module(world, kit_module):
    """A kit from before D2b (or one whose module will not load) leaves the pick
    to HEAD's module, as `git show HEAD:` (never the working tree): the same
    output. With neither module the pick prints nothing."""
    repo, home = world
    os.symlink(repo, home / "genesis")
    kit = _install(repo, home)
    _report(home, _now_ts(), repo)
    want = _pick(home)
    assert want, "control: the whole kit picks the report"
    module = kit / "live_move_repair.py"
    text = module.read_text()
    module.write_text(
        text.replace("def newest_eligible(", "def _not_yet(")
        if kit_module == "older"
        else text + "\n)\n"
    )
    (repo / "scripts" / "lib" / "live_move_repair.py").write_text(
        "raise SystemExit('the working tree')\n"
    )
    assert _pick(home) == want
    _git(repo, "rm", "-q", "--cached", "scripts/lib/live_move_repair.py")
    _git(repo, "commit", "-qm", "no module at HEAD")
    assert _pick(home) == b""
