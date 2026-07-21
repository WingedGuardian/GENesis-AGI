"""Subprocess integration tests for scripts/proactive_memory_hook.py (thin client).

The hook no longer embeds or searches Qdrant itself — it POSTs each prompt to the
genesis-server recall endpoint and falls back to a degraded FTS5-only path on any
failure. These tests exercise that contract as a real subprocess:

- server path: a local STUB server returns a canned recall response → the hook
  prints the server's ``lines`` verbatim, records ``mode="server"`` in
  proactive_metrics.json, and folds the returned ids into the working set.
- degraded fallback: no server (dead port) → the hook prints the degraded banner
  + FTS5 keyword hits, records ``mode="degraded"``, and never crashes.
- off mode: session-local awareness only, no memory recall.

No embedding backend or live genesis-server is required (install-agnostic, CI-safe).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO_DIR / "scripts"
SRC_DIR = REPO_DIR / "src"
HOOK = SCRIPTS_DIR / "proactive_memory_hook.py"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

_FTS_ID = "ftsftsfts0001"
_FTS_CONTENT = "Decision about the recall reranker configuration and rollout plan"
_PROMPT = "what did we decide about the recall reranker"
_SESSION = "prb-subproc-1"


@pytest.fixture()
def fts_db(tmp_path: Path) -> Path:
    """A real DB (full schema) with one episodic memory_fts row matching _PROMPT."""
    import aiosqlite

    from genesis.db.schema import create_all_tables

    db_path = tmp_path / "genesis.db"

    async def _build() -> None:
        async with aiosqlite.connect(str(db_path)) as db:
            await create_all_tables(db)
            await db.execute(
                "INSERT INTO memory_fts (memory_id, content, source_type, tags, collection) "
                "VALUES (?, ?, ?, ?, ?)",
                (_FTS_ID, _FTS_CONTENT, "memory", "", "episodic_memory"),
            )
            await db.execute(
                "INSERT INTO memory_metadata (memory_id, created_at, collection, wing) "
                "VALUES (?, ?, ?, ?)",
                (_FTS_ID, "2026-07-01T00:00:00+00:00", "episodic_memory", "infrastructure"),
            )
            await db.commit()

    asyncio.run(_build())
    return db_path


@contextmanager
def _stub_server(response: dict, status: int = 200):
    """Run a loopback HTTP server that returns ``response`` as JSON for any POST."""
    body = json.dumps(response).encode()

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)  # drain the request body
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:  # silence default stderr logging
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _run_hook(
    prompt: str,
    session_id: str,
    env_extra: dict,
    home: Path,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop("GENESIS_CC_SESSION", None)
    env["HOME"] = str(home)
    env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"prompt": prompt, "session_id": session_id}),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def _metrics(home: Path) -> dict:
    return json.loads((home / ".genesis" / "proactive_metrics.json").read_text())


def test_stub_server_mode_server(fts_db: Path, tmp_path: Path):
    """A 200 from the endpoint → the hook prints server lines + records mode=server."""
    home = tmp_path / "home"
    home.mkdir()
    server_line = f"[Memory | 2w | infrastructure | id:{_FTS_ID[:8]}] Reranker decision recorded"
    response = {
        "status": "ok",
        "lines": [server_line, "Need more? Use `memory_recall` MCP."],
        "results": [
            {
                "memory_id": _FTS_ID,
                "collection": "episodic_memory",
                "kind": "memory",
                "via_graph": False,
                "score": 0.04,
                "origin_class": None,
                "source_pipeline": None,
                "retrieved_count": 0,
            }
        ],
        "procedure": None,
        "shadow": {
            "projected_ids": [_FTS_ID],
            "projected_injected": 1,
            "suppressed": 0,
            "serendipity_boosted": 1,
        },
        "budget": {"stance": "question_decision", "limit": 6, "kb_slots": 2},
        "embedding": None,
        "timings_ms": {"embed": 2.0, "total": 300.0},
        "engine": {
            "reranked": True,
            "graph_expansion": "off",
            "intent": "question",
            "profile": "cc_hook",
        },
    }
    with _stub_server(response) as base_url:
        result = _run_hook(
            _PROMPT,
            _SESSION,
            {
                "GENESIS_DB_PATH": str(fts_db),
                "GENESIS_PROACTIVE_HOOK_URL": base_url,
                "GENESIS_PROACTIVE_HOOK_MODE": "server",
            },
            home,
        )

    assert result.returncode == 0, result.stderr[:2000]
    assert server_line in result.stdout
    # The degraded banner must NOT appear on the healthy server path.
    assert "degraded" not in result.stdout.lower()

    m = _metrics(home)
    assert m["mode"] == "server"
    assert m["server_ms"] is not None
    assert m["fts_only_fallback"] is False
    # The engine's result ids are folded into the session working set.
    ws = json.loads(
        (home / ".genesis" / "sessions" / _SESSION / "surfaced_memories.json").read_text()
    )
    assert _FTS_ID in ws["entries"]


def test_server_path_includes_code_index_hints(fts_db: Path, tmp_path: Path):
    """The server path still surfaces local ``[Code]`` structural hints — the
    engine does semantic memory only, so the hook keeps the code_symbols lane the
    fork fused (Codex #1169)."""
    import sqlite3

    conn = sqlite3.connect(str(fts_db))
    conn.execute(
        "INSERT INTO code_modules (path, package, module_name, loc, file_mtime, last_indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("src/genesis/memory/retrieval.py", "genesis.memory", "retrieval", 100, 0.0, "2026-07-01"),
    )
    conn.execute(
        "INSERT INTO code_symbols (module_path, name, symbol_type, line_start, signature, is_public) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (
            "src/genesis/memory/retrieval.py",
            "recall_reranker",
            "function",
            10,
            "def recall_reranker(q)",
        ),
    )
    conn.commit()
    conn.close()

    home = tmp_path / "home"
    home.mkdir()
    response = {
        "status": "ok",
        "lines": ["[Memory | 1d | id:aaaaaaaa] a semantic memory"],
        "results": [],
        "procedure": None,
        "shadow": {},
        "budget": {},
        "embedding": None,
        "timings_ms": {},
        "engine": {},
    }
    with _stub_server(response) as base_url:
        result = _run_hook(
            "what did we decide about the recall reranker",
            "codesess",
            {
                "GENESIS_DB_PATH": str(fts_db),
                "GENESIS_PROACTIVE_HOOK_URL": base_url,
                "GENESIS_PROACTIVE_HOOK_MODE": "server",
            },
            home,
        )
    assert result.returncode == 0, result.stderr[:2000]
    assert "[Code]" in result.stdout  # structural hint surfaced on the server path
    assert "recall_reranker" in result.stdout


def test_server_down_falls_back_to_fts5(fts_db: Path, tmp_path: Path):
    """No server (dead port) → degraded banner + FTS5 hit + mode=degraded, exit 0."""
    home = tmp_path / "home"
    home.mkdir()
    result = _run_hook(
        _PROMPT,
        _SESSION,
        {
            "GENESIS_DB_PATH": str(fts_db),
            "GENESIS_PROACTIVE_HOOK_URL": "http://127.0.0.1:9",  # nothing listens
            "GENESIS_PROACTIVE_HOOK_MODE": "server",
        },
        home,
    )
    assert result.returncode == 0, result.stderr[:2000]
    assert "degraded" in result.stdout.lower()
    assert f"id:{_FTS_ID[:8]}" in result.stdout  # the seeded FTS5 memory surfaced
    assert "Memory·degraded" in result.stdout

    m = _metrics(home)
    assert m["mode"] == "degraded"
    assert m["fts_only_fallback"] is True


def test_mode_off_no_recall(fts_db: Path, tmp_path: Path):
    """GENESIS_PROACTIVE_HOOK_MODE=off → no memory recall (no server, no FTS5)."""
    home = tmp_path / "home"
    home.mkdir()
    result = _run_hook(
        _PROMPT,
        _SESSION,
        {"GENESIS_DB_PATH": str(fts_db), "GENESIS_PROACTIVE_HOOK_MODE": "off"},
        home,
    )
    assert result.returncode == 0, result.stderr[:2000]
    assert "[Memory" not in result.stdout
    assert "degraded" not in result.stdout.lower()


def test_local_mode_forces_fts5(fts_db: Path, tmp_path: Path):
    """mode=local → keyword-only path WITHOUT a server call; distinct banner."""
    home = tmp_path / "home"
    home.mkdir()
    result = _run_hook(
        _PROMPT,
        _SESSION,
        {"GENESIS_DB_PATH": str(fts_db), "GENESIS_PROACTIVE_HOOK_MODE": "local"},
        home,
    )
    assert result.returncode == 0, result.stderr[:2000]
    assert f"id:{_FTS_ID[:8]}" in result.stdout
    assert "local keyword-only mode" in result.stdout
    assert _metrics(home)["mode"] == "local"


def test_genesis_cc_session_exits_immediately():
    """Hook exits cleanly (code 0) with no output when GENESIS_CC_SESSION=1."""
    env = os.environ.copy()
    env["GENESIS_CC_SESSION"] = "1"
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"session_id": "bg", "prompt": "should not process"}),
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0
    assert "[Memory" not in result.stdout
    assert "[Procedure" not in result.stdout


def test_empty_input_exits_cleanly():
    """Hook handles empty stdin gracefully."""
    env = os.environ.copy()
    env.pop("GENESIS_CC_SESSION", None)
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input="",
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0
