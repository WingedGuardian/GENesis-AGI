"""Tests for genesis.identity.loader — IdentityLoader with STEERING.md support."""

from __future__ import annotations

from pathlib import Path

import pytest

from genesis.identity.loader import IdentityLoader

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STEERING_CONTENT = """\
# Steering Rules

Hard constraints.

---

Never send emails without approval

---

Always cite sources
"""

# A structured file with ## section headers + multi-line bodies — the shape the
# 2026-06-30 incident flattened. Used to prove headers survive add_steering_rule.
_STRUCTURED_STEERING = """\
# Steering Rules

Hard constraints.

---

## Capability Honesty

Never claim a capability you haven't verified.
A wrong explanation is worse than no explanation.

---

## Ambiguous Signals

When a signal is ambiguous, look it up first.
"""

_SOUL_CONTENT = "You are Genesis."
_USER_CONTENT = "The user prefers concise output."


@pytest.fixture()
def identity_dir(tmp_path: Path) -> Path:
    """Create an identity directory with all three files."""
    (tmp_path / "SOUL.md").write_text(_SOUL_CONTENT, encoding="utf-8")
    (tmp_path / "USER.md").write_text(_USER_CONTENT, encoding="utf-8")
    (tmp_path / "STEERING.md").write_text(_STEERING_CONTENT, encoding="utf-8")
    return tmp_path


@pytest.fixture()
def loader(identity_dir: Path) -> IdentityLoader:
    return IdentityLoader(identity_dir=identity_dir)


# ===========================================================================
# TestSteering
# ===========================================================================


class TestSteering:
    """Loading and parsing STEERING.md."""

    def test_steering_loads(self, loader: IdentityLoader) -> None:
        text = loader.steering()
        assert "Never send emails without approval" in text
        assert "Always cite sources" in text

    def test_steering_missing_returns_empty(self, tmp_path: Path) -> None:
        loader = IdentityLoader(identity_dir=tmp_path)
        assert loader.steering() == ""

    def test_steering_rules_parsed(self, loader: IdentityLoader) -> None:
        rules = loader.steering_rules()
        assert "Never send emails without approval" in rules
        assert "Always cite sources" in rules
        assert len(rules) == 2

    def test_identity_block_includes_steering(self, loader: IdentityLoader) -> None:
        block = loader.identity_block()
        assert "Never send emails without approval" in block
        # Also verify SOUL and USER are present
        assert _SOUL_CONTENT in block
        assert _USER_CONTENT in block


# ===========================================================================
# TestAddSteeringRule
# ===========================================================================


class TestAddSteeringRule:
    """Adding rules to STEERING.md with FIFO eviction."""

    def test_add_rule_appended(self, loader: IdentityLoader) -> None:
        loader.add_steering_rule("Do not modify CLAUDE.md")
        rules = loader.steering_rules()
        assert "Do not modify CLAUDE.md" in rules

    def test_fifo_eviction(self, tmp_path: Path) -> None:
        # Start with an empty STEERING.md (no rules)
        loader = IdentityLoader(identity_dir=tmp_path, steering_max_rules=3)
        loader.add_steering_rule("rule-1")
        loader.add_steering_rule("rule-2")
        loader.add_steering_rule("rule-3")
        loader.add_steering_rule("rule-4")

        rules = loader.steering_rules()
        assert len(rules) == 3
        # Oldest rule (rule-1) should have been evicted
        assert "rule-1" not in rules
        assert "rule-4" in rules

    def test_cache_cleared_after_add(self, loader: IdentityLoader) -> None:
        # Read steering to populate cache
        original = loader.steering()
        # Add a new rule
        loader.add_steering_rule("Brand new rule")
        # Next read should reflect the new rule without manual reload()
        updated = loader.steering()
        assert "Brand new rule" in updated
        assert updated != original

    def test_rule_with_separator_line_not_split(self, loader: IdentityLoader) -> None:
        # A rule body containing a bare '---' line must not be silently split
        # into two blocks on the next round-trip (review P2-A). The separator
        # line is neutralized to an em-dash on add.
        loader.add_steering_rule("Never paste this:\n---\nas a raw block")
        rules = loader.steering_rules()
        # Stored as ONE rule (2 fixture rules + 1 new), not split into two.
        assert len(rules) == 3
        assert any(
            "Never paste this" in r and "as a raw block" in r for r in rules
        )
        # The embedded separator line is gone (no longer a bare '---').
        assert not any(
            line.strip() == "---"
            for r in rules
            for line in r.splitlines()
        )


# ===========================================================================
# TestStructurePreservation — regression for the 2026-06-30 STEERING flattening
# ===========================================================================


class TestStructurePreservation:
    """A structured STEERING.md (## headers + multi-line bodies) must survive
    the read→append→write round-trip that add_steering_rule performs. The
    incident: the old per-line parser dropped every ## header and merged bodies.
    """

    @pytest.fixture()
    def structured_loader(self, tmp_path: Path) -> IdentityLoader:
        (tmp_path / "STEERING.md").write_text(_STRUCTURED_STEERING, encoding="utf-8")
        return IdentityLoader(identity_dir=tmp_path)

    def test_blocks_parsed_whole_with_headers(self, structured_loader: IdentityLoader) -> None:
        rules = structured_loader.steering_rules()
        # Two ## sections → two rule blocks, each carrying its header + body.
        assert len(rules) == 2
        assert rules[0].startswith("## Capability Honesty")
        assert "A wrong explanation is worse than no explanation." in rules[0]
        assert rules[1].startswith("## Ambiguous Signals")

    def test_headers_survive_add(self, structured_loader: IdentityLoader) -> None:
        structured_loader.add_steering_rule("Never merge to main without a PR.")
        text = structured_loader.steering()
        # Both original section headers must still be present after the rewrite.
        assert "## Capability Honesty" in text
        assert "## Ambiguous Signals" in text
        # Multi-line body preserved, not flattened into separate rules.
        assert "A wrong explanation is worse than no explanation." in text
        # New rule appended as its own block.
        rules = structured_loader.steering_rules()
        assert len(rules) == 3
        assert "Never merge to main without a PR." in rules[-1]

    def test_eviction_drops_whole_block(self, structured_loader: IdentityLoader) -> None:
        loader = IdentityLoader(
            identity_dir=structured_loader._dir, steering_max_rules=2,
        )
        loader.add_steering_rule("A brand new terse rule.")
        rules = loader.steering_rules()
        # Cap=2: oldest whole block (Capability Honesty) evicted, not a stray line.
        assert len(rules) == 2
        assert not any("Capability Honesty" in r for r in rules)
        assert any("Ambiguous Signals" in r for r in rules)
        assert any("A brand new terse rule." in r for r in rules)


# ===========================================================================
# TestBackwardCompat
# ===========================================================================


class TestBackwardCompat:
    """SOUL.md and USER.md loading remain unchanged."""

    def test_soul_still_works(self, loader: IdentityLoader) -> None:
        assert loader.soul() == _SOUL_CONTENT

    def test_user_still_works(self, loader: IdentityLoader) -> None:
        assert loader.user() == _USER_CONTENT

    def test_reload_clears_steering(self, loader: IdentityLoader) -> None:
        # Populate all caches
        loader.soul()
        loader.user()
        loader.steering()

        # Mutate files on disk after cache is populated
        path = loader._dir / "STEERING.md"
        path.write_text("# Changed\n\n---\nNew rule only\n", encoding="utf-8")

        # Before reload, cache still has old content
        assert "New rule only" not in loader.steering()

        # After reload, fresh content is picked up
        loader.reload()
        assert "New rule only" in loader.steering()
