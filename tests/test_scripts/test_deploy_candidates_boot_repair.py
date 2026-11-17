"""The deploy engine's boot repair checks (#3492 D2c).

Admission refuses a candidate that changes the boot repair kit or the units
that run it, and readiness refuses `live` while the kit is not in place or the
LOADED server unit does not run it under a long enough start timeout (T-D14).
update.sh installs HEAD's kit right before its live rebuild, so readiness there
finds the kit that install just healed (T-D27).

The kit status and the systemctl read are seams (``gate.kit_status``,
``gate.unit_show``); the env test runs the real ``unit_show`` against a stub
systemctl on PATH. Every kit is installed into a scratch HOME.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_scripts._deploy_candidates_world import WIRED_UNIT
from tests.test_scripts._live_repair_world import KIT_FILES, _git_env
from tests.test_scripts._update_source import update_text

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="posix-only")

NEW_REFUSED = (
    "scripts/lib/live_move_repair.py",
    "scripts/lib/live_move_boot.py",
    "scripts/lib/live_move_restore.py",
    "scripts/lib/live_move_restore_rows.py",
    "scripts/lib/live_move_restore_runs.py",
    "scripts/lib/live_move_restore_write.py",
    "scripts/systemd/genesis-server.service.template",
    "scripts/systemd/genesis-bridge.service.template",
)


def _kit_sources() -> tuple[str, ...]:
    tree = ast.parse((SCRIPTS / "lib" / "live_move_repair.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "KIT_SOURCES":
            return ast.literal_eval(node.value)
    raise AssertionError("no KIT_SOURCES")


# ── admission ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", NEW_REFUSED)
def test_a_candidate_changing_the_boot_repair_is_refused(dc, path):
    assert dc.gate.path_refusal(path) is not None, path


def test_every_kit_source_and_every_unit_running_the_kit_is_refused(dc):
    """Derived, so a kit source or unit added later fails here until listed."""
    units = [
        f"scripts/systemd/{p.name}"
        for p in (SCRIPTS / "systemd").glob("*.template")
        if "/.genesis/lib/live-repair/current" in p.read_text()
    ]
    assert len(units) >= 2, units
    for path in [f"scripts/lib/{n}" for n in _kit_sources()] + units:
        assert dc.gate.path_refusal(path) is not None, path


# ── readiness: the kit (T-D14) ─────────────────────────────────────────────


def _repo(dc, tmp_path: Path, env: dict | None = None):
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True, exist_ok=True)
    return dc.core.Repo(tmp_path, env or {"HOME": str(home), "PATH": os.environ["PATH"]})


def _wired(monkeypatch, dc, stdout: str = WIRED_UNIT, rc: int = 0, err: str = "") -> None:
    monkeypatch.setattr(
        dc.gate, "unit_show", lambda repo: subprocess.CompletedProcess([], rc, stdout, err)
    )


@pytest.mark.parametrize("state", ["missing", "partial", "corrupt", "foreign"])
def test_readiness_refuses_a_kit_that_cannot_run(dc, tmp_path, monkeypatch, state):
    _wired(monkeypatch, dc)
    monkeypatch.setattr(dc.gate, "kit_status", lambda repo: {"state": state, "why": "x"})
    fails = dc.gate.boot_repair_failures(_repo(dc, tmp_path))
    assert len(fails) == 1 and f"kit is {state}" in fails[0], fails
    assert "live_repair_kit.py install" in fails[0]


def test_readiness_refuses_a_kit_it_cannot_judge(dc, tmp_path, monkeypatch):
    _wired(monkeypatch, dc)

    def boom(repo):
        raise OSError("unreadable")

    monkeypatch.setattr(dc.gate, "kit_status", boom)
    [fail] = dc.gate.boot_repair_failures(_repo(dc, tmp_path))
    assert "unreadable" in fail and "live_repair_kit.py install" in fail


def test_readiness_passes_an_ok_kit_and_only_notes_a_stale_one(dc, tmp_path, monkeypatch, capsys):
    _wired(monkeypatch, dc)
    monkeypatch.setattr(dc.gate, "kit_status", lambda repo: {"state": "ok", "differs": []})
    assert dc.gate.boot_repair_failures(_repo(dc, tmp_path)) == []
    assert "NOTE" not in capsys.readouterr().out
    stale = {"state": "stale", "differs": ["alert_queue.sh"]}
    monkeypatch.setattr(dc.gate, "kit_status", lambda repo: stale)
    assert dc.gate.boot_repair_failures(_repo(dc, tmp_path)) == []
    out = capsys.readouterr().out
    assert "NOTE" in out and "alert_queue.sh" in out


def test_readiness_failures_carries_the_boot_repair_refusal(dc, dc_ready, monkeypatch, capsys):
    w = dc_ready
    w.candidate("feat/x", {"x.txt": "x\n"})
    monkeypatch.setattr(dc.gate, "kit_status", lambda repo: {"state": "missing"})
    assert w.add(dc, "feat/x") == 1
    assert "the boot repair kit is missing" in capsys.readouterr().err
    assert not w.manifest_path.exists()


def test_the_real_kit_status_reads_the_scratch_home(dc, tmp_path):
    """No seam: the installer is loaded by path and judges HOME's kit (none)."""
    assert dc.gate.kit_status(_repo(dc, tmp_path))["state"] == "missing"


# ── readiness: the loaded unit (T-D14, F8) ─────────────────────────────────


@pytest.fixture
def kit_ok(dc, monkeypatch):
    monkeypatch.setattr(dc.gate, "kit_status", lambda repo: {"state": "ok", "differs": []})


@pytest.mark.parametrize(
    "line",
    [
        # no kit line: the SQLite pre-step only
        "ExecStartPre={ path=/bin/sh ; argv[]=/bin/sh -c mkdir -p d ; ignore_errors=yes }",
        # the kit without --boot
        "ExecStartPre={ path=/bin/sh ; argv[]=/bin/sh -c cd $$1/.genesis/lib/live-repair/current"
        " ; ignore_errors=no }",
        # the kit line made `-`-prefixed: a 75 would be ignored and the start go on
        "ExecStartPre={ path=/bin/sh ; argv[]=/bin/sh -c cd $$1/.genesis/lib/live-repair/current"
        " ; python3 x --boot s r ; ignore_errors=yes }",
    ],
)
def test_readiness_refuses_a_unit_that_does_not_run_the_repair(
    dc, tmp_path, monkeypatch, kit_ok, line
):
    _wired(monkeypatch, dc, f"{line}\nTimeoutStartUSec=1min 30s\n")
    [fail] = dc.gate.boot_repair_failures(_repo(dc, tmp_path))
    assert "does not run the boot repair" in fail
    assert "scripts/update.sh" in fail and "live_repair_kit.py install" in fail
    assert "--take-template genesis-server.service" in fail


@pytest.mark.parametrize(
    "value,ok",
    [
        ("1min 10s", False),
        ("0", False),
        ("garbage", False),
        ("1min 30s", True),
        ("2min", True),
        ("infinity", True),
        ("1h 500ms", True),
    ],
)
def test_readiness_refuses_a_start_timeout_under_the_boot_deadline(
    dc, tmp_path, monkeypatch, kit_ok, value, ok
):
    _wired(monkeypatch, dc, WIRED_UNIT.replace("1min 30s", value))
    fails = dc.gate.boot_repair_failures(_repo(dc, tmp_path))
    assert (fails == []) is ok, fails
    if not ok:
        assert "TimeoutStartUSec" in fails[0] and "daemon-reload" in fails[0]


def test_readiness_refuses_when_systemd_cannot_be_asked(dc, tmp_path, monkeypatch, kit_ok):
    _wired(monkeypatch, dc, "", rc=1, err="Failed to connect to bus: No medium found")
    [fail] = dc.gate.boot_repair_failures(_repo(dc, tmp_path))
    assert "cannot confirm" in fail and "No medium found" in fail

    def hang(repo):
        raise subprocess.TimeoutExpired(["systemctl"], 10)

    monkeypatch.setattr(dc.gate, "unit_show", hang)
    [fail] = dc.gate.boot_repair_failures(_repo(dc, tmp_path))
    assert "cannot confirm" in fail


def _stub_systemctl(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    rec = tmp_path / "rec.json"
    stub = bin_dir / "systemctl"
    stub.write_text(
        "#!/usr/bin/env python3\nimport json, os, sys\n"
        f"json.dump({{'argv': sys.argv[1:], 'env': dict(os.environ)}}, open({str(rec)!r}, 'w'))\n"
        f"sys.stdout.write({WIRED_UNIT!r})\n"
    )
    stub.chmod(0o755)
    return rec


@pytest.mark.parametrize("bus_set", [False, True])
def test_unit_show_defaults_the_user_bus_the_way_the_watchdog_does(dc, tmp_path, kit_ok, bus_set):
    rec = _stub_systemctl(tmp_path)
    env = {"HOME": str(tmp_path / "home"), "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"}
    if bus_set:
        env.update(XDG_RUNTIME_DIR="/x", DBUS_SESSION_BUS_ADDRESS="unix:path=/x/bus")
    assert dc.gate.boot_repair_failures(_repo(dc, tmp_path, env)) == []
    got = json.loads(rec.read_text())
    run = f"/run/user/{os.getuid()}"
    want = ("/x", "unix:path=/x/bus") if bus_set else (run, f"unix:path={run}/bus")
    assert (got["env"]["XDG_RUNTIME_DIR"], got["env"]["DBUS_SESSION_BUS_ADDRESS"]) == want
    assert got["argv"][:4] == ["--user", "show", "genesis-server.service", "-p"]
    assert {"ExecStartPre", "TimeoutStartUSec"} <= set(got["argv"])


def test_the_template_line_is_what_readiness_looks_for(dc):
    """The server template's kit line, as systemd shows it (argv joined, `$$`
    kept), matches: a template change that drops `--boot` fails here first."""
    text = (SCRIPTS / "systemd" / "genesis-server.service.template").read_text()
    [line] = [ln for ln in text.splitlines() if "live-repair/current" in ln]
    assert not line.startswith("ExecStartPre=-")
    # As systemd unescapes it: `\\n` becomes a newline, `%%` a `%`.
    argv = line.removeprefix("ExecStartPre=").replace("\\n", "\n").replace("%%", "%")
    shown = f"ExecStartPre={{ path=/bin/sh ; argv[]={argv} ; ignore_errors=no ; status=0/0 }}"
    assert dc.gate.wired(shown)


def test_an_entry_systemd_prints_across_lines_is_judged_whole(dc):
    """D2c review SF-1: systemd prints a `\\n` inside an entry as a real newline,
    so an entry can span lines. The kit entry is judged whole; a `-` prefix
    (ignore_errors=yes) still fails, and so does --boot on another entry."""
    kit = "/home/u/.genesis/lib/live-repair/current"
    multi = (
        "ExecStartPre={ path=/bin/sh ; argv[]=/bin/sh -c 'echo a\nb' ; ignore_errors=yes ; status=0/0 }\n"
        f"ExecStartPre={{ path=/bin/sh ; argv[]=/bin/sh -c 'd={kit}; x\n"
        'y; python3 "$d/live_move_repair.py" --boot "$s"\' ; ignore_errors=no ; status=0/0 }\n'
        "TimeoutStartUSec=1min 30s\n"
    )
    assert dc.gate.wired(multi)
    assert not dc.gate.wired(multi.replace("; ignore_errors=no ;", "; ignore_errors=yes ;"))
    split = (
        f"ExecStartPre={{ path=/bin/sh ; argv[]=/bin/sh -c 'd={kit}' ; ignore_errors=no ; status=0/0 }}\n"
        "ExecStartPre={ path=/bin/sh ; argv[]=/bin/sh -c 'x --boot y' ; ignore_errors=no ; status=0/0 }\n"
    )
    assert not dc.gate.wired(split), "the kit and --boot on two different entries"


@pytest.mark.parametrize(("span", "secs"), [("1w", 604800), ("2month", 5259600), ("1y", 31557600)])
def test_long_timespans_read(dc, span, secs):
    assert dc.gate.timespan_s(span) == secs


# ── T-D27: update.sh installs HEAD's kit before the live rebuild ────────────


def _block() -> str:
    m = re.search(
        r"# BEGIN live-kit-install[^\n]*\n(.*?)# END live-kit-install", update_text(), re.S
    )
    assert m, "update.sh has no live-kit-install block"
    return m.group(1)


def test_the_kit_install_sits_on_the_live_path_right_before_the_rebuild():
    text = update_text()
    block = text.index("# BEGIN live-kit-install")
    engine = text.index("# BEGIN live-engine")
    assert engine < block < text.index("# END live-engine")
    assert text.index("genesis_checkout_unlock", engine) < block
    rebuild = text.index('echo "--- Rebuilding live ---"')
    assert text.index("# END live-kit-install") < rebuild
    assert 'deploy_candidates.py" rebuild' not in text[block:rebuild]


def _scratch_repo(tmp_path: Path, branch: str = "main") -> Path:
    root = tmp_path / "root"
    lib = root / "scripts" / "lib"
    lib.mkdir(parents=True)
    for name in KIT_FILES:
        shutil.copy2(SCRIPTS / "lib" / name, lib / name)
    env = _git_env()
    for args in (("init", "-q", "-b", branch), ("add", "-A"), ("commit", "-qm", "kit")):
        subprocess.run(["git", "-C", str(root), *args], check=True, env=env, capture_output=True)
    return root


def _run_block(root: Path, home: Path) -> subprocess.CompletedProcess:
    script = (
        "set -Eeuo pipefail\n"
        f'SCRIPT_DIR="{SCRIPTS}"\nGENESIS_ROOT="{root}"\n'
        'exec {_UPDATE_LOCK_FD}>"$HOME/.genesis/update.lock"\n' + _block() + "echo AFTER\n"
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=_git_env(home), timeout=120
    )


def test_the_pre_rebuild_install_heals_a_missing_kit_before_readiness(dc, tmp_path, monkeypatch):
    root = _scratch_repo(tmp_path)
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    _wired(monkeypatch, dc)
    repo = dc.core.Repo(root, {"HOME": str(home), "PATH": os.environ["PATH"]})
    assert "kit is missing" in " ".join(dc.gate.boot_repair_failures(repo))
    r = _run_block(root, home)
    assert r.returncode == 0 and "AFTER" in r.stdout, r.stdout + r.stderr
    assert dc.gate.kit_status(repo)["state"] == "ok"
    assert dc.gate.boot_repair_failures(repo) == []


def test_a_failed_pre_rebuild_install_is_never_fatal(tmp_path):
    root = _scratch_repo(tmp_path, branch="feat/x")  # never a kit from a feature branch
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    r = _run_block(root, home)
    assert r.returncode == 0 and "AFTER" in r.stdout, r.stdout + r.stderr
    assert "NOTE: the live-move repair kit was not installed" in r.stdout
    assert not (home / ".genesis" / "lib" / "live-repair" / "current").exists()
