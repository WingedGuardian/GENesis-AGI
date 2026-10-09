"""Publication selectors fail explicitly rather than rediscover orphan bodies."""

import json

import pytest

from genesis.transcript_analytics import catalog


def document():
    return {
        "format": catalog.FORMAT,
        "revision": "a" * 32,
        "population_revision": "b" * 32,
        "projects_root": "/projects",
        "sources": {"c" * 16: "d" * 32, "e" * 16: "legacy"},
        "collection": {
            "run_id": "f" * 32,
            "started_at": "2026-10-07T00:00:00+00:00",
            "updated_at": "2026-10-07T00:01:00+00:00",
            "completed_at": None,
            "complete": False,
            "discovery_complete": False,
            "discovered": None,
            "visited": 0,
            "since_days": None,
            "rebuilt_committed": 0,
            "unchanged": 0,
            "skipped_old": 0,
            "unbuilt": None,
            "failed_sources": [],
        },
    }


def test_snapshot_is_immutable_and_writer_copy_independent():
    selected = catalog.parse(json.dumps(document()))
    with pytest.raises(TypeError):
        selected.sources["c" * 16] = "legacy"
    with pytest.raises(TypeError):
        selected.collection["visited"] = 1
    candidate = selected.document()
    candidate["sources"].clear()
    candidate["collection"]["failed_sources"].append("another.jsonl")
    assert len(selected.sources) == 2
    assert selected.collection["failed_sources"] == ()
    assert catalog.parse(json.dumps(selected.document())) == selected


@pytest.mark.parametrize(
    "field,value",
    [
        ("format", "unknown"),
        ("revision", "../a"),
        ("population_revision", "A" * 32),
        ("projects_root", "relative"),
        ("projects_root", "/projects/../other"),
        ("projects_root", "/projects\0"),
        ("sources", []),
        ("sources", {"c" * 16: "../body"}),
        ("sources", {"C" * 16: "d" * 32}),
        ("sources", {"c" * 16: 1}),
    ],
)
def test_invalid_selector(field, value):
    candidate = document()
    candidate[field] = value
    with pytest.raises(catalog.Unavailable):
        catalog.parse(json.dumps(candidate))


@pytest.mark.parametrize(
    "field,value",
    [
        ("run_id", "a"),
        ("complete", 1),
        ("discovery_complete", 0),
        ("started_at", "2026-10-07T00:00:00"),
        ("updated_at", "bad"),
        ("completed_at", "2026-10-07T00:00:00+00:00"),
        ("discovered", 0),
        ("visited", True),
        ("visited", -1),
        ("rebuilt_committed", 0.5),
        ("unchanged", None),
        ("skipped_old", "0"),
        ("since_days", True),
        ("since_days", 0),
        ("since_days", float("nan")),
        ("since_days", float("inf")),
        ("since_days", 10**1000),
        ("unbuilt", []),
        ("failed_sources", None),
        ("failed_sources", [1]),
    ],
)
def test_invalid_collection(field, value):
    candidate = document()
    candidate["collection"][field] = value
    with pytest.raises(catalog.Unavailable):
        catalog.parse(json.dumps(candidate))


@pytest.mark.parametrize(
    "fragment",
    [
        '"format":"ta-source-catalog-v1","format":"ta-source-catalog-v1"',
        '"sources":{"cccccccccccccccc":"legacy","cccccccccccccccc":"legacy"}',
    ],
)
def test_duplicate_keys_never_select_last(fragment):
    with pytest.raises(catalog.Unavailable):
        catalog.parse("{" + fragment + "}")


def test_discovery_and_completion():
    candidate = document()
    candidate["collection"].update(discovery_complete=True, discovered=2, unbuilt=[])
    selected = catalog.parse(json.dumps(candidate))
    assert selected.collection["discovered"] == 2
    candidate["collection"].update(complete=True, completed_at="2026-10-07T00:02:00Z")
    assert catalog.parse(json.dumps(candidate)).collection["complete"] is True


def test_missing_catalog_only_allows_legacy_without_public_namespace(tmp_path):
    assert catalog.load(tmp_path) is None
    (tmp_path / ".staging").mkdir()
    assert catalog.load(tmp_path) is None
    (tmp_path / "sources").mkdir()
    with pytest.raises(catalog.Unavailable):
        catalog.load(tmp_path)
    (tmp_path / catalog.FILENAME).write_text(json.dumps(document()))
    assert catalog.load(tmp_path).sources["c" * 16] == "d" * 32
    (tmp_path / catalog.FILENAME).write_text("{")
    with pytest.raises(catalog.Unavailable):
        catalog.load(tmp_path)


def test_dangling_public_namespace_also_requires_catalog(tmp_path):
    (tmp_path / "sources").symlink_to(tmp_path / "missing")
    with pytest.raises(catalog.Unavailable):
        catalog.load(tmp_path)
