"""Knowledge ingestion orchestrator — ties processors, distillation, and storage together.

Provides the end-to-end pipeline: source -> processor -> extracted text ->
distillation -> knowledge units -> storage (SQLite + Qdrant).
"""

from __future__ import annotations

import json
import logging
import typing
from dataclasses import dataclass, field
from pathlib import Path

from genesis.knowledge.distillation import MIN_EXTRACTION_RATIO, DistillationPipeline
from genesis.knowledge.manifest import ManifestManager
from genesis.knowledge.processors.base import ProcessedContent
from genesis.knowledge.processors.registry import ContentProcessorRegistry

logger = logging.getLogger(__name__)


@dataclass
class IngestResult:
    """Result of a single source ingestion."""

    source: str
    source_type: str
    units_created: int
    unit_ids: list[str] = field(default_factory=list)
    quality_flags: list[str] = field(default_factory=list)
    error: str | None = None


class KnowledgeOrchestrator:
    """Orchestrate the full knowledge ingestion pipeline."""

    def __init__(
        self,
        *,
        registry: ContentProcessorRegistry,
        distillation: DistillationPipeline,
        manifest: ManifestManager,
    ) -> None:
        self._registry = registry
        self._distillation = distillation
        self._manifest = manifest

    async def ingest_source(
        self,
        source: str,
        *,
        project_type: str,
        domain: str = "auto",
        purpose: list[str] | None = None,
        user_context: str | None = None,
        on_chunk_done: typing.Callable | None = None,
    ) -> IngestResult:
        """Ingest a single source (file path or URL) into the knowledge base."""
        # 1. Check for duplicate
        if self._manifest.has_source(source):
            existing_ids = self._manifest.get_units_for_source(source)
            return IngestResult(
                source=source,
                source_type="cached",
                units_created=0,
                unit_ids=existing_ids,
                quality_flags=["duplicate_source"],
            )

        # 2. Find processor
        processor = self._registry.get_processor(source)
        if processor is None:
            return IngestResult(
                source=source,
                source_type="unknown",
                units_created=0,
                error=f"No processor found for source: {source}",
            )

        # 3. Process
        try:
            content = await processor.process(source)
        except Exception as exc:
            return IngestResult(
                source=source,
                source_type="error",
                units_created=0,
                error=f"Processing failed: {exc}",
            )

        if not content.text.strip():
            return IngestResult(
                source=source,
                source_type=content.source_type,
                units_created=0,
                quality_flags=["empty_content"],
            )

        # 4. Save extracted text to disk
        extracted_path = self._manifest.save_extracted_text(
            source, content.text, content.source_type
        )

        # 5. Optionally save original
        original_path = None
        source_path = Path(source)
        if source_path.exists():
            original_path = self._manifest.save_original(source, source_path)

        # 6. Distill
        units = await self._distillation.distill(
            content, project_type=project_type, domain=domain,
            user_context=user_context,
            on_chunk_done=on_chunk_done,
        )

        if not units:
            self._manifest.add_source(
                source,
                source_type=content.source_type,
                extracted_path=extracted_path,
                original_path=original_path,
            )
            return IngestResult(
                source=source,
                source_type=content.source_type,
                units_created=0,
                quality_flags=["no_units_extracted"],
            )

        # 7. Store each unit
        unit_ids = await self._store_units(
            units, project_type=project_type, source=source,
            content=content, purpose=purpose,
        )

        # 8. Update manifest
        self._manifest.add_source(
            source,
            source_type=content.source_type,
            extracted_path=extracted_path,
            original_path=original_path,
            unit_ids=unit_ids,
        )

        quality_flags = []
        low_conf = [u for u in units if u.confidence < 0.5]
        if low_conf:
            quality_flags.append(f"{len(low_conf)}_low_confidence_units")

        # Flag thin extraction (output much smaller than input)
        ratio = self._distillation.last_extraction_ratio
        if ratio < MIN_EXTRACTION_RATIO and units:
            quality_flags.append("thin_extraction")

        return IngestResult(
            source=source,
            source_type=content.source_type,
            units_created=len(unit_ids),
            unit_ids=unit_ids,
            quality_flags=quality_flags,
        )

    async def ingest_batch(
        self,
        directory: str,
        *,
        project_type: str,
        domain: str = "auto",
        purpose: list[str] | None = None,
        extensions: list[str] | None = None,
    ) -> list[IngestResult]:
        """Batch-ingest all supported files from a directory."""
        dir_path = Path(directory)
        if not dir_path.is_dir():
            return [IngestResult(
                source=directory, source_type="error", units_created=0,
                error=f"Not a directory: {directory}",
            )]

        results: list[IngestResult] = []
        supported = set(extensions) if extensions else set(self._registry.supported_extensions())

        for file_path in sorted(dir_path.rglob("*")):
            if not file_path.is_file():
                continue
            # Skip symlinks to prevent traversal attacks and infinite recursion
            if file_path.is_symlink():
                continue
            if file_path.suffix.lower() not in supported:
                continue

            result = await self.ingest_source(
                str(file_path),
                project_type=project_type,
                domain=domain,
                purpose=purpose,
            )
            results.append(result)

        return results

    async def _store_units(
        self,
        units: list,
        *,
        project_type: str,
        source: str,
        content: ProcessedContent,
        purpose: list[str] | None,
    ) -> list[str]:
        """Store knowledge units via the existing knowledge_ingest MCP internals."""
        # Import the memory module to access the store + CRUD
        import genesis.mcp.memory_mcp as memory_mod

        memory_mod._require_init()
        assert memory_mod._store is not None
        assert memory_mod._db is not None

        import uuid
        from datetime import UTC, datetime

        unit_ids: list[str] = []
        purpose_json = json.dumps(purpose) if purpose else None
        now_iso = datetime.now(UTC).isoformat()
        embedding_model = getattr(
            memory_mod._store._embeddings, "model_name", "unknown"
        )

        for unit in units:
            unit_id = str(uuid.uuid4())

            # Store to Qdrant via MemoryStore
            qdrant_id = await memory_mod._store.store(
                unit.body,
                f"knowledge:{project_type}/{unit.domain}",
                memory_type="knowledge",
                collection="knowledge_base",
                tags=unit.tags + [unit.domain, project_type],
                confidence=unit.confidence,
                auto_link=False,
                source_pipeline="curated",
            )

            # Store to SQLite via CRUD (_commit=False for batch transaction)
            await memory_mod.knowledge.insert(
                memory_mod._db,
                id=unit_id,
                project_type=project_type,
                domain=unit.domain,
                source_doc=source,
                concept=unit.concept,
                body=unit.body,
                relationships=json.dumps(unit.relationships) if unit.relationships else None,
                caveats=json.dumps(unit.caveats) if unit.caveats else None,
                tags=json.dumps(unit.tags) if unit.tags else None,
                confidence=unit.confidence,
                ingested_at=now_iso,
                qdrant_id=qdrant_id,
                section_title=unit.section_title,
                source_date=unit.source_date,
                embedding_model=embedding_model,
                source_pipeline="curated",
                purpose=purpose_json,
                ingestion_source=source,
                _commit=False,
            )

            unit_ids.append(unit_id)

        # Single commit for all units in the batch
        await memory_mod._db.commit()
        logger.info("Stored %d knowledge units from %s", len(unit_ids), source)
        return unit_ids
