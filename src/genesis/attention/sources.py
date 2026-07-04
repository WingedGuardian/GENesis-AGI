"""Source adapters that feed ``AmbientUtterance`` into the engine. All I/O lives HERE,
never in the pure core. ``SnapshotSource`` reads a transient read-only ambient.db
snapshot; ``row_to_utterance`` is pure and unit-testable. A ``LiveBridgeSource`` (the
future edge adapter emitting the same ``AmbientUtterance``) slots in beside this.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from genesis.attention.clarity import frac_below
from genesis.attention.snapshot import _DEFAULT_DEST as _SNAPSHOTS_DIR
from genesis.attention.types import AmbientUtterance

logger = logging.getLogger(__name__)

_LABEL_TOTAL = re.compile(r"/(\d+)\s*$")  # speaker_label "wN:c/TOTAL" -> TOTAL


def _parse_ts(ts) -> float | None:
    """ISO8601 (UTC, naive-assumed-UTC) -> epoch seconds. None if unparseable."""
    if not isinstance(ts, str):
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _speaker_total(label) -> int | None:
    if not label:
        return None
    m = _LABEL_TOTAL.search(str(label))
    return int(m.group(1)) if m else None


def row_to_utterance(row: dict) -> AmbientUtterance | None:
    """Map an ``ambient_transcripts`` row (dict) -> ``AmbientUtterance``. Returns None
    when the row has no usable timestamp. Robust to missing/corrupt ``meta``."""
    ts = _parse_ts(row.get("ts"))
    if ts is None:
        return None
    meta = {}
    raw = row.get("meta")
    if raw:
        try:
            meta = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            meta = {}
    feats = meta.get("asr_feats") or {}
    audio = meta.get("audio") or {}
    ys = feats.get("ys_log_probs") or []
    try:
        n_tokens = int(feats.get("n_tokens") or 0)
    except (TypeError, ValueError):
        n_tokens = 0
    return AmbientUtterance(
        id=int(row["id"]),
        ts=ts,
        text=row.get("text") or "",
        duration_s=float(row.get("duration_s") or 0.0),
        is_user=row.get("is_user"),
        speaker_total=_speaker_total(row.get("speaker_label")),
        n_tokens=n_tokens,
        frac_lt_1=frac_below(ys, -1.0),
        rms=float(audio.get("rms") or 0.0),
        mode_state="unknown",  # ambient_transcripts carries no interaction-plane state
        source=row.get("source") or "",
        # no audio block (OMI / absent-or-corrupt meta) -> text-only clarity path:
        # loudness is unknowable, not quiet — don't fabricate an rms=0 penalty.
        has_audio=bool(audio),
    )


async def resolve_window_text(
    snapshot_id: str,
    utt_ids: Sequence[int],
    *,
    snapshots_dir: str | Path | None = None,
) -> list[dict] | None:
    """Resolve a window's transcript text from the TRANSIENT snapshot, for the reveal UI.

    Reads a transient read-only analysis snapshot — NOT genesis.db — so this raw SQL is
    intentionally outside the "crud-layer only" rule (that rule guards genesis.db). The
    firewall keeps transcript text off genesis.db; this returns it live to an authenticated
    caller and persists nothing.

    Returns the window's utterances (ts-ordered; ``is_trigger`` marks the last ``utt_id``,
    the utterance the event fired on), or ``None`` if the snapshot file is gone (purged) —
    the route turns ``None`` into a 410. A purged snapshot is permanent: that labeled event
    becomes review-read-only. Do NOT add snapshot auto-purge without addressing this."""
    base = Path(snapshots_dir).expanduser() if snapshots_dir else Path(_SNAPSHOTS_DIR).expanduser()
    # pull_snapshot() writes ``ambient_{id}.db`` + stores the bare id; tolerate either form.
    path = next(
        (p for p in (base / f"ambient_{snapshot_id}.db", base / f"{snapshot_id}.db") if p.exists()),
        None,
    )
    if path is None:
        return None
    ids = [int(i) for i in utt_ids]  # reject non-int; keep the IN() list parametrized
    if not ids:
        return []
    trigger_id = ids[-1]
    placeholders = ",".join("?" for _ in ids)
    conn = await aiosqlite.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = aiosqlite.Row
    try:
        cur = await conn.execute(
            f"SELECT id, ts, text, speaker_label, is_user FROM ambient_transcripts "  # noqa: S608
            f"WHERE id IN ({placeholders}) ORDER BY ts, id",
            ids,
        )
        rows = await cur.fetchall()
    finally:
        await conn.close()
    return [
        {
            "id": r["id"], "ts": r["ts"], "text": r["text"],
            "speaker_label": r["speaker_label"], "is_user": r["is_user"],
            "is_trigger": r["id"] == trigger_id,
        }
        for r in rows
    ]


class SnapshotSource:
    """Yield ts-ordered utterances from a read-only ambient.db snapshot file."""

    def __init__(self, snapshot_path: str | Path):
        self.path = Path(snapshot_path)

    def iter_utterances(self) -> Iterator[AmbientUtterance]:
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(
                "SELECT id, ts, text, duration_s, speaker_label, source, meta, is_user, "
                "speaker_name FROM ambient_transcripts ORDER BY ts, id"
            )
            for r in cur:
                utt = row_to_utterance(dict(r))
                if utt is not None:
                    yield utt
        finally:
            conn.close()
