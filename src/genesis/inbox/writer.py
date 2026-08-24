"""Response writer — Obsidian-compatible markdown with atomic writes.

Responses are written as numbered sibling files next to the source:
  Input:    Untitled.md
  Response: Untitled-1.genesis.md, Untitled-2.genesis.md, ...

Every evaluation gets a unique monotonically increasing number.
No subdirectory needed — the .genesis.md suffix marks response files.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from genesis.env import user_timezone
from genesis.inbox.scanner import RESPONSE_SUFFIX

logger = logging.getLogger(__name__)

_COUNTER_FILE = ".genesis-counters.json"

# Serializes the counter store's read-modify-write across the to_thread
# workers write_response dispatches into (threads share this module).
_COUNTER_LOCK = threading.Lock()


def _counter_store_path() -> Path:
    """Durable counter store, OUTSIDE the vault-sync mirror.

    The old store lived inside the watch dir, where the 5-minute vault sync
    deleted it every cycle (measured: 175 deletions) — so the 2026-07-29
    false-positive wipe of the watch dir reset numbering to 1 and every
    subsequent response silently overwrote an already-delivered vault file.
    """
    from genesis import env

    return env.genesis_home() / "state" / "inbox-counters.json"


class ResponseWriter:
    """Writes evaluation results as Obsidian-compatible markdown."""

    def __init__(self, *, watch_path: Path, timezone: str = ""):
        self._watch_path = watch_path
        tz_name = timezone or user_timezone()
        try:
            self._tz = ZoneInfo(tz_name)
        except (KeyError, ZoneInfoNotFoundError):
            logger.warning("Invalid timezone %r, falling back to UTC", tz_name)
            self._tz = ZoneInfo("UTC")

    async def write_response(
        self,
        *,
        batch_id: str,
        source_files: list[str],
        evaluation_text: str,
        item_count: int,
    ) -> Path:
        """Write an evaluation response file atomically.

        For single-item batches, the response is a sibling of the source file:
          source.md → source-N.genesis.md (monotonically numbered)

        For multi-item batches, the response uses the batch ID:
          <date>-inbox-<batch_slug>.genesis.md

        Returns the path of the written file.
        """
        self._watch_path.mkdir(parents=True, exist_ok=True)
        now_local = datetime.now(UTC).astimezone(self._tz)
        datetime_str = now_local.strftime("%Y-%m-%d %H:%M")
        date_file = now_local.strftime("%Y-%m-%d")

        if item_count == 1 and source_files:
            # Sibling response: Untitled.md → Untitled-1.genesis.md
            source = Path(source_files[0])
            stem = source.stem  # "Untitled" from "Untitled.md"
            base_dir = source.parent
        else:
            # Multi-item batch: date-based filename in watch_path
            slug = batch_id[:8]
            stem = f"{date_file}-inbox-{slug}"
            base_dir = self._watch_path

        # Always numbered — monotonically increasing, never reuses numbers.
        # Off-loop: the counter derivation reads/writes the store file and
        # queries the DB floor (blocking I/O). _next_counter serializes on a
        # module lock — moving it off the event loop forfeited coroutine
        # atomicity, and an unlocked read-modify-write of the store would
        # let two concurrent writes mint the SAME number (the silent-vault-
        # overwrite failure mode this module exists to prevent).
        next_num = await asyncio.to_thread(_next_counter, base_dir, stem, RESPONSE_SUFFIX)
        target = base_dir / f"{stem}-{next_num}{RESPONSE_SUFFIX}"
        if target.exists():
            # Invariant breach: the freshly-minted number already exists on
            # disk (the scan floor makes this impossible except under a race
            # this code failed to serialize). Fail LOUDLY — os.replace below
            # would silently clobber a delivered response.
            raise RuntimeError(
                f"response numbering invariant breach: {target} already exists",
            )

        frontmatter_data = {
            "date": datetime_str,
            "source_files": source_files,
            "batch_id": batch_id,
        }
        frontmatter = _dump_frontmatter(frontmatter_data)

        body = frontmatter + evaluation_text + "\n"

        # Atomic write: .tmp → rename
        tmp_path = target.with_suffix(".tmp")
        await asyncio.to_thread(self._write_atomic, tmp_path, target, body)
        return target

    @staticmethod
    def _write_atomic(tmp_path: Path, target: Path, content: str) -> None:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(str(tmp_path), str(target))


def _db_floor(directory: Path, base_name: str, suffix: str) -> int:
    """Highest number ever recorded for *base_name* in inbox_items.response_path.

    Fourth numbering floor: even if the counter store AND the watch dir are
    both lost, the DB's own response history prevents renumbering backwards
    (a low number silently OVERWRITES an already-delivered vault file).
    Degrades to 0 (with a warning) if the DB is missing/unreadable — a
    response write must never fail on a DB hiccup.
    """
    # NOTE: `directory` is deliberately unused — response_path rows from ANY
    # directory with a matching stem inflate this floor. Over-counting only
    # skips numbers (cosmetic gaps) and inherently covers directory renames;
    # under-counting is the overwrite bug. Kept for signature symmetry.
    del directory
    from genesis import env

    prefix = f"{base_name}-"
    floor = 0
    try:
        db_path = env.genesis_db_path()
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT response_path FROM inbox_items WHERE response_path IS NOT NULL",
            ).fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        logger.warning(
            "Counter DB floor unavailable (%s): falling back to store/scan floors",
            exc,
            exc_info=True,
        )
        return 0
    for (rp,) in rows:
        name = Path(rp).name
        if not name.startswith(prefix) or not name.endswith(suffix):
            continue
        num_part = name.removesuffix(suffix)[len(prefix):]
        try:
            floor = max(floor, int(num_part))
        except ValueError:
            continue  # non-numeric suffix, ignore
    return floor


def _next_counter(directory: Path, base_name: str, suffix: str) -> int:
    """Return the next monotonically increasing number for *base_name*.

    Numbers must NEVER go backwards: a reused number silently overwrites an
    already-delivered response file in the user's vault (the 2026-07-29
    incident — a sync false-positive wiped the watch dir including the old
    in-mirror counter file, numbering restarted at 1, and weeks of responses
    landed as invisible overwrites of files the user had already read).

    Four floors, the max wins:
      1. the durable counter store (``~/.genesis/state/``, OUTSIDE the
         vault-sync mirror — nothing else may delete it),
      2. the legacy in-mirror counter file (migration: an install upgrading
         mid-sequence keeps its high-water mark),
      3. a filesystem scan of the watch dir,
      4. the DB's ``inbox_items.response_path`` history (survives even a
         total loss of 1-3).

    Runs off the event loop (write_response wraps it in to_thread), so the
    store read-modify-write is serialized on a module lock — without it two
    concurrent calls could mint the same number and silently overwrite.
    """
    with _COUNTER_LOCK:
        return _next_counter_locked(directory, base_name, suffix)


def _next_counter_locked(directory: Path, base_name: str, suffix: str) -> int:
    store_path = _counter_store_path()
    dir_key = str(directory)

    # 1. Durable store: {directory: {base_name: high_water}}
    # Blast-radius discipline: only an unreadable/undecodable FILE resets the
    # parsed store; a corrupt individual VALUE zeroes this stem's floor but
    # must never discard (and then re-persist without) every OTHER directory
    # and stem's high-water mark.
    stored_max = 0
    store: dict = {}
    if store_path.exists():
        try:
            loaded = json.loads(store_path.read_text(encoding="utf-8"))
            store = loaded if isinstance(loaded, dict) else {}
        except (json.JSONDecodeError, OSError):
            store = {}  # Unreadable/corrupt file — fall through to other floors
        try:
            stored_max = int(store.get(dir_key, {}).get(base_name, 0))
        except (ValueError, TypeError, AttributeError):
            stored_max = 0  # Corrupt value — keep the rest of the store intact

    # 2. Legacy in-mirror counter file (pre-relocation installs)
    legacy_max = 0
    legacy_path = directory / _COUNTER_FILE
    if legacy_path.exists():
        try:
            legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
            legacy_max = int(legacy.get(base_name, 0))
        except (json.JSONDecodeError, ValueError, TypeError, OSError):
            pass

    # 3. Scan filesystem for highest existing number
    disk_max = 0
    pattern = f"{base_name}-*{suffix}"
    for p in directory.glob(pattern):
        stem_no_suffix = p.name.removesuffix(suffix)
        num_part = stem_no_suffix[len(base_name) + 1:]  # after "{name}-"
        try:
            disk_max = max(disk_max, int(num_part))
        except ValueError:
            continue  # non-numeric suffix, ignore

    # 4. DB response-path history
    db_max = _db_floor(directory, base_name, suffix)

    next_num = max(stored_max, legacy_max, disk_max, db_max) + 1

    # Persist the new high-water mark to the durable store
    try:
        store_path.parent.mkdir(parents=True, exist_ok=True)
        if not isinstance(store.get(dir_key), dict):
            store[dir_key] = {}
        store[dir_key][base_name] = next_num
        store_path.write_text(
            json.dumps(store, indent=2) + "\n", encoding="utf-8",
        )
    except OSError:
        logger.warning(
            "Could not persist counter store %s", store_path, exc_info=True,
        )

    return next_num


def _dump_frontmatter(data: dict) -> str:
    """Render a dict as YAML frontmatter using yaml.safe_dump for proper escaping."""
    buf = io.StringIO()
    yaml.safe_dump(data, buf, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return f"---\n{buf.getvalue()}---\n\n"
