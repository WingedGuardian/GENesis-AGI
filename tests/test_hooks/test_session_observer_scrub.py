"""Integration tests for scripts/hooks/session_observer_hook.py secret hygiene.

The observer appends tool activity to a per-session JSONL that feeds memory
extraction + proactive recall — a long-lived sink. These run the hook as a real
subprocess (the CC contract) with HOME pointed at a temp dir, and assert that
secret bodies never land in the JSONL: secret-bearing file reads/edits are
skipped, secret-reading commands are skipped, and inline secret shapes in any
captured text are redacted. Non-secret activity is still captured in full.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent.parent
HOOK = REPO_DIR / "scripts" / "hooks" / "session_observer_hook.py"

_SID = "test-scrub-session"
_TOKEN = "ghp_" + "a" * 36  # a realistic GitHub PAT shape


def _run(payload: dict, home: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(home)}
    env.pop("GENESIS_CC_SESSION", None)  # must not short-circuit
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _observations(home: Path) -> list[dict]:
    f = home / ".genesis" / "sessions" / _SID / "tool_observations.jsonl"
    if not f.exists():
        return []
    return [json.loads(ln) for ln in f.read_text().splitlines() if ln.strip()]


def _raw_jsonl(home: Path) -> str:
    f = home / ".genesis" / "sessions" / _SID / "tool_observations.jsonl"
    return f.read_text() if f.exists() else ""


def _payload(tool_name: str, tool_input: dict, tool_response) -> dict:
    return {
        "tool_name": tool_name,
        "session_id": _SID,
        "tool_input": tool_input,
        "tool_response": tool_response,
    }


def test_read_secret_file_body_not_captured(tmp_path: Path):
    home = tmp_path
    body = f"API_KEY=sk-{'x' * 30}\nDB_PASSWORD=hunter2secret"
    _run(_payload("Read", {"file_path": "/etc/genesis/secrets.env"}, {"content": body}), home)
    obs = _observations(home)
    assert obs and obs[-1]["output_summary"] == "[skipped: secret-bearing path]"
    assert "sk-" + "x" * 30 not in _raw_jsonl(home)
    assert "hunter2secret" not in _raw_jsonl(home)


def test_bash_reading_secret_file_output_skipped(tmp_path: Path):
    home = tmp_path
    _run(_payload("Bash", {"command": "cat ~/.genesis/secrets.env"}, f"TOKEN={_TOKEN}"), home)
    obs = _observations(home)
    assert obs[-1]["output_summary"] == "[skipped: command reads a secret-bearing file]"
    assert _TOKEN not in _raw_jsonl(home)


def test_inline_token_in_output_redacted(tmp_path: Path):
    home = tmp_path
    _run(_payload("Bash", {"command": "echo done"}, f"here is {_TOKEN} in output"), home)
    obs = _observations(home)
    assert "[REDACTED]" in obs[-1]["output_summary"]
    assert _TOKEN not in _raw_jsonl(home)


def test_secret_in_command_redacted(tmp_path: Path):
    home = tmp_path
    _run(_payload("Bash", {"command": f"export API_KEY=sk-{'z' * 25}"}, "ok"), home)
    obs = _observations(home)
    assert "[REDACTED]" in obs[-1]["key_info"]["command"]
    assert "sk-" + "z" * 25 not in _raw_jsonl(home)


def test_edit_secret_file_bodies_not_captured(tmp_path: Path):
    home = tmp_path
    _run(
        _payload(
            "Edit",
            {
                "file_path": "/app/.env",
                "old_string": "API_KEY=oldsecretvalue",
                "new_string": "API_KEY=newsecretvalue",
            },
            {"success": True},
        ),
        home,
    )
    ki = _observations(home)[-1]["key_info"]
    assert ki.get("edit") == "[redacted: secret-bearing file]"
    assert "old_string" not in ki and "new_string" not in ki
    assert "oldsecretvalue" not in _raw_jsonl(home)


def test_normal_activity_still_captured(tmp_path: Path):
    home = tmp_path
    _run(_payload("Bash", {"command": "ls -la"}, "total 8\ndrwxr-xr-x file.py"), home)
    obs = _observations(home)
    assert obs[-1]["key_info"]["command"] == "ls -la"
    assert "file.py" in obs[-1]["output_summary"]


def test_grep_over_secret_path_skipped(tmp_path: Path):
    """N5: Grep results over a secret-bearing path are not captured."""
    home = tmp_path
    _run(
        _payload(
            "Grep",
            {"pattern": "KEY", "path": "/home/u/.aws/credentials"},
            {"matches": [f"KEY={_TOKEN}"]},
        ),
        home,
    )
    obs = _observations(home)
    assert obs[-1]["output_summary"] == "[skipped: secret-bearing path]"
    assert _TOKEN not in _raw_jsonl(home)


def test_reference_store_tool_not_captured(tmp_path: Path):
    """N5: the reference store carries credentials by design — skip args+result."""
    home = tmp_path
    _run(
        _payload(
            "mcp__genesis-memory__reference_store",
            {"concept": "prod db", "body": f"password: {_TOKEN}"},
            {"stored": True, "value": _TOKEN},
        ),
        home,
    )
    obs = _observations(home)
    assert obs[-1]["key_info"] == {"note": "[redacted: credential-store tool]"}
    assert obs[-1]["output_summary"] == "[skipped: credential-store tool]"
    assert _TOKEN not in _raw_jsonl(home)
