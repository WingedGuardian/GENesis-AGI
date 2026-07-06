#!/usr/bin/env python3
"""Re-parse fence-polluted knowledge units through the fixed intake atomizer.

The 2026-07-03 context-layer audit found ~390 knowledge units whose body is a
raw ```json-fenced findings payload stored verbatim: surplus/wing-audit output
arrived fenced, the pre-fix atomizer could not parse it, and the whole envelope
was stored as a single unit (fixed in src/genesis/surplus/intake.py).

This one-off script:
  1. Selects intake-provenance rows still carrying a fence
     (``source_doc LIKE 'intake:%' AND body LIKE '%```json%'``). Non-intake
     rows with legitimate inline JSON fences are deliberately OUT of scope —
     verified 2026-07-03: 388 intake rows vs 3 legitimate manual/ingest docs.
  2. Re-runs the FIXED atomizer over each fenced payload. Per-row outcome
     (verified against the live population 2026-07-03):
       - parses to findings/object → RECOVER as proper units;
       - empty envelope → DELETE (nothing to store, audit decision D2);
       - payload IS a fence but its JSON is malformed/truncated → DELETE (D2);
       - prose that merely CONTAINS an inline fence → SKIP, keep the row —
         that is legitimate single-item content, not pollution.
  3. With --execute: ingests recovered findings as proper units (original
     domain/project/confidence preserved; original unit id and ingested_at
     carried as tags), then deletes the replaced/junk row everywhere it
     lives: the memory-layer cascade FIRST via store.delete() (Qdrant point
     + memory_metadata + memory_fts + links + pending_embeddings — ingest
     wrote the fenced body to that layer too, so a knowledge_units-only
     delete would leave it recallable via FTS), then knowledge_units +
     knowledge_fts, then ingestion-manifest bookkeeping. If the cascade
     fails, the row is KEPT so a re-run can retry it, and the script exits
     nonzero. Every deleted row is first dumped verbatim to a JSONL backup
     under ~/.genesis/output/ for manual salvage.
  4. Default is DRY-RUN: reports what would happen, writes nothing.

Idempotent: re-ingest upserts on (project, domain, concept), so a partially
completed run can safely be re-run.

Run from the repo root with the venv active:
    python scripts/cleanup_fenced_knowledge_units.py            # dry-run
    python scripts/cleanup_fenced_knowledge_units.py --execute
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("cleanup_fenced_ku")


def _derive_task_type(title: str, multi_types: frozenset[str]) -> str:
    """Reverse the single_item title back to a task type for re-atomization.

    The polluted rows were stored via the single_item path, whose title is
    ``task_type.replace("_", " ").title()``. Fall back to a known
    multi-finding type — the type only gates the multi-parse path; every
    selected row is a fenced JSON payload that needs that path.
    """
    derived = title.strip().lower().replace(" ", "_")
    return derived if derived in multi_types else "anticipatory_research"


async def main(execute: bool, limit: int | None) -> int:
    import aiosqlite
    from qdrant_client import QdrantClient

    from genesis.db.crud import knowledge as knowledge_crud
    from genesis.env import genesis_db_path, qdrant_url
    from genesis.memory.embeddings import EmbeddingProvider
    from genesis.memory.knowledge_ingest import ingest_knowledge_unit
    from genesis.memory.linker import MemoryLinker
    from genesis.memory.store import MemoryStore
    from genesis.surplus import intake

    if not hasattr(intake, "FENCE_RE"):
        logger.error("intake.py fence fix is not present — refusing to run.")
        return 1

    db = await aiosqlite.connect(str(genesis_db_path()))
    db.row_factory = aiosqlite.Row
    store = None
    backup_fh = None
    if execute:
        from datetime import UTC, datetime

        qdrant = QdrantClient(url=qdrant_url(), timeout=10)
        store = MemoryStore(
            embedding_provider=EmbeddingProvider(),
            qdrant_client=qdrant,
            db=db,
            linker=MemoryLinker(qdrant_client=qdrant, db=db),
        )
        backup_dir = Path.home() / ".genesis" / "output"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup_path = backup_dir / f"ku_cleanup_backup_{stamp}.jsonl"
        backup_fh = open(backup_path, "w", encoding="utf-8")  # noqa: SIM115 — closed in finally with db
        logger.info("Deleted-row backup: %s", backup_path)

    try:
        rows = await knowledge_crud.select_fenced_intake_rows(db)
        if limit is not None:
            rows = rows[:limit]
        logger.info("Selected %d fence-polluted intake rows%s", len(rows),
                    "" if execute else " (DRY-RUN — no writes)")

        recovered_units = 0
        rows_recovered = 0
        rows_empty = 0
        rows_junk = 0
        rows_kept = 0
        junk_ids: list[str] = []
        kept_ids: list[str] = []
        failed_ids: list[str] = []

        for row in rows:
            title, sep, payload = row["body"].partition("\n\n")
            if not sep:
                payload = row["body"]
                title = ""
            task_type = _derive_task_type(title, intake.MULTI_FINDING_TASK_TYPES)
            findings, path = intake.atomize(payload, task_type)

            if path in ("json_findings", "json_single") and findings:
                rows_recovered += 1
                recovered_units += len(findings)
                action = f"recover {len(findings)} unit(s) via {path}"
            elif path == "empty_findings":
                rows_empty += 1
                findings = []
                action = "delete (empty findings envelope)"
            elif payload.lstrip().startswith("```"):
                # The payload IS a fence but its JSON is malformed/truncated —
                # nothing recoverable behind it (D2: delete unparseable).
                rows_junk += 1
                junk_ids.append(row["id"])
                findings = []
                action = f"delete (malformed whole-fence payload — path={path})"
            else:
                # Prose that merely contains an inline fence — legitimate
                # single-item content, not envelope pollution. Keep as-is.
                rows_kept += 1
                kept_ids.append(row["id"])
                logger.info("%s [%s] %.60s → keep (prose with inline fence)",
                            row["id"][:8], row["domain"], title or row["body"])
                continue
            logger.info("%s [%s] %.60s → %s", row["id"][:8], row["domain"],
                        title or row["body"], action)

            if not execute:
                continue

            authority = row["source_doc"].removeprefix("intake:")
            for finding in findings:
                await ingest_knowledge_unit(
                    store=store,
                    db=db,
                    content=intake.kb_content_for_finding(finding),
                    project=row["project_type"],
                    domain=row["domain"],
                    authority=authority,
                    provenance={
                        "source_doc": row["source_doc"],
                        "source_pipeline": row["source_pipeline"] or "surplus",
                    },
                    memory_class="fact",
                    confidence=row["confidence"],
                    tags_json=json.dumps([
                        row["domain"], row["project_type"], authority,
                        f"refenced-from:{row['id']}",
                        f"orig-ingested:{row['ingested_at']}",
                    ]),
                )

            # Backup, then delete the polluted original. Order matters: the
            # memory-layer cascade FIRST — store.delete() removes the Qdrant
            # point and its memory_metadata / memory_fts / links /
            # pending_embeddings rows. If the cascade fails, keep the
            # knowledge_units row so the selector re-finds it on a re-run
            # (deleting it first would strand the memory-layer copies with
            # no retry path).
            backup_fh.write(json.dumps(row) + "\n")
            backup_fh.flush()
            if row["qdrant_id"]:
                try:
                    await store.delete(row["qdrant_id"])
                except Exception:
                    logger.error(
                        "store.delete failed for %s (%s) — row kept for re-run",
                        row["id"], row["qdrant_id"], exc_info=True,
                    )
                    failed_ids.append(row["id"])
                    continue
            await knowledge_crud.delete(db, row["id"])
            try:
                from genesis.knowledge.manifest import ManifestManager

                ManifestManager().remove_unit(row["id"])
            except Exception:
                logger.debug("Manifest cleanup skipped for %s", row["id"],
                             exc_info=True)

        logger.info(
            "%s: %d rows → %d recovered rows (%d new units), "
            "%d empty envelopes deleted, %d malformed-fence junk deleted, "
            "%d prose rows kept",
            "EXECUTED" if execute else "DRY-RUN", len(rows), rows_recovered,
            recovered_units, rows_empty, rows_junk, rows_kept,
        )
        if junk_ids:
            logger.info("Deleted-as-junk ids: %s", ", ".join(junk_ids))
        if kept_ids:
            logger.info("Kept prose ids: %s", ", ".join(kept_ids))
        if failed_ids:
            logger.error(
                "%d row(s) failed the memory-layer cascade and were KEPT "
                "for re-run: %s", len(failed_ids), ", ".join(failed_ids),
            )
            return 1
        return 0
    finally:
        if backup_fh is not None:
            backup_fh.close()
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--execute", action="store_true",
                        help="apply changes (default: dry-run report only)")
    parser.add_argument("--limit", type=int, default=None,
                        help="process at most N rows (for a cautious first run)")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(execute=args.execute, limit=args.limit)))
