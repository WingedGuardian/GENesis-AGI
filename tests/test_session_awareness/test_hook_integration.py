"""Subprocess integration: the ambient fold must not change hook output.

Runs scripts/proactive_memory_hook.py as a real subprocess (the same way
Claude Code invokes it) against an isolated HOME + a STUB recall server, and
proves:

1. stdout is byte-identical with the ambient layer enabled vs disabled
   (GENESIS_SESSION_AWARENESS_DISABLED=1) across a same-theme sequence — the
   PR1 zero-behavior-change contract.
2. The enabled run accumulates session_theme.json (ema_turns == folded turns)
   and records a fire once the theme settles.
3. Harness-envelope prompts do not fold.

Since the thin-client flip, the prompt embedding the fold consumes comes from
the genesis-server recall response (the hook no longer embeds locally), so the
stub server supplies a fixed ``embedding`` — no live embedding backend needed.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent.parent
HOOK = REPO_DIR / "scripts" / "proactive_memory_hook.py"
SRC_DIR = REPO_DIR / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Fixed prompt embedding returned by the stub — a same-theme sequence folds to a
# settled EMA and fires. Dimension is arbitrary; the fold only needs consistency.
_EMBED = [0.05] * 256

_STUB_RESPONSE = {
    "status": "ok",
    "lines": [],  # no memory lines → stdout is just the session-local awareness
    "results": [],
    "procedure": None,
    "shadow": {},
    "budget": {"stance": "general", "limit": 3, "kb_slots": 1},
    "embedding": _EMBED,
    "timings_ms": {"embed": 1.0, "total": 100.0},
    "engine": {
        "reranked": False,
        "graph_expansion": "off",
        "intent": "general",
        "profile": "cc_hook",
    },
}

_PROMPTS = [
    "how does the genesis memory retrieval pipeline rank episodic memories",
    "explain scoring in the memory retrieval pipeline for episodic recall",
    "why does memory retrieval ranking boost linked episodic memories",
    "show the retrieval pipeline ranking weights for episodic memory",
    "compare retrieval ranking scores across episodic memory pipelines",
]

_SESSION = "ambient-itest-1"


@contextmanager
def _stub_server(response: dict):
    body = json.dumps(response).encode()

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _make_home(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """Isolated HOME + an empty DB (the stub supplies recall; DB just needs to exist)."""
    home = tmp_path / name
    (home / ".genesis").mkdir(parents=True)
    db_path = home / "genesis.db"
    sqlite3.connect(str(db_path)).close()
    return home, db_path


def _run_hook(home: Path, db_path: Path, base_url: str, prompt: str, *, disabled: bool) -> str:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["GENESIS_DB_PATH"] = str(db_path)
    env["GENESIS_PROACTIVE_HOOK_URL"] = base_url
    env["GENESIS_PROACTIVE_HOOK_MODE"] = "server"
    env.pop("GENESIS_CC_SESSION", None)
    if disabled:
        env["GENESIS_SESSION_AWARENESS_DISABLED"] = "1"
    else:
        env.pop("GENESIS_SESSION_AWARENESS_DISABLED", None)
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"prompt": prompt, "session_id": _SESSION}),
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=str(REPO_DIR),
    )
    assert proc.returncode == 0, proc.stderr[:2000]
    return proc.stdout


def _theme_file(home: Path) -> Path:
    return home / ".genesis" / "sessions" / _SESSION / "session_theme.json"


def test_stdout_identical_and_theme_accumulates(tmp_path):
    home_on, db_on = _make_home(tmp_path, "on")
    home_off, db_off = _make_home(tmp_path, "off")

    with _stub_server(_STUB_RESPONSE) as base_url:
        for prompt in _PROMPTS:
            out_on = _run_hook(home_on, db_on, base_url, prompt, disabled=False)
            out_off = _run_hook(home_off, db_off, base_url, prompt, disabled=True)
            assert out_on == out_off  # byte-identical stdout, turn by turn

    # Enabled home accumulated the theme from the server-supplied embedding.
    theme = json.loads(_theme_file(home_on).read_text())
    assert theme["ema_turns"] >= 3
    assert theme["fired_count"] >= 1  # same-theme sequence settles → fire
    assert theme["ema"] is not None
    # …disabled home never wrote a statefile.
    assert not _theme_file(home_off).exists()


def test_envelope_prompt_does_not_fold(tmp_path):
    home, db = _make_home(tmp_path, "env")
    with _stub_server(_STUB_RESPONSE) as base_url:
        _run_hook(
            home,
            db,
            base_url,
            "<task-notification>background agent finished memory retrieval task"
            "</task-notification>",
            disabled=False,
        )
    assert not _theme_file(home).exists()
