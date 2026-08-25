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
import tempfile
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
# workers write_response dispatches into (threads share this module). This
# only reduces intra-process contention; cross-process allocation safety comes
# from the atomic os.link reservation in _allocate_and_link.
_COUNTER_LOCK = threading.Lock()

# Bound on re-derivation attempts when a concurrent writer takes our number.
# Each retry advances past the collision, so this only trips under pathological
# contention (or a wedged filesystem), where failing loud beats spinning.
_ALLOC_MAX_RETRIES = 50

# Process umask, read ONCE at import (single-threaded). os.umask is a
# process-global getter-by-setter: reading it per-write on the to_thread pool
# would race (wrong perms) and could permanently clobber the process umask for
# every unrelated file the server writes. Runtime umask changes are effectively
# never, so an import-time snapshot is correct in practice.
_UMASK = os.umask(0o022)
os.umask(_UMASK)


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

        frontmatter_data = {
            "date": datetime_str,
            "source_files": source_files,
            "batch_id": batch_id,
        }
        body = _dump_frontmatter(frontmatter_data) + evaluation_text + "\n"

        # Allocate the number and place the file in one atomic, cross-process
        # step. The number must NEVER be reused (a reused number silently
        # overwrites a delivered vault response — the 2026-07-29 incident).
        # A module lock only serializes threads in ONE interpreter, but a
        # second WRITER PROCESS (scripts/inbox_check.py overlapping the
        # server) can read the same floors and mint the same number. So the
        # reservation is the filesystem itself: os.link() is atomic and fails
        # if the target exists, across processes. On collision we re-derive
        # (the loser's next _next_counter sees the winner's file on disk / in
        # the store and bumps) and retry.
        return await asyncio.to_thread(
            self._allocate_and_link, base_dir, stem, RESPONSE_SUFFIX, body,
        )

    @staticmethod
    def _allocate_and_link(
        base_dir: Path, stem: str, suffix: str, body: str,
    ) -> Path:
        # Write the content to a unique tmp in the SAME dir (same fs → link is
        # atomic and the target appears fully-formed, never partial). The
        # ".genesis-" prefix means a crash-leaked tmp is BOTH scanner-ignored
        # (dot-prefixed) AND excluded from the vault sync (--exclude ".genesis-*")
        # — it can never leak into the user's Obsidian folder.
        fd, tmp_name = tempfile.mkstemp(
            dir=base_dir, prefix=".genesis-tmp-", suffix=".tmp",
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(body)
            # mkstemp forces 0o600; the published response (a hardlink to this
            # inode) should carry the perms an ordinary write would (umask-
            # derived, from the import-time snapshot — never read os.umask here,
            # it is process-global and races on the to_thread pool). Best-effort:
            # a chmod failure (e.g. a network/FUSE fs) must NEVER fail the write.
            try:
                os.chmod(tmp_path, 0o666 & ~_UMASK)
            except OSError:
                logger.warning(
                    "Could not set response perms on %s (best-effort)",
                    tmp_path, exc_info=True,
                )
            for _ in range(_ALLOC_MAX_RETRIES):
                next_num = _next_counter(base_dir, stem, suffix)
                target = base_dir / f"{stem}-{next_num}{suffix}"
                try:
                    # atomic create-or-fail: EEXIST → retry; other errno
                    # (ENOSPC/EMLINK/EXDEV/EACCES) propagate and fail loud.
                    os.link(str(tmp_path), str(target))
                except FileExistsError:
                    continue  # another writer took this number — re-derive
                return target
            raise RuntimeError(
                f"could not allocate a unique response number for {stem!r} "
                f"after {_ALLOC_MAX_RETRIES} attempts",
            )
        finally:
            # Best-effort cleanup: a failed tmp unlink must NEVER mask an
            # already-published response (os.link succeeded → the file exists).
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                logger.warning(
                    "Could not remove allocation tmp %s", tmp_path, exc_info=True,
                )


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
    from urllib.parse import quote

    from genesis import env

    prefix = f"{base_name}-"
    floor = 0
    try:
        db_path = env.genesis_db_path()
        # Percent-encode the path: a valid filename char that is URI-reserved
        # (?, #, %) would otherwise be parsed as a query/fragment, silently
        # opening the WRONG database (or degrading to a 0 floor) and defeating
        # this recovery floor. safe="/" keeps path separators literal.
        uri = f"file:{quote(str(db_path), safe='/')}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
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
        # A single malformed row (a BLOB/non-str response_path) must never
        # crash the whole floor and fail the response write — skip it.
        try:
            name = Path(rp).name
            if not name.startswith(prefix) or not name.endswith(suffix):
                continue
            floor = max(floor, int(name.removesuffix(suffix)[len(prefix):]))
        except (TypeError, ValueError, OSError):
            continue  # non-str path or non-numeric suffix — ignore
    return floor


def _next_counter(directory: Path, base_name: str, suffix: str) -> int:
    """Return the next monotonically increasing number for *base_name*.

    Numbers must NEVER go backwards: a reused number silently overwrites an
    already-delivered response file in the user's vault (the 2026-07-29
    incident — a sync false-positive wiped the watch dir including the old
    in-mirror counter file, numbering restarted at 1, and weeks of responses
    landed as invisible overwrites of files the user had already read).

    CORRECTNESS rests on TWO things only, neither of which can fail a write:
      * the DB delivery floor (``inbox_items.response_path`` — the server's
        append-only, never-rolled-back log), consulted UNCONDITIONALLY, and
      * the atomic ``os.link`` reservation in the caller (cross-process safe).
    Everything else is a best-effort OPTIMIZATION that must never raise into
    the write path:
      1. the durable counter store (``~/.genesis/state/``) — a cache/extra
         floor; a stale or lost-updated value is harmless because the DB is
         the authority, so it needs no cross-process lock,
      2. the legacy in-mirror counter file (migration high-water),
      3. a filesystem scan of the watch dir.
    The module ``threading.Lock`` only trims intra-process link-retry
    contention (a pure in-memory acquire — it cannot fail on a read-only FS);
    there is deliberately NO ``fcntl`` lock and NO store ``mkdir`` here — an
    unavailable ~/.genesis/state (read-only GENESIS_HOME) must degrade to the
    DB/disk floors + os.link, never fail the response (Codex P1).
    """
    store_path = _counter_store_path()
    with _COUNTER_LOCK:
        return _next_counter_locked(directory, base_name, suffix, store_path)


def _next_counter_locked(
    directory: Path, base_name: str, suffix: str, store_path: Path,
) -> int:
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

    # 4. DB response-path history — the ONE floor a vault-sync restore cannot
    #    roll back (inbox_items is the server's own append-only delivery log,
    #    not synced from the vault). Consulted UNCONDITIONALLY: any gate that
    #    trusts the local store/disk floors to decide whether to skip it
    #    under-counts when those floors are stale-but-nonzero — a PARTIAL
    #    restore (a sync-conflict rollback that leaves a few old files + an old
    #    store) has store>0 AND disk>0 yet both trail the DB, so a gate re-mints
    #    an already-delivered number and overwrites a vault file (the exact
    #    2026-07-29 failure class). Earlier gates on stored_max, then on
    #    stored_max||disk_max, each left a floor combination that under-counts;
    #    removing the gate removes the whole class. The O(history) scan is
    #    bounded (personal-scale inbox) and runs off the event loop — on a
    #    silent-data-loss path, correctness beats the micro-optimization.
    db_max = _db_floor(directory, base_name, suffix)

    next_num = max(stored_max, legacy_max, disk_max, db_max) + 1

    # Persist the new high-water mark to the durable store — BEST EFFORT ONLY.
    # The store is a cache/extra floor, never the authority (the DB is), so any
    # failure here (read-only GENESIS_HOME, a missing/permission-damaged state
    # dir) is swallowed and the response is STILL written — correctness rests on
    # the DB floor + os.link, not on this. Atomic tmp+os.replace so a reader
    # never sees a torn file and a crash can't truncate the store.
    try:
        store_path.parent.mkdir(parents=True, exist_ok=True)
        if not isinstance(store.get(dir_key), dict):
            store[dir_key] = {}
        store[dir_key][base_name] = next_num
        fd, tmp_name = tempfile.mkstemp(
            dir=store_path.parent, prefix=".inbox-counters-", suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(store, indent=2) + "\n")
            os.replace(tmp_name, store_path)
        except OSError:
            Path(tmp_name).unlink(missing_ok=True)
            raise
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
