"""The `taste` Tier-1 skill is discoverable and has well-formed frontmatter.

Malformed frontmatter would silently drop the skill from the catalog (and the
injection hook), so this pins that it parses to the expected identity.
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import private_module

_REPO = Path(__file__).resolve().parents[2]
# Private copy that does NOT leak the shared name — see tests.conftest. The
# sibling test module loads the same script, and `scripts/export_agents_md.py`
# imports it by name.
_gen = private_module("generate_skill_catalog", _REPO / "scripts" / "generate_skill_catalog.py")

_TASTE_DIR = _REPO / ".claude" / "skills" / "taste"


def test_taste_skill_frontmatter_parses():
    info = _gen._extract_skill_info(_TASTE_DIR)
    assert info is not None
    assert info["name"] == "taste"
    assert info["description"].strip()  # non-empty description
    assert "ui" in info["keywords"]
    assert "taste" in info["keywords"]
    # `design` is deliberately excluded — too broad (fires on "design the DB
    # schema" / "design an API") and would burn a Tier-1 nudge slot.
    assert "design" not in info["keywords"]


def test_taste_skill_indexed_in_tier1():
    results = _gen._scan_tier(_REPO / ".claude" / "skills", 1, _REPO)
    assert "taste" in {r["name"] for r in results}
