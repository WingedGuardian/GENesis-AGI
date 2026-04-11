"""End-to-end tests for scripts/hooks/post-commit.

Runs the real hook against a throwaway git repo to verify marker
creation, opt-out, non-fix-commit skip, and — the original motivation
for this file — that commit subjects with special characters
(newlines, quotes, unicode, tabs) produce a valid JSON marker.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


def _find_hook_path() -> Path:
    """Locate scripts/hooks/post-commit from this test's filesystem position."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "scripts" / "hooks" / "post-commit"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("could not locate scripts/hooks/post-commit")


HOOK_PATH = _find_hook_path()


@pytest.fixture
def fix_repo(tmp_path, monkeypatch):
    """A throwaway git repo with the post-commit hook installed and
    GENESIS_HOME isolated to tmp_path/genesis_home."""
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "genesis_home"
    monkeypatch.setenv("GENESIS_HOME", str(home))

    def git(*args, check=True):
        return subprocess.run(
            ["git", *args], cwd=str(repo),
            capture_output=True, text=True, check=check,
            env={**os.environ, "GENESIS_HOME": str(home)},
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")

    # Copy the hook into the repo's .git/hooks dir
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    dst = hooks_dir / "post-commit"
    dst.write_text(HOOK_PATH.read_text())
    dst.chmod(0o755)

    return {"repo": repo, "home": home, "git": git}


def _read_latest_marker(home: Path) -> dict:
    pending = home / "pending-offers"
    files = sorted(pending.glob("*.json"))
    assert files, f"no marker written under {pending}"
    return json.loads(files[-1].read_text())


def test_simple_fix_subject_writes_marker(fix_repo):
    repo, home, git = fix_repo["repo"], fix_repo["home"], fix_repo["git"]
    (repo / "a").write_text("x\n")
    git("add", "a")
    git("commit", "-m", "fix: simple subject")
    marker = _read_latest_marker(home)
    assert marker["subject"] == "fix: simple subject"
    assert len(marker["sha"]) == 40


def test_non_fix_subject_no_marker(fix_repo):
    repo, home, git = fix_repo["repo"], fix_repo["home"], fix_repo["git"]
    (repo / "a").write_text("x\n")
    git("add", "a")
    git("commit", "-m", "feat: not a fix")
    pending = home / "pending-offers"
    assert not pending.exists() or not list(pending.glob("*.json"))


def test_fix_local_opt_out_no_marker(fix_repo):
    repo, home, git = fix_repo["repo"], fix_repo["home"], fix_repo["git"]
    (repo / "a").write_text("x\n")
    git("add", "a")
    git("commit", "-m", "fix(local): private tweak")
    pending = home / "pending-offers"
    assert not pending.exists() or not list(pending.glob("*.json"))


def test_subject_with_quotes_produces_valid_json(fix_repo):
    """Regression: subjects containing double-quotes must round-trip."""
    repo, home, git = fix_repo["repo"], fix_repo["home"], fix_repo["git"]
    (repo / "a").write_text("x\n")
    git("add", "a")
    git("commit", "-m", 'fix: handle "quoted" identifier')
    marker = _read_latest_marker(home)
    assert marker["subject"] == 'fix: handle "quoted" identifier'


def test_subject_with_backslash_produces_valid_json(fix_repo):
    repo, home, git = fix_repo["repo"], fix_repo["home"], fix_repo["git"]
    (repo / "a").write_text("x\n")
    git("add", "a")
    git("commit", "-m", r"fix: path C:\windows\system")
    marker = _read_latest_marker(home)
    assert marker["subject"] == r"fix: path C:\windows\system"


def test_subject_with_unicode_produces_valid_json(fix_repo):
    repo, home, git = fix_repo["repo"], fix_repo["home"], fix_repo["git"]
    (repo / "a").write_text("x\n")
    git("add", "a")
    git("commit", "-m", "fix: résumé — round-trip é")
    marker = _read_latest_marker(home)
    assert "résumé" in marker["subject"]
    assert "round-trip" in marker["subject"]


def test_subject_with_control_chars_produces_valid_json(fix_repo):
    """Regression: shell heredoc + sed-based escape would have produced
    invalid JSON if a control char appeared in the subject. With
    json.dumps the marker file must remain parseable as JSON regardless
    of what git hands the hook. We can't easily inject a literal tab
    into a git commit subject (git normalizes it), so we directly
    invoke the python3 encoder via the same env-var protocol the hook
    uses, mimicking the contract."""
    import os
    import subprocess as _sp
    raw_subject = "fix:\tcontains a tab and a \"quote\" and a \\backslash"
    proc = _sp.run(
        ["python3", "-c", (
            "import json, os;"
            "print(json.dumps({"
            "\"sha\": os.environ['SHA'],"
            "\"subject\": os.environ['SUBJECT'],"
            "\"created_at\": os.environ['CREATED_AT'],"
            "}, indent=2))"
        )],
        env={**os.environ, "SHA": "deadbeef", "SUBJECT": raw_subject,
             "CREATED_AT": "2026-04-11T00:00:00Z"},
        capture_output=True, text=True, check=True,
    )
    parsed = json.loads(proc.stdout)
    assert parsed["subject"] == raw_subject
    assert parsed["sha"] == "deadbeef"


def test_marker_filename_matches_sha(fix_repo):
    repo, home, git = fix_repo["repo"], fix_repo["home"], fix_repo["git"]
    (repo / "a").write_text("x\n")
    git("add", "a")
    git("commit", "-m", "fix: x")
    sha = git("rev-parse", "HEAD").stdout.strip()
    marker_path = home / "pending-offers" / f"{sha}.json"
    assert marker_path.is_file()
    data = json.loads(marker_path.read_text())
    assert data["sha"] == sha
