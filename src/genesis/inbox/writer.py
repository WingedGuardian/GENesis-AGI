"""Response writer — Obsidian-compatible markdown with atomic writes.

Responses are written as sibling files next to the source:
  Input:    Untitled.md
  Response: Untitled.genesis.md

No subdirectory needed — the .genesis.md suffix marks response files.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from genesis.inbox.scanner import RESPONSE_SUFFIX

logger = logging.getLogger(__name__)

_COUNTER_FILE = ".genesis-counters.json"


class ResponseWriter:
    """Writes evaluation results as Obsidian-compatible markdown."""

    def __init__(self, *, watch_path: Path, timezone: str = "UTC"):
        self._watch_path = watch_path
        self._tz = ZoneInfo(timezone)

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
          source.md → source.genesis.md

        For multi-item batches, the response uses the batch ID:
          <date>-inbox-<batch_slug>.genesis.md

        Returns the path of the written file.
        """
        self._watch_path.mkdir(parents=True, exist_ok=True)
        now_local = datetime.now(UTC).astimezone(self._tz)
        datetime_str = now_local.strftime("%Y-%m-%d %H:%M")
        date_file = now_local.strftime("%Y-%m-%d")

        if item_count == 1 and source_files:
            # Sibling response: Untitled.md → Untitled.genesis.md
            source = Path(source_files[0])
            stem = source.stem  # "Untitled" from "Untitled.md"
            target = source.parent / f"{stem}{RESPONSE_SUFFIX}"
        else:
            # Multi-item batch: date-based filename in watch_path
            slug = batch_id[:8]
            target = self._watch_path / f"{date_file}-inbox-{slug}{RESPONSE_SUFFIX}"

        # Ensure unique filename — monotonically increasing, never reuses numbers
        if target.exists():
            base = target.with_suffix("").with_suffix("")  # strip .genesis.md
            next_num = _next_counter(base.parent, base.name, RESPONSE_SUFFIX)
            target = base.parent / f"{base.name}-{next_num}{RESPONSE_SUFFIX}"

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


def _next_counter(directory: Path, base_name: str, suffix: str) -> int:
    """Return the next monotonically increasing number for *base_name*.

    Uses a persistent counter file as primary source of truth, with a
    filesystem scan as fallback.  The higher of the two wins, so numbers
    never go backwards even if the counter file is lost or files are deleted.
    """
    counter_path = directory / _COUNTER_FILE

    # 1. Read persisted high-water mark
    stored_max = 0
    if counter_path.exists():
        try:
            data = json.loads(counter_path.read_text(encoding="utf-8"))
            stored_max = int(data.get(base_name, 0))
        except (json.JSONDecodeError, ValueError, OSError):
            pass  # Corrupted — fall through to filesystem scan

    # 2. Scan filesystem for highest existing number (fallback)
    disk_max = 0
    pattern = f"{base_name}-*{suffix}"
    for p in directory.glob(pattern):
        stem_no_suffix = p.name.removesuffix(suffix)
        num_part = stem_no_suffix[len(base_name) + 1:]  # after "{name}-"
        try:
            disk_max = max(disk_max, int(num_part))
        except ValueError:
            continue  # non-numeric suffix, ignore

    # 3. Next number = max(stored, disk) + 1
    next_num = max(stored_max, disk_max) + 1

    # 4. Persist updated counter
    try:
        counters: dict[str, int] = {}
        if counter_path.exists():
            try:
                counters = json.loads(counter_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                counters = {}
        counters[base_name] = next_num
        counter_path.write_text(
            json.dumps(counters, indent=2) + "\n", encoding="utf-8",
        )
    except OSError:
        logger.warning("Could not persist counter file %s", counter_path)

    return next_num


def _dump_frontmatter(data: dict) -> str:
    """Render a dict as YAML frontmatter using yaml.safe_dump for proper escaping."""
    buf = io.StringIO()
    yaml.safe_dump(data, buf, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return f"---\n{buf.getvalue()}---\n\n"
