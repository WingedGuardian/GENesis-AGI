"""Detached ambient worker: retrieve + rank on a drift-trigger fire.

Spawned by the proactive hook (``start_new_session=True``) when a
session theme settles. Opens its OWN read-only DB connection, Qdrant
client, and embedding provider — it must never touch server state.
Writes three places:

- ``~/.genesis/sessions/<id>/ambient_verdict.json`` — the latest outcome
  (atomic)
- ``~/.genesis/session_awareness/shadow_log.jsonl`` — append-only tuning
  record (size-capped; skips are counted in the verdict, never silent)
- ``call_site_last_run`` row ``ambient_arbiter`` — neural-monitor
  telemetry, one row per arbiter ATTEMPT (including pre-spawn failures,
  which record success=0 with the reason; empty candidate sets record
  nothing — judge_candidates short-circuits without spawning CC). Uses a
  separate short-lived RW connection; the retrieval connection stays
  ``mode=ro`` so the zero-write invariant on memory rows holds.

PR2 runs ``--no-arbiter`` only; PR3 adds the arbiter judgment stage.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
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


async def _record_arbiter_telemetry(
    db_path: Path | str, verdict: dict, n_candidates: int,
) -> bool:
    """Best-effort ``call_site_last_run`` row for the neural monitor.

    Never raises; True only when the row demonstrably landed (a swallowed
    INSERT failure reports False), so the verdict/shadow log stays honest
    about missing telemetry.
    """
    try:
        from genesis.observability.call_site_recorder import (
            record_last_run_detached,
        )

        from .arbiter import ARBITER_MODEL

        arbiter = verdict.get("arbiter", "unknown")
        parts = [
            f"arbiter={arbiter}",
            f"picks={len(verdict.get('picks') or [])}",
            f"candidates={n_candidates}",
            f"lat_ms={verdict.get('arbiter_latency_ms', 0)}",
        ]
        if verdict.get("reason"):
            parts.append(str(verdict["reason"])[:80])
        return await record_last_run_detached(
            str(db_path),
            "ambient_arbiter",
            provider="cc",
            model_id=ARBITER_MODEL,
            response_text="|".join(parts),
            success=arbiter == "ok",
        )
    except Exception:
        return False


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
        if not _append_shadow_log(verdict, state_root):
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
            entity_shadow: list[dict] = []
            candidates = await rank_candidates(
                ema=state["ema"],
                entity_query=entity_query,
                db=db,
                qdrant_client=qdrant,
                embedding_provider=provider,
                entity_shadow_out=entity_shadow,
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
            # E4 shadow telemetry: what the entity lane would have added
            # (empty once the lane goes live — hits then ride candidates).
            "entity_shadow": entity_shadow,
        }
        if not no_arbiter:
            # Deferred: --no-arbiter runs never load the subprocess
            # machinery (and its genesis.security/env imports).
            from .arbiter import judge_candidates

            arbiter_t0 = time.monotonic()
            verdict.update(
                await judge_candidates(theme_stats, entity_query, candidates)
            )
            verdict["arbiter_latency_ms"] = int(
                (time.monotonic() - arbiter_t0) * 1000
            )
            picks = verdict.get("picks") or []
            verdict["picked_memory_ids"] = [
                candidates[n - 1]["memory_id"]
                for n in picks
                if 1 <= n <= len(candidates)
            ]
            if candidates:
                # Empty candidate sets short-circuit judge_candidates without
                # spawning CC — no run happened, so nothing to record.
                verdict["telemetry_recorded"] = await _record_arbiter_telemetry(
                    resolved_db, verdict, len(candidates)
                )
        return finish(verdict)
    except Exception as exc:  # recorded, never raised — detached process
        return finish({"status": "error", "error": f"{type(exc).__name__}: {exc}"})
    finally:
        slot.release()
