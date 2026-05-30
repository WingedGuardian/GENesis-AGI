"""MCP tools for PageIndex tree-based document querying.

Provides document_index, document_query, and document_delete tools
on the genesis-memory FastMCP server.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..memory import mcp

logger = logging.getLogger(__name__)


_tree_client_cache: object | None = None
_tree_client_checked = False


def _get_tree_client():
    """Lazy-create and cache a TreeIndexClient. Returns None if not configured."""
    global _tree_client_cache, _tree_client_checked
    if not _tree_client_checked:
        from genesis.knowledge.tree_index import get_client

        _tree_client_cache = get_client()
        _tree_client_checked = True
    return _tree_client_cache


@mcp.tool()
async def document_index(path: str) -> dict:
    """Upload a document to PageIndex for tree-indexed querying.

    Builds a hierarchical tree index that enables structure-aware
    retrieval — better than chunk+embed for long, structured documents.

    Returns the doc_id and a summary of the tree structure.
    Use document_query to ask questions about the indexed document.

    For large PDFs, processing may take 1-3 minutes.

    Args:
        path: Path to the PDF file.
    """
    from genesis.knowledge.tree_index import load_tree_index, save_tree_index

    resolved = Path(path).expanduser().resolve()
    if not resolved.exists():
        return {"error": f"File not found: {path}"}
    if resolved.suffix.lower() != ".pdf":
        return {"error": "Only PDF files are supported for tree indexing"}

    # Check cache first
    cached = load_tree_index(str(resolved))
    if cached:
        return {
            "doc_id": cached["doc_id"],
            "source": cached["source"],
            "indexed_at": cached["indexed_at"],
            "tree_summary": _summarize_tree(cached["tree"]),
            "status": "cached",
        }

    client = _get_tree_client()
    if client is None:
        return {"error": "PageIndex not configured — set API_KEY_PAGEINDEX"}

    try:
        doc_id = await client.upload_document(str(resolved))
        tree = await client.get_tree(doc_id)
        save_tree_index(str(resolved), doc_id, tree)
        return {
            "doc_id": doc_id,
            "source": str(resolved),
            "tree_summary": _summarize_tree(tree),
            "status": "indexed",
        }
    except Exception as exc:
        logger.error("document_index failed for %s: %s", path, exc)
        return {"error": str(exc)}


@mcp.tool()
async def document_query(
    question: str,
    path_or_doc_id: str,
) -> dict:
    """Query a document using PageIndex tree-based retrieval.

    Provide either a file path (auto-indexes if needed) or a doc_id
    from a previous document_index call. Returns an answer with
    page citations.

    Args:
        question: The question to ask about the document.
        path_or_doc_id: A file path or a PageIndex doc_id (starts with 'pi-').
    """
    from genesis.knowledge.tree_index import load_tree_index, save_tree_index

    client = _get_tree_client()
    if client is None:
        return {"error": "PageIndex not configured — set API_KEY_PAGEINDEX"}

    # Determine doc_id
    doc_id: str | None = None
    if path_or_doc_id.startswith("pi-"):
        doc_id = path_or_doc_id
    else:
        resolved = Path(path_or_doc_id).expanduser().resolve()
        cached = load_tree_index(str(resolved))
        if cached:
            doc_id = cached["doc_id"]
        elif resolved.exists():
            if resolved.suffix.lower() != ".pdf":
                return {"error": "Only PDF files are supported for tree indexing"}
            # Auto-index
            try:
                doc_id = await client.upload_document(str(resolved))
                tree = await client.get_tree(doc_id)
                save_tree_index(str(resolved), doc_id, tree)
            except Exception as exc:
                return {"error": f"Auto-indexing failed: {exc}"}
        else:
            return {"error": f"Not a valid doc_id or file path: {path_or_doc_id}"}

    try:
        response = await client.query_document(doc_id, question)
        choices = response.get("choices", [])
        answer = choices[0]["message"]["content"] if choices else ""
        return {
            "answer": answer,
            "citations": response.get("citations", []),
            "doc_id": doc_id,
            "usage": response.get("usage"),
        }
    except Exception as exc:
        # If doc_id is stale, the error will surface here
        logger.error("document_query failed for %s: %s", doc_id, exc)
        return {"error": str(exc), "doc_id": doc_id}


@mcp.tool()
async def document_delete(doc_id: str) -> dict:
    """Delete a document from PageIndex and remove local cache.

    Use after finishing analysis of a temporarily indexed document.

    Args:
        doc_id: The PageIndex document ID to delete.
    """
    from genesis.knowledge.tree_index import (
        _INDICES_DIR,
    )

    client = _get_tree_client()
    if client is None:
        return {"error": "PageIndex not configured — set API_KEY_PAGEINDEX"}

    # Delete from cloud
    try:
        await client.delete_document(doc_id)
    except Exception as exc:
        logger.warning("Cloud delete failed for %s: %s", doc_id, exc)

    # Remove local cache matching this doc_id
    removed = False
    if _INDICES_DIR.exists():
        for f in _INDICES_DIR.glob("*.json"):
            try:
                import json

                data = json.loads(f.read_text())
                if data.get("doc_id") == doc_id:
                    f.unlink()
                    removed = True
                    break
            except Exception:
                continue

    return {
        "deleted": True,
        "doc_id": doc_id,
        "local_cache_removed": removed,
    }


def _summarize_tree(tree: dict) -> str:
    """Produce a concise summary of a tree structure."""
    result_nodes = tree.get("result", [])
    if not result_nodes:
        return "Empty tree"

    sections = []
    for node in result_nodes:
        title = node.get("title", "Untitled")
        page = node.get("page_index", "?")
        children = node.get("nodes", [])
        child_count = len(children)
        if child_count:
            sections.append(f"  {title} (p.{page}, {child_count} subsections)")
        else:
            sections.append(f"  {title} (p.{page})")

    return f"{len(result_nodes)} top-level sections:\n" + "\n".join(sections)
