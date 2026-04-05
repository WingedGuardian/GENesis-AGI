#!/usr/bin/env python3
"""UserPromptSubmit hook: proactive memory surfacing.

Searches the memory system for memories relevant to the user's current
message and injects them as context. Uses tiered retrieval:
1. FTS5 keyword search (always, ~5ms) — covers both episodic and knowledge
2. Qdrant vector search on episodic_memory (~400-500ms, falls back gracefully)
3. RRF fusion across both sources

After the routing fix, ALL internal memory lives in episodic_memory.
knowledge_base is reserved for external domain data from modules.

Budget: <1.5s total. FTS5-only if Ollama unavailable or slow.

Reads hook input from stdin as JSON:
  {"session_id": "...", "prompt": "...", ...}
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sqlite3
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Load secrets.env so embedding API keys (DeepInfra, Qwen) and OLLAMA_URL
# are available. CC subprocesses don't inherit these from the shell — they're
# only loaded by the Genesis runtime (bridge/AZ), not by Claude Code.
from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_DIR / "secrets.env")

# Skip in dispatched sessions (bridge, reflection, surplus)
if os.environ.get("GENESIS_CC_SESSION") == "1":
    sys.exit(0)


def _genesis_db_path() -> Path:
    return importlib.import_module("genesis.env").genesis_db_path()


def _qdrant_url() -> str:
    return importlib.import_module("genesis.env").qdrant_url()


_DB_PATH = _genesis_db_path()
_QDRANT_URL = _qdrant_url()
_QDRANT_COLLECTION = "episodic_memory"
# Total budget for embedding in a UserPromptSubmit hook. Cloud-first chain
# means typical latency is ~500ms (DeepInfra GPU). Budget of 3s is generous
# enough for one cloud round-trip plus retries, but bounded to prevent hangs
# when all backends are down.
_EMBED_TIMEOUT_S = 3.0
_MAX_RESULTS = 3
_MIN_PROMPT_WORDS = 1  # Stop words already filter greetings to 0 keywords
_METRICS_PATH = Path.home() / ".genesis" / "proactive_metrics.json"

# Common English stop words to filter from search queries
_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "must", "need",
    "i", "me", "my", "we", "our", "you", "your", "he", "she", "it",
    "they", "them", "their", "this", "that", "these", "those",
    "what", "which", "who", "whom", "where", "when", "why", "how",
    "not", "no", "nor", "but", "or", "and", "so", "if", "then",
    "than", "too", "very", "just", "about", "also", "of", "in",
    "on", "at", "to", "for", "with", "from", "by", "as", "into",
    "through", "during", "before", "after", "above", "below",
    "up", "down", "out", "off", "over", "under", "again",
    "let", "lets", "let's", "please", "ok", "okay", "yeah", "yes",
    "hey", "hi", "hello", "thanks", "thank",
    # Conversational filler that dilutes FTS5 queries
    "now", "deal", "set", "get", "got", "put", "make", "made",
    "thing", "things", "stuff", "like", "want", "know", "think",
    "look", "right", "well", "going", "really", "actually",
    "already", "still", "here", "there", "start", "something",
    "kind", "sort", "sure", "guess", "maybe", "basically",
    "pretty", "anyway", "gonna", "wanna", "gotta",
    "more", "both", "generally", "specifically", "topics", "topic",
    "way", "take", "give", "come", "talk", "tell", "said",
    "use", "using", "used", "try", "first", "last",
    "new", "old", "big", "little", "much", "many", "few",
    "whole", "part", "point", "matter",
})


def _is_garbage(content: str) -> bool:
    """Filter out content that should never surface as proactive memory."""
    if "<external-content" in content:
        return True
    stripped = content.lstrip()
    if stripped.startswith("{") and any(
        k in stripped[:100] for k in ('"drift_detected"', '"tags"', '"type":', '"operation"')
    ):
        return True  # Raw JSON observation blob
    return stripped.startswith("---\n") and "type:" in stripped[:200]


def _extract_keywords(prompt: str) -> list[str]:
    """Extract significant keywords from user prompt."""
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in prompt)
    words = cleaned.lower().split()
    keywords = [w for w in words if w not in _STOP_WORDS and len(w) >= 3]
    return keywords[:8]


def _keywords_from_files(file_paths: list[str]) -> list[str]:
    """Decompose file paths into searchable keywords.

    E.g., 'src/genesis/memory/store.py' → ['genesis', 'memory', 'store']
    """
    keywords: list[str] = []
    for fp in file_paths[:5]:  # Top 5 most recent
        # Strip common prefixes and extensions
        path = fp.replace("${HOME}/genesis/", "")
        parts = path.replace("/", " ").replace("_", " ").replace(".", " ").split()
        for part in parts:
            part = part.lower()
            if part not in _STOP_WORDS and len(part) >= 3 and part not in ("src", "py", "md", "txt", "json", "yaml", "tests", "test"):
                keywords.append(part)
    # Deduplicate preserving order
    seen: set[str] = set()
    result: list[str] = []
    for k in keywords:
        if k not in seen:
            seen.add(k)
            result.append(k)
    return result[:6]


def _load_recent_files(session_id: str) -> list[str]:
    """Load recently-touched files for this session from PostToolUse state."""
    if not session_id or "/" in session_id or ".." in session_id:
        return []
    state_file = Path(os.path.expanduser("~/.genesis/sessions")) / session_id / "recent_files.json"
    if not state_file.exists():
        return []
    try:
        data = json.loads(state_file.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _escape_fts5(query: str) -> str:
    """Escape special FTS5 characters."""
    return "".join(c if c.isalnum() or c.isspace() else " " for c in query)


def _search_fts5(db_path: Path, keywords: list[str]) -> list[dict]:
    """Search memory_fts using FTS5 with OR-joined keywords."""
    if not keywords:
        return []

    fts_query = " OR ".join('"' + _escape_fts5(k) + '"' for k in keywords)

    try:
        conn = sqlite3.connect(str(db_path), timeout=2)
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT memory_id, content, source_type, collection, rank
                FROM memory_fts
                WHERE memory_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (fts_query, _MAX_RESULTS * 2),
            )
            rows = [dict(row) for row in cursor.fetchall()]
            # Require minimum keyword overlap for multi-keyword queries
            if len(keywords) >= 3:
                def _keyword_overlap(content: str, kws: list[str]) -> int:
                    content_lower = content.lower()
                    return sum(1 for k in kws if k in content_lower)
                rows = [r for r in rows if _keyword_overlap(r.get("content", ""), keywords) >= 2]
            return rows
        finally:
            conn.close()
    except Exception as exc:
        print(f"FTS5 search error: {exc}", file=sys.stderr)
        return []


async def _embed_text(text: str) -> list[float] | None:
    """Get embedding from the configured backend chain (cloud-first).

    Returns None if all backends fail or the total budget is exceeded.
    Budget exists because this runs in a UserPromptSubmit hook that blocks
    the conversation — can't let 3 × 30s backend timeouts stack up.

    Backend order is cloud-first (DeepInfra → DashScope → Ollama) for
    latency: cloud GPU ~500ms vs Ollama CPU ~1500ms. This differs from the
    storage path (Ollama → cloud) which optimizes for cost.
    """
    try:
        mod = importlib.import_module("genesis.memory.embeddings")
        EmbeddingProvider = mod.EmbeddingProvider  # noqa: N806
        DeepInfraBackend = mod.DeepInfraBackend  # noqa: N806
        DashScopeBackend = mod.DashScopeBackend  # noqa: N806
        OllamaBackend = mod.OllamaBackend  # noqa: N806

        # Build cloud-first chain for recall (latency-optimized)
        backends: list = []
        di_key = os.environ.get("API_KEY_DEEPINFRA", "").strip()
        if di_key:
            backends.append(DeepInfraBackend(api_key=di_key))
        ds_key = os.environ.get("API_KEY_QWEN", "").strip()
        if ds_key:
            backends.append(DashScopeBackend(api_key=ds_key))
        # Ollama last — slow CPU inference, but free
        env_mod = importlib.import_module("genesis.env")
        if env_mod.ollama_enabled():
            model = os.environ.get(
                "OLLAMA_EMBEDDING_MODEL", "qwen3-embedding:0.6b-fp16",
            )
            backends.append(OllamaBackend(url=env_mod.ollama_url(), model=model))

        if not backends:
            # No backends configured — fall back to default chain
            provider = EmbeddingProvider(cache_dir=None)
        else:
            provider = EmbeddingProvider(backends=backends, cache_dir=None)

        return await asyncio.wait_for(provider.embed(text), timeout=_EMBED_TIMEOUT_S)
    except TimeoutError:
        print(
            f"Embedding exceeded {_EMBED_TIMEOUT_S}s budget (all backends slow/down)",
            file=sys.stderr,
        )
    except Exception as exc:
        print(f"Embedding error: {exc}", file=sys.stderr)
    return None


async def _search_qdrant(vector: list[float]) -> list[dict]:
    """Search episodic_memory for similar memories."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.post(
                f"{_QDRANT_URL}/collections/{_QDRANT_COLLECTION}/points/search",
                json={
                    "vector": vector,
                    "limit": _MAX_RESULTS * 2,
                    "with_payload": True,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                results = []
                for hit in data.get("result", []):
                    payload = hit.get("payload", {})
                    results.append({
                        "memory_id": str(hit.get("id", "")),
                        "content": payload.get("content", ""),
                        "score": hit.get("score", 0.0),
                        "source_session_id": payload.get("source_session_id"),
                        "memory_class": payload.get("memory_class", "fact"),
                        "_retrieved_count": payload.get("retrieved_count", 0),
                    })
                return results
    except Exception as exc:
        print(f"Qdrant search error: {exc}", file=sys.stderr)
    return []


def _rrf_fusion(
    fts_results: list[dict],
    vector_results: list[dict],
    k: int = 60,
) -> list[dict]:
    """Reciprocal Rank Fusion of FTS5 and vector results."""
    scores: dict[str, float] = {}
    content_map: dict[str, dict] = {}

    for rank, r in enumerate(fts_results):
        mid = r.get("memory_id", "")
        if not mid:
            continue
        scores[mid] = scores.get(mid, 0.0) + 1.0 / (k + rank + 1)
        content_map[mid] = r

    for rank, r in enumerate(vector_results):
        mid = r.get("memory_id", "")
        if not mid:
            continue
        scores[mid] = scores.get(mid, 0.0) + 1.0 / (k + rank + 1)
        if mid not in content_map:
            content_map[mid] = r

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [content_map[mid] for mid, _ in ranked[:_MAX_RESULTS] if mid in content_map]


def _format_results(results: list[dict]) -> str:
    """Format surfaced memories for injection.

    Two-tier output: rank 1 and rules always get full content (200 chars).
    Rank 2+ non-rules get compact format (80 chars) to reduce noise.
    """
    if not results:
        return ""

    lines = []
    for rank, r in enumerate(results):
        is_rule = r.get("memory_class") == "rule"
        # Full content for rank 1 or rules; compact for lower-ranked non-rules
        max_len = 200 if (rank == 0 or is_rule) else 80
        content = r.get("content", "")[:max_len]
        session_id = r.get("source_session_id", "")
        session_hint = f" (session: {session_id[:8]})" if session_id else ""
        lines.append(f"[Memory] {content}{session_hint}")

    # Remind the session about deeper search options beyond this hook
    lines.append(
        "[Memory] Need more? Use `memory_recall` MCP (semantic search) "
        "or query `cc_sessions` in SQLite. Grep transcripts is last resort."
    )

    return "\n".join(lines)


async def _increment_retrieved(results: list[dict]) -> None:
    """Increment retrieved_count in Qdrant for surfaced memories.

    Fire-and-forget after output. Mirrors retrieval.py:178-189.
    """
    try:
        import httpx
        async with httpx.AsyncClient(timeout=1.0) as client:
            for r in results:
                mid = r.get("memory_id", "")
                old_count = r.get("_retrieved_count", 0)
                if not mid:
                    continue
                try:
                    await client.post(
                        f"{_QDRANT_URL}/collections/{_QDRANT_COLLECTION}/points/payload",
                        json={
                            "payload": {"retrieved_count": old_count + 1},
                            "points": [mid],
                        },
                    )
                except Exception as inner_exc:
                    print(
                        f"retrieved_count update failed for {mid}: {inner_exc}",
                        file=sys.stderr,
                    )
    except Exception as exc:
        print(f"Increment retrieved_count error: {exc}", file=sys.stderr)


def _ensure_knowledge_retrieved_count(db_path: Path) -> None:
    """Self-healing migration: add retrieved_count to knowledge_units if missing."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=2)
        try:
            conn.execute(
                "ALTER TABLE knowledge_units "
                "ADD COLUMN retrieved_count INTEGER NOT NULL DEFAULT 0"
            )
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists
        finally:
            conn.close()
    except Exception:
        pass  # Never block


def _record_activity(db_path: Path, latency_ms: float, success: bool) -> None:
    """Write to activity_log — picked up by ProviderActivityTracker."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=1)
        try:
            conn.execute(
                "INSERT INTO activity_log (provider, latency_ms, success, cache_hit)"
                " VALUES (?, ?, ?, 0)",
                ("proactive_memory", latency_ms, int(success)),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass  # Never block user prompt


def _record_detail(
    fts_count: int,
    vector_count: int,
    fused_count: int,
    embed_latency_ms: float | None,
    total_latency_ms: float,
    fts_only_fallback: bool,
) -> None:
    """Atomic JSON write — latest invocation detail for health dashboard."""
    data = {
        "timestamp": datetime.now(UTC).isoformat(),
        "fts_results": fts_count,
        "vector_results": vector_count,
        "fused_results": fused_count,
        "embed_latency_ms": embed_latency_ms,
        "total_latency_ms": total_latency_ms,
        "fts_only_fallback": fts_only_fallback,
    }
    try:
        _METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(_METRICS_PATH.parent), suffix=".tmp")
        try:
            os.write(fd, json.dumps(data).encode())
        finally:
            os.close(fd)
        os.replace(tmp, str(_METRICS_PATH))
    except Exception:
        pass  # Never block user prompt


async def _run(prompt: str, session_id: str = "") -> None:
    """Main async entry point."""
    start = time.monotonic()

    keywords = _extract_keywords(prompt)

    # Augment keywords with file-context from PostToolUse tracking
    recent_files = _load_recent_files(session_id)
    if recent_files:
        file_keywords = _keywords_from_files(recent_files)
        # Append file keywords after prompt keywords (lower priority in FTS5)
        keywords = keywords + [k for k in file_keywords if k not in keywords]

    if len(keywords) < _MIN_PROMPT_WORDS:
        return

    if not _DB_PATH.exists():
        return

    # Self-heal: ensure knowledge_units has retrieved_count column (once)
    _SENTINEL = Path.home() / ".genesis" / ".knowledge_retrieved_count_migrated"
    if not _SENTINEL.exists():
        _ensure_knowledge_retrieved_count(_DB_PATH)
        _SENTINEL.parent.mkdir(parents=True, exist_ok=True)
        _SENTINEL.touch(exist_ok=True)

    # FTS5 search (synchronous, fast) — covers both episodic and knowledge
    fts_results = _search_fts5(_DB_PATH, keywords)

    # Vector search (async, may timeout)
    vector_results: list[dict] = []
    embed_latency_ms: float | None = None
    embed_start = time.monotonic()
    vector = await _embed_text(prompt)
    embed_latency_ms = (time.monotonic() - embed_start) * 1000
    if vector:
        vector_results = await _search_qdrant(vector)

    fts_only_fallback = len(vector_results) == 0 and len(fts_results) > 0

    # Fuse results
    fused: list[dict] = []
    fused_count = 0
    if fts_results or vector_results:
        fused = _rrf_fusion(fts_results, vector_results)
        fused = [r for r in fused if not _is_garbage(r.get("content", ""))]
        fused_count = len(fused)
        output = _format_results(fused)
        if output:
            print(output)
            sys.stdout.flush()

    # Post-output: increment retrieved_count (fire-and-forget, after flush)
    if fused:
        await _increment_retrieved(fused)

    total_ms = (time.monotonic() - start) * 1000

    had_results = fused_count > 0
    _record_activity(_DB_PATH, total_ms, success=had_results)
    _record_detail(
        fts_count=len(fts_results),
        vector_count=len(vector_results),
        fused_count=fused_count,
        embed_latency_ms=embed_latency_ms,
        total_latency_ms=total_ms,
        fts_only_fallback=fts_only_fallback,
    )


def main() -> None:
    """Hook entry point."""
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return
        data = json.loads(raw)
        prompt = data.get("prompt", "")
        if not prompt:
            return

        session_id = data.get("session_id", "")
        asyncio.run(_run(prompt, session_id=session_id))
    except Exception:
        # Hooks must never crash — log to stderr for debugging
        import traceback
        print(traceback.format_exc(), file=sys.stderr)


if __name__ == "__main__":
    main()
