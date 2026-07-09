"""Subprocess integration: the ambient fold must not change hook output.

Runs scripts/proactive_memory_hook.py as a real subprocess (the same way
Claude Code invokes it) against an isolated HOME + minimal DB, and proves:

1. stdout is byte-identical with the ambient layer enabled vs disabled
   (GENESIS_SESSION_AWARENESS_DISABLED=1) across a 4-turn same-theme
   sequence — the PR1 zero-behavior-change contract.
2. The enabled run accumulates session_theme.json (ema_turns == embedded
   turns) and records a fire once the theme settles.
3. Harness-envelope prompts do not fold.

Requires an embedding backend (the fold only runs when the hook obtains
a vector); skipped otherwise, mirroring test_proactive_hook_subprocess.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parent.parent.parent
HOOK = REPO_DIR / "scripts" / "proactive_memory_hook.py"
SRC_DIR = REPO_DIR / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def _embedding_available() -> bool:
    try:
        from genesis.memory.embeddings import EmbeddingProvider

        async def _check():
            provider = EmbeddingProvider()
            return await asyncio.wait_for(provider.embed("test"), timeout=5.0)

        result = asyncio.run(_check())
        return result is not None and len(result) > 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _embedding_available(),
    reason="No embedding backend available (Ollama/DeepInfra/DashScope)",
)

_PROMPTS = [
    "how does the genesis memory retrieval pipeline rank episodic memories",
    "explain scoring in the memory retrieval pipeline for episodic recall",
    "why does memory retrieval ranking boost linked episodic memories",
    "show the retrieval pipeline ranking weights for episodic memory",
    "compare retrieval ranking scores across episodic memory pipelines",
]

_SESSION = "ambient-itest-1"


def _make_home(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """Isolated HOME with the install's config + a minimal DB."""
    home = tmp_path / name
    (home / ".genesis").mkdir(parents=True)
    real_config = Path.home() / ".genesis" / "config" / "genesis.yaml"
    if real_config.exists():
        cfg = home / ".genesis" / "config"
        cfg.mkdir(parents=True, exist_ok=True)
        shutil.copy(real_config, cfg / "genesis.yaml")
    db_path = home / "genesis.db"
    sqlite3.connect(str(db_path)).close()  # empty DB: hook runs, finds nothing
    return home, db_path


def _run_hook(home: Path, db_path: Path, prompt: str, *, disabled: bool) -> str:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["GENESIS_DB_PATH"] = str(db_path)
    env.pop("GENESIS_CC_SESSION", None)
    # Dead Qdrant on purpose: test runs must NEVER touch the production
    # vector store (the hook's _increment_retrieved would bump real
    # points' retrieved_count). Also makes stdout deterministic — the
    # hook degrades to FTS-only against the empty tmp DB.
    env["QDRANT_URL"] = "http://127.0.0.1:1"
    if disabled:
        env["GENESIS_SESSION_AWARENESS_DISABLED"] = "1"
    else:
        env.pop("GENESIS_SESSION_AWARENESS_DISABLED", None)
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"prompt": prompt, "session_id": _SESSION}),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=str(REPO_DIR),
    )
    assert proc.returncode == 0
    return proc.stdout


def _theme_file(home: Path) -> Path:
    return home / ".genesis" / "sessions" / _SESSION / "session_theme.json"


def test_stdout_identical_and_theme_accumulates(tmp_path):
    home_on, db_on = _make_home(tmp_path, "on")
    home_off, db_off = _make_home(tmp_path, "off")

    for prompt in _PROMPTS:
        out_on = _run_hook(home_on, db_on, prompt, disabled=False)
        out_off = _run_hook(home_off, db_off, prompt, disabled=True)
        assert out_on == out_off  # byte-identical stdout, turn by turn

    # Enabled home accumulated the theme… (>= 3: a turn whose embed
    # exceeded the hook's 3s budget folds nothing — tolerated, the
    # sequence has slack for one such miss)
    theme = json.loads(_theme_file(home_on).read_text())
    assert theme["ema_turns"] >= 3
    assert theme["fired_count"] >= 1  # same-theme sequence settles → fire
    assert theme["ema"] is not None
    # …disabled home never wrote a statefile.
    assert not _theme_file(home_off).exists()


def test_envelope_prompt_does_not_fold(tmp_path):
    home, db = _make_home(tmp_path, "env")
    _run_hook(
        home, db,
        "<task-notification>background agent finished memory retrieval task"
        "</task-notification>",
        disabled=False,
    )
    assert not _theme_file(home).exists()
