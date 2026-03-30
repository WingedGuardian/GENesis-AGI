"""ConversationCollector — count user interactions since last reflection."""

from __future__ import annotations

import glob
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from genesis.awareness.types import SignalReading
from genesis.db.crud import awareness_ticks
from genesis.env import cc_project_dir

logger = logging.getLogger(__name__)

CC_JSONL_DIR = str(Path.home() / ".claude" / "projects" / cc_project_dir())
_NORMALIZE_CEILING = 10  # 10+ interactions = 1.0


class ConversationCollector:
    """Counts user interactions (CC CLI + Telegram) since last reflection.

    Normalizes: 0 interactions = 0.0, 10+ interactions = 1.0.
    """

    signal_name = "conversations_since_reflection"

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def collect(self) -> SignalReading:
        # Find cutoff: last tick where a reflection was triggered
        last_ref = await awareness_ticks.last_reflected_tick(self._db)
        cutoff = last_ref["created_at"] if last_ref else "2000-01-01T00:00:00"

        # Source 1: CC CLI turns from JSONL files
        jsonl_turns = self._count_jsonl_turns(cutoff)

        # Source 2: Telegram/terminal sessions since cutoff
        tg_sessions = await self._count_channel_sessions(cutoff)

        total = jsonl_turns + tg_sessions
        value = min(1.0, total / _NORMALIZE_CEILING)

        return SignalReading(
            name=self.signal_name,
            value=round(value, 3),
            source="cc_sessions",
            collected_at=datetime.now(UTC).isoformat(),
        )

    def _count_jsonl_turns(self, cutoff: str) -> int:
        """Count user messages in CC CLI JSONL files since cutoff timestamp."""
        jsonl_dir = os.path.expanduser(CC_JSONL_DIR)
        try:
            files = sorted(
                glob.glob(f"{jsonl_dir}/*.jsonl"),
                key=os.path.getmtime,
                reverse=True,
            )
        except OSError:
            return 0

        count = 0
        for filepath in files[:3]:  # Check last 3 session files
            try:
                with open(filepath, "rb") as f:
                    f.seek(0, 2)
                    size = f.tell()
                    f.seek(max(0, size - 200_000))
                    if size > 200_000:
                        f.readline()  # skip partial line
                    for line in f:
                        try:
                            d = json.loads(line)
                            if d.get("type") == "user":
                                ts = d.get("timestamp", "")
                                if ts > cutoff:
                                    count += 1
                        except (json.JSONDecodeError, KeyError):
                            continue
            except OSError:
                continue
        return count

    async def _count_channel_sessions(self, cutoff: str) -> int:
        """Count Telegram/terminal sessions active since cutoff."""
        try:
            cursor = await self._db.execute(
                """SELECT COUNT(*) FROM cc_sessions
                   WHERE channel IN ('telegram', 'terminal')
                   AND last_activity_at > ?""",
                (cutoff,),
            )
            row = await cursor.fetchone()
            return row[0] if row else 0
        except Exception:
            logger.error("Channel session count failed", exc_info=True)
            return 0
