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


def _search_code_index(db_path: Path, keywords: list[str]) -> list[dict]:
    """Search code_modules/code_symbols for relevant code entities.

    Returns results in memory-like format for RRF fusion. ~5ms (SQLite).
    Gracefully returns [] if code index tables don't exist yet.
    """
    if not keywords:
        return []

    try:
        conn = sqlite3.connect(str(db_path), timeout=2)
        try:
            conn.row_factory = sqlite3.Row
            # Search symbols by name match (exact prefix or contains)
            # Escape SQL LIKE special chars in user-derived keywords
            placeholders = " OR ".join(["name LIKE ? ESCAPE '\\'"] * len(keywords))
            params = [f"%{k.replace('%', '\\%').replace('_', '\\_')}%" for k in keywords]
            cursor = conn.execute(
                f"""
                SELECT cs.name, cs.symbol_type, cs.signature, cs.module_path,
                       cs.parent_class, cm.package
                FROM code_symbols cs
                JOIN code_modules cm ON cs.module_path = cm.path
                WHERE ({placeholders}) AND cs.is_public = 1
                ORDER BY cs.line_start
                LIMIT 6
                """,
                params,
            )
            rows = cursor.fetchall()
            if not rows:
                return []

            results = []
            for row in rows:
                # Format as memory-like content for fusion
                sig = row["signature"] or f"{row['symbol_type']} {row['name']}"
                loc = row["module_path"]
                if row["parent_class"]:
                    loc = f"{row['module_path']}:{row['parent_class']}"
                content = f"[Code] {sig} — {loc}"
                results.append({
                    "memory_id": f"code:{row['module_path']}:{row['name']}",
                    "content": content,
                    "source_type": "code_index",
                    "memory_class": "fact",
                })
            return results
        finally:
            conn.close()
    except sqlite3.OperationalError:
        # Table doesn't exist yet — index hasn't run
        return []
    except Exception as exc:
        print(f"Code index search error: {exc}", file=sys.stderr)
        return []


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


async def _search_qdrant(vector: list[float], wing_filter: str | None = None) -> list[dict]:
    """Search episodic_memory for similar memories.

    Args:
        vector: Embedding vector to search with.
        wing_filter: Optional wing to filter results (e.g., "memory", "routing").
    """
    try:
        import httpx
        body: dict = {
            "vector": vector,
            "limit": _MAX_RESULTS * 2,
            "with_payload": True,
        }
        if wing_filter:
            body["filter"] = {
                "must": [{"key": "wing", "match": {"value": wing_filter}}]
            }

        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.post(
                f"{_QDRANT_URL}/collections/{_QDRANT_COLLECTION}/points/search",
                json=body,
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
                        "_wing": payload.get("wing"),
                    })
                return results
    except Exception as exc:
        print(f"Qdrant search error: {exc}", file=sys.stderr)
    return []


def _rrf_fusion(
    fts_results: list[dict],
    vector_results: list[dict],
    wing_results: list[dict] | None = None,
    code_results: list[dict] | None = None,
    k: int = 60,
) -> list[dict]:
    """Reciprocal Rank Fusion of FTS5, vector, wing-filtered, and code index results.

    Wing-filtered results get a 1.5x bonus to prioritize domain-relevant
    content without exclusively filtering (cross-domain results still surface).
    Code index results get a 0.5x weight (supplementary, not primary).
    """
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

    # Wing-filtered results get 1.5x RRF bonus (domain-relevant boost)
    if wing_results:
        _WING_BOOST = 1.5
        for rank, r in enumerate(wing_results):
            mid = r.get("memory_id", "")
            if not mid:
                continue
            scores[mid] = scores.get(mid, 0.0) + _WING_BOOST / (k + rank + 1)
            if mid not in content_map:
                content_map[mid] = r

    # Code index results — supplementary signal (0.5x weight)
    if code_results:
        _CODE_WEIGHT = 0.5
        for rank, r in enumerate(code_results):
            mid = r.get("memory_id", "")
            if not mid:
                continue
            scores[mid] = scores.get(mid, 0.0) + _CODE_WEIGHT / (k + rank + 1)
            if mid not in content_map:
                content_map[mid] = r


    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [content_map[mid] for mid, _ in ranked[:_MAX_RESULTS] if mid in content_map]


def _format_age(iso_str: str) -> str:
    """Format ISO datetime as human-readable age (e.g., '3d', '2w', '4mo')."""
    try:
        dt = datetime.fromisoformat(iso_str)
        delta = datetime.now(UTC) - dt
        days = delta.days
        if days < 1:
            return "<1d"
        if days < 7:
            return f"{days}d"
        if days < 30:
            return f"{days // 7}w"
        if days < 365:
            return f"{days // 30}mo"
        return f"{days // 365}y"
    except (ValueError, TypeError):
        return "?"


def _enrich_with_metadata(results: list[dict]) -> None:
    """Backfill created_at and wing from SQLite memory_metadata.

    After RRF fusion, results may come from FTS5 (which lacks wing/created_at)
    or Qdrant (which has wing but not created_at in the extracted fields).
    This single batch query fills gaps uniformly for all sources.
    """
    ids = [
        r.get("memory_id", "")
        for r in results
        if r.get("memory_id", "") and not r["memory_id"].startswith("code:")
    ]
    if not ids:
        return
    try:
        conn = sqlite3.connect(str(_DB_PATH), timeout=2)
        try:
            conn.row_factory = sqlite3.Row
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT memory_id, created_at, wing FROM memory_metadata"  # noqa: S608
                f" WHERE memory_id IN ({placeholders})",
                ids,
            ).fetchall()
            meta = {row["memory_id"]: dict(row) for row in rows}
            for r in results:
                mid = r.get("memory_id", "")
                if mid in meta:
                    r.setdefault("_created_at", meta[mid].get("created_at"))
                    if not r.get("_wing"):
                        r["_wing"] = meta[mid].get("wing")
        finally:
            conn.close()
    except Exception:
        pass  # Best-effort enrichment — never block the hook


def _format_results(results: list[dict]) -> str:
    """Format surfaced memories for injection with age, wing, and ID.

    Enriched format gives the model staleness awareness (age), domain
    context (wing), and a handle for targeted recall (memory ID).
    Rank 1 and rules get 200 chars; rank 2+ non-rules get 120 chars.
    """
    if not results:
        return ""

    _enrich_with_metadata(results)

    lines = []
    for rank, r in enumerate(results):
        is_rule = r.get("memory_class") == "rule"
        max_len = 300 if (rank == 0 or is_rule) else 200
        content = r.get("content", "")
        # Strip extraction-pipeline prefixes like [discovery], [feature], etc.
        # These are baked into stored content but waste display chars.
        if content.startswith("[") and "] " in content[:30]:
            content = content[content.index("] ") + 2:]
        # Smart truncation: cut at last sentence boundary before limit
        if len(content) > max_len:
            for i in range(max_len - 1, max(max_len - 60, 0), -1):
                if content[i] in ".!?":
                    content = content[: i + 1]
                    break
            else:
                content = content[:max_len]


        mid = r.get("memory_id", "")
        age = _format_age(r.get("_created_at", ""))
        wing = r.get("_wing") or ""

        parts = ["Memory"]
        if age != "?":
            parts.append(age)
        if wing and wing != "memory":
            parts.append(wing)
        if mid and not mid.startswith("code:"):
            parts.append(f"id:{mid[:8]}")

        tag = " | ".join(parts)
        lines.append(f"[{tag}] {content}")

    # Remind the session about deeper search options beyond this hook
    lines.append(
        "Need more? Use `memory_recall` MCP (semantic search) "
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

    # Detect active wing from prompt and recent files
    active_wing: str | None = None
    try:
        from genesis.memory.taxonomy import detect_wing_from_prompt
        active_wing = detect_wing_from_prompt(prompt, file_paths=recent_files)
    except Exception:
        pass  # Taxonomy module failure must not block hook

    # FTS5 search (synchronous, fast) — covers both episodic and knowledge
    fts_results = _search_fts5(_DB_PATH, keywords)

    # Code index search (synchronous, fast) — structural code matches
    code_results = _search_code_index(_DB_PATH, keywords)

    # Vector search (async, may timeout)
    vector_results: list[dict] = []
    wing_results: list[dict] = []
    embed_latency_ms: float | None = None
    embed_start = time.monotonic()
    vector = await _embed_text(prompt)
    embed_latency_ms = (time.monotonic() - embed_start) * 1000
    if vector:
        if active_wing:
            # Run both searches in parallel to stay within budget
            vector_results, wing_results = await asyncio.gather(
                _search_qdrant(vector),
                _search_qdrant(vector, wing_filter=active_wing),
            )
        else:
            vector_results = await _search_qdrant(vector)

    fts_only_fallback = len(vector_results) == 0 and len(fts_results) > 0

    # Fuse results (with wing boost and code index if available)
    fused: list[dict] = []
    fused_count = 0
    if fts_results or vector_results or wing_results or code_results:
        fused = _rrf_fusion(
            fts_results, vector_results,
            wing_results=wing_results,
            code_results=code_results or None,
        )
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
