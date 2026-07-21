#!/usr/bin/env python3
"""UserPromptSubmit hook: proactive memory surfacing (thin client).

Session-local awareness (heartbeat, intent trail, working set, ambient fold)
runs in-process; MEMORY RECALL is delegated to the genesis-server engine via
``POST /api/genesis/hook/recall`` so recall logic (reranker, fusion, entity
lane, graph expansion, injection defense, procedure surfacing) lives in exactly
one place. On any server failure the hook falls back to a degraded FTS5-only
keyword search (no write-backs) so a prompt is never blocked.

Modes (``GENESIS_PROACTIVE_HOOK_MODE``, default ``server``):
  server — call the endpoint; degrade to FTS5 on failure
  local  — skip the endpoint, always use the FTS5 degraded path
  off    — session-local awareness only, no memory recall
Endpoint base URL: ``GENESIS_PROACTIVE_HOOK_URL`` (default http://127.0.0.1:5000).

Budget: <2.2s client (server times out first at 2.0s → clean fallback).

Reads hook input from stdin as JSON:
  {"session_id": "...", "prompt": "...", ...}
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
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


_DB_PATH = _genesis_db_path()
_MAX_RESULTS = 3  # Degraded-fallback result cap (the server owns the live budget).
_MIN_PROMPT_WORDS = 1  # Stop words already filter greetings to 0 keywords
_METRICS_PATH = Path.home() / ".genesis" / "proactive_metrics.json"

# Recall delegation to the genesis-server engine (thin-client flip). The hook
# posts each prompt here; recall logic lives server-side in exactly one place.
_HOOK_MODE = os.environ.get("GENESIS_PROACTIVE_HOOK_MODE", "server").strip().lower()
_SERVER_BASE = os.environ.get(
    "GENESIS_PROACTIVE_HOOK_URL",
    "http://127.0.0.1:5000",
).rstrip("/")
_RECALL_ENDPOINT = f"{_SERVER_BASE}/api/genesis/hook/recall"
# Client budget slightly ABOVE the server's 2.0s _async_route timeout so the
# server times out first and returns a clean 503 (→ fallback), rather than the
# client aborting mid-flight; the short connect timeout catches a down server fast.
_SERVER_TIMEOUT_S = 2.2
_SERVER_CONNECT_TIMEOUT_S = 0.25

# Kill-switch flag consumed by _compute_suppress_ids (H-1 shadow suppression set).
_WS_GATE_DISABLED_FLAG = Path.home() / ".genesis" / "ws_gate_disabled"

# Automated-subsystem writes excluded from the degraded-fallback FTS5 lane
# (parity with the server engine's recall filter). NULL source_subsystem
# (user-sourced + legacy rows) is always preserved.
_PROACTIVE_EXCLUDED_SUBSYSTEMS: tuple[str, ...] = ("ego", "triage", "reflection", "autonomy")

# Common English stop words to filter from search queries
_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "can",
        "shall",
        "must",
        "need",
        "i",
        "me",
        "my",
        "we",
        "our",
        "you",
        "your",
        "he",
        "she",
        "it",
        "they",
        "them",
        "their",
        "this",
        "that",
        "these",
        "those",
        "what",
        "which",
        "who",
        "whom",
        "where",
        "when",
        "why",
        "how",
        "not",
        "no",
        "nor",
        "but",
        "or",
        "and",
        "so",
        "if",
        "then",
        "than",
        "too",
        "very",
        "just",
        "about",
        "also",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "with",
        "from",
        "by",
        "as",
        "into",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "up",
        "down",
        "out",
        "off",
        "over",
        "under",
        "again",
        "let",
        "lets",
        "let's",
        "please",
        "ok",
        "okay",
        "yeah",
        "yes",
        "hey",
        "hi",
        "hello",
        "thanks",
        "thank",
        # Conversational filler that dilutes FTS5 queries
        "now",
        "deal",
        "set",
        "get",
        "got",
        "put",
        "make",
        "made",
        "thing",
        "things",
        "stuff",
        "like",
        "want",
        "know",
        "think",
        "look",
        "right",
        "well",
        "going",
        "really",
        "actually",
        "already",
        "still",
        "here",
        "there",
        "start",
        "something",
        "kind",
        "sort",
        "sure",
        "guess",
        "maybe",
        "basically",
        "pretty",
        "anyway",
        "gonna",
        "wanna",
        "gotta",
        "more",
        "both",
        "generally",
        "specifically",
        "topics",
        "topic",
        "way",
        "take",
        "give",
        "come",
        "talk",
        "tell",
        "said",
        "use",
        "using",
        "used",
        "try",
        "first",
        "last",
        "new",
        "old",
        "big",
        "little",
        "much",
        "many",
        "few",
        "whole",
        "part",
        "point",
        "matter",
    }
)


# ---------------------------------------------------------------------------
# Session intent trail — pivot detection + injection
# ---------------------------------------------------------------------------

_TRAIL_DIR = Path.home() / ".genesis" / "sessions"
_PIVOT_SIMILARITY_THRESHOLD = 0.3
_PIVOT_DEBOUNCE_MSGS = 3
_MAX_TRAIL_DISPLAY = 50  # Show up to this many pivots — the full session arc for
# typical sessions. Was 8; the small window dropped early-session topics across long
# multi-phase sessions (e.g. an audit phase scrolling off before a later backup phase),
# leaving only the recent tail visible after compaction.
_GENESIS_PREFIX = str(Path.home() / "genesis") + "/"


def _trail_path(session_id: str) -> Path:
    """Path to the intent trail file for a session."""
    return _TRAIL_DIR / session_id / "intent_trail.json"


def _load_trail(session_id: str) -> dict:
    """Load intent trail from disk. Returns empty structure if missing."""
    path = _trail_path(session_id)
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return {"session_id": session_id, "pivots": [], "last_keywords": [], "msg_count": 0}


def _save_trail(session_id: str, trail: dict) -> None:
    """Atomic write of intent trail to disk."""
    path = _trail_path(session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            os.write(fd, json.dumps(trail).encode())
        finally:
            os.close(fd)
        os.replace(tmp, str(path))
    except Exception:
        pass  # Never block


def _jaccard_similarity(a: list[str], b: list[str]) -> float:
    """Jaccard similarity between two keyword lists."""
    set_a, set_b = set(a), set(b)
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union)


def _detect_pivot(current_kw: list[str], trail: dict) -> bool:
    """Detect whether the current message represents a topic pivot."""
    if not current_kw:
        return False
    last_kw = trail.get("last_keywords", [])
    if not last_kw:
        # First message with keywords — always a pivot (initial topic)
        return True
    # Debounce: require minimum messages between pivots
    msg_count = trail.get("msg_count", 0)
    pivots = trail.get("pivots", [])
    if pivots:
        last_pivot_msg = pivots[-1].get("at_msg", 0)
        if msg_count - last_pivot_msg < _PIVOT_DEBOUNCE_MSGS:
            return False
    similarity = _jaccard_similarity(current_kw, last_kw)
    return similarity < _PIVOT_SIMILARITY_THRESHOLD


# Harness-injected prompt envelopes — task-completion notifications, system
# reminders, slash-command metadata, and local-command output. These are not
# user messages; recording them as conversation pivots pollutes the L1 "Active
# Work" view and the session-trail line with raw <task-notification> blobs.
_HARNESS_ENVELOPE_PREFIXES = (
    "<task-notification>",
    "<system-reminder>",
    "<local-command-",
    "<command-",  # name, message, args, flag, and any future <command-*> variant
)


def _is_harness_envelope(prompt: str) -> bool:
    """True if *prompt* is a harness-injected envelope, not genuine user input."""
    return prompt.lstrip().startswith(_HARNESS_ENVELOPE_PREFIXES)


def _record_pivot_observation(
    db_path: Path,
    session_id: str,
    label: str,
    trigger: str,
) -> None:
    """Write a conversation_pivot observation to the DB."""
    try:
        import uuid as _uuid

        now = datetime.now(UTC)
        expires_at = (now + timedelta(days=7)).isoformat()
        conn = sqlite3.connect(str(db_path), timeout=2)
        try:
            conn.execute(
                "INSERT INTO observations"
                " (id, source, type, content, priority, created_at, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    _uuid.uuid4().hex,
                    f"session:{session_id}",
                    "conversation_pivot",
                    f"Conversation pivot: {label}. Trigger: {trigger[:80]}",
                    "low",
                    now.isoformat(),
                    expires_at,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass  # Never block


def _update_and_format_trail(
    session_id: str,
    keywords: list[str],
    prompt: str,
) -> str | None:
    """Update intent trail and return formatted line for injection.

    Returns None if trail has fewer than 2 pivots (not useful yet).
    """
    if not session_id:
        return None

    # Skip harness-injected turns (task notifications, system reminders,
    # slash-command metadata, local-command output) — they are not user
    # messages and would pollute the pivot trail and L1 "Active Work".
    if _is_harness_envelope(prompt):
        return None

    trail = _load_trail(session_id)
    trail["msg_count"] = trail.get("msg_count", 0) + 1

    # Only consider prompt-derived keywords (not file keywords) for pivots
    prompt_keywords = _extract_keywords(prompt)

    if _detect_pivot(prompt_keywords, trail):
        label = " ".join(prompt_keywords[:4])
        pivot = {
            "idx": len(trail["pivots"]),
            "label": label,
            "ts": datetime.now(UTC).isoformat(),
            "trigger": prompt[:80],
            "at_msg": trail["msg_count"],
        }
        trail["pivots"].append(pivot)
        _record_pivot_observation(_DB_PATH, session_id, label, prompt)

    if prompt_keywords:
        trail["last_keywords"] = prompt_keywords

    _save_trail(session_id, trail)

    # Format output — only show if 2+ pivots
    pivots = trail.get("pivots", [])
    if len(pivots) < 2:
        return None

    # Show last N pivots to keep the line compact
    display = pivots[-_MAX_TRAIL_DISPLAY:]
    labels = [p["label"] for p in display]
    prefix = "… → " if len(pivots) > _MAX_TRAIL_DISPLAY else ""
    return f"[Session trail] {prefix}{' → '.join(labels)}"


# ---------------------------------------------------------------------------
# Working set — per-session record of already-injected memories (H-1 PR1)
# ---------------------------------------------------------------------------
# Record-only measurement layer: tracks which memory/KB/code IDs this hook
# has already injected into the session (surfaced_memories.json) and appends
# per-prompt overlap stats to injection_log.jsonl. The 7-day rollup
# (observability/snapshots/proactive_memory.py) is the data gate for the
# PR2 novelty-gate decision. Nothing here may affect injection output.

_WS_VERSION = 1
_WS_FILENAME = "surfaced_memories.json"
_WS_LOG_FILENAME = "injection_log.jsonl"
_WS_MAX_ENTRIES = 300  # Evict oldest-surfaced beyond this (bounds file size)
_WS_MAX_RESETS = 20  # resets[] is written by the PR2 reset path; capped here


def _ws_path(session_id: str) -> Path | None:
    """Path to the working-set file, or None for unusable session IDs.

    Session IDs arrive via hook stdin — refuse anything that could
    escape the sessions dir (mirrors _load_recent_files).
    """
    if not session_id or "/" in session_id or ".." in session_id:
        return None
    return _TRAIL_DIR / session_id / _WS_FILENAME


def _empty_working_set(session_id: str) -> dict:
    return {
        "version": _WS_VERSION,
        "session_id": session_id,
        "turn": 0,
        "entries": {},
        "procedures": {},
        "resets": [],
    }


def _load_working_set(session_id: str) -> dict:
    """Load the session working set. Empty structure if missing/corrupt."""
    path = _ws_path(session_id)
    if path is None:
        return _empty_working_set(session_id)
    try:
        if path.exists():
            data = json.loads(path.read_text())
            if (
                isinstance(data, dict)
                and isinstance(data.get("entries"), dict)
                and isinstance(data.get("procedures"), dict)
            ):
                data.setdefault("version", _WS_VERSION)
                data.setdefault("session_id", session_id)
                data.setdefault("turn", 0)
                data.setdefault("resets", [])
                return data
    except Exception:
        pass
    return _empty_working_set(session_id)


def _save_working_set(session_id: str, ws: dict) -> None:
    """Atomic write of the working set (mirrors _save_trail). Never raises."""
    path = _ws_path(session_id)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            os.write(fd, json.dumps(ws).encode())
        finally:
            os.close(fd)
        os.replace(tmp, str(path))
    except Exception:
        pass  # Never block


def _ws_kind(result: dict) -> str:
    """Classify a fused result for working-set bookkeeping."""
    if result.get("memory_id", "").startswith("code:"):
        return "code"
    if result.get("collection") == "knowledge_base":
        return "kb"
    return "memory"


def _ws_overlap(ws: dict, injected_ids: list[str]) -> tuple[list[str], float]:
    """Repeat IDs (already in the working set) + overlap percentage.

    Must run on the PRE-update snapshot — call before _ws_record.
    """
    entries = ws.get("entries", {})
    repeats = [mid for mid in injected_ids if mid in entries]
    pct = round(100.0 * len(repeats) / len(injected_ids), 1) if injected_ids else 0.0
    return repeats, pct


def _ws_record(
    ws: dict,
    injected: list[tuple[str, str]],
    proc_id: str | None,
    now_iso: str,
) -> None:
    """Record injected (memory_id, kind) pairs + surfaced procedure in place."""
    ws["turn"] = ws.get("turn", 0) + 1
    turn = ws["turn"]
    entries = ws.setdefault("entries", {})
    for mid, kind in injected:
        entry = entries.get(mid)
        if entry is None:
            entries[mid] = {
                "first_ts": now_iso,
                "last_ts": now_iso,
                "count": 1,
                "first_turn": turn,
                "last_turn": turn,
                "kind": kind,
            }
        else:
            entry["last_ts"] = now_iso
            entry["count"] = entry.get("count", 0) + 1
            entry["last_turn"] = turn
    if proc_id:
        procedures = ws.setdefault("procedures", {})
        proc = procedures.get(proc_id)
        if proc is None:
            procedures[proc_id] = {"last_ts": now_iso, "count": 1}
        else:
            proc["last_ts"] = now_iso
            proc["count"] = proc.get("count", 0) + 1
    # Bound file size: evict the oldest-surfaced entries beyond the cap
    if len(entries) > _WS_MAX_ENTRIES:
        by_age = sorted(entries.items(), key=lambda kv: kv[1].get("last_ts", ""))
        for mid, _ in by_age[: len(entries) - _WS_MAX_ENTRIES]:
            del entries[mid]
    resets = ws.get("resets", [])
    if len(resets) > _WS_MAX_RESETS:
        ws["resets"] = resets[-_WS_MAX_RESETS:]


def _append_injection_log(session_id: str, record: dict) -> None:
    """Append one per-prompt overlap record. Best-effort, never raises."""
    path = _ws_path(session_id)
    if path is None:
        return
    try:
        log_path = path.parent / _WS_LOG_FILENAME
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass  # Never block


def _compute_suppress_ids(session_id: str) -> frozenset[str]:
    """IDs already surfaced this session — the PR2b suppression set.

    Empty when there's no session, or when the kill-switch flag
    (``~/.genesis/ws_gate_disabled``) exists — a hot disable without a deploy.
    In PR2a this feeds the shadow projection only; the real injection is
    unaffected regardless.
    """
    if not session_id:
        return frozenset()
    try:
        if _WS_GATE_DISABLED_FLAG.exists():
            return frozenset()
    except OSError:
        pass
    ws = _load_working_set(session_id)
    return frozenset(ws.get("entries", {}).keys())


def _ws_measure(
    fused: list[dict],
    session_id: str,
    surfaced_proc_id: str | None,
    now_iso: str,
    shadow: dict | None = None,
) -> dict:
    """The whole working-set measurement step for one prompt.

    Computes overlap vs the PRE-update working set, records this prompt's
    injection, and appends the injection-log line. Contract: NEVER prints
    (hook stdout is context injected into the conversation) and NEVER
    raises — any internal failure returns the stats gathered so far with
    safe defaults for the rest.

    ``shadow`` (H-1 PR2a) is the novelty-gate projection for this prompt —
    supplied by the server engine's ``_shadow_projection`` on the server path,
    and ``None`` on the degraded fallback. When non-empty its
    ``projected_injected``/``suppressed``/``serendipity_boosted`` fields are
    merged into the injection-log record and returned stats. It never changes the
    injected output — this step already runs after stdout is flushed.
    """
    stats: dict = {
        "injected_ids": [],
        "repeat_count": 0,
        "overlap_pct": None,
        "working_set_size": None,
        "zero_retrieved_injected": 0,
        "procedure_repeat": False,
    }
    try:
        stats["injected_ids"] = [r["memory_id"] for r in fused if r.get("memory_id")]
        # Baseline for the PR2 serendipity boost: how often does today's
        # ranking already surface never-retrieved episodic memories?
        # FTS-only entries lack _retrieved_count → excluded (unknown ≠ 0).
        stats["zero_retrieved_injected"] = sum(
            1
            for r in fused
            if r.get("collection") != "knowledge_base" and r.get("_retrieved_count", -1) == 0
        )
        if session_id and (stats["injected_ids"] or surfaced_proc_id):
            ws = _load_working_set(session_id)
            repeats, overlap_pct = _ws_overlap(ws, stats["injected_ids"])
            stats["repeat_count"] = len(repeats)
            stats["overlap_pct"] = overlap_pct
            stats["procedure_repeat"] = bool(
                surfaced_proc_id and surfaced_proc_id in ws.get("procedures", {})
            )
            _ws_record(
                ws,
                [(r["memory_id"], _ws_kind(r)) for r in fused if r.get("memory_id")],
                surfaced_proc_id,
                now_iso,
            )
            _save_working_set(session_id, ws)
            stats["working_set_size"] = len(ws.get("entries", {}))
            record = {
                "ts": now_iso,
                "turn": ws.get("turn", 0),
                "injected": len(stats["injected_ids"]),
                "repeats": stats["repeat_count"],
                "overlap_pct": stats["overlap_pct"],
                "ws_size": stats["working_set_size"],
                "proc": bool(surfaced_proc_id),
                "proc_repeat": stats["procedure_repeat"],
            }
            if shadow:
                for key in ("projected_injected", "suppressed", "serendipity_boosted"):
                    record[key] = shadow.get(key)
                    stats[key] = shadow.get(key)
            _append_injection_log(session_id, record)
    except Exception:
        pass  # Measurement must never block the prompt
    return stats


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
        # Strip project root prefix to extract meaningful path components
        path = fp.replace(_GENESIS_PREFIX, "")
        parts = path.replace("/", " ").replace("_", " ").replace(".", " ").split()
        for part in parts:
            part = part.lower()
            if (
                part not in _STOP_WORDS
                and len(part) >= 3
                and part not in ("src", "py", "md", "txt", "json", "yaml", "tests", "test")
            ):
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
                """,  # noqa: S608 - literal SQL fragments; values bound as parameters
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
                results.append(
                    {
                        "memory_id": f"code:{row['module_path']}:{row['name']}",
                        "content": content,
                        "source_type": "code_index",
                        "memory_class": "fact",
                    }
                )
            return results
        finally:
            conn.close()
    except sqlite3.OperationalError:
        # Table doesn't exist yet — index hasn't run
        return []
    except Exception as exc:
        print(f"Code index search error: {exc}", file=sys.stderr)
        return []


def _search_fts5(
    db_path: Path,
    keywords: list[str],
    collection: str | None = None,
    now_iso: str | None = None,
) -> list[dict]:
    """Search memory_fts using FTS5 with OR-joined keywords.

    Excludes automated-subsystem writes (ego/triage/reflection) via a
    LEFT JOIN with memory_metadata. NULL source_subsystem (user-sourced
    + legacy pre-1.5b rows) is preserved.

    Also drops bitemporally-invalid rows (``invalid_at`` in the past) and
    superseded/deprecated rows (``deprecated = 1``), for parity with the main
    retrieval path (``memory.crud.search_ranked``) and the hook's own Qdrant
    path. NULL ``invalid_at`` = "valid forever" and NULL/0 ``deprecated`` =
    "not deprecated" are always preserved, so the filter can never over-drop
    live context.

    ``collection``: if provided, restrict to this collection
    (e.g. ``"episodic_memory"``). Default ``None`` searches all.
    ``now_iso``: as-of instant for the invalid_at check; defaults to now.
    """
    if not keywords:
        return []
    if now_iso is None:
        now_iso = datetime.now(UTC).isoformat()

    fts_query = " OR ".join('"' + _escape_fts5(k) + '"' for k in keywords)
    placeholders = ",".join("?" * len(_PROACTIVE_EXCLUDED_SUBSYSTEMS))

    collection_clause = ""
    collection_params: tuple = ()
    if collection:
        collection_clause = "AND memory_fts.collection = ?"
        collection_params = (collection,)

    try:
        conn = sqlite3.connect(str(db_path), timeout=2)
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                f"""
                SELECT memory_fts.memory_id, memory_fts.content,
                       memory_fts.source_type, memory_fts.collection,
                       memory_fts.rank,
                       memory_metadata.origin_class
                FROM memory_fts
                LEFT JOIN memory_metadata
                  ON memory_fts.memory_id = memory_metadata.memory_id
                WHERE memory_fts MATCH ?
                  {collection_clause}
                  AND (memory_metadata.source_subsystem IS NULL
                       OR memory_metadata.source_subsystem
                           NOT IN ({placeholders}))
                  AND (memory_metadata.invalid_at IS NULL
                       OR memory_metadata.invalid_at > ?)
                  AND (memory_metadata.deprecated IS NULL
                       OR memory_metadata.deprecated = 0)
                ORDER BY rank
                LIMIT ?
                """,  # noqa: S608 -- placeholders bound separately
                (
                    fts_query,
                    *collection_params,
                    *_PROACTIVE_EXCLUDED_SUBSYSTEMS,
                    now_iso,
                    _MAX_RESULTS * 2,
                ),
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
        if r.get("memory_id", "")
        and not r["memory_id"].startswith("code:")
        and not r.get("_enriched")
    ]
    if not ids:
        return
    try:
        conn = sqlite3.connect(str(_DB_PATH), timeout=2)
        try:
            conn.row_factory = sqlite3.Row
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT memory_id, created_at, wing, collection, origin_class"  # noqa: S608
                f" FROM memory_metadata WHERE memory_id IN ({placeholders})",
                ids,
            ).fetchall()
            meta = {row["memory_id"]: dict(row) for row in rows}
            for r in results:
                mid = r.get("memory_id", "")
                if mid in meta:
                    r.setdefault("_created_at", meta[mid].get("created_at"))
                    if not r.get("_wing"):
                        r["_wing"] = meta[mid].get("wing")
                    r.setdefault("collection", meta[mid].get("collection"))
                    # WS-3 stored provenance — fills paths whose search value
                    # is missing OR None (Qdrant dicts always carry the key,
                    # None for pre-backfill payloads, so setdefault would
                    # refuse the fill). A real search-path value always wins.
                    if r.get("origin_class") is None:
                        r["origin_class"] = meta[mid].get("origin_class")
                    # The ``_enriched`` marker keeps a second enrichment pass
                    # (e.g. from _format_degraded) from re-querying rows this
                    # pass already resolved.
                    r["_enriched"] = True
        finally:
            conn.close()
    except Exception:
        pass  # Best-effort enrichment — never block the hook


def _ensure_knowledge_retrieved_count(db_path: Path) -> None:
    """Self-healing migration: add retrieved_count to knowledge_units if missing."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=2)
        try:
            conn.execute(
                "ALTER TABLE knowledge_units ADD COLUMN retrieved_count INTEGER NOT NULL DEFAULT 0"
            )
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists
        finally:
            conn.close()
    except Exception:
        pass  # Never block


async def _call_server(
    prompt: str,
    session_id: str,
    file_keywords: list[str],
    suppress_ids: frozenset[str],
) -> dict | None:
    """POST the prompt to the genesis-server proactive recall endpoint.

    Returns the parsed JSON dict on a 200 (status ``ok`` or ``disabled``), or
    None on ANY failure (connection refused, timeout, non-200, bad JSON) so the
    caller falls back to the degraded FTS5 path. Never raises — a down or slow
    server must never block the user's prompt.
    """
    import httpx

    payload = {
        "prompt": prompt,
        "session_id": session_id,
        "profile": "cc_hook",
        "file_keywords": file_keywords,
        "suppress_ids": list(suppress_ids)[:300],
        "hook_version": 2,
    }
    try:
        timeout = httpx.Timeout(_SERVER_TIMEOUT_S, connect=_SERVER_CONNECT_TIMEOUT_S)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(_RECALL_ENDPOINT, json=payload)
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _format_degraded(results: list[dict], *, forced_local: bool = False) -> str:
    """Render FTS5/code fallback hits with a visible degraded marker.

    Used ONLY when the server recall path is unavailable (server down/slow) or
    deliberately bypassed (``GENESIS_PROACTIVE_HOOK_MODE=local``). Reuses the
    offline SQLite enrichment (_enrich_with_metadata) + age formatting, but tags
    every line ``[Memory·degraded | …]`` so the model knows this is keyword-only
    recall, not the full engine. No write-backs happen on this path.
    """
    if not results:
        return ""
    _enrich_with_metadata(results)
    from genesis.security.sanitizer import strip_boundary_markers

    banner = (
        "[Memory recall in local keyword-only mode (GENESIS_PROACTIVE_HOOK_MODE=local)]"
        if forced_local
        else "[Memory recall degraded — genesis-server unreachable; keyword-only results]"
    )
    lines = [banner]
    for rank, r in enumerate(results):
        max_len = 300 if rank == 0 else 200
        content = strip_boundary_markers(r.get("content", "") or "")
        # Strip extraction-pipeline prefixes ([discovery], [feature], …).
        if content.startswith("[") and "] " in content[:30]:
            content = content[content.index("] ") + 2 :]
        if len(content) > max_len:
            content = content[:max_len]
        mid = r.get("memory_id", "")
        age = _format_age(r.get("_created_at", ""))
        wing = r.get("_wing") or ""
        # Preserve external-world provenance even on the degraded path: a stored
        # ``external_untrusted`` episodic hit must NOT read as first-party memory
        # (the degraded banner already signals keyword-only mode globally). Matches
        # the server renderer's Memory·external tier + the injection-defense cut.
        parts = (
            ["Memory·external"]
            if r.get("origin_class") == "external_untrusted"
            else ["Memory·degraded"]
        )
        if age != "?":
            parts.append(age)
        if wing and wing != "memory":
            parts.append(wing)
        if mid and not mid.startswith("code:"):
            parts.append(f"id:{mid[:8]}")
        lines.append(f"[{' | '.join(parts)}] {content}")
    lines.append(
        "Need more? Use `memory_recall` MCP (semantic search) "
        "or query `cc_sessions` in SQLite. Grep transcripts is last resort."
    )
    return "\n".join(lines)


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
    heartbeat_ms: float = 0.0,
    injected_ids: list[str] | None = None,
    repeat_count: int = 0,
    overlap_pct: float | None = None,
    working_set_size: int | None = None,
    zero_retrieved_injected: int = 0,
    procedure_repeat: bool = False,
    projected_injected: int | None = None,
    suppressed: int = 0,
    serendipity_boosted: int = 0,
    mode: str = "server",
    server_ms: float | None = None,
) -> None:
    """Atomic JSON write — latest invocation detail for health dashboard.

    The working-set fields (H-1 PR1) describe this prompt's injection vs
    the session's already-surfaced set; ``overlap_pct``/``working_set_size``
    are None when no injection happened or no session ID was available. The
    shadow fields (H-1 PR2a) are the novelty-gate projection for this prompt —
    ``projected_injected`` is None when no shadow ran.

    ``mode`` (server | degraded | local | off) + ``server_ms`` expose the
    thin-client recall path, so the fallback rate is directly observable in
    ``proactive_metrics.json`` (the 1-week latency/fallback review reads this).
    """
    data = {
        "timestamp": datetime.now(UTC).isoformat(),
        "mode": mode,
        "server_ms": round(server_ms, 1) if server_ms is not None else None,
        "fts_results": fts_count,
        "vector_results": vector_count,
        "fused_results": fused_count,
        "embed_latency_ms": embed_latency_ms,
        "total_latency_ms": total_latency_ms,
        "fts_only_fallback": fts_only_fallback,
        "heartbeat_ms": round(heartbeat_ms, 1),
        "budget_exceeded": total_latency_ms > 2000,
        "injected_ids": injected_ids or [],
        "repeat_count": repeat_count,
        "overlap_pct": overlap_pct,
        "working_set_size": working_set_size,
        "zero_retrieved_injected": zero_retrieved_injected,
        "procedure_repeat": procedure_repeat,
        "projected_injected": projected_injected,
        "suppressed": suppressed,
        "serendipity_boosted": serendipity_boosted,
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


def _extract_genesis_summary(session_id: str) -> str | None:
    """Extract what Genesis was doing from tool_observations.jsonl.

    Reads the last 5 entries from the session's tool observation log
    and produces a compact summary like "Grep observations.py, Read
    essential_knowledge.py, Bash ran tests".

    Returns None if file doesn't exist or is empty.
    """
    if not session_id:
        return None

    obs_path = Path.home() / ".genesis" / "sessions" / session_id / "tool_observations.jsonl"
    if not obs_path.exists():
        return None

    try:
        # Read last 5 lines efficiently
        lines: list[str] = []
        with open(obs_path, "rb") as f:
            # Seek from end to find last N lines
            try:
                f.seek(0, 2)
                size = f.tell()
                # Read last 4KB — should contain 5+ entries
                read_size = min(size, 4096)
                f.seek(size - read_size)
                chunk = f.read().decode("utf-8", errors="replace")
                lines = [ln for ln in chunk.strip().split("\n") if ln.strip()][-5:]
            except Exception:
                return None

        if not lines:
            return None

        tools: list[str] = []
        for line in lines:
            try:
                entry = json.loads(line)
                tool_name = entry.get("tool_name", "")
                key_info = entry.get("key_info", {})
                if tool_name == "Grep":
                    pattern = key_info.get("pattern", "")[:30]
                    tools.append(f"Grep {pattern}")
                elif tool_name == "Read":
                    path = key_info.get("file_path", "")
                    fname = path.rsplit("/", 1)[-1] if "/" in path else path
                    tools.append(f"Read {fname}")
                elif tool_name == "Bash":
                    cmd = key_info.get("command", "")[:25]
                    tools.append(f"Bash {cmd}")
                elif tool_name == "Edit":
                    path = key_info.get("file_path", "")
                    fname = path.rsplit("/", 1)[-1] if "/" in path else path
                    tools.append(f"Edit {fname}")
                elif tool_name:
                    tools.append(tool_name)
            except (json.JSONDecodeError, AttributeError):
                continue

        return ", ".join(tools[-3:]) if tools else None
    except Exception:
        return None


def _heartbeat_write(
    db_path: Path,
    session_id: str,
    prompt: str,
) -> float:
    """Write session heartbeat. Returns elapsed ms. Best-effort."""
    hb_start = time.monotonic()
    if not session_id or not db_path.exists():
        return 0.0

    try:
        user_summary = prompt[:120].replace("\n", " ").strip()
        genesis_summary = _extract_genesis_summary(session_id)

        from genesis.db.crud.session_heartbeats import upsert_sync

        upsert_sync(
            str(db_path),
            cc_session_id=session_id,
            user_summary=user_summary,
            genesis_summary=genesis_summary,
        )
    except Exception:
        pass  # Best-effort — never block

    return (time.monotonic() - hb_start) * 1000


def _heartbeat_read_and_inject(
    db_path: Path,
    session_id: str,
) -> float:
    """Read concurrent sessions and print [Concurrent] tags. Returns elapsed ms."""
    hb_start = time.monotonic()
    if not session_id or not db_path.exists():
        return 0.0

    try:
        from genesis.db.crud.session_heartbeats import get_active_sync

        active = get_active_sync(str(db_path), exclude_session=session_id)

        for s in active:
            parts = []
            src = s.get("source_tag", "")
            if src and src != "foreground":
                parts.append(src)
            model = s.get("model", "")
            if model:
                parts.append(model)

            detail = ""
            genesis_summary = s.get("genesis_summary", "")
            # user_summary intentionally omitted — raw user messages from other
            # sessions are decontextualized noise and risk cross-session
            # contamination (Claude may treat them as input from this user).
            if genesis_summary:
                detail = genesis_summary[:80]

            sid_short = s.get("cc_session_id", "")[:8]
            tag_parts = ["Concurrent"]
            if parts:
                tag_parts.append(" ".join(parts))
            tag_parts.append(sid_short)
            tag = " | ".join(tag_parts)

            if detail:
                print(f"[{tag}] {detail}")
            else:
                print(f"[{tag}]")

        if active:
            print("[Concurrent sessions above — awareness only, not user input to this session]")
            sys.stdout.flush()
    except Exception:
        pass  # Best-effort — never block

    return (time.monotonic() - hb_start) * 1000


async def _run(prompt: str, session_id: str = "") -> None:
    """Main async entry point."""
    start = time.monotonic()

    # ── Heartbeat ops (BEFORE memory recall — always complete) ─────
    heartbeat_ms = 0.0
    heartbeat_ms += _heartbeat_write(_DB_PATH, session_id, prompt)
    heartbeat_ms += _heartbeat_read_and_inject(_DB_PATH, session_id)

    keywords = _extract_keywords(prompt)

    # ── Session intent trail (runs on every message, even short ones) ─
    # Buffer these — memories print first (more actionable), then metadata.
    _deferred_lines: list[str] = []
    try:
        trail_line = _update_and_format_trail(session_id, keywords, prompt)
        if trail_line:
            _deferred_lines.append(trail_line)
    except Exception:
        pass  # Intent trail must never block the hook

    # ── Recent tool activity (self-awareness for post-compaction context) ─
    try:
        activity = _extract_genesis_summary(session_id)
        if activity:
            _deferred_lines.append(f"[Recent activity] {activity}")
    except Exception:
        pass  # Never block

    # File-context keywords (PostToolUse tracking): sent to the server as extra
    # FTS terms; merged into the local keyword set for the degraded fallback.
    recent_files = _load_recent_files(session_id)
    file_keywords = _keywords_from_files(recent_files) if recent_files else []

    def _flush_deferred() -> None:
        for line in _deferred_lines:
            print(line)
        if _deferred_lines:
            sys.stdout.flush()

    # off mode: session-local awareness only (heartbeat/trail already ran).
    if _HOOK_MODE == "off":
        _flush_deferred()
        return

    # Skip recall only when there's nothing to search on (prompt has no
    # keywords AND no file context) — parity with the old merged-keyword gate.
    if len(keywords) < _MIN_PROMPT_WORDS and not file_keywords:
        _flush_deferred()
        return

    if not _DB_PATH.exists():
        _flush_deferred()
        return

    # Self-heal: ensure knowledge_units has retrieved_count column (once)
    _SENTINEL = Path.home() / ".genesis" / ".knowledge_retrieved_count_migrated"
    if not _SENTINEL.exists():
        _ensure_knowledge_retrieved_count(_DB_PATH)
        _SENTINEL.parent.mkdir(parents=True, exist_ok=True)
        _SENTINEL.touch(exist_ok=True)

    suppress_ids = _compute_suppress_ids(session_id)
    now_iso = datetime.now(UTC).isoformat()

    # ── Delegated recall: server engine (default) with FTS5 fallback ─
    server_data: dict | None = None
    server_ms: float | None = None
    if _HOOK_MODE != "local":
        _t_srv = time.monotonic()
        server_data = await _call_server(prompt, session_id, file_keywords, suppress_ids)
        server_ms = (time.monotonic() - _t_srv) * 1000

    if server_data is not None:
        # ── SERVER PATH: the engine owns recall, formatting, procedure
        # surfacing, and the retrieved_count / surfaced_count / immunity
        # write-backs. The hook only prints and measures. ─
        lines = server_data.get("lines") or []
        for line in lines:
            print(line)
        if lines:
            sys.stdout.flush()

        # Code-index structural hints ([Code] symbol — location). The server
        # engine surfaces SEMANTIC memory only; the pre-flip fork also fused local
        # code_symbols matches on code/debug prompts, so keep that lane hook-side —
        # a cheap local SQLite lookup, distinct from (and additive to) memory
        # recall. Best-effort; empty when the code index hasn't been built.
        # Suppressed when the engine is deliberately silenced (status "disabled" =
        # config kill switch → inject NOTHING), so code hints don't leak past it.
        if server_data.get("status") != "disabled":
            code_keywords = keywords + [k for k in file_keywords if k not in keywords]
            for ch in _search_code_index(_DB_PATH, code_keywords)[:_MAX_RESULTS]:
                content = ch.get("content")
                if content:
                    print(content)
                sys.stdout.flush()

        # Adapt structured rows for H-1 measurement: the engine emits pre-bump
        # ``retrieved_count``; _ws_measure reads ``_retrieved_count`` (default -1
        # → FTS-only hits stay excluded from the never-surfaced stat, exactly as
        # the old fork behaved).
        fused: list[dict] = []
        for r in server_data.get("results") or []:
            row = dict(r)
            if "retrieved_count" in r:
                row["_retrieved_count"] = r["retrieved_count"]
            fused.append(row)
        proc = server_data.get("procedure")
        surfaced_proc_id = proc.get("id") if isinstance(proc, dict) else None
        shadow = server_data.get("shadow") or {}
        embedding = server_data.get("embedding")

        _flush_deferred()

        ws_stats = _ws_measure(fused, session_id, surfaced_proc_id, now_iso, shadow=shadow)
        total_ms = (time.monotonic() - start) * 1000
        _record_activity(_DB_PATH, total_ms, success=bool(fused))
        _record_detail(
            # On the server path the HOOK does zero local fts/vector search — the
            # server owns retrieval. Report 0/0 (fused_count + mode carry meaning);
            # labeling server hits as "vector" would lie to the 1-week review.
            fts_count=0,
            vector_count=0,
            fused_count=len(fused),
            embed_latency_ms=None,
            total_latency_ms=total_ms,
            fts_only_fallback=False,
            heartbeat_ms=heartbeat_ms,
            injected_ids=ws_stats["injected_ids"],
            repeat_count=ws_stats["repeat_count"],
            overlap_pct=ws_stats["overlap_pct"],
            working_set_size=ws_stats["working_set_size"],
            zero_retrieved_injected=ws_stats["zero_retrieved_injected"],
            procedure_repeat=ws_stats["procedure_repeat"],
            projected_injected=ws_stats.get("projected_injected"),
            suppressed=ws_stats.get("suppressed", 0),
            serendipity_boosted=ws_stats.get("serendipity_boosted", 0),
            mode="server",
            server_ms=server_ms,
        )
        # Ambient fold on the server-returned prompt embedding (None-safe).
        _ambient_fold(embedding, session_id, prompt, recent_files)
        return

    # ── DEGRADED FALLBACK: FTS5 + code index only, NO write-backs ────
    # Reached when the server is unreachable/slow (mode "server") or when the
    # operator forced local recall (GENESIS_PROACTIVE_HOOK_MODE=local).
    fallback_mode = "local" if _HOOK_MODE == "local" else "degraded"
    fallback_keywords = keywords + [k for k in file_keywords if k not in keywords]
    fts_results = _search_fts5(
        _DB_PATH,
        fallback_keywords,
        collection="episodic_memory",
        now_iso=now_iso,
    )
    from genesis.memory.provenance import is_garbage

    code_results = _search_code_index(_DB_PATH, fallback_keywords)
    fused = [r for r in fts_results if not is_garbage(r.get("content", ""))][:_MAX_RESULTS]
    if len(fused) < _MAX_RESULTS and code_results:
        fused += code_results[: _MAX_RESULTS - len(fused)]

    if fused:
        output = _format_degraded(fused, forced_local=(fallback_mode == "local"))
        if output:
            print(output)
            sys.stdout.flush()

    _flush_deferred()

    # WS-3 gate 4 (injection) shadow record — DEGRADED PATH ONLY. The server
    # path emits server-side (mcp/memory/core.py::_proactive_impl); here the hook
    # injects FTS5 content locally, so it must record blockable external-world
    # hits that reach the prompt itself. Fire-and-forget after flush; the items
    # were enriched with origin_class by _format_degraded above.
    if fused:
        try:
            from genesis.security.immunity_shadow import (
                item_is_blockable,
                record_would_block_sync,
            )

            blockable = sum(
                1
                for r in fused
                if item_is_blockable(
                    collection=r.get("collection"),
                    source_pipeline=r.get("source_pipeline"),
                    origin_class=r.get("origin_class"),
                )
            )
            if blockable:
                conn = sqlite3.connect(str(_DB_PATH), timeout=2)
                try:
                    record_would_block_sync(
                        conn,
                        gate="injection",
                        source_kind="proactive_hook",
                        source_ref="scripts/proactive_memory_hook.py::_run",
                        blockable_count=blockable,
                    )
                finally:
                    conn.close()
        except Exception as exc:
            print(f"Immunity shadow emit skipped: {exc}", file=sys.stderr)

    ws_stats = _ws_measure(fused, session_id, None, now_iso, shadow=None)
    total_ms = (time.monotonic() - start) * 1000
    _record_activity(_DB_PATH, total_ms, success=bool(fused))
    _record_detail(
        fts_count=len(fts_results),
        vector_count=0,
        fused_count=len(fused),
        embed_latency_ms=None,
        total_latency_ms=total_ms,
        fts_only_fallback=True,
        heartbeat_ms=heartbeat_ms,
        injected_ids=ws_stats["injected_ids"],
        repeat_count=ws_stats["repeat_count"],
        overlap_pct=ws_stats["overlap_pct"],
        working_set_size=ws_stats["working_set_size"],
        zero_retrieved_injected=ws_stats["zero_retrieved_injected"],
        procedure_repeat=ws_stats["procedure_repeat"],
        mode=fallback_mode,
        server_ms=server_ms,
    )
    # No embedding on the fallback path — the ambient fold degrades silently.
    _ambient_fold(None, session_id, prompt, recent_files)


def _turn_pivoted(session_id: str) -> bool:
    """True if the intent trail recorded a pivot on the current turn."""
    try:
        trail = _load_trail(session_id)
        pivots = trail.get("pivots", [])
        return bool(pivots) and pivots[-1].get("at_msg") == trail.get("msg_count")
    except Exception:
        return False


def _ambient_fold(
    vector: list[float] | None,
    session_id: str,
    prompt: str,
    recent_files: list[str],
) -> None:
    """Fail-open bridge into genesis.session_awareness (WS-C).

    Skips harness envelopes (not genuine user turns) and embed-less
    turns. GENESIS_SESSION_AWARENESS_DISABLED=1 is the ops kill switch;
    dispatched sessions never get here (GENESIS_CC_SESSION guard at
    import). Must never raise or print.
    """
    try:
        if (
            not vector
            or not session_id
            or os.environ.get("GENESIS_SESSION_AWARENESS_DISABLED") == "1"
            or _is_harness_envelope(prompt)
        ):
            return
        from genesis.session_awareness import hook_fold

        result = hook_fold(
            session_id=session_id,
            vector=vector,
            prompt_keywords=_extract_keywords(prompt),
            file_keywords=_keywords_from_files(recent_files) if recent_files else [],
            pivoted=_turn_pivoted(session_id),
            prompt_text=prompt,
        )
        if result and result.get("fired"):
            _spawn_ambient_worker(session_id)
    except Exception:
        pass  # Ambient layer must never affect the hook


def _spawn_ambient_worker(session_id: str) -> None:
    """Detached worker spawn (WS-C PR2) — the genesis_session_end idiom.

    start_new_session=True: the worker outlives this hook process.
    stdout is discarded; stderr goes to a shared error log (the worker
    records outcomes in ambient_verdict.json / shadow_log.jsonl, so
    stderr only matters for crashes-before-logging). The full pipeline
    (retrieve + rank + arbiter) runs in SHADOW: verdicts are recorded,
    nothing is ever injected into a session until the PR5 live flip.
    """
    try:
        script = Path(__file__).resolve().parent / "ambient_awareness_worker.py"
        err_dir = Path.home() / ".genesis" / "session_awareness"
        err_dir.mkdir(parents=True, exist_ok=True)
        with (
            (err_dir / "worker_err.log").open("ab") as log_fh,
            contextlib.suppress(OSError),
        ):
            subprocess.Popen(
                [
                    sys.executable,
                    str(script),
                    "--session-id",
                    session_id,
                ],
                stdout=subprocess.DEVNULL,
                stderr=log_fh,
                start_new_session=True,
            )
    except Exception:
        pass  # Spawn failure must never affect the hook


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
