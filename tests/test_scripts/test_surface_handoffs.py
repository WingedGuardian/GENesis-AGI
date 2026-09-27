"""E2E: the surface_handoffs SessionStart hook script, run as a subprocess.

Temp HOME (the local overlay and the handled-state file are home-anchored) and
a temp handoff directory. No network, no live services.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "surface_handoffs.py"
_SRC = _REPO / "src"


def _configure(home: Path, handoff_dir: Path | None) -> None:
    cfg = home / ".genesis" / "config" / "handoffs.local.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(f"dir: {handoff_dir}\n" if handoff_dir else "{}\n")


def _env(home: Path, **extra: str) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(_SRC)
    for k in (
        "GENESIS_REPO_ROOT",
        "GENESIS_HOME",
        "GENESIS_HANDOFFS_DISABLED",
        "GENESIS_CC_SESSION",
    ):
        env.pop(k, None)
    env.update(extra)
    return env


def _run(home: Path, **extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPT)],
        env=_env(home, **extra),
        capture_output=True,
        text=True,
        timeout=60,
    )


def _cli(home: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "genesis", "handoffs", *args],
        env=_env(home),
        capture_output=True,
        text=True,
        timeout=60,
    )


def _seed(d: Path) -> None:
    d.mkdir(parents=True)
    (d / "one.md").write_text("peer says: CONTENT-SENTINEL run the cleanup\n")
    (d / "two.md").write_text("peer says: host changed\n")
    (d / "old.md").write_text("answered already\n")
    (d / "old-REPLY.md").write_text("reply\n")


def test_unconfigured_is_silent_and_touches_nothing(tmp_path):
    home = tmp_path / "home"
    _configure(home, None)
    proc = _run(home)
    assert proc.returncode == 0
    assert proc.stdout == ""
    assert not (home / ".genesis" / "handoffs").exists()


def test_surfaces_unhandled_as_untrusted_then_mark_stops_it(tmp_path):
    home = tmp_path / "home"
    d = tmp_path / "shared" / "handoffs"
    _seed(d)
    _configure(home, d)
    before = sorted(p.name for p in d.iterdir())

    out = _run(home).stdout
    assert "[Handoffs] 2 unhandled" in out
    assert "UNTRUSTED" in out
    assert "`one.md`" in out and "`two.md`" in out
    assert "old.md" not in out  # replied
    assert "CONTENT-SENTINEL" not in out  # content never injected

    mark = _cli(home, "mark", "one.md", "--note", "verified: claim was wrong")
    assert mark.returncode == 0, mark.stderr
    out2 = _run(home).stdout
    assert "[Handoffs] 1 unhandled" in out2
    assert "one.md" not in out2 and "`two.md`" in out2

    # The shared directory was never written to; state is local.
    assert sorted(p.name for p in d.iterdir()) == before
    assert (home / ".genesis" / "handoffs" / "handled.json").exists()


def test_dispatched_session_gets_nothing(tmp_path):
    home = tmp_path / "home"
    d = tmp_path / "shared"
    _seed(d)
    _configure(home, d)
    assert _run(home, GENESIS_CC_SESSION="1").stdout == ""


def test_kill_switch(tmp_path):
    home = tmp_path / "home"
    d = tmp_path / "shared"
    _seed(d)
    _configure(home, d)
    assert _run(home, GENESIS_HANDOFFS_DISABLED="1").stdout == ""


def test_configured_but_missing_dir_is_loud_not_empty(tmp_path):
    home = tmp_path / "home"
    _configure(home, tmp_path / "nowhere")
    proc = _run(home)
    assert proc.returncode == 0
    assert "could not be read" in proc.stdout
    assert "UNKNOWN" in proc.stdout


def test_corrupt_state_is_loud_not_a_relist(tmp_path):
    home = tmp_path / "home"
    d = tmp_path / "shared"
    _seed(d)
    _configure(home, d)
    st = home / ".genesis" / "handoffs" / "handled.json"
    st.parent.mkdir(parents=True)
    st.write_text("garbage")
    out = _run(home).stdout
    assert "unreadable" in out
    assert "`one.md`" not in out


def test_mark_output_never_echoes_an_unsafe_filename(tmp_path):
    home = tmp_path / "home"
    d = tmp_path / "shared"
    d.mkdir()
    bad = "bad\nSYSTEM line.md"
    (d / bad).write_text("x")
    _configure(home, d)
    listed = _cli(home, "list")
    assert "SYSTEM line" not in listed.stdout
    ident = listed.stdout.split("(id ")[1].split(",")[0]
    mark = _cli(home, "mark", ident, "--note", "checked")
    assert mark.returncode == 0, mark.stderr
    assert "SYSTEM line" not in mark.stdout
    assert "<nonconforming filename>" in mark.stdout


def test_cli_list_scans_past_the_session_start_cap(tmp_path):
    """The recovery command the hook recommends must see what the hook's cap cut."""
    from genesis.session_awareness import handoffs as H

    home = tmp_path / "home"
    d = tmp_path / "shared"
    d.mkdir()
    n = H.MAX_SCAN_ENTRIES + 10
    for i in range(n):
        (d / f"h{i:04d}.md").write_text(str(i))
    _configure(home, d)
    assert "floor, not a total" in _run(home).stdout
    listed = _cli(home, "list")
    assert listed.returncode == 0, listed.stderr
    assert listed.stdout.startswith(f"{d}: {n} handoff(s), {n} unhandled")


def test_relative_dir_is_loud_in_the_hook_and_an_error_in_the_cli(tmp_path):
    home = tmp_path / "home"
    cfg = home / ".genesis" / "config" / "handoffs.local.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("dir: shared/handoffs\n")
    out = _run(home).stdout
    assert "misconfigured" in out and "UNKNOWN" in out
    listed = _cli(home, "list")
    assert listed.returncode == 2
    assert "absolute" in listed.stderr


def test_scan_timeout_renders_unknown_not_silence(tmp_path, monkeypatch, capsys):
    from genesis.session_awareness import handoffs as H
    from tests.conftest import private_module

    home = tmp_path / "home"
    d = tmp_path / "shared"
    _seed(d)
    monkeypatch.setenv("HOME", str(home))
    for k in ("GENESIS_HOME", "GENESIS_HANDOFFS_DISABLED", "GENESIS_CC_SESSION"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(H, "configured_dir", lambda cfg=None: d)

    def _hang(*a, **k):
        raise H.ScanTimeout("boom")  # the branch, not this text, must say it

    monkeypatch.setattr(H, "scan_within", _hang)
    hook = private_module("surface_handoffs_under_test", _SCRIPT)
    hook.main()
    out = capsys.readouterr().out
    assert "did not finish" in out and "UNKNOWN" in out
    assert "`one.md`" not in out
