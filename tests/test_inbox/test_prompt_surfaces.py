"""Lockstep guards for the inbox evaluator's disposition surfaces.

Three copies of the recommendation vocabulary must stay in agreement — the
system prompt (identity/INBOX_EVALUATE.md), the on-demand skill
(skills/evaluate/SKILL.md), and the parser (inbox/recommendation.py +
monitor.py). These tests pin the enum TOKENS to the parser (a drift would
silently break follow-up routing) and pin the build-biased disposition rebias
so it can't be quietly reverted. Read committed files via repo_root() — no
runtime state, install-agnostic.
"""

from __future__ import annotations

from genesis.env import repo_root

_INBOX = "src/genesis/identity/INBOX_EVALUATE.md"
_SKILL = "src/genesis/skills/evaluate/SKILL.md"

_ACTION_TOKENS = {"ADOPT", "ADAPT", "WATCH", "IGNORE"}
_SCOPE_TOKENS = {"V4", "V5", "Future", "Never"}


def _read(rel: str) -> str:
    return (repo_root() / rel).read_text(encoding="utf-8")


def test_action_tokens_present_in_both_surfaces():
    """Both prompt surfaces declare the same action enum."""
    for rel in (_INBOX, _SKILL):
        text = _read(rel)
        missing = sorted(t for t in _ACTION_TOKENS if t not in text)
        assert not missing, f"{rel} missing action tokens: {missing}"


def test_scope_tokens_present_in_both_surfaces():
    for rel in (_INBOX, _SKILL):
        text = _read(rel)
        missing = sorted(t for t in _SCOPE_TOKENS if t not in text)
        assert not missing, f"{rel} missing scope tokens: {missing}"


def test_action_tokens_match_parser_vocabulary():
    """Prompt action tokens must all be understood by the parser — else a
    recommendation routes nowhere (or is silently skipped)."""
    from genesis.inbox.monitor import InboxMonitor
    from genesis.inbox.recommendation import _SKIP_ACTIONS

    handled = (
        set(InboxMonitor._ACTION_MAP)
        | {a.replace(" ", "_") for a in _SKIP_ACTIONS}
        | set(_SKIP_ACTIONS)
    )
    for tok in _ACTION_TOKENS:
        low = tok.lower()
        assert low in handled, (
            f"action token {tok!r} is in the prompt but neither routed by "
            f"_ACTION_MAP nor listed in _SKIP_ACTIONS"
        )


def test_disposition_rebias_present():
    """The build-biased disposition must stay in both surfaces — guards against
    a silent revert to the old defer-by-default vocabulary."""
    for rel in (_INBOX, _SKILL):
        text = _read(rel).lower()
        assert "no current use case" in text, f"{rel}: banned-veto list missing"
        assert "out of our wheelhouse" in text, f"{rel}: banned-veto list missing"
        assert "named trigger" in text, f"{rel}: WATCH-trigger rule missing"


def test_stale_v3_docs_not_referenced_in_evaluator_surfaces():
    """The evaluator surfaces must not point at the retired March-v3 docs;
    CURRENT.md is the live subsystem-map reference."""
    for rel in (_SKILL, "src/genesis/skills/research/SKILL.md"):
        text = _read(rel)
        assert "genesis-v3-gap-assessment.md" not in text, f"{rel}: stale doc ref"
        assert "genesis-v3-autonomous-behavior-design.md" not in text, f"{rel}: stale doc ref"
        assert "CURRENT.md" in text, f"{rel}: missing CURRENT.md reference"
