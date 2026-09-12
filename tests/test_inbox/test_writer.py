"""Tests for inbox response writer."""

from __future__ import annotations

import os
import sqlite3
import sys
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


_CROSS_PROC_WORKER = '''
import asyncio, os, sys
from pathlib import Path
from genesis.inbox.writer import ResponseWriter
w = ResponseWriter(watch_path=Path(sys.argv[1]))
p = asyncio.run(w.write_response(
    batch_id="x", source_files=[sys.argv[1] + "/doc.md"],
    evaluation_text="proc-" + sys.argv[2], item_count=1,
))
sys.stdout.write(p.name)
'''


def test_cross_process_allocation_mints_distinct_numbers(tmp_path: Path):
    """Codex P1: the module lock only serializes threads in ONE interpreter;
    a second writer PROCESS (scripts/inbox_check.py overlapping the server)
    can mint the same number and clobber an evaluation. The os.link reservation
    must make allocation atomic ACROSS processes. Runs N real subprocesses
    writing the same stem concurrently and asserts distinct filenames + no
    lost content."""
    import subprocess

    watch = tmp_path / "inbox"
    watch.mkdir()
    (watch / "doc.md").write_text("src")
    env = {
        **os.environ,
        "GENESIS_HOME": str(tmp_path / "ghome"),   # isolate the counter store
        "GENESIS_DB_PATH": str(tmp_path / "nope.db"),  # DB floor → 0
    }
    n = 8
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _CROSS_PROC_WORKER, str(watch), str(i)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        for i in range(n)
    ]
    names = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
        names.append(out.strip())

    assert len(set(names)) == n, f"processes minted duplicate numbers: {names}"
    files = sorted(watch.glob("doc-*.genesis.md"))
    assert len(files) == n, f"expected {n} distinct response files, got {len(files)}"
    # No content lost: every proc's body survives in exactly one file.
    bodies = "".join(f.read_text() for f in files)
    for i in range(n):
        assert f"proc-{i}" in bodies, f"proc-{i}'s evaluation was clobbered"


@pytest.mark.asyncio
async def test_allocation_safe_even_with_lock_disabled(
    writer: ResponseWriter, tmp_path: Path, monkeypatch,
):
    """The atomic os.link reservation — not the lock — is the correctness
    guarantee. With the lock neutered, concurrent allocations still mint
    distinct numbers (the loser of a link race re-derives and retries)."""
    import asyncio
    import contextlib

    from genesis.inbox import writer as writer_mod

    monkeypatch.setattr(writer_mod, "_COUNTER_LOCK", contextlib.nullcontext())
    source = tmp_path / "doc.md"
    source.write_text("x")
    paths = await asyncio.gather(*[
        writer.write_response(
            batch_id=f"n{i}", source_files=[str(source)],
            evaluation_text=str(i), item_count=1,
        )
        for i in range(6)
    ])
    names = sorted(p.name for p in paths)
    assert len(set(names)) == 6, f"duplicate numbers minted without the lock: {names}"


@pytest.mark.asyncio
async def test_db_floor_consulted_when_local_floors_stale_but_nonzero(
    writer: ResponseWriter, tmp_path: Path, monkeypatch,
):
    """Deciding-round finding: a PARTIAL restore leaves the store AND the disk
    nonzero but STALE (both trail the DB's true delivery history). Any gate
    that skips the DB when the local floors are nonzero re-mints an
    already-delivered number and overwrites a vault file. The DB floor — the
    one floor a vault restore can't roll back — must be consulted
    unconditionally."""
    import json as _json

    from genesis.inbox import writer as writer_mod

    # Partial restore: store says doc=5, disk holds doc-1..5, DB knows doc-118.
    store_path = writer_mod._counter_store_path()
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(_json.dumps({str(tmp_path): {"doc": 5}}))
    for n in (1, 2, 3, 4, 5):
        (tmp_path / f"doc-{n}.genesis.md").write_text("stale-restored")
    db = _floor_db(tmp_path, ["/x/doc-118.genesis.md"])
    monkeypatch.setattr("genesis.env.genesis_db_path", lambda: db)

    source = tmp_path / "doc.md"
    source.write_text("x")
    p = await writer.write_response(
        batch_id="pr", source_files=[str(source)],
        evaluation_text="a", item_count=1,
    )
    assert p.name == "doc-119.genesis.md", (
        f"got {p.name}: DB floor (118) skipped because store(5)+disk(5) were "
        "nonzero-but-stale — this overwrites delivered vault responses"
    )


@pytest.mark.asyncio
async def test_db_floor_consulted_when_store_empty_despite_disk_files(
    writer: ResponseWriter, tmp_path: Path, monkeypatch,
):
    """Recovery correctness: the DB floor is gated on the AUTHORITATIVE store,
    not on disk/legacy. After a store loss the watch dir may hold only a few
    partially-recovered files (low disk_max) while the vault history reaches
    far higher — skipping the DB floor because disk is nonzero would mint a
    number that OVERWRITES a delivered vault file."""
    db = _floor_db(tmp_path, [f"/x/doc-{n}.genesis.md" for n in (117, 118)])
    monkeypatch.setattr("genesis.env.genesis_db_path", lambda: db)

    source = tmp_path / "doc.md"
    source.write_text("x")
    # A few low-numbered local files survived (disk_max=3); the store is empty.
    for n in (1, 2, 3):
        (tmp_path / f"doc-{n}.genesis.md").write_text("recovered")

    p = await writer.write_response(
        batch_id="rc", source_files=[str(source)],
        evaluation_text="a", item_count=1,
    )
    assert p.name == "doc-119.genesis.md", (
        f"got {p.name}: DB floor (118) skipped because disk_max(3) was nonzero "
        "— this overwrites a delivered vault response"
    )


@pytest.mark.asyncio
async def test_db_floor_runs_when_disk_empty_even_if_store_lags(
    writer: ResponseWriter, tmp_path: Path, monkeypatch,
):
    """Whole-class-audit BLOCKER: the DB recovery floor must run whenever the
    watch dir is wiped (disk_max==0), NOT only when the store reads 0. The
    store is best-effort/cross-process and can silently lag the DB; gating on
    stored_max alone would skip the DB after a wipe and re-mint an
    already-delivered number, overwriting a vault file."""
    import json as _json

    from genesis.inbox import writer as writer_mod

    # Store LAGS the DB: it holds doc=5 (a stale/lost-update value), the watch
    # dir is empty (wiped), and the DB knows doc-118 was delivered.
    store_path = writer_mod._counter_store_path()
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(_json.dumps({str(tmp_path): {"doc": 5}}))
    db = _floor_db(tmp_path, ["/x/doc-118.genesis.md"])
    monkeypatch.setattr("genesis.env.genesis_db_path", lambda: db)

    source = tmp_path / "doc.md"
    source.write_text("x")
    p = await writer.write_response(
        batch_id="lag", source_files=[str(source)],
        evaluation_text="a", item_count=1,
    )
    assert p.name == "doc-119.genesis.md", (
        f"got {p.name}: DB floor (118) suppressed by a lagging store after a "
        "wipe — this overwrites a delivered vault response"
    )


@pytest.mark.asyncio
async def test_write_survives_unwritable_counter_store(
    writer: ResponseWriter, tmp_path: Path, monkeypatch,
):
    """Codex P1(b) regression: the counter store is a best-effort OPTIMIZATION,
    never load-bearing. If ~/.genesis/state cannot be created/written (read-only
    GENESIS_HOME) while the inbox stays writable, the response MUST still be
    written (DB/disk floors + os.link carry correctness) — not fail and get
    baselined complete with no response."""
    from genesis.inbox import writer as writer_mod

    # Point the store at a path under a read-only parent so mkdir/write fail.
    ro = tmp_path / "ro-home"
    ro.mkdir()
    (ro / "state").mkdir()
    monkeypatch.setattr(
        writer_mod, "_counter_store_path",
        lambda: ro / "state" / "inbox-counters.json",
    )
    ro_state = ro / "state"
    original_mode = ro_state.stat().st_mode
    ro_state.chmod(0o500)  # read+execute only → mkstemp/replace fail
    try:
        source = tmp_path / "doc.md"
        source.write_text("x")
        p1 = await writer.write_response(
            batch_id="ro1", source_files=[str(source)],
            evaluation_text="a", item_count=1,
        )
        p2 = await writer.write_response(
            batch_id="ro2", source_files=[str(source)],
            evaluation_text="b", item_count=1,
        )
    finally:
        ro_state.chmod(original_mode)
    # Writes succeeded and numbering still advanced via the disk floor.
    assert p1.name == "doc-1.genesis.md"
    assert p2.name == "doc-2.genesis.md"
    assert p1.exists() and p2.exists()


@pytest.mark.asyncio
async def test_db_floor_reads_path_with_uri_reserved_char(
    writer: ResponseWriter, tmp_path: Path, monkeypatch,
):
    """Codex P2: a DB path containing a URI-reserved char (# / ?) must be
    percent-encoded, not interpolated raw — else SQLite opens the wrong file
    and the recovery floor silently degrades to 0."""
    weird_dir = tmp_path / "a#b"
    weird_dir.mkdir()
    db = weird_dir / "genesis.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE inbox_items (response_path TEXT)")
    conn.execute(
        "INSERT INTO inbox_items VALUES (?)", ("/x/doc-77.genesis.md",),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("genesis.env.genesis_db_path", lambda: db)

    source = tmp_path / "doc.md"
    source.write_text("x")
    p = await writer.write_response(
        batch_id="u1", source_files=[str(source)],
        evaluation_text="a", item_count=1,
    )
    assert p.name == "doc-78.genesis.md", (
        f"got {p.name}: DB at a '#'-containing path not read (URI misparse)"
    )


@pytest.mark.asyncio
async def test_db_floor_skips_malformed_row(
    writer: ResponseWriter, tmp_path: Path, monkeypatch,
):
    """Codex P2: a BLOB / non-str response_path row must be skipped, not crash
    the floor and fail the response write."""
    db = tmp_path / "floor.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE inbox_items (response_path)")  # no affinity
    conn.execute("INSERT INTO inbox_items VALUES (?)", (b"\x00\x01binary",))
    conn.execute("INSERT INTO inbox_items VALUES (?)", ("/x/doc-118.genesis.md",))
    conn.commit()
    conn.close()
    monkeypatch.setattr("genesis.env.genesis_db_path", lambda: db)

    source = tmp_path / "doc.md"
    source.write_text("x")
    p = await writer.write_response(
        batch_id="mr", source_files=[str(source)],
        evaluation_text="a", item_count=1,
    )
    assert p.name == "doc-119.genesis.md", (
        f"got {p.name}: a malformed DB row broke the floor (expected the good "
        "row doc-118 to still be honored)"
    )


@pytest.mark.asyncio
async def test_response_file_has_standard_perms(
    writer: ResponseWriter, tmp_path: Path,
):
    """Codex P2: the published response must carry umask-derived perms (as an
    ordinary write would), not mkstemp's owner-only 0o600."""
    from genesis.inbox import writer as writer_mod

    source = tmp_path / "doc.md"
    source.write_text("x")
    p = await writer.write_response(
        batch_id="pm", source_files=[str(source)],
        evaluation_text="a", item_count=1,
    )
    expected = 0o666 & ~writer_mod._UMASK
    assert (p.stat().st_mode & 0o777) == expected, (
        f"perms {oct(p.stat().st_mode & 0o777)} != {oct(expected)} "
        "(mkstemp's 0o600 leaked to the response)"
    )


@pytest.mark.asyncio
async def test_chmod_failure_does_not_fail_write(
    writer: ResponseWriter, tmp_path: Path, monkeypatch,
):
    """Whole-class audit F1: the perms chmod is a best-effort optimization —
    a chmod failure (e.g. a network/FUSE fs) must NOT fail the response write
    (that would be baselined complete with no response)."""
    real_chmod = os.chmod

    def boom(path, *a, **k):
        if str(path).endswith(".tmp") and ".genesis-tmp-" in str(path):
            raise OSError("simulated chmod failure")
        return real_chmod(path, *a, **k)

    monkeypatch.setattr(os, "chmod", boom)
    source = tmp_path / "doc.md"
    source.write_text("x")
    p = await writer.write_response(
        batch_id="cf", source_files=[str(source)],
        evaluation_text="a", item_count=1,
    )
    assert p.name == "doc-1.genesis.md" and p.exists(), (
        "a chmod failure masked the response write"
    )


@pytest.mark.asyncio
async def test_concurrent_writes_do_not_touch_process_umask(
    writer: ResponseWriter, tmp_path: Path,
):
    """Whole-class audit F2: perms come from the import-time _UMASK snapshot,
    never a per-write os.umask() (process-global — racy on the to_thread pool
    and can permanently corrupt the process umask). Concurrent writes must
    leave the live process umask untouched."""
    import asyncio

    before = os.umask(0o022)
    os.umask(before)
    source = tmp_path / "doc.md"
    source.write_text("x")
    await asyncio.gather(*[
        writer.write_response(
            batch_id=f"u{i}", source_files=[str(source)],
            evaluation_text=str(i), item_count=1,
        )
        for i in range(6)
    ])
    after = os.umask(0o022)
    os.umask(after)
    assert after == before, (
        f"process umask changed {oct(before)}→{oct(after)} during writes "
        "(per-write os.umask race)"
    )


@pytest.mark.asyncio
async def test_tmp_cleanup_failure_does_not_mask_publish(
    writer: ResponseWriter, tmp_path: Path, monkeypatch,
):
    """Codex P2: a failed tmp unlink after a successful os.link must not mask
    the already-published response."""
    import pathlib

    real_unlink = pathlib.Path.unlink

    def boom(self, *args, **kwargs):
        if self.name.startswith(".genesis-tmp-"):
            raise OSError("simulated unlink failure")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "unlink", boom)
    source = tmp_path / "doc.md"
    source.write_text("x")
    p = await writer.write_response(
        batch_id="cm", source_files=[str(source)],
        evaluation_text="a", item_count=1,
    )
    assert p.name == "doc-1.genesis.md" and p.exists(), (
        "a tmp-cleanup failure masked a successfully published response"
    )


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
