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
Always cite sources
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
