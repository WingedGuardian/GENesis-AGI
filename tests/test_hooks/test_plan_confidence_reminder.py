"""The plan-confidence reminder: fires every time, judges nothing, blocks nothing.

THIS SUITE IS SMALL ON PURPOSE, and its predecessor's size is the reason. That
version read the plan and stayed silent when it found a confidence figure, so it
needed controls for a percentage regex, a due-diligence vocabulary, an opt-out
parser, fence-stripping, a size cap, and a boundary sweep across six offsets --
and every defect two independent reviewers found lived in that apparatus rather
than in the hook. Nothing here tests a detector, because there is no detector.

What is left to assert is the contract: it emits, it emits EVERY time regardless
of plan content, it never costs a tool call, and it is scoped to the two plan-mode
boundaries -- EnterPlanMode and ExitPlanMode -- each with wording that is true of
its own moment. That last clause is a fix, not a flourish: the ExitPlanMode-only
revision claimed to arrive before the plan was presented, and it does not.

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


def _payload(
    plan: object = "# Plan\n\nDo the thing.", tool: str = "ExitPlanMode"
) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": {"plan": plan, "planFilePath": "/synthetic/plans/p.md"},
    }


def _emitted(proc: subprocess.CompletedProcess) -> dict:
    return json.loads(proc.stdout)["hookSpecificOutput"]


# --------------------------------------------------------------------------
# IT FIRES, AND IT ALLOWS.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("tool", ["EnterPlanMode", "ExitPlanMode"])
def test_it_emits_the_reminder_and_allows_the_tool(tool):
    proc = _run(_payload(tool=tool))
    assert proc.returncode == 0
    out = _emitted(proc)
    assert out["hookEventName"] == "PreToolUse"
    # NOT `permissionDecision: "allow"`, which every other advisory hook here
    # emits. `allow` asserts the permission prompt should be SKIPPED, and
    # ExitPlanMode's whole purpose is to put a decision in front of the user.
    # READ from the CC bundle: the field is optional, the permission switch is
    # gated on its presence, and additionalContext is yielded independently — so
    # silence delivers the reminder and expresses no opinion.
    assert "permissionDecision" not in out, (
        "an advisory hook must express no opinion on whether the user is asked"
    )
    assert "CONFIDENCE" in out["additionalContext"]
    assert "DUE DILIGENCE" in out["additionalContext"]


def test_each_moment_states_what_it_can_still_change():
    """THE FIX A REVIEWER FORCED, pinned so it cannot be undone by tidying.

    The ExitPlanMode-only revision told the model the reminder arrived "before
    this plan goes to the user". It does not: the plan is authored before the
    PreToolUse hook is ever called, and `allow` passes it through unchanged. So
    the two moments must not share wording — EnterPlanMode can still reach the
    plan being written, ExitPlanMode can only reach the revision and the next
    plan, and saying otherwise is the defect rather than a phrasing preference.
    """
    early = _emitted(_run(_payload(tool="EnterPlanMode")))["additionalContext"]
    late = _emitted(_run(_payload(tool="ExitPlanMode")))["additionalContext"]

    assert early != late, "one wording cannot be true of both moments"
    assert "cannot change it" in late, (
        "the ExitPlanMode reminder must not imply it reaches this plan"
    )
    assert "before you have written a line" in early, (
        "the EnterPlanMode reminder must say it is actionable now"
    )
    # Neither may claim the late path reaches the plan under review.
    assert "before this plan goes to the user" not in late.lower()


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
    # Weaker than the behavioural assertions above and kept as a second net: a
    # source scan catches the field being re-added on a branch those tests do
    # not reach. It matches the JSON KEY form specifically — `"x":` — because
    # the docstring discusses `permissionDecision` at length and a bare
    # substring scan would fail on the prose explaining why it is absent. That
    # is the anchoring failure this repo has paid for before.
    assert '"permissionDecision":' not in src, (
        "permissionDecision was re-introduced into an emitted payload"
    )


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

@pytest.mark.parametrize("tool", ["Bash", "Write", "SomeFutureTool", "EnterWorktree"])
def test_it_says_nothing_about_other_tools(tool):
    """Scoping is intrinsic, not inherited from the settings matcher."""
    payload = _payload()
    payload["tool_name"] = tool
    proc = _run(payload)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_a_payload_with_no_tool_name_still_FIRES():
    """The `is not None` branch exists so a hand-fed payload is not silently a
    no-op, and until an audit ran four mutations nothing pinned it: the
    malformed-payload cases assert exit 0 only, which a hook that emits NOTHING
    also satisfies. Dropping the guard would flip emit to silent with the whole
    suite green, in the fail direction that costs the reminder."""
    proc = _run({"hook_event_name": "PreToolUse", "tool_input": {"plan": "x"}})
    assert proc.returncode == 0
    out = _emitted(proc)
    assert "CONFIDENCE" in out["additionalContext"]
    # The conservative wording: it claims less about what the reminder reaches.
    assert "cannot change it" in out["additionalContext"]


def test_every_scoped_tool_has_wording_of_its_own():
    """SCOPE is DERIVED from MOMENT, so a tool cannot be in one and not the
    other. Pinned as behaviour rather than trusted as a definition: a mutation
    adding a tool to a hand-written SCOPE and not to MOMENT emitted the
    ExitPlanMode wording for an ENTRY moment, and 25/25 tests stayed green."""
    assert set(hook.SCOPE) == set(hook.MOMENT)
    seen = set()
    for tool in hook.SCOPE:
        text = _emitted(_run(_payload(tool=tool)))["additionalContext"]
        assert text not in seen, f"{tool} reuses another moment's wording"
        seen.add(text)


def test_the_emit_goes_THROUGH_the_bounded_writer():
    """The docstring promises the cap cannot be breached by a later edit that
    grows the text. Asserting `len(stdout) < CAP` does not pin that — the
    reminder is a fixed string nowhere near the cap, so a bare
    `print(json.dumps(...))` passes it too, and so does a `text_keys` naming a
    field that does not exist. Both of those mutations survived the suite.

    So pin the CALL, with its arguments, by driving `main()` in-process: the
    payload read and the writer are both replaced, and the recorded arguments
    have to name the field that actually carries the prose."""
    # IDENTITY FIRST, and it is not redundant with the call check below. That
    # check patches the module global, so it pins "main() calls whatever
    # `print_json_bounded` names" — a mutant that rebinds the name to a bare
    # print would be masked by the patch itself and survive. This line is what
    # pins that the name refers to hook_output's bounded writer.
    # Compare the CODE OBJECT'S FILE, not `__module__`: a module loaded by path
    # carries its loader's arbitrary name, so `__module__` differs between two
    # loads of the same file and the assertion fails for a reason that has
    # nothing to do with the property. (It did, on the first attempt — caught
    # only because the mutation sweep asserts a GREEN baseline first.)
    assert (
        Path(hook.print_json_bounded.__code__.co_filename).resolve()
        == (_WORKTREE / "scripts" / "hooks" / "hook_output.py").resolve()
    ), "the name no longer refers to hook_output's bounded writer"
    assert hook.print_json_bounded.__qualname__ == "print_json_bounded"

    recorded: dict = {}

    def _recorder(payload, **kwargs):
        recorded["payload"] = payload
        recorded["kwargs"] = kwargs

    real_read, real_write = hook.read_payload, hook.print_json_bounded
    hook.read_payload = lambda: _payload(tool="EnterPlanMode")
    hook.print_json_bounded = _recorder
    try:
        assert hook.main() == 0
    finally:
        hook.read_payload, hook.print_json_bounded = real_read, real_write

    assert recorded, "main() did not emit through print_json_bounded"
    assert recorded["kwargs"]["text_keys"] == (
        "hookSpecificOutput.additionalContext",
    ), "the trimmable field must be the one carrying the prose, or an oversize " \
       "payload loses the decision instead of the text"
    emitted = recorded["payload"]["hookSpecificOutput"]
    assert "permissionDecision" not in emitted
    assert emitted["additionalContext"].startswith(hook.MOMENT["EnterPlanMode"])


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
