"""On-disk manifest for knowledge source provenance tracking.

Maintains a mapping from ingested sources to their extracted text files
and knowledge unit IDs. Enables re-distillation, audit trails, and
source deduplication.

Directory structure:
    ~/.genesis/knowledge/
    ├── sources/     — extracted text as markdown (always kept)
    ├── originals/   — original source files (text-based, <10MB only)
    └── manifest.json
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_KNOWLEDGE_ROOT = Path.home() / ".genesis" / "knowledge"
_SOURCES_DIR = _KNOWLEDGE_ROOT / "sources"
_ORIGINALS_DIR = _KNOWLEDGE_ROOT / "originals"
_MANIFEST_PATH = _KNOWLEDGE_ROOT / "manifest.json"

# Only keep originals for text-based files under this size
_MAX_ORIGINAL_SIZE = 10 * 1024 * 1024  # 10MB
_TEXT_EXTENSIONS = {".txt", ".md", ".rst", ".csv", ".json", ".yaml", ".yml", ".toml", ".html"}


class ManifestManager:
    """Manages the knowledge source manifest and on-disk storage."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or _KNOWLEDGE_ROOT
        self.sources_dir = self.root / "sources"
        self.originals_dir = self.root / "originals"
        self.manifest_path = self.root / "manifest.json"
        self._manifest: dict[str, dict] | None = None

    def ensure_dirs(self) -> None:
        """Create directory structure on first use."""
        self.sources_dir.mkdir(parents=True, exist_ok=True)
        self.originals_dir.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, dict]:
        if self._manifest is not None:
            return self._manifest
        if self.manifest_path.exists():
            try:
                self._manifest = json.loads(self.manifest_path.read_text())
            except (json.JSONDecodeError, OSError):
                logger.warning("Corrupt manifest.json — starting fresh")
                self._manifest = {}
        else:
            self._manifest = {}
        return self._manifest

    def _save(self) -> None:
        manifest = self._load()
        self.ensure_dirs()
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest, indent=2, default=str))
        tmp.replace(self.manifest_path)

    @staticmethod
    def source_hash(source: str) -> str:
        """Deterministic hash for a source path or URL."""
        return hashlib.sha256(source.encode()).hexdigest()[:16]

    def has_source(self, source: str) -> bool:
        """Check if a source has already been ingested."""
        return self.source_hash(source) in self._load()

    def save_extracted_text(self, source: str, text: str, source_type: str) -> Path:
        """Save extracted text to sources/ directory. Returns the file path."""
        self.ensure_dirs()
        h = self.source_hash(source)
        filename = f"{h}.md"
        path = self.sources_dir / filename
        path.write_text(text)
        return path

    def save_original(self, source: str, source_path: Path) -> Path | None:
        """Copy original source to originals/ if text-based and small enough.

        Returns the copied path, or None if skipped.
        """
        if not source_path.exists():
            return None
        if source_path.stat().st_size > _MAX_ORIGINAL_SIZE:
            return None
        if source_path.suffix.lower() not in _TEXT_EXTENSIONS:
            return None
        self.ensure_dirs()
        h = self.source_hash(source)
        dest = self.originals_dir / f"{h}{source_path.suffix}"
        dest.write_bytes(source_path.read_bytes())
        return dest

    def add_source(
        self,
        source: str,
        *,
        source_type: str,
        extracted_path: Path,
        original_path: Path | None = None,
        unit_ids: list[str] | None = None,
    ) -> None:
        """Register a source in the manifest."""
        manifest = self._load()
        h = self.source_hash(source)
        manifest[h] = {
            "source": source,
            "source_type": source_type,
            "extracted_path": str(extracted_path),
            "original_path": str(original_path) if original_path else None,
            "unit_ids": unit_ids or [],
            "ingested_at": datetime.now(UTC).isoformat(),
        }
        self._save()

    def add_unit_ids(self, source: str, unit_ids: list[str]) -> None:
        """Append unit IDs to an existing source entry."""
        manifest = self._load()
        h = self.source_hash(source)
        if h in manifest:
            existing = manifest[h].get("unit_ids", [])
            manifest[h]["unit_ids"] = existing + unit_ids
            self._save()

    def get_units_for_source(self, source: str) -> list[str]:
        """Get all knowledge unit IDs produced from a source."""
        manifest = self._load()
        h = self.source_hash(source)
        entry = manifest.get(h)
        return entry.get("unit_ids", []) if entry else []

    def list_sources(self) -> list[dict]:
        """List all ingested sources with metadata."""
        manifest = self._load()
        return [
            {"hash": h, **entry}
            for h, entry in manifest.items()
        ]
