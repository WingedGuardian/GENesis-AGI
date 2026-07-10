"""Detached ambient worker: retrieve + rank on a drift-trigger fire.

Spawned by the proactive hook (``start_new_session=True``) when a
session theme settles. Opens its OWN read-only DB connection, Qdrant
client, and embedding provider — it must never touch server state.
Writes exactly two places, both under ``~/.genesis``:

- ``sessions/<id>/ambient_verdict.json`` — the latest outcome (atomic)
- ``session_awareness/shadow_log.jsonl`` — append-only tuning record
  (size-capped; skips are counted in the verdict, never silent)

PR2 runs ``--no-arbiter`` only; PR3 adds the arbiter judgment stage.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .accumulator import top_entities
from .ranking import rank_candidates
from .slots import acquire_slot
from .statefiles import load_state
from .trigger import stability

SHADOW_LOG_MAX_BYTES = 50 * 1024 * 1024  # cap, counted when hit
ENTITY_QUERY_TERMS = 8
VERDICT_FILENAME = "ambient_verdict.json"


def _state_root() -> Path:
    return Path.home() / ".genesis" / "session_awareness"


def _sessions_root() -> Path:
    return Path.home() / ".genesis" / "sessions"


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        os.write(fd, json.dumps(data).encode())
    finally:
        os.close(fd)
    os.replace(tmp, str(path))


def _append_shadow_log(record: dict, state_root: Path) -> bool:
    """Append to shadow_log.jsonl. False (and no write) once capped."""
    log_path = state_root / "shadow_log.jsonl"
    try:
        if log_path.exists() and log_path.stat().st_size >= SHADOW_LOG_MAX_BYTES:
            return False
        state_root.mkdir(parents=True, exist_ok=True)
        with log_path.open("a") as fh:
            fh.write(json.dumps(record) + "\n")
        return True
    except OSError:
        return False


async def run_worker(
    session_id: str,
    *,
    no_arbiter: bool = False,
    sessions_root: Path | None = None,
    state_root: Path | None = None,
    db_path: Path | None = None,
    qdrant_url: str | None = None,
) -> dict:
    """One retrieval pass for *session_id*. Returns the verdict it wrote."""
    sessions_root = sessions_root or _sessions_root()
    state_root = state_root or _state_root()
    now_iso = datetime.now(UTC).isoformat()
    verdict_path = sessions_root / session_id / VERDICT_FILENAME

    def finish(verdict: dict) -> dict:
        verdict.setdefault("session_id", session_id)
        verdict.setdefault("generated_at", now_iso)
        _atomic_write_json(verdict_path, verdict)
        logged = _append_shadow_log(verdict, state_root)
        if not logged:
            verdict["shadow_log_skipped"] = True
            _atomic_write_json(verdict_path, verdict)
        return verdict

    state = load_state(session_id, base=sessions_root)
    if not state.get("ema"):
        return finish({"status": "no_theme"})

    slot = await acquire_slot(state_root / "locks")
    if slot is None:
        return finish({"status": "slots_busy"})

    try:
        import aiosqlite
        from qdrant_client import QdrantClient

        from genesis import env as genesis_env
        from genesis.memory.embeddings import EmbeddingProvider

        resolved_db = db_path or genesis_env.genesis_db_path()
        url = qdrant_url or genesis_env.qdrant_url()

        db = await aiosqlite.connect(
            f"file:{resolved_db}?mode=ro", uri=True,
        )
        try:
            qdrant = QdrantClient(url=url, timeout=10)
            provider = EmbeddingProvider()
            entity_query = " ".join(top_entities(state, ENTITY_QUERY_TERMS))
            candidates = await rank_candidates(
                ema=state["ema"],
                entity_query=entity_query,
                db=db,
                qdrant_client=qdrant,
                embedding_provider=provider,
            )
        finally:
            await db.close()

        theme_stats = {
            "ema_turns": state.get("ema_turns", 0),
            "stability": stability(state.get("ring", [])),
            "fired_count": state.get("fired_count", 0),
            "outlier_skips": state.get("outlier_skips", 0),
        }
        verdict: dict = {
            "status": "no_arbiter" if no_arbiter else "judged",
            "theme": theme_stats,
            "entity_query": entity_query,
            "candidates": candidates,
        }
        if not no_arbiter:
            from .arbiter import judge_candidates

            verdict.update(
                await judge_candidates(theme_stats, entity_query, candidates)
            )
            picks = verdict.get("picks") or []
            verdict["picked_memory_ids"] = [
                candidates[n - 1]["memory_id"]
                for n in picks
                if 1 <= n <= len(candidates)
            ]
        return finish(verdict)
    except Exception as exc:  # recorded, never raised — detached process
        return finish({"status": "error", "error": f"{type(exc).__name__}: {exc}"})
    finally:
        slot.release()
