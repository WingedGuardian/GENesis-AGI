"""Cognitive self-modification ledger — capture + rollback of cognitive file edits.

Genesis autonomously overwrites its own cognitive config files at runtime (skill
``SKILL.md`` refinement, daily ``TRIAGE_CALIBRATION.md`` regen, daily
``USER_KNOWLEDGE.md`` synthesis). This module brackets those overwrites: it captures
the PRE-IMAGE before each write into the ``cognitive_file_modifications`` ledger, so a
self-edit that degrades cognition can be reverted with :func:`rollback`.

Design notes:
- **The write is primary; the ledger is best-effort.** A failing ledger insert is
  logged (``exc_info``) and swallowed — instrumentation must never block cognition.
  An actual file-write failure still propagates (same as the raw ``write_text`` it
  replaces).
- **Atomic, same-directory writes.** Temp file lives beside the target so the rename
  never crosses a filesystem boundary (NEVER ``/tmp`` or ``cc-tmp``).
- **Operator surface only.** No automated cognitive path reads this ledger (same
  discipline as ``ego_calibration_snapshots``). Rollback is manual/programmatic in v1;
  auto-rollback on a degradation signal is a deliberate, separately-flagged future PR.
- **Known v1 limitation:** two concurrent writers to the SAME file can capture a stale
  pre-image (the read-image and the write are not one transaction). The rollback drift
  guard catches the common case; this is acceptable for the weekly/daily per-file jobs.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from genesis.db.crud import cognitive_file_modifications as cfm_crud

logger = logging.getLogger(__name__)

# Keep the most-recent N ledger rows per target file (rows store full file
# contents). 30 → ~30 days of rollback history for the daily-regenerated files.
_KEEP_PER_TARGET = 30


def _read_current(path: Path) -> str | None:
    """Read a file's current contents, or ``None`` if absent/unreadable."""
    try:
        return path.read_text(encoding="utf-8") if path.exists() else None
    except OSError:
        logger.warning("cognitive_ledger: could not read %s", path, exc_info=True)
        return None


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically via a same-directory temp + rename.

    The temp name is UNIQUE so concurrent writers to the same target never share
    (and corrupt) a temp file, and it is removed if the write fails before the
    rename (no orphaned ``.cogtmp`` left behind).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.cogtmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)  # os.replace — atomic on the same filesystem, overwrites
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


async def record_existing(
    db: aiosqlite.Connection,
    *,
    actor: str,
    path: Path,
    prior_content: str | None,
    applied_content: str,
    summary: str | None = None,
    metadata: dict | None = None,
) -> str | None:
    """Record a write the caller ALREADY performed (best-effort).

    Use this when the caller owns the write (e.g. a sync writer that already ran).
    Returns the new mod id, or ``None`` if the ledger insert failed (the file write
    is unaffected). Never raises.
    """
    try:
        mod_id = await cfm_crud.record(
            db,
            actor=actor,
            target_path=str(path),
            prior_content=prior_content,
            applied_content=applied_content,
            change_summary=summary,
            metadata=metadata,
        )
    except Exception:
        logger.warning(
            "cognitive_ledger: ledger insert failed for %s (write unaffected)",
            path, exc_info=True,
        )
        return None
    # Prune-on-write: bound table growth (self-bounding, no scheduler job).
    try:
        await cfm_crud.prune_keep_per_target(db, str(path), keep=_KEEP_PER_TARGET)
    except Exception:
        logger.warning("cognitive_ledger: prune failed for %s", path, exc_info=True)
    return mod_id


async def record_file_modification(
    db: aiosqlite.Connection,
    *,
    actor: str,
    path: Path,
    new_content: str,
    summary: str | None = None,
    metadata: dict | None = None,
) -> str | None:
    """Capture the pre-image, write ``new_content`` atomically, record the ledger row.

    The write is the primary operation (propagates on failure, like the raw
    ``write_text`` it replaces). The ledger half is best-effort. Returns the mod id
    (or ``None`` if only the ledger insert failed — the write still happened).
    """
    prior = _read_current(path)
    _atomic_write(path, new_content)  # primary op — may raise (preserves behavior)
    return await record_existing(
        db, actor=actor, path=path, prior_content=prior,
        applied_content=new_content, summary=summary, metadata=metadata,
    )


async def rollback(
    db: aiosqlite.Connection, mod_id: str, *, force: bool = False,
) -> dict:
    """Revert a recorded cognitive file modification to its pre-image.

    Drift guard: if the file on disk no longer matches the ``applied_content`` of
    this entry (a later write replaced it), the rollback is REFUSED unless
    ``force=True`` — the operator should roll back the newer entry instead. Emits an
    observation on every outcome (success / refused / failed) so it is visible in the
    dashboard, not just in the return value.
    """
    row = await cfm_crud.get(db, mod_id)
    if row is None:
        return {"ok": False, "refused": False, "mod_id": mod_id,
                "reason": "no such modification"}
    if row.get("status") == "rolled_back":
        return {"ok": False, "refused": False, "mod_id": mod_id,
                "target_path": row.get("target_path"),
                "reason": "already rolled back"}

    path = Path(row["target_path"])
    current = _read_current(path)

    # Drift guard — refuse if the file changed since this modification.
    if current != row.get("applied_content") and not force:
        result = {
            "ok": False, "refused": True, "mod_id": mod_id,
            "target_path": row["target_path"],
            "reason": ("file changed since this modification — a later write replaced "
                       "it; roll back the newer entry, or pass force=True"),
        }
        await _emit_rollback_observation(db, row, result)
        return result

    # Restore the pre-image (or remove the file if it didn't exist before).
    try:
        if row.get("prior_content") is None:
            path.unlink(missing_ok=True)
            restored = "absent"
        else:
            _atomic_write(path, row["prior_content"])
            restored = "prior"
    except OSError as exc:
        result = {"ok": False, "refused": False, "mod_id": mod_id,
                  "target_path": row["target_path"], "reason": f"restore failed: {exc}"}
        await _emit_rollback_observation(db, row, result)
        return result

    await cfm_crud.mark_rolled_back(db, mod_id)
    result = {"ok": True, "refused": False, "mod_id": mod_id,
              "target_path": row["target_path"], "restored_to": restored,
              "forced": force}
    await _emit_rollback_observation(db, row, result)
    return result


async def recent(
    db: aiosqlite.Connection, *, limit: int = 20, actor: str | None = None,
) -> list[dict]:
    """Most-recent ledger rows (newest first), optionally filtered by actor."""
    return await cfm_crud.recent(db, limit=limit, actor=actor)


async def _emit_rollback_observation(
    db: aiosqlite.Connection, row: dict, result: dict,
) -> None:
    """Best-effort observation so a rollback (or its refusal) is visible. Never raises."""
    try:
        from genesis.db.crud import observations

        if result.get("ok"):
            outcome = "rolled_back"
        elif result.get("refused"):
            outcome = "refused"
        else:
            outcome = "failed"
        await observations.create(
            db,
            id=str(uuid.uuid4()),
            source="cognitive_ledger",
            type="self_mod_rollback",
            content=json.dumps({
                "mod_id": result.get("mod_id"),
                "actor": row.get("actor"),
                "target_path": row.get("target_path"),
                "outcome": outcome,
                "reason": result.get("reason"),
                "restored_to": result.get("restored_to"),
                "forced": result.get("forced", False),
            }),
            priority="high",
            created_at=datetime.now(UTC).isoformat(),
        )
    except Exception:
        logger.warning(
            "cognitive_ledger: failed to emit rollback observation", exc_info=True,
        )
