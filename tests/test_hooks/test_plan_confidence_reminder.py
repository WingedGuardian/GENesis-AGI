"""The plan-confidence reminder: fires every time, judges nothing, blocks nothing.

THIS SUITE IS SMALL ON PURPOSE, and its predecessor's size is the reason. That
version read the plan and stayed silent when it found a confidence figure, so it
needed controls for a percentage regex, a due-diligence vocabulary, an opt-out
parser, fence-stripping, a size cap, and a boundary sweep across six offsets --
and every defect two independent reviewers found lived in that apparatus rather
than in the hook. Nothing here tests a detector, because there is no detector.

What is left to assert is the contract: it emits, it emits EVERY time regardless
of plan content, it never costs a tool call, and it is scoped to ExitPlanMode.

Install-agnostic: synthetic payloads, subprocess isolation, no network, no DB.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parent.parent.parent
_HOOK = _WORKTREE / "scripts" / "hooks" / "plan_confidence_reminder.py"


def _load():
    spec = importlib.util.spec_from_file_location("_plan_confidence_reminder", _HOOK)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_plan_confidence_reminder"] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop("_plan_confidence_reminder", None)
        raise
    return mod


hook = _load()


def _run(payload: object) -> subprocess.CompletedProcess:
    """Invoke it as CC does -- a fresh process fed JSON on stdin."""
    return subprocess.run(
        [sys.executable, str(_HOOK)],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True, text=True, timeout=30,
    )


def _payload(plan: object = "# Plan\n\nDo the thing.") -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "ExitPlanMode",
        "tool_input": {"plan": plan, "planFilePath": "/synthetic/plans/p.md"},
    }


def _emitted(proc: subprocess.CompletedProcess) -> dict:
    return json.loads(proc.stdout)["hookSpecificOutput"]


# --------------------------------------------------------------------------
# IT FIRES, AND IT ALLOWS.
# --------------------------------------------------------------------------

def test_it_emits_the_reminder_and_allows_the_tool():
    proc = _run(_payload())
    assert proc.returncode == 0
    out = _emitted(proc)
    assert out["permissionDecision"] == "allow"
    assert out["hookEventName"] == "PreToolUse"
    assert "CONFIDENCE" in out["additionalContext"]
    assert "DUE DILIGENCE" in out["additionalContext"]


@pytest.mark.parametrize(
    "plan",
    [
        "# Plan\n\nDo the thing.",                       # states nothing
        "# Plan\n\nItem A: 85% confident. MEASURED: 7.",  # states everything
        "Confidence: none - docs only",                   # an old opt-out form
        "",                                               # empty
        "x" * 200_000,                                    # far past the old cap
    ],
    ids=["bare", "already-compliant", "old-optout", "empty", "huge"],
)
def test_it_fires_regardless_of_what_the_plan_SAYS(plan):
    """THE POINT. A previous version stayed silent when it found a percentage,
    and exempted anything past 64KB. Both were detection, and detection is what
    the owner ruled out: fire the language every time, whether or not it has
    already been done."""
    proc = _run(_payload(plan))
    assert proc.returncode == 0
    assert "CONFIDENCE" in _emitted(proc)["additionalContext"]


def test_two_identical_calls_both_fire():
    """No state, no self-disarm: the reminder is not something to get past."""
    for _ in range(2):
        assert "CONFIDENCE" in _emitted(_run(_payload()))["additionalContext"]


# --------------------------------------------------------------------------
# IT NEVER COSTS A TOOL CALL. An advisory hook that can fail is a gate.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    ["", "   ", "not json", "[]", '"a string"', "null", '{"tool_input": 3}',
     '{"tool_name": "ExitPlanMode"}'],
)
def test_a_malformed_payload_never_blocks(raw):
    assert _run(raw).returncode == 0


def test_it_never_returns_a_blocking_exit_code():
    """exit 2 is the PreToolUse deny convention. This hook must never reach it --
    nothing about a plan lacking a figure is irreversible, which is the only
    thing that earns a refusal (the install's standing hook axiom)."""
    src = _HOOK.read_text(encoding="utf-8")
    assert "return 2" not in src and "exit(2)" not in src
    assert "\"deny\"" not in src and "'deny'" not in src


def test_a_crash_inside_main_still_exits_zero():
    """The copy lives BESIDE the real hook, not in tmp_path.

    A copy elsewhere cannot import `hook_input`, so it dies at import time and
    tests the wrong thing -- which is how the first version of this test failed
    for a reason unrelated to the property it names.
    """
    broken = _HOOK.parent / "_broken_reminder_probe.py"
    broken.write_text(
        _HOOK.read_text(encoding="utf-8").replace(
            "    payload = read_payload()",
            "    raise RuntimeError('injected')\n    payload = read_payload()",
        ),
        encoding="utf-8",
    )
    try:
        proc = subprocess.run([sys.executable, str(broken)],
                              input=json.dumps(_payload()),
                              capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, proc.stderr
    finally:
        broken.unlink(missing_ok=True)


def test_even_an_unimportable_hook_cannot_block(tmp_path):
    """The one failure the try/except CANNOT catch is an ImportError at module
    scope. It exits 1 -- and under the PreToolUse contract only exit 2 blocks, so
    the tool still runs and the cost is a missing reminder. Pinned because the
    distinction is what makes an advisory hook safe to leave unattended."""
    orphan = tmp_path / "orphan.py"
    orphan.write_text(_HOOK.read_text(encoding="utf-8"), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(orphan)], input=json.dumps(_payload()),
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode != 2, "an advisory hook must never reach the deny code"


# --------------------------------------------------------------------------
# SCOPE.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tool", ["Bash", "Write", "SomeFutureTool"])
def test_it_says_nothing_about_other_tools(tool):
    """Scoping is intrinsic, not inherited from the settings matcher."""
    payload = _payload()
    payload["tool_name"] = tool
    proc = _run(payload)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_the_reminder_names_both_asks_and_stays_under_the_cap():
    """It replaces a sentence that always named both. And it is emitted through
    the bounded writer, so it cannot breach the harness's stdout cap -- the one
    bound here that is externally imposed rather than invented."""
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("_ho", _WORKTREE / "scripts" / "hooks" / "hook_output.py")
    ho = module_from_spec(spec)
    sys.modules["_ho"] = ho
    spec.loader.exec_module(ho)

    assert "CONFIDENCE" in hook.REMINDER and "DUE DILIGENCE" in hook.REMINDER
    assert len(_run(_payload()).stdout) < ho.HOOK_STDOUT_CAP
