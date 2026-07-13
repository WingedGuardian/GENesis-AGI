"""Store: atomic roundtrip, corrupt/missing tolerance."""

from __future__ import annotations

from genesis.infra_profile import store


def test_roundtrip(tmp_path):
    path = tmp_path / "profile.json"
    profile = {"schema_version": 1, "sections": {"cpu": {"hash": "x"}}}
    store.save_profile(profile, path=path)
    assert store.load_profile(path) == profile


def test_missing_loads_empty(tmp_path):
    assert store.load_profile(tmp_path / "absent.json") == {}


def test_corrupt_loads_empty(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text("{not json")
    assert store.load_profile(path) == {}


def test_non_dict_loads_empty(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text("[1, 2]")
    assert store.load_profile(path) == {}


def test_annotations_roundtrip(tmp_path):
    path = tmp_path / "annotations.json"
    ann = {"schema_version": 1, "sections": {}}
    store.save_annotations(ann, path=path)
    assert store.load_annotations(path) == ann
