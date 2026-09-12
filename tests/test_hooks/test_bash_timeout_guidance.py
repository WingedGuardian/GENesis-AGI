"""The Bash tool's timeout ceiling, pinned wherever we give advice about it.

The Bash tool takes a `timeout` in milliseconds with a **hard maximum of
600000ms (10 minutes)**. A larger value is not honoured — the call is still
SIGTERMed at ten minutes (MEASURED 2026-08-27: `timeout: 1600000` died at exactly
`10m 0s`). So "raise the timeout" is a fix that TOPS OUT; past ten minutes the
only correct form is `run_in_background: true`.

Both places we ship advice about this told sessions to set `>=900000ms` — above
the ceiling, therefore unreachable. A session following our own guidance believed
it had fifteen minutes and was killed at ten, which is the same
confident-wrong-answer shape the sibling pipe guard exists for: the advice
"works", and the expectation it creates is false.

These tests are cheap insurance against the number drifting back. They assert the
invariant (never recommend an unreachable value) rather than any one wording, so
they do not fight ordinary edits to the prose.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

# The Bash tool's documented hard maximum, in milliseconds.
_CEILING_MS = 600_000

# Surfaces that give a session advice about the Bash tool's timeout.
_ADVICE_SURFACES = (
    Path(".claude/hooks/cc-deploy-timeout-guard"),
    Path(".claude/skills/genesis-development/SKILL.md"),
)

# A millisecond-scale number (6+ digits) presented as a timeout value.
_MS_LITERAL = re.compile(r"\b(\d{6,})\s*ms\b")


def _advice_files():
    for rel in _ADVICE_SURFACES:
        path = _REPO / rel
        assert path.exists(), f"advice surface missing: {rel}"
        yield rel, path.read_text()


class TestNoUnreachableTimeoutIsRecommended:
    def test_no_millisecond_value_above_the_ceiling(self):
        """The actual defect: '>=900000ms' cannot be satisfied."""
        offenders = []
        for rel, text in _advice_files():
            for raw in _MS_LITERAL.findall(text):
                if int(raw) > _CEILING_MS:
                    offenders.append(f"{rel}: {raw}ms > {_CEILING_MS}ms ceiling")
        assert not offenders, (
            "guidance recommends a Bash timeout above the tool's hard maximum, so a "
            "session that follows it will be SIGTERMed earlier than it expects: "
            + "; ".join(offenders)
        )


class TestTheCeilingIsStated:
    def test_surfaces_name_the_real_maximum(self):
        """Naming the default without the ceiling is what let the wrong number
        stand: '120000ms default' reads as 'raise it as needed'."""
        for rel, text in _advice_files():
            assert "600000" in text or "600,000" in text, (
                f"{rel} advises on the Bash timeout but never states the 600000ms "
                "ceiling — without it, 'set the timeout higher' reads as unbounded"
            )

    def test_surfaces_route_long_work_to_the_background(self):
        for rel, text in _advice_files():
            assert "run_in_background" in text, (
                f"{rel} must name run_in_background as the form for work that can "
                "exceed the ceiling — there is no foreground timeout that covers it"
            )
