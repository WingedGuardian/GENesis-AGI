"""Subprocess integration test for scripts/proactive_memory_hook.py.

Tests the procedure surfacing path end-to-end: seeds a procedural_memory
row with a real embedding, runs the hook as a subprocess with a semantically
related prompt, and asserts the procedure is surfaced in stdout.

Requires:
- An embedding backend (Ollama at the configured URL, or DeepInfra/DashScope API key)
- The genesis.env module importable (src on sys.path)

Skipped in CI if no embedding backend is reachable.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO_DIR / "scripts"
SRC_DIR = REPO_DIR / "src"

# Add src to path so we can import genesis modules for test setup
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def _embedding_available() -> bool:
    """Check if at least one embedding backend is reachable (5s timeout)."""
    try:
        from genesis.memory.embeddings import EmbeddingProvider

        async def _check():
            provider = EmbeddingProvider()
            return await asyncio.wait_for(provider.embed("test"), timeout=5.0)

        result = asyncio.run(_check())
        return result is not None and len(result) > 0
    except Exception:
        return False


# Skip if no embedding backend available
pytestmark = pytest.mark.skipif(
    not _embedding_available(),
    reason="No embedding backend available (Ollama/DeepInfra/DashScope)",
)

# The procedure principle and the test prompt must be semantically related
# so that cosine similarity >= 0.7
_PROCEDURE_PRINCIPLE = (
    "Never git add . or commit directly to main when other sessions are active. "
    "Use git worktrees for concurrent session safety."
)
_TEST_PROMPT = "how do I safely use git when multiple sessions are running"
_TASK_TYPE = "git_concurrent_session_safety"


@pytest.fixture()
def seeded_db(tmp_path: Path):
    """Create a minimal DB with one seeded procedural_memory row."""
    from genesis.learning.procedural.embedding import pack_embedding
    from genesis.memory.embeddings import EmbeddingProvider

    db_path = tmp_path / "test_genesis.db"
    conn = sqlite3.connect(str(db_path))

    # Create the procedural_memory table (minimal schema)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS procedural_memory (
            id TEXT PRIMARY KEY,
            task_type TEXT,
            principle TEXT,
            principle_embedding BLOB,
            confidence REAL DEFAULT 0.9,
            deprecated INTEGER DEFAULT 0,
            quarantined INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)

    # Embed the principle text using the real embedding chain
    provider = EmbeddingProvider()
    vector = asyncio.run(provider.embed(_PROCEDURE_PRINCIPLE))
    assert vector is not None, "Embedding failed during test setup"

    proc_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO procedural_memory (id, task_type, principle, principle_embedding, confidence) "
        "VALUES (?, ?, ?, ?, ?)",
        (proc_id, _TASK_TYPE, _PROCEDURE_PRINCIPLE, pack_embedding(vector), 0.92),
    )
    conn.commit()
    conn.close()

    return db_path, proc_id


def test_procedure_surfacing_subprocess(seeded_db, tmp_path: Path):
    """Run the hook as subprocess and verify procedure is surfaced."""
    db_path, proc_id = seeded_db

    # Build env: point at test DB, ensure no GENESIS_CC_SESSION skip
    env = os.environ.copy()
    env["GENESIS_DB_PATH"] = str(db_path)
    env.pop("GENESIS_CC_SESSION", None)
    # Qdrant URL — use the real one (needed for memory recall, but
    # we're testing procedure surfacing which uses SQLite only)
    env.setdefault("QDRANT_URL", "http://localhost:6333")

    # Build hook input
    hook_input = json.dumps({
        "session_id": "test-session-001",
        "prompt": _TEST_PROMPT,
    })

    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "proactive_memory_hook.py")],
        input=hook_input,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    # The hook should not crash (exit 0)
    assert result.returncode == 0, (
        f"Hook crashed with exit code {result.returncode}.\n"
        f"stderr: {result.stderr[:2000]}"
    )

    # Check stdout for procedure surfacing
    stdout = result.stdout
    assert "[Procedure |" in stdout or f"id:{proc_id[:8]}" in stdout, (
        f"Procedure not surfaced in output.\n"
        f"stdout: {stdout[:2000]}\n"
        f"stderr: {result.stderr[:500]}"
    )

    # Verify the task type appears
    if "[Procedure |" in stdout:
        assert _TASK_TYPE in stdout or proc_id[:8] in stdout, (
            f"Procedure found but wrong task_type/id.\n"
            f"Expected task_type={_TASK_TYPE} or id prefix={proc_id[:8]}\n"
            f"Got: {stdout[:500]}"
        )


def test_genesis_cc_session_exits_immediately():
    """Hook exits cleanly (code 0) when GENESIS_CC_SESSION=1."""
    env = os.environ.copy()
    env["GENESIS_CC_SESSION"] = "1"

    hook_input = json.dumps({
        "session_id": "test-bg-session",
        "prompt": "this should not be processed",
    })

    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "proactive_memory_hook.py")],
        input=hook_input,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    assert result.returncode == 0
    # No output expected since it exits before processing
    assert "[Procedure |" not in result.stdout
    assert "[Memory |" not in result.stdout


def test_empty_input_exits_cleanly():
    """Hook handles empty stdin gracefully."""
    env = os.environ.copy()
    env.pop("GENESIS_CC_SESSION", None)

    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "proactive_memory_hook.py")],
        input="",
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

    assert result.returncode == 0
