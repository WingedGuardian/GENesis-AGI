"""The plan-confidence gate: a plan must state confidence and diligence, or say why not.

Install-agnostic: synthetic payloads, subprocess isolation, no network, no live DB,
and NOTHING read from ~/.claude/plans (the corpus measurement that justified the
thresholds is recorded in the PR body, not run here -- a test that reads the
author's own plan directory passes on one machine and fails on every other).

TWO DIRECTIONS ARE TESTED ON PURPOSE. A guard like this has two failure modes with
very different costs: a MISS lets an unmeasured plan through (the status quo this
replaces), while a FALSE BLOCK refuses compliant work with no way to present the
fix. The second is strictly worse, so the fail-open paths below are not
completeness padding -- they are the more important half.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parent.parent.parent
_GUARD = _WORKTREE / "scripts" / "hooks" / "plan_confidence_guard.py"


def _load():
    spec = importlib.util.spec_from_file_location("_plan_confidence_guard", _GUARD)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_plan_confidence_guard"] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop("_plan_confidence_guard", None)
        raise
    return mod


guard = _load()


def _run(payload: object) -> subprocess.CompletedProcess:
    """Invoke the guard as CC does -- a fresh process fed JSON on stdin."""
    return subprocess.run(
        [sys.executable, str(_GUARD)],
        input=json.dumps(payload) if not isinstance(payload, str) else payload,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _payload(plan: object, path: str = "/tmp/p.md") -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "ExitPlanMode",
        "tool_input": {"plan": plan, "planFilePath": path},
    }


# --------------------------------------------------------------------------
# POSITIVE CONTROL. Without a detector that is seen to FIRE, "no gaps found"
# and "no detector" are the same result.
# --------------------------------------------------------------------------

_DEFICIENT = "# Plan\n\nRefactor the widget. Then wire it up and ship it.\n"
_COMPLIANT = (
    "# Plan\n\nRefactor the widget: 85% confident.\n"
    "MEASURED: 7 callers across 5 modules.\n"
)


def test_the_detector_fires_on_a_plan_with_neither_signal():
    assert guard.confidence_gaps(_DEFICIENT) == ["confidence", "diligence"]


def test_the_detector_passes_a_plan_with_both_signals():
    assert guard.confidence_gaps(_COMPLIANT) == []


@pytest.mark.parametrize(
    "plan,expected",
    [
        # A figure alone is ENOUGH. This is the anti-validator case: the earlier
        # revision refused this for containing no word from a diligence list.
        ("Item A: 85% confident.", []),
        ("Range: 70-90% confident.", []),
        (
            "92% confident - I traced every caller with Serena and read "
            "chain.py end to end.",
            [],
        ),
        # No figure -> bounce. Diligence is REPORTED alongside when also absent.
        ("MEASURED: 7 callers.", ["confidence"]),
        ("Refactor the widget, then ship it.", ["confidence", "diligence"]),
        ("Confidence: none - pure docs edit, no runtime surface", []),
        # A bare `none` is the decision left unmade wearing the grammar of one.
        ("Confidence: none", ["confidence", "diligence"]),
        ("Confidence: none - TBD", ["confidence", "diligence"]),
    ],
)
def test_the_trigger_is_the_missing_FIGURE_never_the_missing_VOCABULARY(plan, expected):
    assert guard.confidence_gaps(plan) == expected


def test_a_plan_stating_diligence_in_its_own_words_is_never_bounced():
    """The 21/203 regression guard. Diligence expressed without any word from
    `_DILIGENCE_WORDS` must pass, because the trigger is the figure."""
    plan = (
        "# Plan\n\n88% confident on the routing change.\n"
        "I read chain.py end to end and traced every call site by hand.\n"
    )
    assert guard.confidence_gaps(plan) == []
    assert _run(_payload(plan)).returncode == 0


def test_a_fenced_illustration_does_not_satisfy_the_gate():
    """A plan is markdown full of code fences, and this guard's OWN guidance shows
    a confidence line. Without stripping fences, quoting the remedy would satisfy
    the gate without stating anything about the actual plan."""
    fenced = "# Plan\n\nDo the thing.\n\n```\nItem A: 85% confident. MEASURED: yes.\n```\n"
    assert guard.confidence_gaps(fenced) == ["confidence", "diligence"]


def test_the_fence_stripper_is_actually_loaded_not_silently_falling_back():
    """Guard-the-guard. `readable_plan` degrades to raw text when the sibling
    module cannot load, and that degradation is SILENT and passes more plans -- so
    the fenced test above would go green for the wrong reason. Assert the stripper
    is really doing work."""
    assert "fenced" not in guard.readable_plan("a\n```\nfenced\n```\nb")


# --------------------------------------------------------------------------
# THE CLOSED-LOOP GUARD. A gate that prescribes a remedy it would itself refuse
# blocks an author who types exactly what they were told to type.
# --------------------------------------------------------------------------

def test_every_example_in_the_guidance_passes_the_detector():
    conf = (
        "Item A - route the hook: 85% confident; DISPROVEN if the probe shows "
        "the event does not fire."
    )
    dd = "MEASURED: 7 callers across 5 modules (Serena find_referencing_symbols)."
    none = "Confidence: none - pure documentation edit, no runtime surface"
    for line in (conf, dd, none):
        assert line in guard.GUIDANCE, f"guidance no longer contains: {line!r}"
    assert guard.confidence_gaps(f"{conf}\n{dd}") == []
    assert guard.confidence_gaps(f"{none}\nverified against source.") == []


# --------------------------------------------------------------------------
# FAIL OPEN. The more important half.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("plan", [None, "", "   \n\t ", 12345, [], {}])
def test_an_unreadable_plan_is_never_refused(plan):
    """A guard that cannot read its subject must not block on a guess."""
    assert _run(_payload(plan)).returncode == 0


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "not json at all", "[]", '"a string"', "null", '{"tool_input": 3}'],
)
def test_a_malformed_payload_is_never_refused(raw):
    assert _run(raw).returncode == 0


def test_an_oversized_plan_fails_open_rather_than_judging_a_prefix():
    assert guard.confidence_gaps("x" * (guard._MAX_PLAN + 1)) == []


@pytest.mark.parametrize("pad", [1_000, 60_000, 65_000, 65_600, 100_000, 400_000])
def test_a_verdict_never_depends_on_WHERE_in_the_plan_the_figures_SIT(pad):
    """THE test the previous one only claimed to be.

    Its predecessor fed `"x" * (_MAX_PLAN + 1)` -- content-free, so it returned at
    the length check before `readable_plan` was ever called and asserted nothing
    about prefix scanning. Meanwhile the real scan window was the imported
    stripper's 65_536, and identical content passed at 65_052 and was REFUSED at
    65_650. Same content, two offsets, opposite verdicts.

    So: hold the content fixed and move it. A guard that reads a prefix fails this
    at the boundary; one that refuses to judge what it cannot read does not."""
    tail = "\nItem A: 92% confident. MEASURED: 7 callers traced.\n"
    body = "filler line.\n" * (pad // 13)
    assert guard.confidence_gaps(body + tail) == [], f"false refusal with {pad} chars ahead"
    assert guard.confidence_gaps(tail + body) == [], f"false refusal with {pad} chars after"


def test_the_cap_matches_the_stripper_that_actually_bounds_the_scan():
    """`_MAX_PLAN` is not a free choice: `readable_plan` delegates to the sibling,
    which truncates at its own `_MAX_BODY`. If this module's cap ever exceeds that,
    plans between the two are judged on a prefix again -- silently, and only for
    the largest plans, which is where it is least likely to be noticed."""
    import importlib.util as _u

    spec = _u.spec_from_file_location(
        "_e2e_for_cap_check", _WORKTREE / "scripts" / "e2e_declaration.py"
    )
    assert spec and spec.loader
    sib = _u.module_from_spec(spec)
    sys.modules["_e2e_for_cap_check"] = sib
    spec.loader.exec_module(sib)
    assert guard._MAX_PLAN <= sib._MAX_BODY, (
        f"guard scans to {guard._MAX_PLAN} but the stripper truncates at "
        f"{sib._MAX_BODY} -- the gap is judged on a prefix"
    )


@pytest.mark.parametrize("tool", ["Bash", "Write", "SomeFutureTool"])
def test_the_guard_ignores_every_tool_that_is_not_ExitPlanMode(tool):
    """Scoping must be intrinsic. Today the settings matcher is the only thing
    aiming this hook; a broadened matcher must not turn it into a gate on any tool
    carrying a `plan` key."""
    payload = _payload(_DEFICIENT)
    payload["tool_name"] = tool
    assert _run(payload).returncode == 0


def test_the_guard_is_not_wrapped_in_run_guard():
    """`run_guard` converts an unexpected crash into exit 2. Its own docstring
    restricts it to irreversible-action guards: 'never for advisory or convenience
    guards, which must stay fail-open so a bug never blocks legit work.' A crash
    here would refuse EVERY plan in every session with no way to present the fix.

    Checked by AST, not substring: the module DOCSTRING names `run_guard` in order
    to explain why it is absent, so a text scan fails on the very comment that
    documents the decision. Assert on imports and calls -- the things that would
    actually change behaviour."""
    import ast

    tree = ast.parse(_GUARD.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert all(a.name != "run_guard" for a in node.names), "imports run_guard"
        if isinstance(node, ast.Import):
            assert all(a.name != "run_guard" for a in node.names), "imports run_guard"
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            assert name != "run_guard", "calls run_guard"


def test_a_crash_inside_main_exits_zero_not_two(tmp_path, monkeypatch):
    """The fail-open promise is about UNEXPECTED failure, so break something the
    guard does not defend against and assert the direction."""
    broken = tmp_path / "broken_guard.py"
    src = _GUARD.read_text(encoding="utf-8").replace(
        "    gaps = confidence_gaps(plan)",
        "    raise RuntimeError('injected')\n    gaps = confidence_gaps(plan)",
    )
    assert "injected" in src, "mutation did not apply"
    broken.write_text(src, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(broken)],
        input=json.dumps(_payload(_DEFICIENT)),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, "a crashing guard must fail OPEN, not block"


# --------------------------------------------------------------------------
# THE BLOCK ITSELF, end to end through the real process.
# --------------------------------------------------------------------------

def test_a_deficient_plan_is_refused_with_exit_2():
    proc = _run(_payload(_DEFICIENT))
    assert proc.returncode == 2


def test_the_refusal_names_what_is_missing_and_where():
    # Synthetic path with NO /home/<user> shape: this repo is public, and the
    # portability scanner bans that shape from tracked files by CLASS, not by
    # whether the username in it happens to be real.
    fake = "/synthetic/plans/foo.md"
    proc = _run(_payload(_DEFICIENT, path=fake))
    assert "confidence" in proc.stderr and "diligence" in proc.stderr
    assert fake in proc.stderr, "name the file being refused"
    assert "Confidence: none" in proc.stderr, "the escape hatch must be discoverable"


def test_a_compliant_plan_is_allowed_silently():
    proc = _run(_payload(_COMPLIANT))
    assert proc.returncode == 0
    assert proc.stderr.strip() == ""


def test_the_acceptance_bar_the_real_defect_replayed():
    """The motivating failure: a substantial plan presented with no confidence.
    Reconstructed in the shape the corpus actually showed -- long, structured,
    full of prose that SOUNDS diligent, and carrying no figure anywhere."""
    real_shape = (
        "# Refactor the routing chain\n\n"
        "## Context\nThe chain retries provider refusals that cannot change.\n\n"
        "## Approach\nAdd a refusal classifier, wire it into the retry loop,\n"
        "and add a rung below Large. I have read the relevant modules and\n"
        "traced the call sites carefully.\n\n"
        "## Files\n- src/genesis/routing/chain.py\n- src/genesis/routing/errors.py\n"
    )
    assert guard.confidence_gaps(real_shape) == ["confidence", "diligence"]
    assert _run(_payload(real_shape)).returncode == 2
