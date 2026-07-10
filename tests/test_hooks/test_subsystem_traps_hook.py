"""Tests for scripts/hooks/subsystem_traps_hook.py (PostToolUse Edit|Write).

Runs the hook as a real subprocess with fixture stdin JSON and a synthetic
CURRENT.md (via GENESIS_CURRENT_MD_PATH) + isolated session dir (via
GENESIS_SESSIONS_DIR), covering: subsystem match + trap extraction,
once-per-session-per-subsystem dedup, non-src silence, garbled-file
fail-open, and the payload cap.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent.parent
HOOK = REPO_DIR / "scripts" / "hooks" / "subsystem_traps_hook.py"

FIXTURE_MD = """# Map

## 1. Memory — retrieval and store

```yaml subsystem-map
entry: memory
modules: [memory, qdrant]
verified: abc123 2026-07-10
```

- **CRAG** lives in the MCP wrapper only.
- **Recall is read-MOSTLY** — hits bump usage counters.
  second line of this bullet must NOT appear.

**Do not touch:** the drain's shadow hardwiring. **Trap:** FTS5 fallback.

## 2. Execution

```yaml subsystem-map
entry: execution-cc
modules: [cc]
verified: abc123 2026-07-10
```

- Profile machinery lives in direct_session.py.
"""


def _run(payload: dict, tmp_path: Path, md_text: str = FIXTURE_MD) -> subprocess.CompletedProcess:
    md = tmp_path / "CURRENT.md"
    md.write_text(md_text)
    env = {
        "GENESIS_CURRENT_MD_PATH": str(md),
        "GENESIS_SESSIONS_DIR": str(tmp_path / "sessions"),
        "PATH": "/usr/bin:/bin",
    }
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def _edit(file_path: str, session_id: str = "sess-1") -> dict:
    return {
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path},
        "session_id": session_id,
    }


def _context(proc: subprocess.CompletedProcess) -> str | None:
    if not proc.stdout.strip():
        return None
    return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]


def test_memory_edit_injects_entry_and_traps(tmp_path):
    proc = _run(_edit("/repo/src/genesis/memory/retrieval.py"), tmp_path)
    assert proc.returncode == 0
    ctx = _context(proc)
    assert ctx is not None
    assert "'memory'" in ctx and "Memory — retrieval and store" in ctx
    assert "Do not touch:" in ctx and "CRAG" in ctx
    assert "second line of this bullet" not in ctx  # first-line-only bullets


def test_second_edit_same_subsystem_silent(tmp_path):
    assert _context(_run(_edit("/r/src/genesis/memory/store.py"), tmp_path))
    proc = _run(_edit("/r/src/genesis/memory/drift.py"), tmp_path)
    assert proc.returncode == 0 and proc.stdout.strip() == ""


def test_different_subsystem_same_session_injects(tmp_path):
    assert _context(_run(_edit("/r/src/genesis/memory/store.py"), tmp_path))
    ctx = _context(_run(_edit("/r/src/genesis/cc/invoker.py"), tmp_path))
    assert ctx is not None and "Execution" in ctx


def test_qdrant_module_maps_to_memory_entry(tmp_path):
    ctx = _context(_run(_edit("/r/src/genesis/qdrant/collections.py"), tmp_path))
    assert ctx is not None and "Memory" in ctx


def test_non_src_paths_silent(tmp_path):
    for path in ("/r/scripts/foo.py", "/r/tests/test_memory/test_x.py",
                 "/r/docs/notes.md", "/r/src/genesis"):
        proc = _run(_edit(path), tmp_path)
        assert proc.returncode == 0 and proc.stdout.strip() == "", path


def test_garbled_current_md_fails_open(tmp_path):
    proc = _run(
        _edit("/r/src/genesis/memory/store.py"), tmp_path,
        md_text="not markdown \x00 at all",
    )
    assert proc.returncode == 0 and proc.stdout.strip() == ""


def test_payload_cap(tmp_path):
    bullets = "\n".join(f"- trap number {i} " + "x" * 300 for i in range(20))
    md = FIXTURE_MD.replace("- **CRAG** lives in the MCP wrapper only.", bullets)
    proc = _run(_edit("/r/src/genesis/memory/store.py"), tmp_path, md_text=md)
    ctx = _context(proc)
    assert ctx is not None and len(ctx) <= 1200


def test_traversal_session_id_silent(tmp_path):
    proc = _run(_edit("/r/src/genesis/memory/store.py", session_id="../evil"), tmp_path)
    assert proc.returncode == 0 and proc.stdout.strip() == ""


# The real platform-data entry: modules wrap onto a continuation line, and
# loose top-level modules are named WITH `.py` (matching check_subsystem_map).
_WRAPPED_MD = """# Map

## 12. Platform & data

```yaml subsystem-map
entry: platform-data
modules: [db, runtime, resilience, observability, security, codebase,
          restore, util, env.py, _config_overlay.py]
verified: abc123 2026-07-10
```

- **Do not touch:** the migration runner's transaction proxy.
"""


def test_multiline_modules_continuation_dir_module(tmp_path):
    """A dir module on the wrapped continuation line still matches (parser
    reads until ']', not just the first modules: line)."""
    for module in ("util", "restore"):
        proc = _run(
            _edit(f"/r/src/genesis/{module}/x.py", session_id=module),
            tmp_path, md_text=_WRAPPED_MD,
        )
        ctx = _context(proc)
        assert ctx is not None, module
        assert "Platform & data" in ctx


def test_loose_top_level_py_module_matches_with_suffix(tmp_path):
    """A loose top-level module edited as src/genesis/env.py matches the
    map's `env.py` entry — the segment is kept verbatim, not .py-stripped."""
    for i, module in enumerate(("env.py", "_config_overlay.py")):
        proc = _run(
            _edit(f"/r/src/genesis/{module}", session_id=f"loose-{i}"),
            tmp_path, md_text=_WRAPPED_MD,
        )
        ctx = _context(proc)
        assert ctx is not None, module
        assert "Platform & data" in ctx


def test_malformed_stdin_fails_open(tmp_path):
    md = tmp_path / "CURRENT.md"
    md.write_text(FIXTURE_MD)
    proc = subprocess.run(
        [sys.executable, str(HOOK)], input="not json", capture_output=True,
        text=True, timeout=30,
        env={"GENESIS_CURRENT_MD_PATH": str(md), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0 and proc.stdout.strip() == ""


def test_registered_in_settings_json():
    settings = json.loads((REPO_DIR / ".claude" / "settings.json").read_text())
    commands = [
        h["command"]
        for e in settings["hooks"]["PostToolUse"]
        for h in e.get("hooks", [])
        if e.get("matcher") in ("Write|Edit", "Edit|Write")
    ]
    assert any("subsystem_traps_hook.py" in c for c in commands)
