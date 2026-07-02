"""Tests for the knowledge ingestion manifest (source→units provenance).

Focus: `remove_unit` — the unit-granular tombstone that keeps the manifest's
source-identity gate (`has_source`) honest when knowledge units are deleted.
"""

from pathlib import Path

from genesis.knowledge.manifest import ManifestManager


def _mgr(tmp_path: Path) -> ManifestManager:
    return ManifestManager(root=tmp_path / "knowledge")


def _add(mgr: ManifestManager, source: str, unit_ids: list[str]) -> None:
    mgr.add_source(
        source,
        source_type="text",
        extracted_path=mgr.sources_dir / f"{ManifestManager.source_hash(source)}.md",
        unit_ids=unit_ids,
    )


def test_remove_unit_from_multi_unit_source_keeps_entry(tmp_path: Path):
    """Removing one unit of a multi-unit source leaves the source registered."""
    mgr = _mgr(tmp_path)
    _add(mgr, "doc.txt", ["u1", "u2", "u3"])
    assert mgr.remove_unit("u2") is True
    assert mgr.has_source("doc.txt") is True
    assert mgr.get_units_for_source("doc.txt") == ["u1", "u3"]


def test_remove_last_unit_tombstones_source(tmp_path: Path):
    """Removing the last live unit deletes the entry so re-ingest is unblocked."""
    mgr = _mgr(tmp_path)
    _add(mgr, "doc.txt", ["only"])
    assert mgr.remove_unit("only") is True
    assert mgr.has_source("doc.txt") is False


def test_remove_all_units_sequentially_tombstones(tmp_path: Path):
    """The entry survives until the LAST unit is removed, then tombstones."""
    mgr = _mgr(tmp_path)
    _add(mgr, "doc.txt", ["u1", "u2"])
    assert mgr.remove_unit("u1") is True
    assert mgr.has_source("doc.txt") is True
    assert mgr.remove_unit("u2") is True
    assert mgr.has_source("doc.txt") is False


def test_remove_unit_not_found_returns_false(tmp_path: Path):
    """A unit_id belonging to no entry is a no-op returning False."""
    mgr = _mgr(tmp_path)
    _add(mgr, "doc.txt", ["u1"])
    assert mgr.remove_unit("nope") is False
    assert mgr.get_units_for_source("doc.txt") == ["u1"]


def test_remove_unit_idempotent(tmp_path: Path):
    """A second delete of the same unit_id is a harmless no-op (False)."""
    mgr = _mgr(tmp_path)
    _add(mgr, "doc.txt", ["u1", "u2"])
    assert mgr.remove_unit("u1") is True
    assert mgr.remove_unit("u1") is False
    assert mgr.get_units_for_source("doc.txt") == ["u2"]


def test_remove_unit_leaves_no_units_extracted_entry_untouched(tmp_path: Path):
    """A legitimately-empty `no_units_extracted` entry is never matched/tombstoned."""
    mgr = _mgr(tmp_path)
    _add(mgr, "empty.txt", [])  # add_source(..., unit_ids=None) writes []
    assert mgr.has_source("empty.txt") is True
    assert mgr.remove_unit("whatever") is False
    assert mgr.has_source("empty.txt") is True


def test_remove_unit_only_affects_matching_source(tmp_path: Path):
    """Removal is scoped to the source that owns the unit_id."""
    mgr = _mgr(tmp_path)
    _add(mgr, "a.txt", ["a1", "a2"])
    _add(mgr, "b.txt", ["b1"])
    assert mgr.remove_unit("a1") is True
    assert mgr.get_units_for_source("a.txt") == ["a2"]
    assert mgr.get_units_for_source("b.txt") == ["b1"]


def test_remove_unit_persists_across_reload(tmp_path: Path):
    """The tombstone must be written to disk, not just the in-memory cache."""
    mgr = _mgr(tmp_path)
    _add(mgr, "doc.txt", ["u1"])
    assert mgr.remove_unit("u1") is True
    fresh = _mgr(tmp_path)  # new instance → reads manifest.json from disk
    assert fresh.has_source("doc.txt") is False


def test_remove_unit_partial_removal_persists_across_reload(tmp_path: Path):
    """A non-tombstoning removal also persists the shortened unit_ids to disk."""
    mgr = _mgr(tmp_path)
    _add(mgr, "doc.txt", ["u1", "u2"])
    assert mgr.remove_unit("u1") is True
    fresh = _mgr(tmp_path)
    assert fresh.get_units_for_source("doc.txt") == ["u2"]
