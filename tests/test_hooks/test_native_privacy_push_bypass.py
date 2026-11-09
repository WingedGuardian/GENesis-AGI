"""The user-command guard refuses native-hook bypass without executing Git."""
from pathlib import Path

import pytest

from tests.conftest import private_module

SOURCE = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("command", [
    "git push --no-verify public topic",
    "git push --dry-run --no-verify public topic",
    "env FIXTURE=1 git -C /owned push --no-verify public topic",
    "bash -c 'git push --no-verify public topic'",
])
def test_user_push_cannot_disable_native_privacy(monkeypatch, tmp_path, capsys, command):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / ".genesis"))
    module = private_module("native_privacy_bypass_guard", SOURCE / "scripts/hooks/git_push_guard.py")
    monkeypatch.setattr(module, "read_payload", lambda: {"tool_input": {"command": command}})

    def forbidden(*args, **kwargs):
        raise AssertionError("A parsed native-hook bypass must refuse before external commands")

    monkeypatch.setattr(module.subprocess, "run", forbidden)
    assert module._run_merge_and_push_gates() == 2
    assert "native pre-push" in capsys.readouterr().err
