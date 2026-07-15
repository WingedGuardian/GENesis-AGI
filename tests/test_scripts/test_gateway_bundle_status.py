"""Tests for the guardian-gateway.sh ``bundle-status`` read-only verb (F.4).

Runs the REAL gateway script against a throwaway ``$HOME`` install dir, focusing
on the shell-specific guard branches where gateway bugs hide (the F.0 SIGPIPE
lesson): the venv-missing guard, the src-skew guard (must NOT fall through to a
full ``run_check`` recovery cycle on a stale gateway), and correct dispatch to
``-m genesis.guardian --bundle-status`` when everything is present. The verb's
actual JSON body is covered by ``test_bundle_watch.bundle_archive_status``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_GATEWAY = Path(__file__).resolve().parents[2] / "scripts" / "guardian-gateway.sh"


def _run(home: Path, verb: str):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["SSH_ORIGINAL_COMMAND"] = verb
    return subprocess.run(
        ["bash", str(_GATEWAY)],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def _install(home: Path) -> Path:
    d = home / ".local" / "share" / "genesis-guardian"
    (d / "config").mkdir(parents=True)
    (d / "config" / "guardian.yaml").write_text("container_name: genesis\n")
    return d


def _stub_python(install: Path, body: str) -> Path:
    py = install / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text(body)
    py.chmod(0o755)
    return py


def test_bundle_status_no_venv(tmp_path):
    _install(tmp_path)  # no .venv/bin/python
    proc = _run(tmp_path, "bundle-status")
    assert proc.returncode == 1
    assert "guardian venv not found" in proc.stderr


def test_bundle_status_skew_guard_blocks_fallthrough(tmp_path):
    """A gateway older than the src (no bundle_watch.py) must return a clean error
    — NEVER fall through main()'s if-chain into run_check (a full recovery cycle
    on a routine read-only poll)."""
    install = _install(tmp_path)
    _stub_python(install, "#!/bin/sh\necho SHOULD_NOT_RUN\n")  # would print if invoked
    # bundle_watch.py deliberately absent.
    proc = _run(tmp_path, "bundle-status")
    assert proc.returncode == 1
    assert "predates bundle-status" in proc.stderr
    assert "SHOULD_NOT_RUN" not in proc.stdout


def test_bundle_status_dispatches_when_present(tmp_path):
    """venv + bundle_watch.py present → the verb invokes
    `-m genesis.guardian --bundle-status` (proven by a stub python echoing argv)."""
    install = _install(tmp_path)
    _stub_python(install, '#!/bin/sh\necho "ARGS: $*"\n')
    mod = install / "src" / "genesis" / "guardian"
    mod.mkdir(parents=True)
    (mod / "bundle_watch.py").write_text("# stub for the skew guard\n")

    proc = _run(tmp_path, "bundle-status")
    assert proc.returncode == 0, proc.stderr
    assert "--bundle-status" in proc.stdout
