"""section_hash: key-order invariant, metrics never involved, value-sensitive."""

from __future__ import annotations

from genesis.infra_profile.hashing import section_hash


def test_key_order_invariant():
    assert section_hash({"a": 1, "b": [1, 2]}) == section_hash({"b": [1, 2], "a": 1})


def test_nested_key_order_invariant():
    assert section_hash({"x": {"a": 1, "b": 2}}) == section_hash({"x": {"b": 2, "a": 1}})


def test_value_change_moves_hash():
    assert section_hash({"a": 1}) != section_hash({"a": 2})


def test_list_order_is_significant():
    # Deterministic list order is the COLLECTOR's contract; the hash must not
    # mask an order change by silently sorting.
    assert section_hash({"a": [1, 2]}) != section_hash({"a": [2, 1]})


def test_non_json_values_hash_via_str():
    from pathlib import Path

    assert section_hash({"p": Path("/x")}) == section_hash({"p": "/x"})
