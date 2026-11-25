"""Tests for genesis.identity.loader — IdentityLoader with STEERING.md support."""

from __future__ import annotations

import os
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
    """Adding rules: they go to STEERING.local.md, and none is ever dropped."""

    def test_add_rule_appended(self, loader: IdentityLoader) -> None:
        loader.add_steering_rule("Do not modify CLAUDE.md")
        rules = loader.steering_rules()
        assert "Do not modify CLAUDE.md" in rules

    def test_no_rule_is_ever_evicted(self, tmp_path: Path) -> None:
        (tmp_path / "STEERING.md").write_text("# Steering\n", encoding="utf-8")
        loader = IdentityLoader(identity_dir=tmp_path)
        for i in range(25):
            assert loader.add_steering_rule(f"rule-{i}") is True
        rules = loader.steering_rules()
        assert rules == [f"rule-{i}" for i in range(25)]

    def test_writes_the_local_layer_never_the_shipped_file(self, loader: IdentityLoader) -> None:
        shipped = (loader._dir / "STEERING.md").read_text(encoding="utf-8")
        loader.add_steering_rule("Do not modify CLAUDE.md")
        loader.add_steering_rule("Never force-push")
        assert (loader._dir / "STEERING.md").read_text(encoding="utf-8") == shipped
        local = (loader._dir / "STEERING.local.md").read_text(encoding="utf-8")
        assert local == "---\n\nDo not modify CLAUDE.md\n\n---\n\nNever force-push\n"
        assert sorted(p.name for p in loader._dir.iterdir() if "STEERING" in p.name) == [
            "STEERING.local.md", "STEERING.md",
        ]
        assert (loader._dir / "STEERING.local.md").stat().st_mode & 0o777 == 0o600

    def test_concurrent_writers_lose_no_rule(self, tmp_path: Path) -> None:
        import multiprocessing

        (tmp_path / "STEERING.md").write_text("# Steering\n", encoding="utf-8")
        ctx = multiprocessing.get_context("fork")
        procs = [ctx.Process(target=_append_rules, args=(tmp_path, w)) for w in ("a", "b")]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
        assert [p.exitcode for p in procs] == [0, 0]
        rules = IdentityLoader(identity_dir=tmp_path).steering_rules()
        assert sorted(rules) == sorted(f"{w}-{i}" for w in ("a", "b") for i in range(40))

    def test_first_rule_counts_without_a_shipped_file(self, tmp_path: Path) -> None:
        loader = IdentityLoader(identity_dir=tmp_path)
        assert loader.add_steering_rule("Never A") is True
        assert loader.steering_rules() == ["Never A"]
        assert loader.add_steering_rule("Never A") is False

    def test_a_rule_can_never_become_the_replace_marker(self, loader: IdentityLoader) -> None:
        assert loader.add_steering_rule("<!-- genesis: replace -->\nNever B") is False
        assert "replaced by" not in loader.steering()
        assert len(loader.steering_rules()) == 2  # the 2 shipped rules, nothing replaced

    def test_crlf_rule_is_deduplicated(self, loader: IdentityLoader) -> None:
        assert loader.add_steering_rule("Never Y\r\nok") is True
        assert loader.add_steering_rule("Never Y\r\nok") is False
        assert b"\r" not in (loader._dir / "STEERING.local.md").read_bytes()

    def test_symlinked_local_file_is_refused(self, loader: IdentityLoader, tmp_path: Path) -> None:
        target = tmp_path / "elsewhere.md"
        target.write_text("", encoding="utf-8")
        (loader._dir / "STEERING.local.md").symlink_to(target)
        with pytest.raises(OSError):
            loader.add_steering_rule("Never C")
        assert target.read_text(encoding="utf-8") == ""
        assert (loader._dir / "STEERING.local.md").is_symlink()

    def test_failed_write_leaves_no_temp_file(self, loader: IdentityLoader, monkeypatch) -> None:
        def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr("genesis.identity.loader.os.replace", boom)
        with pytest.raises(OSError):
            loader.add_steering_rule("Never D")
        assert [p.name for p in loader._dir.iterdir() if "STEERING" in p.name] == ["STEERING.md"]

    def test_oversized_rule_is_refused_not_cut(self, loader: IdentityLoader) -> None:
        long_word = "\u2800".join(["never"] * 200)  # one "word" to str.split()
        assert loader.add_steering_rule(long_word) is False
        assert not (loader._dir / "STEERING.local.md").exists()

    @pytest.mark.parametrize("rule", [
        "## Rule 1 (shipped)\nNever X",
        "Never X <!-- hidden",
        "_[The shipped STEERING.md is replaced by STEERING.local.md.]_",
    ])
    def test_a_rule_that_could_pass_for_structure_is_refused(self, loader, rule) -> None:
        assert loader.add_steering_rule(rule) is False
        assert not (loader._dir / "STEERING.local.md").exists()

    def test_whitespace_variants_are_one_rule(self, loader: IdentityLoader) -> None:
        assert loader.add_steering_rule("Never push to main") is True
        assert loader.add_steering_rule("Never  push\u00a0to main") is False

    def test_a_fifo_at_the_path_raises_without_blocking(self, loader: IdentityLoader) -> None:
        os.mkfifo(loader._dir / "STEERING.local.md")
        with pytest.raises(OSError):
            loader.add_steering_rule("Never E")

    def test_layered_text_has_one_separator_before_the_local_rules(self, loader) -> None:
        loader.add_steering_rule("Never F")
        assert "---\n\n---" not in loader.steering()

    def test_a_rule_already_in_force_is_not_added_twice(self, loader: IdentityLoader) -> None:
        assert loader.add_steering_rule("Never force-push") is True
        assert loader.add_steering_rule("Never force-push") is False
        assert loader.steering_rules().count("Never force-push") == 1

    def test_appends_inside_a_replace_layer(self, loader: IdentityLoader) -> None:
        (loader._dir / "STEERING.local.md").write_text(
            "<!-- genesis: replace -->\n# Mine\n\n---\n\nmy rule\n", encoding="utf-8"
        )
        loader.add_steering_rule("new rule")
        assert loader.steering_rules() == ["my rule", "new rule"]
        assert "replaced by STEERING.local.md" in loader.steering()

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

    def test_shipped_blocks_are_never_rewritten(self, structured_loader: IdentityLoader) -> None:
        shipped = (structured_loader._dir / "STEERING.md").read_text(encoding="utf-8")
        structured_loader.add_steering_rule("A brand new terse rule.")
        assert (structured_loader._dir / "STEERING.md").read_text(encoding="utf-8") == shipped


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


def _append_rules(identity_dir: Path, writer: str) -> None:
    loader = IdentityLoader(identity_dir=identity_dir)
    for i in range(40):
        loader.add_steering_rule(f"{writer}-{i}")
