"""Display names for model ids, by rule rather than a table that goes stale (#4850)."""

from __future__ import annotations

import pytest
from genesis.cc.model_names import display_name


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("claude-sonnet-5-5", "Sonnet 5.5"),
        ("claude-opus-5-5", "Opus 5.5"),
        ("claude-opus-4-8[1m]", "Opus 4.8"),
        ("claude-haiku-4-5-20251001", "Haiku 4.5"),
        ("claude-fable-5", "Fable 5"),
        ("sonnet", "Sonnet"),
        ("glm-5.3[1m]", "glm-5.3"),
        ("", ""),
    ],
)
def test_display_name(model_id, expected):
    assert display_name(model_id) == expected
