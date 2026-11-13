"""genesis-secret-redact's publish guard: a `gh` command carrying a known secrets.env value is refused.

Canary values only; the guard runs as the real hook process with SECRETS_PATH pointed at a scratch file.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GUARD = REPO / "plugins" / "genesis-secret-redact" / "bin" / "publish_guard.py"
KEY = "ck_" + "9a8b7c6d5e4f30211203f4e5d6c7b8a9"


@pytest.fixture
def env(tmp_path):
    secrets = tmp_path / "secrets.env"
    secrets.write_text(f"CANARY_API_KEY={KEY}\n")
    return {**os.environ, "SECRETS_PATH": str(secrets)}


def _decide(command: str, env: dict, cwd: Path) -> subprocess.CompletedProcess:
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}
    return subprocess.run(
        [sys.executable, "-I", str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _denied(run: subprocess.CompletedProcess) -> bool:
    if not run.stdout.strip():
        return False
    return json.loads(run.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize(
    "command",
    [
        f'gh pr create --title t --body "uses {KEY}"',
        f"gh pr comment 12 --body 'key={KEY}'",
        f"GH_REPO=o/r gh issue create -t x -b {KEY}",
        f"/usr/local/bin/gh pr edit 3 --body {KEY}",
        f"echo $(gh api repos/o/r/issues/1/comments -f body={KEY})",
        f"cd /tmp && gh pr review 4 --comment -b {KEY}",
    ],
)
def test_a_value_in_the_command_is_refused(env, tmp_path, command):
    run = _decide(command, env, tmp_path)
    assert run.returncode == 0 and _denied(run)
    reason = json.loads(run.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "CANARY_API_KEY" in reason and KEY not in reason


@pytest.mark.parametrize(
    "spelling", ["--body-file {f}", "-F {f}", "--body-file={f}", "-f body=@{f}", "-F body=@{f}"]
)
def test_a_value_in_a_named_file_is_refused(env, tmp_path, spelling):
    body = tmp_path / "body.md"
    body.write_text(f"Summary\n\nthe key is {KEY}\n")
    run = _decide("gh pr create --title t " + spelling.format(f="body.md"), env, tmp_path)
    assert _denied(run)
    assert "body.md" in json.loads(run.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize(
    "command",
    [
        f"gh secret set CANARY_API_KEY --body {KEY}",
        f"echo {KEY} | gh auth login --with-token",
        f"git commit -m 'note {KEY}'",
        "gh pr create --title t --body 'nothing secret here'",
        "ghost pr create --body x",
    ],
)
def test_other_commands_pass(env, tmp_path, command):
    assert not _denied(_decide(command, env, tmp_path))


def test_a_file_without_values_passes(env, tmp_path):
    (tmp_path / "body.md").write_text("all clean\n")
    assert not _denied(_decide("gh pr create --title t --body-file body.md", env, tmp_path))


def test_no_secrets_file_passes(tmp_path):
    env = {**os.environ, "SECRETS_PATH": str(tmp_path / "absent.env")}
    assert not _denied(_decide(f"gh pr comment 1 --body {KEY}", env, tmp_path))


def test_an_unloadable_resolver_fails_open_and_says_so(tmp_path):
    env = {**os.environ, "SECRETS_PATH": str(tmp_path / "secrets.env"), "GENESIS_REPO_ROOT": ""}
    (tmp_path / "secrets.env").write_text(f"CANARY_API_KEY={KEY}\n")
    bad = tmp_path / "plugin-copy"
    (bad / "bin").mkdir(parents=True)
    (bad / "bin" / "publish_guard.py").write_text(GUARD.read_text())
    (bad / "bin" / "known_values.py").write_text("raise RuntimeError('broken loader')\n")
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": f"gh pr comment 1 --body {KEY}"},
        "cwd": str(tmp_path),
    }
    run = subprocess.run(
        [sys.executable, "-I", str(bad / "bin" / "publish_guard.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert run.returncode == 0 and run.stdout == ""
    assert "did not run" in run.stderr


def test_the_plugin_wires_the_guard_on_bash():
    hooks = json.loads((GUARD.parent.parent / "hooks" / "hooks.json").read_text())
    commands = [
        h["command"]
        for e in hooks["hooks"]["PreToolUse"]
        if e["matcher"] == "^(Monitor|Bash)$"
        for h in e["hooks"]
    ]
    assert any("bin/publish_guard.py" in c for c in commands)


@pytest.mark.parametrize(
    "wrapper",
    [
        "timeout 60 gh",
        "env gh",
        "command gh",
        "sudo gh",
        "time gh",
        "nice gh",
        "xargs gh",
        "if true; then gh",
        "bash -c 'gh",
        'bash -c "gh',
        "! gh",
        "{ gh",
    ],
)
def test_a_wrapped_gh_is_still_checked(env, tmp_path, wrapper):
    # Narrowing the gate to segment starts let 11 of these 12 through (measured in review).
    assert _denied(_decide(f"{wrapper} pr comment 1 --body {KEY}", env, tmp_path))


def test_a_file_named_after_a_cd_is_found(env, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b2.md").write_text(f"key {KEY}\n")
    assert _denied(_decide("cd sub && gh pr comment 1 --body-file b2.md", env, tmp_path))


def test_monitor_gh_is_checked(env, tmp_path):
    payload = {
        "tool_name": "Monitor",
        "tool_input": {"command": f"gh pr comment 1 --body {KEY}"},
        "cwd": str(tmp_path),
    }
    run = subprocess.run(
        [sys.executable, "-I", str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert _denied(run)


@pytest.mark.parametrize(
    "command",
    [
        "bash -c 'gh pr comment 1 --body-file body.md'",
        "sh -c \"bash -c 'gh issue comment 2 --body-file body.md'\"",
        "timeout 60 bash -lc 'cd . && gh pr create --title t -F body=@body.md'",
        'gh pr comment 1 --body "$(cat body.md)"',
        'gh pr comment 1 --body "$(<body.md)"',
    ],
)
def test_a_file_named_inside_a_wrapped_or_substituted_command_is_checked(env, tmp_path, command):
    (tmp_path / "body.md").write_text(f"the key is {KEY}\n")
    assert _denied(_decide(command, env, tmp_path))
