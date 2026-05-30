"""PageIndex cloud API wrapper for tree-based document indexing.

Provides async wrappers around the synchronous PageIndex SDK for
structured document retrieval. Documents are uploaded to PageIndex's
cloud, which builds a hierarchical tree index for LLM-based navigation.

Usage:
    client = get_client()
    if client:
        doc_id = await client.upload_document("/path/to/file.pdf")
        tree = await client.get_tree(doc_id)
        answer = await client.query_document(doc_id, "What are the risk factors?")
        await client.delete_document(doc_id)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_INDICES_DIR = Path.home() / ".genesis" / "knowledge" / "indices"


class TreeIndexClient:
    """Async wrapper around the synchronous PageIndex SDK."""

    def __init__(self, api_key: str | None = None) -> None:
        key = api_key or os.environ.get("API_KEY_PAGEINDEX")
        if not key:
            raise RuntimeError(
                "PageIndex API key required — set API_KEY_PAGEINDEX in secrets.env"
            )
        from pageindex import PageIndexClient

        self._client = PageIndexClient(api_key=key)

    async def upload_document(
        self,
        path: str,
        *,
        timeout_seconds: int = 300,
    ) -> str:
        """Upload a PDF and poll until processing completes.

        Returns the doc_id on success.
        Raises TimeoutError if processing doesn't complete within timeout.
        Raises RuntimeError if PageIndex reports processing failure.
        """
        result = await asyncio.to_thread(self._client.submit_document, path)
        doc_id = result.get("doc_id") or result.get("id")
        if not doc_id:
            raise RuntimeError(f"No doc_id in submit response: {result}")

        # Poll with exponential backoff
        delays = [2, 4, 8, 16, 30]
        elapsed = 0
        attempt = 0

        while elapsed < timeout_seconds:
            delay = delays[min(attempt, len(delays) - 1)]
            await asyncio.sleep(delay)
            elapsed += delay
            attempt += 1

            status = await asyncio.to_thread(self._client.get_document, doc_id)
            state = status.get("status", "unknown")

            if state == "completed":
                logger.info(
                    "PageIndex document %s ready (%d pages, %ds)",
                    doc_id,
                    status.get("pageNum", 0),
                    elapsed,
                )
                return doc_id

            if state == "failed":
                raise RuntimeError(
                    f"PageIndex processing failed for {path}: {status}"
                )

        raise TimeoutError(
            f"PageIndex processing timed out after {timeout_seconds}s for {path}"
        )

    async def get_tree(self, doc_id: str) -> dict:
        """Fetch the hierarchical tree index for a document."""
        return await asyncio.to_thread(
            self._client.get_tree, doc_id, node_summary=True
        )

    async def query_document(self, doc_id: str, question: str) -> dict:
        """Query a document using PageIndex chat completions.

        Returns the full response dict with 'choices' and 'citations'.
        """
        return await asyncio.to_thread(
            self._client.chat_completions,
            messages=[{"role": "user", "content": question}],
            doc_id=doc_id,
            enable_citations=True,
        )

    async def delete_document(self, doc_id: str) -> None:
        """Delete a document from PageIndex cloud."""
        await asyncio.to_thread(self._client.delete_document, doc_id)
        logger.info("Deleted PageIndex document %s", doc_id)


def get_client() -> TreeIndexClient | None:
    """Factory that returns None if PageIndex is not configured.

    Safe to call unconditionally — returns None if:
    - API_KEY_PAGEINDEX not set
    - pageindex SDK not installed
    """
    try:
        api_key = os.environ.get("API_KEY_PAGEINDEX")
        if not api_key:
            return None
        return TreeIndexClient(api_key=api_key)
    except Exception:
        logger.debug("PageIndex client not available", exc_info=True)
        return None


def _source_hash(source: str) -> str:
    """Deterministic hash for a source path."""
    return hashlib.sha256(source.encode()).hexdigest()[:16]


def save_tree_index(source: str, doc_id: str, tree: dict) -> Path:
    """Persist a tree index to disk. Returns the file path."""
    _INDICES_DIR.mkdir(parents=True, exist_ok=True)
    h = _source_hash(source)
    path = _INDICES_DIR / f"{h}.json"
    data = {
        "source": source,
        "doc_id": doc_id,
        "tree": tree,
        "indexed_at": datetime.now(UTC).isoformat(),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(path)
    return path


def load_tree_index(source: str) -> dict | None:
    """Load a cached tree index from disk. Returns None if not found."""
    h = _source_hash(source)
    path = _INDICES_DIR / f"{h}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("Corrupt tree index at %s", path)
        return None


def delete_tree_index(source: str) -> None:
    """Remove a cached tree index from disk."""
    h = _source_hash(source)
    path = _INDICES_DIR / f"{h}.json"
    if path.exists():
        path.unlink()
