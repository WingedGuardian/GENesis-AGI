"""Tests for the guardian-gateway.sh grow-root / set-container-limits verbs (PR-C).

Runs the REAL gateway against a throwaway ``$HOME`` install, exercising the
shell guard branches where gateway bugs hide: arg-regex rejection (incl. a
flag-shaped token that must NOT hijack main()'s if-chain), the venv-missing and
src-skew guards, and correct dispatch when everything is present. The execute
logic itself is covered by test_grow_capacity.
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


def _stub_python(install: Path, body: str) -> None:
    py = install / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text(body)
    py.chmod(0o755)


def _with_src(install: Path) -> None:
    mod = install / "src" / "genesis" / "guardian"
    mod.mkdir(parents=True, exist_ok=True)
    (mod / "grow_capacity.py").write_text("# stub for the skew guard\n")


# ── grow-root ─────────────────────────────────────────────────────────────


def test_grow_root_rejects_nonnumeric(tmp_path):
    _install(tmp_path)
    proc = _run(tmp_path, "grow-root 40G")  # suffix not allowed
    assert proc.returncode == 1
    assert "invalid arg" in proc.stderr


def test_grow_root_rejects_flag_shaped_token(tmp_path):
    """A flag-shaped arg must be rejected by the regex, never word-split into a
    hijacking argv token (the configure-provisioning lesson)."""
    _install(tmp_path)
    proc = _run(tmp_path, "grow-root --storage-expand")
    assert proc.returncode == 1
    assert "invalid arg" in proc.stderr


def test_grow_root_skew_guard(tmp_path):
    install = _install(tmp_path)
    _stub_python(install, "#!/bin/sh\necho SHOULD_NOT_RUN\n")  # no grow_capacity.py
    proc = _run(tmp_path, "grow-root 40")
    assert proc.returncode == 1
    assert "predates grow-root" in proc.stderr
    assert "SHOULD_NOT_RUN" not in proc.stdout


def test_grow_root_dispatches_when_present(tmp_path):
    install = _install(tmp_path)
    _stub_python(install, '#!/bin/sh\necho "ARGS: $*"\n')
    _with_src(install)
    proc = _run(tmp_path, "grow-root 40")
    assert proc.returncode == 0, proc.stderr
    assert "--grow-root 40" in proc.stdout


# ── set-container-limits ────────────────────────────────────────────────────


def test_set_limits_rejects_bad_args(tmp_path):
    _install(tmp_path)
    assert _run(tmp_path, "set-container-limits abc 2").returncode == 1
    assert _run(tmp_path, "set-container-limits 20480").returncode == 1  # missing cpu


def test_set_limits_accepts_dash_axis(tmp_path):
    install = _install(tmp_path)
    _stub_python(install, '#!/bin/sh\necho "ARGS: $*"\n')
    _with_src(install)
    proc = _run(tmp_path, "set-container-limits 20480 -")
    assert proc.returncode == 0, proc.stderr
    assert "--set-container-limits 20480 -" in proc.stdout


def test_set_limits_skew_guard(tmp_path):
    install = _install(tmp_path)
    _stub_python(install, "#!/bin/sh\necho SHOULD_NOT_RUN\n")
    proc = _run(tmp_path, "set-container-limits 20480 4")
    assert proc.returncode == 1
    assert "predates set-container-limits" in proc.stderr
    assert "SHOULD_NOT_RUN" not in proc.stdout
