"""Tests for scripts/check_review_depth.py — the CI review-depth advisory.

Advisory by design: exit 0 always; emits a ::warning:: annotation ONLY when the PR
range (base...HEAD) classifies substantial. Hermetic tmp_path repos with a simulated
``refs/remotes/origin/main`` base ref.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS))  # so check_review_depth's lazy `import review_scope` resolves


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_chk = _load("check_review_depth")


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=True
    ).stdout


def _mk(tmp_path: Path, second: dict[str, str]) -> Path:
    """Seed commit becomes origin/main; a second commit adds `second` files."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "seed.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")
    base = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "update-ref", "refs/remotes/origin/main", base)  # simulate the remote base
    for name, content in second.items():
        (repo / name).write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "work")
    return repo


def test_ci_flags_substantial(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
    repo = _mk(tmp_path, {"big.py": "def f():\n" + "".join(f"    y{i} = {i}\n" for i in range(60))})
    rc = _chk.main(cwd=str(repo))
    out = capsys.readouterr().out
    assert rc == 0
    assert "::warning" in out
    assert "SUBSTANTIAL" in out


def test_ci_quiet_on_inline(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
    repo = _mk(tmp_path, {"seed.py": "x = 2\n"})  # 1-line inline change
    rc = _chk.main(cwd=str(repo))
    out = capsys.readouterr().out
    assert rc == 0
    assert "::warning" not in out
    assert "inline" in out


def test_ci_skips_without_base(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
    monkeypatch.delenv("PR_BASE_SHA", raising=False)
    repo = tmp_path / "plain"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")  # a git repo but NO origin/main ref
    (repo / "seed.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")
    rc = _chk.main(cwd=str(repo))
    out = capsys.readouterr().out
    assert rc == 0
    assert "no resolvable base" in out


_BIG = {"big.py": "def f():\n" + "".join(f"    y{i} = {i}\n" for i in range(60))}


def test_ci_resolves_from_github_base_ref(tmp_path, capsys, monkeypatch):
    # The PRIMARY real-CI path: base from origin/$GITHUB_BASE_REF (not the fallback).
    monkeypatch.delenv("PR_BASE_SHA", raising=False)
    repo = _mk(tmp_path, _BIG)
    base = _git(repo, "rev-parse", "refs/remotes/origin/main").strip()
    _git(repo, "update-ref", "refs/remotes/origin/dev", base)
    monkeypatch.setenv("GITHUB_BASE_REF", "dev")
    rc = _chk.main(cwd=str(repo))
    out = capsys.readouterr().out
    assert rc == 0
    assert "::warning" in out and "origin/dev" in out


def test_ci_resolves_from_pr_base_sha(tmp_path, capsys, monkeypatch):
    # Event base SHA takes precedence and works even with origin/* refs removed.
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
    repo = _mk(tmp_path, _BIG)
    base = _git(repo, "rev-parse", "refs/remotes/origin/main").strip()
    _git(repo, "update-ref", "-d", "refs/remotes/origin/main")  # prove the SHA path resolves
    monkeypatch.setenv("PR_BASE_SHA", base)
    rc = _chk.main(cwd=str(repo))
    out = capsys.readouterr().out
    assert rc == 0
    assert "::warning" in out and "SUBSTANTIAL" in out


def test_ci_warns_loudly_on_unknown(tmp_path, capsys, monkeypatch):
    # A git error / unreachable range returns "unknown" — it must FAIL LOUD, never
    # masquerade as "no depth requirement" clearance.
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
    repo = _mk(tmp_path, {"seed.py": "x = 2\n"})
    import review_scope

    monkeypatch.setattr(review_scope, "classify_range_substantiality", lambda *a, **k: "unknown")
    rc = _chk.main(cwd=str(repo))
    out = capsys.readouterr().out
    assert rc == 0
    assert "::warning" in out and "not computable" in out.lower()


def test_ci_fails_open_on_exception(tmp_path, capsys, monkeypatch):
    # An unexpected exception must never fail the build (exit-0-always contract) — but it
    # must ALSO emit a distinct ::warning:: (Codex P2): a green job with no annotation is
    # indistinguishable from "not substantial", so a crash must not read as clearance.
    monkeypatch.delenv("GITHUB_BASE_REF", raising=False)
    repo = _mk(tmp_path, {"seed.py": "x = 2\n"})
    import review_scope

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(review_scope, "classify_range_substantiality", _boom)
    rc = _chk.main(cwd=str(repo))
    out = capsys.readouterr().out
    assert rc == 0
    assert "skipped" in out.lower()
    assert "::warning" in out and "clearance" in out.lower()
