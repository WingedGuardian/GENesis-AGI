"""Tests for the guardian-gateway.sh ``test-alert`` verb.

Runs the REAL gateway script against a throwaway ``$HOME`` install with a stub
python that echoes its arguments. Covers forwarding, denial of any argument,
the venv-missing guard and the src-skew guard: an old src has no --test-alert
branch, so the flag would fall through ``main()`` into a full ``run_check``
cycle, and the CLI must never run. The CLI itself is covered by
``tests/test_guardian/test_test_alert.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

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


def _install(home: Path, *, main_text: str | None = 'if "--test-alert" in sys.argv:\n') -> Path:
    d = home / ".local" / "share" / "genesis-guardian"
    (d / "config").mkdir(parents=True)
    (d / "config" / "guardian.yaml").write_text("container_name: genesis\n")
    py = d / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text('#!/bin/sh\necho "ARGS: $*"\necho "ENV: $GUARDIAN_SECRETS"\n')
    py.chmod(0o755)
    if main_text is not None:
        mod = d / "src" / "genesis" / "guardian"
        mod.mkdir(parents=True)
        (mod / "__main__.py").write_text(main_text)
    return d


def test_verb_is_forwarded_with_config_and_secrets(tmp_path):
    install = _install(tmp_path)
    proc = _run(tmp_path, "test-alert")
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert lines[0] == "ARGS: -m genesis.guardian --test-alert"
    assert lines[1] == f"ENV: {install / 'secrets.env'}"


@pytest.mark.parametrize(
    "verb", ["test-alert x", "test-alert --test", "test-alert;id", "test-alerts"]
)
def test_any_argument_is_denied(tmp_path, verb):
    _install(tmp_path)
    proc = _run(tmp_path, verb)
    assert proc.returncode == 1
    assert "ARGS:" not in proc.stdout


def test_no_venv(tmp_path):
    install = _install(tmp_path)
    (install / ".venv" / "bin" / "python").unlink()
    proc = _run(tmp_path, "test-alert")
    assert proc.returncode == 1
    assert "guardian venv not found" in proc.stderr


@pytest.mark.parametrize("main_text", [None, 'if "--test-approval" in sys.argv:\n'])
def test_skew_guard_blocks_fallthrough(tmp_path, main_text):
    """A src without the --test-alert branch (or with no __main__.py) returns a
    clean error and never invokes the CLI."""
    _install(tmp_path, main_text=main_text)
    proc = _run(tmp_path, "test-alert")
    assert proc.returncode == 1
    assert "predates test-alert" in proc.stderr
    assert "ARGS:" not in proc.stdout


def _json_lines(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]


def test_guard_failures_carry_the_documented_shape(tmp_path):
    install = _install(tmp_path, main_text=None)
    (line,) = _json_lines(_run(tmp_path, "test-alert").stderr)
    assert set(line) == {"ok", "action", "sent", "error"} and line["sent"] is False
    (install / ".venv" / "bin" / "python").unlink()
    (line,) = _json_lines(_run(tmp_path, "test-alert").stderr)
    assert set(line) == {"ok", "action", "sent", "error"} and line["sent"] is False


@pytest.mark.parametrize(("rc", "expect_line"), [(124, True), (1, False), (0, False)])
def test_a_timeout_prints_one_unconfirmed_line_and_keeps_the_code(tmp_path, rc, expect_line):
    install = _install(tmp_path)
    (install / ".venv" / "bin" / "python").write_text(f"#!/bin/sh\nexit {rc}\n")
    proc = _run(tmp_path, "test-alert")
    assert proc.returncode == rc
    lines = _json_lines(proc.stdout)
    if expect_line:
        assert lines == [
            {"ok": False, "action": "test-alert", "sent": False, "error": "timed out; delivery not confirmed"}
        ]
    else:
        assert lines == []
