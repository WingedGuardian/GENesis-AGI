"""Tests for the rubric registry."""

from __future__ import annotations

import pytest

from genesis.eval.rubrics import (
    _RUBRICS,
    Rubric,
    get_rubric,
    list_rubrics,
    register_rubric,
)


@pytest.fixture
def fresh_registry():
    """Snapshot/restore the module registry around each test that mutates it."""
    snapshot = dict(_RUBRICS)
    yield
    _RUBRICS.clear()
    _RUBRICS.update(snapshot)


def test_first_party_rubrics_are_registered():
    """Importing the package auto-registers built-in rubrics."""
    rubric = get_rubric("memory_recall_grounding")
    assert rubric.name == "memory_recall_grounding"
    assert rubric.version == "1.0.0"
    assert "{actual}" in rubric.prompt_template
    assert "{query}" in rubric.prompt_template
    assert "query" in rubric.extra_placeholders


def test_register_and_lookup(fresh_registry):
    r = Rubric(
        name="test_rubric",
        version="1.0.0",
        description="test",
        prompt_template="{actual} vs {expected}",
    )
    register_rubric(r)
    assert get_rubric("test_rubric") is r


def test_register_duplicate_same_value_is_idempotent(fresh_registry):
    """Re-registering the *same* rubric must not raise — modules are
    sometimes imported twice during test collection."""
    r = Rubric(
        name="test_rubric",
        version="1.0.0",
        description="test",
        prompt_template="{actual} vs {expected}",
    )
    register_rubric(r)
    register_rubric(r)  # should not raise


def test_register_collision_with_different_value_raises(fresh_registry):
    r1 = Rubric(
        name="collide",
        version="1.0.0",
        description="v1",
        prompt_template="a {actual} b {expected}",
    )
    r2 = Rubric(
        name="collide",
        version="2.0.0",
        description="v2",
        prompt_template="x {actual} y {expected}",
    )
    register_rubric(r1)
    with pytest.raises(ValueError, match="name collision"):
        register_rubric(r2)


def test_get_rubric_unknown_raises_with_suggestions():
    with pytest.raises(KeyError, match="memory_recall_grounding"):
        # Error message must list available rubrics so typos surface.
        get_rubric("nonexistent_rubric")


def test_list_rubrics_returns_sorted():
    names = [r.name for r in list_rubrics()]
    assert names == sorted(names)
    assert "memory_recall_grounding" in names
