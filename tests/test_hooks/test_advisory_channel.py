"""Channel regression tests for the advisory hooks (audit H-2 / B7).

web_tools_gate, agent_tool_guidance, stealth_skill_nudge and the
cbm-discovery-gate all emitted a bare top-level ``{"additionalContext": …}``,
which Claude Code silently discards — the nudge never reached the model. They
must emit it nested under ``hookSpecificOutput`` with the correct
``hookEventName``. These run each hook as a subprocess (fresh TMPDIR so the
once-per-session sentinels don't suppress the first fire) and assert the wrapper.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
PY = sys.executable


def _run(script: str, payload: dict, tmp: Path, *, use_bash: bool = False) -> dict | None:
    cmd = ["bash", str(REPO / script)] if use_bash else [PY, str(REPO / script)]
    env = {"PATH": "/usr/bin:/bin", "TMPDIR": str(tmp), "HOME": str(tmp)}
    proc = subprocess.run(
        cmd, input=json.dumps(payload), capture_output=True, text=True, timeout=30, env=env
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def _assert_wrapped(out: dict | None, event: str) -> None:
    assert out is not None, "hook produced no output (nudge dropped)"
    # The bug: a bare top-level additionalContext (silently discarded by CC).
    assert "additionalContext" not in out, "top-level additionalContext is discarded by CC"
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == event
    assert isinstance(hso["additionalContext"], str) and hso["additionalContext"]


def test_web_tools_gate_wrapped(tmp_path: Path) -> None:
    out = _run(
        "scripts/hooks/web_tools_gate.py",
        {"tool_name": "WebFetch", "tool_input": {"url": "https://x.com"}, "session_id": "s1"},
        tmp_path,
    )
    _assert_wrapped(out, "PreToolUse")


def test_agent_tool_guidance_wrapped(tmp_path: Path) -> None:
    out = _run(
        "scripts/hooks/agent_tool_guidance.py",
        {
            "tool_name": "Agent",
            "tool_input": {"prompt": "research the codebase architecture"},
            "session_id": "s2",
        },
        tmp_path,
    )
    _assert_wrapped(out, "PreToolUse")


def test_stealth_skill_nudge_wrapped(tmp_path: Path) -> None:
    out = _run(
        "scripts/hooks/stealth_skill_nudge.py",
        {
            "tool_name": "mcp__genesis-health__browser_navigate",
            "tool_input": {"url": "https://x.com"},
            "tool_response": {"layer": "camoufox"},
            "session_id": "s3",
        },
        tmp_path,
    )
    _assert_wrapped(out, "PostToolUse")


def test_cbm_discovery_gate_wrapped(tmp_path: Path) -> None:
    out = _run(
        ".claude/hooks/cbm-discovery-gate.sh",
        {"tool_name": "Read", "tool_input": {"file_path": "/x/module.py"}},
        tmp_path,
        use_bash=True,
    )
    _assert_wrapped(out, "PreToolUse")
