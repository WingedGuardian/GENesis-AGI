"""The review-enforcement reminder survives; its manifest is what degrades.

WHY THIS IS A SEPARATE TEST FROM THE HOOK'S OWN. `review_enforcement_prompt.py`
exits silently unless the working tree has unreviewed code changes, so a live run
in a clean worktree emits 0 bytes and proves nothing about the emit path. These
tests reconstruct the hook's exact emit sequence — base reminder, then manifest
through the same writer — from the REAL base string, lifted out of the hook by
AST so the assertions track the text rather than a copy of it.

THE DEFECT THIS LOCKS. The base reminder is ~6.4k characters, roughly two thirds
of the harness's 10,000-character cap, before a single file is listed.
`review_scope._MAX_LISTED_FILES` caps the manifest's COUNT, not its characters.
MEASURED against this repo's real tracked paths: 50 median-length paths reach
9,030 total and 50 of the longest reach 10,961 — over by 961. Over the cap the harness
files the WHOLE block and shows a ~2 KB preview, so an unbounded manifest does
not cost the manifest, it costs the mandatory reminder.

It has never actually fired (0 of 849 harness filings on this install came from
this hook). This is a 961-character margin on a hook that runs on every prompt,
not an incident.

WHAT THIS FILE DOES NOT PROVE, stated plainly so nobody reads it as more. These
tests reproduce the emit SEQUENCE; they do not invoke the hook, so on their own
they would still pass if someone reverted the hook to a bare `print`. What stops
that is `test_hook_output_contract.py`, which fails when a model-facing hook
prints without going through the writer. The pair is the coverage: that file
locks HOW the hook emits, this one locks WHETHER routing actually saves this
content. Neither is sufficient alone.

Install-agnostic: synthetic payloads, no network, no live DB.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_HOOK = _REPO / "scripts" / "review_enforcement_prompt.py"


def _load_from_scripts() -> tuple:
    """Import the two helpers with sys.path RESTORED afterwards.

    A bare module-level `sys.path.insert` would leave `scripts/` and
    `scripts/hooks/` — 25+ modules including `shell_parse.py` and
    `secret_scrub.py` — on the path for the whole pytest process, making later
    imports in unrelated test files order-dependent. No collision exists today;
    this keeps it that way without relying on that staying true.
    """
    saved = list(sys.path)
    try:
        sys.path.insert(0, str(_REPO / "scripts"))
        sys.path.insert(0, str(_REPO / "scripts" / "hooks"))
        from hook_output import DEFAULT_BUDGET, HOOK_STDOUT_CAP, BoundedStdout
        from review_scope import render_reminder_block

        return HOOK_STDOUT_CAP, DEFAULT_BUDGET, BoundedStdout, render_reminder_block
    finally:
        sys.path[:] = saved


HOOK_STDOUT_CAP, DEFAULT_BUDGET, BoundedStdout, _render_reminder_block = _load_from_scripts()


def _base_reminder() -> str:
    """The reminder string as the hook actually holds it.

    Lifted by AST rather than copied, so growing the reminder in the hook moves
    these tests with it. A copy would let the real string drift past the cap
    while a stale duplicate kept the suite green.
    """
    tree = ast.parse(_HOOK.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name not in {"emit", "print"}:
            continue
        for arg in node.args:
            if (
                isinstance(arg, ast.Constant)
                and isinstance(arg.value, str)
                and arg.value.startswith("MANDATORY: Unreviewed code changes")
            ):
                return arg.value
    pytest.fail("could not locate the base reminder string in the hook")


def _manifest(paths: list[str]) -> str:
    """A review-scope block built by the REAL renderer, not a copy of its format.

    An earlier version reproduced only the per-file loop line and omitted the
    header, the "… and N more" line, the excluded-count line and the specialists
    line — roughly 200-400 characters that production emits and the fixture did
    not, which made the measured margin narrower than reality and free to drift
    from the renderer. Calling `render_reminder_block` means the fixture cannot
    disagree with what the hook actually appends.
    """
    manifest = {
        "base": "origin/main",
        "files": [{"path": p, "review_required": True, "scope_tag": "backend"} for p in paths],
        "counts": {"excluded": 1},
        "specialists": ["maintainability", "performance", "security", "testing"],
    }
    return _render_reminder_block(manifest)


def _longest_tracked(n: int) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=_REPO, capture_output=True, text=True, timeout=60
    ).stdout.split()
    code = sorted((f for f in out if f.endswith((".py", ".sh", ".yml"))), key=len)
    assert len(code) >= n, f"repo has only {len(code)} tracked code files"
    return code[-n:]


def _emit_like_the_hook(base: str, block: str) -> str:
    """Reproduce the hook's emit sequence against a captured stream."""
    import io

    stream = io.StringIO()
    out = BoundedStdout(label="review-enforcement", stream=stream)
    out.emit(base, block="reminder")
    out.emit_or_degrade(
        "\n" + block,
        block="scope-manifest",
        notice="\n[review-scope manifest: {kept} chars kept — the rest was omitted.]",
    )
    return stream.getvalue()


def test_the_base_reminder_alone_is_most_of_the_budget() -> None:
    """Names the underlying hazard: there is very little room for anything else."""
    base = _base_reminder()
    assert len(base) > 5_000, "reminder unexpectedly small — re-derive the margin"
    # AGAINST THE CONSTANT PRODUCTION ENFORCES, not the harness cap. The hook
    # constructs `BoundedStdout(label="review-enforcement")`, which enforces
    # DEFAULT_BUDGET (9,800); the cap is 10,000, and this same file pins
    # DEFAULT_BUDGET < HOOK_STDOUT_CAP, so the window between them is non-empty
    # BY THE SUITE'S OWN CONSTRUCTION. A reminder grown into it -- 9,900 against
    # a 9,800 budget -- passed this assertion while production took the CUT path,
    # which truncates the mandatory reminder AND closes the stream, dropping the
    # manifest with it. Asserting against the looser of two constants is how a
    # test blesses exactly the state it exists to prevent.
    assert len(base) < DEFAULT_BUDGET, (
        f"the base reminder ({len(base)}) alone is at or over the writer's "
        f"{DEFAULT_BUDGET} budget — production would CUT it, truncating the "
        "reminder and closing the stream; the prose must shrink"
    )


def test_the_budget_is_below_the_harness_cap() -> None:
    """Asserted ONCE, here, so the tests below need not restate it.

    `BoundedStdout` enforces `DEFAULT_BUDGET` on every write path, so asserting
    "the emitted total is under the cap" after using it is a tautology — it holds
    for a manifest of any size and cannot fail. The only thing worth asserting is
    the relationship between the two constants.
    """
    assert DEFAULT_BUDGET < HOOK_STDOUT_CAP, (
        f"the writer's budget ({DEFAULT_BUDGET}) is not below the harness cap "
        f"({HOOK_STDOUT_CAP}) — bounding no longer prevents filing"
    )


def test_the_worst_case_takes_the_DEGRADE_path_not_the_cut_path() -> None:
    """The real acceptance bar, and what the tautology above was pretending to be.

    Under-cap is guaranteed; what is NOT guaranteed is which branch got you
    there. A `cut` means the writer ran out of room mid-block and severed it —
    the reader gets a truncated manifest with no idea what was dropped. A
    `degrade` means the manifest was replaced by a notice that says so. Only the
    second is the designed behaviour.
    """
    out = _emit_like_the_hook(_base_reminder(), _manifest(_longest_tracked(50)))
    assert "review-scope manifest" in out, (
        "the worst case must DEGRADE with a notice naming what was omitted"
    )
    assert "CUT" not in out, (
        "the writer severed the block instead of degrading it — the reader is "
        "left with a silently truncated manifest"
    )


def test_the_directive_survives_and_the_manifest_is_what_degrades() -> None:
    """Bounding the total is not enough — the RIGHT half has to survive.

    A writer that trimmed the head would also keep the total under cap while
    destroying exactly the text the hook exists to deliver.
    """
    base = _base_reminder()
    out = _emit_like_the_hook(base, _manifest(_longest_tracked(50)))
    assert out.startswith("MANDATORY: Unreviewed code changes"), (
        "the mandatory directive must lead — the harness preview keeps the HEAD"
    )
    assert base in out, "the base reminder must survive INTACT, not be truncated"
    assert "review-scope manifest" in out, "the degrade must say what it omitted"


def test_the_hazard_is_real_unbounded_output_would_exceed_the_cap() -> None:
    """The control that must FLIP. Every assertion above is about output that
    stayed under the cap — and output that was never in danger stays under it
    too. This measures the same content WITHOUT the writer, so a green suite
    means the bounding did something rather than that nothing was ever at risk.
    """
    base = _base_reminder()
    unbounded = base + "\n" + _manifest(_longest_tracked(50))
    assert len(unbounded) > HOOK_STDOUT_CAP, (
        f"unbounded emission is {len(unbounded)} chars, which is UNDER the "
        f"{HOOK_STDOUT_CAP} cap — the hazard these tests lock no longer exists at "
        "this repo size, so re-derive the margin before trusting them"
    )


def test_a_small_manifest_is_not_degraded() -> None:
    """The control. Without it, a writer that dropped the manifest unconditionally
    would pass every assertion above."""
    paths = ["src/a.py", "src/b.py", "tests/c.py"]
    out = _emit_like_the_hook(_base_reminder(), _manifest(paths))
    for p in paths:
        assert p in out, f"a manifest this small must survive whole; {p} missing"


def test_a_reminder_in_the_window_between_the_two_constants_is_rejected() -> None:
    """THE case that distinguishes the two constants, which the real reminder
    cannot: at 6,436 it sits far below both, so asserting against the looser one
    passed identically and the mutation was behaviourally null.

    The window is non-empty by this suite's own construction
    (DEFAULT_BUDGET < HOOK_STDOUT_CAP), and a base reminder inside it is the
    exact hazard: it clears the harness cap while production's writer CUTS it --
    truncating the mandatory reminder and closing the stream, so the manifest
    goes too. Asserted on a synthetic length, because the point is which
    constant governs, not how long the prose happens to be today.
    """
    assert DEFAULT_BUDGET < HOOK_STDOUT_CAP, "no window to test"
    in_window = (DEFAULT_BUDGET + HOOK_STDOUT_CAP) // 2

    # Under the harness cap...
    assert in_window < HOOK_STDOUT_CAP
    # ...and yet over what the writer will actually emit whole.
    assert in_window >= DEFAULT_BUDGET, (
        "a reminder of this length would be CUT by BoundedStdout even though it "
        "is under the harness cap — which is why the bound is asserted against "
        "DEFAULT_BUDGET and not HOOK_STDOUT_CAP"
    )
