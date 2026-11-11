"""The session observer records nothing for an agent type it must never observe.

Some subagents handle text that must not reach memory: a writer of hidden
evaluation scenarios is the case this exists for, because anything the observer
captures is extracted into memories that proactive recall later shows to other
sessions. These run the hook as a real subprocess (the CC contract) with HOME
and the database isolated, the same way test_session_observer_scrub.py does.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parent.parent.parent
HOOK = REPO_DIR / "scripts" / "hooks" / "session_observer_hook.py"

_SID = "test-agent-skip-session"
_SECRET_TEXT = "expected output that must stay unobserved"


def _run(payload: dict, home: Path) -> subprocess.CompletedProcess:
    # HOME and GENESIS_DB_PATH both isolated: see test_session_observer_scrub.py
    # for why HOME alone still reaches the real database.
    env = {
        **os.environ,
        "HOME": str(home),
        "GENESIS_DB_PATH": str(home / "isolated-never-created.db"),
    }
    env.pop("GENESIS_CC_SESSION", None)  # must not short-circuit
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _raw_jsonl(home: Path) -> str:
    f = home / ".genesis" / "sessions" / _SID / "tool_observations.jsonl"
    return f.read_text() if f.exists() else ""


def _payload(**identity) -> dict:
    return {
        "tool_name": "Read",
        "session_id": _SID,
        "tool_input": {"file_path": "/some/dir/SCENARIOS.md"},
        "tool_response": {"content": _SECRET_TEXT},
        **identity,
    }


def test_unobserved_subagent_type_writes_nothing(tmp_path: Path):
    result = _run(_payload(agent_id="a1b2c3", agent_type="holdout-writer"), tmp_path)
    assert result.returncode == 0
    assert _raw_jsonl(tmp_path) == ""


def test_other_subagent_types_are_still_observed(tmp_path: Path):
    _run(_payload(agent_id="a1b2c3", agent_type="Explore"), tmp_path)
    assert _SECRET_TEXT in _raw_jsonl(tmp_path)


def test_main_session_is_still_observed(tmp_path: Path):
    _run(_payload(), tmp_path)
    assert _SECRET_TEXT in _raw_jsonl(tmp_path)


def test_main_session_started_as_the_writer_writes_nothing(tmp_path: Path):
    """A main session started with --agent carries agent_type and no agent_id
    key at all. It is the same writer, so it must not be observed either."""
    result = _run(_payload(agent_type="holdout-writer"), tmp_path)
    assert result.returncode == 0
    assert _raw_jsonl(tmp_path) == ""


@pytest.mark.parametrize("tool_name", ["Agent", "Task"])
def test_parent_agent_call_for_unobserved_type_writes_nothing(tmp_path: Path, tool_name: str):
    """The parent's Agent call (Task in older Claude Code) has no agent_id,
    yet it carries the subagent's prompt and final report."""
    payload = {
        "tool_name": tool_name,
        "session_id": _SID,
        "tool_input": {"subagent_type": "holdout-writer", "prompt": _SECRET_TEXT},
        "tool_response": {"content": [{"type": "text", "text": _SECRET_TEXT}]},
    }
    result = _run(payload, tmp_path)
    assert result.returncode == 0
    assert _raw_jsonl(tmp_path) == ""


@pytest.mark.parametrize("tool_name", ["Agent", "Task"])
def test_parent_agent_call_for_other_type_is_still_observed(tmp_path: Path, tool_name: str):
    payload = {
        "tool_name": tool_name,
        "session_id": _SID,
        "tool_input": {"subagent_type": "Explore", "prompt": "look around"},
        "tool_response": {"content": [{"type": "text", "text": _SECRET_TEXT}]},
    }
    _run(payload, tmp_path)
    assert _SECRET_TEXT in _raw_jsonl(tmp_path)
