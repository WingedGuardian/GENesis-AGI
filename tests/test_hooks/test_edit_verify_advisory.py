"""Tests for scripts/hooks/edit_verify_advisory.py (PostToolUse Edit|Write).

The hook is run as a real subprocess with fixture stdin JSON — the same shape
Claude Code delivers — so these tests cover the actual contract: mutate via
ruff format/autofix, report ONLY unfixable diagnostics via the PostToolUse
``hookSpecificOutput.additionalContext`` JSON channel, and fail open (exit 0)
on every malformed or out-of-scope input.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent.parent
HOOK = REPO_DIR / "scripts" / "hooks" / "edit_verify_advisory.py"


def _run_hook(stdin_text: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _invoke(file_path: str, tool_name: str = "Edit") -> subprocess.CompletedProcess:
    payload = {
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path},
        "session_id": "test-session",
    }
    return _run_hook(json.dumps(payload))


def _context_of(proc: subprocess.CompletedProcess) -> str | None:
    if not proc.stdout.strip():
        return None
    out = json.loads(proc.stdout)
    return out["hookSpecificOutput"]["additionalContext"]


def test_autofixable_only_is_silent_and_mutates(tmp_path):
    """Import order + spacing are fixed on disk; nothing is reported."""
    f = tmp_path / "fixable.py"
    f.write_text("import sys\nimport os\n\nprint(os.name, sys.argv)\n")
    proc = _invoke(str(f))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""
    assert f.read_text().startswith("import os\nimport sys\n")


def test_unfixable_diagnostic_reported_as_additional_context(tmp_path):
    f = tmp_path / "broken.py"
    f.write_text("def f(x):\n    return undefined_name\n")
    proc = _invoke(str(f))
    assert proc.returncode == 0
    ctx = _context_of(proc)
    assert ctx is not None
    assert "F821" in ctx
    assert "[ruff advisory]" in ctx
    assert "Advisory only" in ctx  # never framed as blocking


def test_write_tool_also_covered(tmp_path):
    f = tmp_path / "broken.py"
    f.write_text("def f():\n    return undefined_name\n")
    proc = _invoke(str(f), tool_name="Write")
    assert proc.returncode == 0
    assert _context_of(proc) is not None


def test_diagnostic_cap_honored(tmp_path):
    """A file with many unfixable issues reports at most the cap + a marker."""
    lines = [f"def f{i}():\n    return undef_{i}\n" for i in range(20)]
    f = tmp_path / "many.py"
    f.write_text("\n".join(lines))
    proc = _invoke(str(f))
    ctx = _context_of(proc)
    assert ctx is not None
    diag_lines = [ln for ln in ctx.splitlines() if ":" in ln and "F821" in ln]
    assert len(diag_lines) <= 12
    assert "more" in ctx


def test_non_python_file_silent(tmp_path):
    f = tmp_path / "notes.md"
    f.write_text("# hello\n")
    proc = _invoke(str(f))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_missing_file_silent(tmp_path):
    proc = _invoke(str(tmp_path / "nope.py"))
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_other_tools_silent(tmp_path):
    f = tmp_path / "broken.py"
    f.write_text("def f():\n    return undefined_name\n")
    proc = _invoke(str(f), tool_name="Read")
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_malformed_stdin_fails_open():
    for bad in ("", "not json", '{"tool_name": "Edit"}', '{"tool_input": 5}'):
        proc = _run_hook(bad)
        assert proc.returncode == 0
        assert proc.stdout.strip() == ""


def test_registered_in_settings_json():
    """The Edit|Write PostToolUse matcher runs THIS script (replacing the old
    inline silent fixer) — one hook, because parallel hooks on the same
    matcher would race the autofix."""
    settings = json.loads((REPO_DIR / ".claude" / "settings.json").read_text())
    entries = [
        e for e in settings["hooks"]["PostToolUse"]
        if e.get("matcher") in ("Edit|Write", "Write|Edit")
    ]
    commands = [h["command"] for e in entries for h in e["hooks"]]
    advisory = [c for c in commands if "edit_verify_advisory.py" in c]
    assert len(advisory) == 1
    assert not any("ruff" in c for c in commands if "edit_verify_advisory" not in c)
