"""The PostToolUse hook must survive malformed input and still refresh liveness.

The throttle itself is covered in tests/test_scripts/test_session_heartbeat_throttle.py.
What is covered HERE is the WIRING that calls it, which had no test at all and
shipped a live defect: `_maybe_refresh_heartbeat` was called from a ``finally:``
that read a variable bound INSIDE the ``try``, so empty stdin and malformed JSON
-- the exact paths the surrounding ``except Exception: return`` exists to swallow
-- raised UnboundLocalError PAST that except. This hook is registered on the
``".*"`` PostToolUse matcher, so that put a Python traceback in front of the user
on every tool call in every session.

These run the script as a SUBPROCESS on purpose. The defect was an exit code and
a stderr stream, and neither is observable by importing main() and calling it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_HOOK = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "session_observer_hook.py"


def _run(payload: str, *, home: Path) -> subprocess.CompletedProcess:
    """Invoke the hook exactly as Claude Code does: JSON on stdin, isolated HOME."""
    return subprocess.run(
        [sys.executable, str(_HOOK)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=60,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin", "GENESIS_REPO_ROOT": str(home / "norepo")},
    )


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        ("empty", ""),
        ("whitespace only", "   \n  "),
        ("not json", "not json at all"),
        ("truncated json", '{"session_id": "x"'),
        ("json array", "[]"),
        ("json null", "null"),
        ("json string", '"just a string"'),
        ("json number", "42"),
    ],
)
def test_malformed_input_never_crashes_the_hook(tmp_path, label, payload):
    """REGRESSION for the UnboundLocalError above.

    A hook on the ".*" matcher runs on every tool call, so a nonzero exit here is
    a traceback shown to the user for every single one. The contract in this
    file's own docstring is "Hooks must never crash or block".
    """
    proc = _run(payload, home=tmp_path)
    assert proc.returncode == 0, (
        f"{label}: hook exited {proc.returncode} -- stderr:\n{proc.stderr[:400]}"
    )
    assert proc.stderr == "", f"{label}: hook wrote to stderr:\n{proc.stderr[:400]}"


def test_a_wellformed_payload_also_exits_clean(tmp_path):
    """The happy path, with no database present -- the refresh must degrade, not raise."""
    payload = json.dumps(
        {"tool_name": "Bash", "session_id": "wiring-test-1", "tool_input": {"command": "echo hi"}}
    )
    proc = _run(payload, home=tmp_path)
    assert proc.returncode == 0, f"exited {proc.returncode} -- stderr:\n{proc.stderr[:400]}"
    assert proc.stderr == ""


def test_a_skipped_tool_still_refreshes_liveness(tmp_path):
    """_process returns early for _SKIP_TOOLS, which is exactly why the refresh
    sits OUTSIDE it: a session that spends five minutes inside a skipped tool
    must not read as dead to its peers.

    Asserted on the stamp file rather than the DB, because the stamp is what the
    throttle writes and it needs no schema to observe.
    """
    payload = json.dumps({"tool_name": "TodoWrite", "session_id": "wiring-test-2"})
    proc = _run(payload, home=tmp_path)
    assert proc.returncode == 0, proc.stderr[:400]
    stamp = tmp_path / ".genesis" / "sessions" / "wiring-test-2" / "heartbeat.stamp"
    assert stamp.exists(), (
        "a skipped tool did not refresh liveness -- the refresh has fallen back "
        "inside _process, where _SKIP_TOOLS returns before reaching it"
    )


def test_a_payload_without_a_session_id_is_a_no_op(tmp_path):
    proc = _run(json.dumps({"tool_name": "Bash"}), home=tmp_path)
    assert proc.returncode == 0, proc.stderr[:400]
    assert not (tmp_path / ".genesis" / "sessions").exists()


def test_an_unsafe_session_id_creates_nothing_outside_the_sessions_dir(tmp_path):
    """The id comes from stdin; path containment is delegated to session_path."""
    proc = _run(json.dumps({"tool_name": "Bash", "session_id": "../../escaped"}), home=tmp_path)
    assert proc.returncode == 0, proc.stderr[:400]
    assert not (tmp_path.parent / "escaped").exists()
