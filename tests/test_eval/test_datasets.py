"""Tests for eval dataset loader."""

from __future__ import annotations

import pytest

from genesis.eval.datasets import list_datasets, load_dataset
from genesis.eval.types import ScorerType, TaskCategory


def test_load_classification_dataset():
    """Load the real classification dataset and verify structure."""
    cases = load_dataset("classification")
    assert len(cases) == 15
    for case in cases:
        assert case.input_text
        assert case.expected_output in ("0", "1", "2", "3", "4")
        assert case.scorer_type == ScorerType.EXACT_MATCH
        assert case.category == TaskCategory.CLASSIFICATION


def test_list_datasets():
    names = list_datasets()
    assert "classification" in names


def test_load_nonexistent_raises():
    with pytest.raises(FileNotFoundError):
        load_dataset("does_not_exist_xyz")


def test_load_empty_dataset(tmp_path):
    """Empty dataset file returns empty list."""
    f = tmp_path / "empty.yaml"
    f.write_text("metadata: {}\ncases: []\n")
    cases = load_dataset("empty", datasets_dir=tmp_path)
    assert cases == []


def test_load_malformed_case(tmp_path):
    """Missing required fields raises ValueError."""
    f = tmp_path / "bad.yaml"
    f.write_text("metadata: {}\ncases:\n  - id: x\n")
    with pytest.raises(ValueError, match="'input' and 'expected' required"):
        load_dataset("bad", datasets_dir=tmp_path)
