"""Tests for identity document loader."""

from __future__ import annotations


def test_load_soul(tmp_path):
    from genesis.identity.loader import IdentityLoader

    (tmp_path / "SOUL.md").write_text("# Genesis\nYou are Genesis.\n")
    (tmp_path / "USER.md").write_text("# User\nTimezone: EST\n")
    loader = IdentityLoader(tmp_path)
    assert "You are Genesis" in loader.soul()


def test_load_user(tmp_path):
    from genesis.identity.loader import IdentityLoader

    (tmp_path / "SOUL.md").write_text("# Genesis\nYou are Genesis.\n")
    (tmp_path / "USER.md").write_text("# User\nTimezone: EST\n")
    loader = IdentityLoader(tmp_path)
    assert "Timezone: EST" in loader.user()


def test_missing_soul_returns_empty(tmp_path):
    from genesis.identity.loader import IdentityLoader

    loader = IdentityLoader(tmp_path)
    assert loader.soul() == ""


def test_missing_user_returns_empty(tmp_path):
    from genesis.identity.loader import IdentityLoader

    loader = IdentityLoader(tmp_path)
    assert loader.user() == ""


def test_caching(tmp_path):
    from genesis.identity.loader import IdentityLoader

    (tmp_path / "SOUL.md").write_text("original")
    loader = IdentityLoader(tmp_path)
    first = loader.soul()
    (tmp_path / "SOUL.md").write_text("CHANGED")
    second = loader.soul()
    assert first == second  # cached


def test_reload_clears_cache(tmp_path):
    from genesis.identity.loader import IdentityLoader

    (tmp_path / "SOUL.md").write_text("original")
    loader = IdentityLoader(tmp_path)
    loader.soul()
    (tmp_path / "SOUL.md").write_text("CHANGED")
    loader.reload()
    assert loader.soul() == "CHANGED"


def test_identity_combined(tmp_path):
    from genesis.identity.loader import IdentityLoader

    (tmp_path / "SOUL.md").write_text("# Genesis\nYou are Genesis.\n")
    (tmp_path / "USER.md").write_text("# User\nTimezone: EST\n")
    loader = IdentityLoader(tmp_path)
    combined = loader.identity_block()
    assert "You are Genesis" in combined
    assert "Timezone: EST" in combined


# ── write_user_md tests ────────────────────────────────────────────────


def test_write_user_md_renders_sections(tmp_path):
    from genesis.identity.loader import IdentityLoader

    loader = IdentityLoader(tmp_path)
    loader.write_user_md({
        "role": "Engineer",
        "goals": "Build things",
        "expertise": "Python, systems",
    }, evidence_count=10)

    text = (tmp_path / "USER.md").read_text()
    assert "## Identity" in text
    assert "**Role**: Engineer" in text
    assert "## Goals" in text
    assert "**Goals**: Build things" in text
    assert "## Expertise" in text
    assert "**Expertise**: Python, systems" in text
    assert "10 evidence points" in text


def test_write_user_md_unmapped_fields_go_to_observed(tmp_path):
    from genesis.identity.loader import IdentityLoader

    loader = IdentityLoader(tmp_path)
    loader.write_user_md({"custom_field": "some value"})

    text = (tmp_path / "USER.md").read_text()
    assert "## Observed Patterns" in text
    assert "**Custom Field**: some value" in text


def test_write_user_md_clears_cache(tmp_path):
    from genesis.identity.loader import IdentityLoader

    (tmp_path / "USER.md").write_text("old content")
    loader = IdentityLoader(tmp_path)
    assert loader.user() == "old content"

    loader.write_user_md({"role": "New role"})
    assert "New role" in loader.user()


def test_write_user_md_empty_model(tmp_path):
    from genesis.identity.loader import IdentityLoader

    loader = IdentityLoader(tmp_path)
    loader.write_user_md({})

    text = (tmp_path / "USER.md").read_text()
    assert "# User Profile" in text
    assert "Auto-synthesized" in text


def test_write_user_md_non_string_values(tmp_path):
    from genesis.identity.loader import IdentityLoader

    loader = IdentityLoader(tmp_path)
    loader.write_user_md({
        "domains": ["Python", "Go", "Rust"],
        "preferences": {"style": "direct", "depth": "high"},
        "score": 42,
    })

    text = (tmp_path / "USER.md").read_text()
    assert "Python, Go, Rust" in text
    assert "style: direct" in text
    assert "42" in text


def test_write_user_md_overwrites_existing(tmp_path):
    from genesis.identity.loader import IdentityLoader

    (tmp_path / "USER.md").write_text("<!-- stub -->")
    loader = IdentityLoader(tmp_path)
    loader.write_user_md({"role": "Builder"})

    text = (tmp_path / "USER.md").read_text()
    assert "stub" not in text
    assert "Builder" in text
