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

import pytest

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


# ── Baseline-gated mutation: no whole-file reflow of legacy lines ─────────
#
# The mutation (ruff format / check --fix) rewrites the WHOLE file, so on a
# file whose committed baseline is not already ruff-clean it reflows unrelated
# legacy lines — inflating the PR diff and colliding with concurrent worktrees.
# These lock in that each mutation runs ONLY when the committed baseline is
# already clean for that tool (so it can touch only the just-edited region),
# while new / non-repo files still format freely.


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def gitrepo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "-c", "init.defaultBranch=main", "init", "-q")
    _git(r, "config", "user.email", "t@e.st")
    _git(r, "config", "user.name", "tester")
    (r / "seed.py").write_text("seed = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "seed")
    return r


def test_format_dirty_baseline_not_reflowed(gitrepo: Path):
    """Committed file is format-dirty (but lint-clean). An edit must NOT trigger
    a whole-file `ruff format` — the messy legacy lines stay byte-for-byte."""
    f = gitrepo / "legacy.py"
    messy = "x = {'a':1,'b':2}\ny = [1,2,  3]\n"  # ruff format WOULD rewrite this
    f.write_text(messy)
    _git(gitrepo, "add", "-A")
    _git(gitrepo, "commit", "-qm", "legacy")
    f.write_text(messy + "\n\ndef added():\n    return 1\n")  # the edit
    proc = _invoke(str(f))
    assert proc.returncode == 0
    after = f.read_text()
    assert "{'a':1,'b':2}" in after  # legacy dict untouched (no reflow)
    assert "[1,2,  3]" in after


def test_clean_baseline_edit_region_formatted(gitrepo: Path):
    """Committed file is fully clean → formatting only touches the just-added
    line, which is safe (no legacy to disturb) → mutation runs."""
    f = gitrepo / "clean.py"
    f.write_text("x = 1\n")
    _git(gitrepo, "add", "-A")
    _git(gitrepo, "commit", "-qm", "clean")
    f.write_text("x = 1\ny = {'a':1}\n")  # add a format-dirty line
    proc = _invoke(str(f))
    assert proc.returncode == 0
    assert '{"a": 1}' in f.read_text()  # clean baseline → formatted


def test_new_untracked_file_formatted(gitrepo: Path):
    """A file with no committed baseline has nothing legacy to disturb → format."""
    f = gitrepo / "brand_new.py"
    f.write_text("y = {'a':1}\n")  # never committed
    proc = _invoke(str(f))
    assert proc.returncode == 0
    assert '{"a": 1}' in f.read_text()


def test_git_unavailable_fails_safe(tmp_path: Path):
    """S2: if `git` cannot run (missing binary) the baseline is UNKNOWN — a
    tracked file might be dirty — so the mutation must NOT fire (fail-safe),
    rather than treating it as a new file and reflowing it."""
    f = tmp_path / "messy.py"
    original = "y = {'a':1}\n"  # ruff format WOULD normalize this
    f.write_text(original)
    payload = {"tool_name": "Edit", "tool_input": {"file_path": str(f)}, "session_id": "t"}
    # A PATH with no `git` on it → subprocess FileNotFoundError inside the hook.
    # ruff is resolved via sys.executable's dir (absolute), so it still runs.
    import os

    env = {**os.environ, "PATH": "/nonexistent-dir"}
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0
    assert f.read_text() == original  # NOT reflowed — fail-safe on git error


def test_lint_dirty_baseline_autofix_skipped(gitrepo: Path):
    """Committed file is lint-dirty (unused import) but format-clean → the
    localized `check --fix` is skipped, so the legacy unused import is NOT
    auto-removed (that would be an unrelated diff on someone's edit)."""
    f = gitrepo / "lintbad.py"
    f.write_text("import os\nx = 1\n")  # F401 unused — lint-dirty, format-clean
    _git(gitrepo, "add", "-A")
    _git(gitrepo, "commit", "-qm", "lintbad")
    f.write_text("import os\nx = 1\ny = 2\n")  # the edit
    proc = _invoke(str(f))
    assert proc.returncode == 0
    assert "import os" in f.read_text()  # autofix skipped → import preserved


def test_registered_in_settings_json():
    """The Edit|Write PostToolUse matcher runs THIS script (replacing the old
    inline silent fixer) — one hook, because parallel hooks on the same
    matcher would race the autofix."""
    settings = json.loads((REPO_DIR / ".claude" / "settings.json").read_text())
    entries = [
        e
        for e in settings["hooks"]["PostToolUse"]
        if e.get("matcher") in ("Edit|Write", "Write|Edit")
    ]
    commands = [h["command"] for e in entries for h in e["hooks"]]
    advisory = [c for c in commands if "edit_verify_advisory.py" in c]
    assert len(advisory) == 1
    assert not any("ruff" in c for c in commands if "edit_verify_advisory" not in c)
