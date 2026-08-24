"""Tests for inbox response writer."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from genesis.inbox.writer import ResponseWriter


@pytest.fixture
def writer(tmp_path: Path) -> ResponseWriter:
    return ResponseWriter(watch_path=tmp_path, timezone="Europe/Berlin")


@pytest.mark.asyncio
async def test_single_item_sibling_file(writer: ResponseWriter, tmp_path: Path):
    """Single-item batch produces a sibling .genesis.md file."""
    source = tmp_path / "Untitled.md"
    source.write_text("test content")
    path = await writer.write_response(
        batch_id="abc12345-6789",
        source_files=[str(source)],
        evaluation_text="# Evaluation\n\nLooks good.",
        item_count=1,
    )
    assert path.exists()
    assert path.name == "Untitled-1.genesis.md"
    assert path.parent == tmp_path
    content = path.read_text()
    assert "Looks good" in content


@pytest.mark.asyncio
async def test_multi_item_batch_uses_batch_id(writer: ResponseWriter, tmp_path: Path):
    """Multi-item batch uses date-based batch filename."""
    path = await writer.write_response(
        batch_id="batch123-xyz",
        source_files=["a.md", "b.md"],
        evaluation_text="# Eval",
        item_count=2,
    )
    assert path.exists()
    assert path.name.endswith(".genesis.md")
    assert "batch123" in path.name


@pytest.mark.asyncio
async def test_write_no_tmp_leftover(writer: ResponseWriter, tmp_path: Path):
    source = tmp_path / "test.md"
    source.write_text("x")
    await writer.write_response(
        batch_id="batch123",
        source_files=[str(source)],
        evaluation_text="test",
        item_count=1,
    )
    tmps = list(tmp_path.glob("*.tmp"))
    assert tmps == []


@pytest.mark.asyncio
async def test_write_valid_frontmatter(writer: ResponseWriter, tmp_path: Path):
    path = await writer.write_response(
        batch_id="batch123",
        source_files=["links.md", "notes.md"],
        evaluation_text="# Eval",
        item_count=2,
    )
    content = path.read_text()
    assert content.startswith("---\n")
    assert "batch_id: batch123" in content
    assert "links.md" in content


@pytest.mark.asyncio
async def test_write_unique_filenames(writer: ResponseWriter, tmp_path: Path):
    source = tmp_path / "same.md"
    source.write_text("x")
    p1 = await writer.write_response(
        batch_id="same1234",
        source_files=[str(source)],
        evaluation_text="first",
        item_count=1,
    )
    p2 = await writer.write_response(
        batch_id="same1234",
        source_files=[str(source)],
        evaluation_text="second",
        item_count=1,
    )
    assert p1 != p2
    assert p1.exists()
    assert p2.exists()
    assert p1.name == "same-1.genesis.md"
    assert p2.name == "same-2.genesis.md"


@pytest.mark.asyncio
async def test_monotonic_skips_deleted_numbers(writer: ResponseWriter, tmp_path: Path):
    """Deleted file numbers are never reused — next write always increments."""
    source = tmp_path / "doc.md"
    source.write_text("x")
    # Write three responses: doc-1, doc-2, doc-3
    p1 = await writer.write_response(
        batch_id="m1", source_files=[str(source)],
        evaluation_text="first", item_count=1,
    )
    p2 = await writer.write_response(
        batch_id="m2", source_files=[str(source)],
        evaluation_text="second", item_count=1,
    )
    p3 = await writer.write_response(
        batch_id="m3", source_files=[str(source)],
        evaluation_text="third", item_count=1,
    )
    assert p1.name == "doc-1.genesis.md"
    assert p2.name == "doc-2.genesis.md"
    assert p3.name == "doc-3.genesis.md"
    # Delete the highest numbered file
    p3.unlink()
    # Next write should be doc-4, NOT doc-3 (the deleted number)
    p4 = await writer.write_response(
        batch_id="m4", source_files=[str(source)],
        evaluation_text="fourth", item_count=1,
    )
    assert p4.name == "doc-4.genesis.md"


@pytest.mark.asyncio
async def test_monotonic_counter_survives_all_files_deleted(
    writer: ResponseWriter, tmp_path: Path,
):
    """Counter file preserves high-water mark even if all response files are deleted."""
    source = tmp_path / "note.md"
    source.write_text("x")
    # Write three responses
    await writer.write_response(
        batch_id="c1", source_files=[str(source)],
        evaluation_text="a", item_count=1,
    )
    p2 = await writer.write_response(
        batch_id="c2", source_files=[str(source)],
        evaluation_text="b", item_count=1,
    )
    p3 = await writer.write_response(
        batch_id="c3", source_files=[str(source)],
        evaluation_text="c", item_count=1,
    )
    # Delete numbered response files
    p2.unlink()
    p3.unlink()
    # Next write must be note-4, not note-1 (counter persists)
    p4 = await writer.write_response(
        batch_id="c4", source_files=[str(source)],
        evaluation_text="d", item_count=1,
    )
    assert p4.name == "note-4.genesis.md"


@pytest.mark.asyncio
async def test_monotonic_survives_counter_file_deletion(
    writer: ResponseWriter, tmp_path: Path,
):
    """If counter file is lost, falls back to highest number on disk."""
    source = tmp_path / "plan.md"
    source.write_text("x")
    # Write two responses (creates counter file)
    await writer.write_response(
        batch_id="f1", source_files=[str(source)],
        evaluation_text="a", item_count=1,
    )
    await writer.write_response(
        batch_id="f2", source_files=[str(source)],
        evaluation_text="b", item_count=1,
    )
    # Delete counter file (now OUTSIDE the watch path — see _isolate_counter_store)
    from genesis.inbox import writer as writer_mod

    counter_file = writer_mod._counter_store_path()
    assert counter_file.exists()
    counter_file.unlink()
    # Falls back to disk scan: plan-1.genesis.md and plan-2.genesis.md exist
    # so next should be plan-3
    p3 = await writer.write_response(
        batch_id="f3", source_files=[str(source)],
        evaluation_text="c", item_count=1,
    )
    assert p3.name == "plan-3.genesis.md"


@pytest.mark.asyncio
async def test_monotonic_ignores_non_numeric_suffixes(
    writer: ResponseWriter, tmp_path: Path,
):
    """Non-numeric suffixed files like base-draft.genesis.md don't affect numbering."""
    source = tmp_path / "ideas.md"
    source.write_text("x")
    # Create base + a non-numeric suffixed file
    (tmp_path / "ideas.genesis.md").write_text("base")
    (tmp_path / "ideas-draft.genesis.md").write_text("draft")
    (tmp_path / "ideas-2.genesis.md").write_text("two")
    p = await writer.write_response(
        batch_id="n1", source_files=[str(source)],
        evaluation_text="new", item_count=1,
    )
    # Should be ideas-3, ignoring the -draft file
    assert p.name == "ideas-3.genesis.md"


@pytest.mark.asyncio
async def test_write_preserves_content(writer: ResponseWriter, tmp_path: Path):
    source = tmp_path / "x.md"
    source.write_text("x")
    text = "## Detailed Analysis\n\nThis is a **thorough** evaluation."
    path = await writer.write_response(
        batch_id="batch999",
        source_files=[str(source)],
        evaluation_text=text,
        item_count=1,
    )
    content = path.read_text()
    assert text in content


@pytest.mark.asyncio
async def test_write_escapes_special_yaml_chars(writer: ResponseWriter, tmp_path: Path):
    """Frontmatter values with newlines, tabs, colons, quotes are properly escaped."""
    import yaml

    source = tmp_path / "special.md"
    source.write_text("x")
    path = await writer.write_response(
        batch_id="batch-with-\"quotes\"",
        source_files=[str(source) + "\nnewline\ttab: colon"],
        evaluation_text="body",
        item_count=1,
    )
    content = path.read_text()
    # Extract frontmatter between --- markers
    parts = content.split("---")
    assert len(parts) >= 3
    fm = yaml.safe_load(parts[1])
    # The dangerous characters should survive round-trip through yaml
    assert "\n" in fm["source_files"][0] or "newline" in fm["source_files"][0]
    assert fm["batch_id"] == 'batch-with-"quotes"'


# ── 2026-07-29 incident class: numbering must survive watch-dir destruction ──


def _floor_db(tmp_path: Path, rows: list[str | None]) -> Path:
    """Build a throwaway sqlite DB with an inbox_items.response_path column."""
    db = tmp_path / "floor.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE inbox_items (response_path TEXT)")
    conn.executemany("INSERT INTO inbox_items VALUES (?)", [(r,) for r in rows])
    conn.commit()
    conn.close()
    return db


@pytest.mark.asyncio
async def test_counter_survives_full_watch_dir_wipe(
    writer: ResponseWriter, tmp_path: Path,
):
    """REPRO of the 2026-07-29 incident: a sync false-positive wipes EVERY
    ``*.genesis.md`` in the watch dir (and, before the fix, the counter file
    that lived beside them).  Numbering must continue, never restart at 1 —
    a restart silently OVERWRITES already-delivered files in the vault."""
    source = tmp_path / "doc.md"
    source.write_text("x")
    for i in range(1, 4):
        await writer.write_response(
            batch_id=f"w{i}", source_files=[str(source)],
            evaluation_text=str(i), item_count=1,
        )
    # The wipe: every response file AND any counter file in the watch dir
    for f in list(tmp_path.glob("*.genesis.md")):
        f.unlink()
    legacy = tmp_path / ".genesis-counters.json"
    if legacy.exists():
        legacy.unlink()
    p = await writer.write_response(
        batch_id="w4", source_files=[str(source)],
        evaluation_text="after wipe", item_count=1,
    )
    assert p.name == "doc-4.genesis.md", (
        f"numbering restarted at {p.name} after a watch-dir wipe — "
        "this is the overwrite bug"
    )


@pytest.mark.asyncio
async def test_no_counter_file_written_into_watch_path(
    writer: ResponseWriter, tmp_path: Path,
):
    """The counter store must live OUTSIDE the sync mirror: anything inside
    the watch dir is deleted by the vault sync every cycle (measured: 175
    deletions of the old in-mirror counter file)."""
    source = tmp_path / "note.md"
    source.write_text("x")
    await writer.write_response(
        batch_id="s1", source_files=[str(source)],
        evaluation_text="a", item_count=1,
    )
    assert not (tmp_path / ".genesis-counters.json").exists(), (
        "counter file written into the watch dir — the sync mirror deletes it"
    )


@pytest.mark.asyncio
async def test_db_floor_rescues_after_total_state_loss(
    writer: ResponseWriter, tmp_path: Path, monkeypatch,
):
    """Even with the counter store AND the watch dir both empty, the DB's
    response_path history (inbox_items) must floor the numbering."""
    db = _floor_db(
        tmp_path,
        [f"/some/old/inbox/doc-{n}.genesis.md" for n in (7, 118, 42)],
    )
    monkeypatch.setattr("genesis.env.genesis_db_path", lambda: db)

    source = tmp_path / "doc.md"
    source.write_text("x")
    p = await writer.write_response(
        batch_id="d1", source_files=[str(source)],
        evaluation_text="rescued", item_count=1,
    )
    assert p.name == "doc-119.genesis.md", (
        f"got {p.name}: DB floor (max=118) not honored after total state loss"
    )


@pytest.mark.asyncio
async def test_db_floor_ignores_other_stems_and_junk(
    writer: ResponseWriter, tmp_path: Path, monkeypatch,
):
    """DB floor parses strictly: other stems, non-numeric suffixes, and NULLs
    must not affect this stem's numbering."""
    db = _floor_db(
        tmp_path,
        [
            "/x/other-99.genesis.md",
            "/x/doc-draft.genesis.md",
            "/x/doc-3.genesis.md",
            None,
            "/x/docs-50.genesis.md",  # longer stem, must not match "doc"
        ],
    )
    monkeypatch.setattr("genesis.env.genesis_db_path", lambda: db)

    source = tmp_path / "doc.md"
    source.write_text("x")
    p = await writer.write_response(
        batch_id="d2", source_files=[str(source)],
        evaluation_text="strict", item_count=1,
    )
    assert p.name == "doc-4.genesis.md"


@pytest.mark.asyncio
async def test_unreadable_db_does_not_break_writes(
    writer: ResponseWriter, tmp_path: Path, monkeypatch,
):
    """A missing/unreadable DB must degrade to the other floors, not raise."""
    monkeypatch.setattr(
        "genesis.env.genesis_db_path", lambda: tmp_path / "nope" / "absent.db",
    )
    source = tmp_path / "doc.md"
    source.write_text("x")
    p1 = await writer.write_response(
        batch_id="u1", source_files=[str(source)],
        evaluation_text="a", item_count=1,
    )
    p2 = await writer.write_response(
        batch_id="u2", source_files=[str(source)],
        evaluation_text="b", item_count=1,
    )
    assert p1.name == "doc-1.genesis.md"
    assert p2.name == "doc-2.genesis.md"


@pytest.mark.asyncio
async def test_concurrent_writes_mint_distinct_numbers(
    writer: ResponseWriter, tmp_path: Path, monkeypatch,
):
    """Locking regression (architect finding): _next_counter runs off-loop
    via to_thread, so without the module lock two overlapping writes could
    read the same high-water mark, mint the SAME number, and os.replace
    would silently clobber the first response — the exact vault-overwrite
    failure mode this module exists to prevent.

    The patched _db_floor injects latency INSIDE the read-modify-write
    window so an unlocked implementation fails deterministically instead of
    only under a lucky interleaving."""
    import asyncio
    import time as time_mod

    from genesis.inbox import writer as writer_mod

    real_db_floor = writer_mod._db_floor

    def slow_db_floor(directory, base_name, suffix):
        time_mod.sleep(0.05)
        return real_db_floor(directory, base_name, suffix)

    monkeypatch.setattr(writer_mod, "_db_floor", slow_db_floor)

    source = tmp_path / "doc.md"
    source.write_text("x")
    paths = await asyncio.gather(*[
        writer.write_response(
            batch_id=f"g{i}", source_files=[str(source)],
            evaluation_text=str(i), item_count=1,
        )
        for i in range(5)
    ])
    names = sorted(p.name for p in paths)
    assert len(set(names)) == 5, f"duplicate numbers minted concurrently: {names}"
    assert names == sorted(f"doc-{n}.genesis.md" for n in range(1, 6))


@pytest.mark.asyncio
async def test_corrupt_store_value_spares_other_entries(
    writer: ResponseWriter, tmp_path: Path,
):
    """Blast-radius lock (architect finding): a corrupt individual VALUE in
    the store must zero only that stem's floor — never discard (and then
    re-persist without) every other directory/stem's high-water mark."""
    import json

    from genesis.inbox import writer as writer_mod

    store_path = writer_mod._counter_store_path()
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps({
        str(tmp_path): {"doc": "garbage"},
        "/other/inbox": {"Genesis": 118},
    }))
    source = tmp_path / "doc.md"
    source.write_text("x")
    await writer.write_response(
        batch_id="cv1", source_files=[str(source)],
        evaluation_text="a", item_count=1,
    )
    persisted = json.loads(store_path.read_text())
    assert persisted.get("/other/inbox", {}).get("Genesis") == 118, (
        "sibling directory's high-water mark destroyed by a corrupt value"
    )


@pytest.mark.asyncio
async def test_flat_legacy_store_copied_to_new_location_is_safe(
    writer: ResponseWriter, tmp_path: Path,
):
    """An operator copying the OLD flat-format file ({stem: N}) into the new
    location must not crash numbering (values are dicts in the new schema)."""
    import json

    from genesis.inbox import writer as writer_mod

    store_path = writer_mod._counter_store_path()
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(json.dumps({"doc": 7}))  # legacy flat shape
    source = tmp_path / "doc.md"
    source.write_text("x")
    p = await writer.write_response(
        batch_id="fl1", source_files=[str(source)],
        evaluation_text="a", item_count=1,
    )
    # Flat shape isn't the new schema: its keys are stems, not directories,
    # so it contributes no floor — numbering starts fresh from other floors.
    assert p.name == "doc-1.genesis.md"


@pytest.mark.asyncio
async def test_legacy_in_mirror_counter_still_honored(
    writer: ResponseWriter, tmp_path: Path,
):
    """Migration: a surviving legacy ``.genesis-counters.json`` in the watch
    dir (old location) is still read as a floor, so an install upgrading
    mid-sequence never renumbers backwards."""
    import json

    (tmp_path / ".genesis-counters.json").write_text(json.dumps({"doc": 9}))
    source = tmp_path / "doc.md"
    source.write_text("x")
    p = await writer.write_response(
        batch_id="l1", source_files=[str(source)],
        evaluation_text="migrated", item_count=1,
    )
    assert p.name == "doc-10.genesis.md"
