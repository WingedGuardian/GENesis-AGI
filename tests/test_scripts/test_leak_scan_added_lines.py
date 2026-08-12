"""Tests for scripts/ci/leak_scan_added_lines.py — the CI leak-scan range selector.

The range is the security-critical half of the private-pattern gate. These tests
build scratch git repos that reproduce the exact CI topology (a PR **merge ref**
= merge(main@ci, PR-head)) and prove:
  * the stale-base range re-flags a value main added-then-removed (the bug), and
  * anchoring on HEAD^1 (main@ci) scans the PR's own commits only (the fix),
  * a PR-authored add-then-remove is STILL caught (gate not weakened),
  * an unmergeable PR (single-parent HEAD) fails CLOSED when main is unreachable.

Fixture tokens are obviously synthetic and live only in tmp_path scratch repos —
they are never committed to this repo, so the privacy gate does not see them.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "leak_scan_added_lines.py"
_spec = importlib.util.spec_from_file_location("leak_scan_added_lines", _MODULE_PATH)
lsa = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(lsa)

_CI_DIR = _MODULE_PATH.parent
_PPS_PATH = _CI_DIR / "private_pattern_scan.py"

MAIN_SECRET = "MAINONLY_SECRET_a1b2c3"
PR_ADDED = "PRADDED_TOKEN_d4e5f6"


def _git(repo: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t.invalid",
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(repo),
    }
    cp = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return cp.stdout.strip()


def _commit(repo: Path, fname: str, content: str, msg: str) -> str:
    (repo / fname).write_text(content, encoding="utf-8")
    _git(repo, "add", fname)
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _rm_commit(repo: Path, fname: str, msg: str) -> str:
    _git(repo, "rm", "-q", fname)
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def merge_ref_repo(tmp_path: Path) -> dict:
    """Build main (add-then-remove a secret) + a PR branch, then the CI merge ref.

    main:  A ─ B(+secret) ─ C(-secret) ─ D(+maindata)
    pr:    A ─ E(+pr_value)
    HEAD = M = merge(D, E)   (parent1 = D = main@ci, parent2 = E = PR head)
    base.sha (stale, frozen at PR creation) = A
    """
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    a = _commit(repo, "base.txt", "hello\n", "A base")
    _commit(repo, "leak.txt", f"{MAIN_SECRET}\n", "B add secret on main")
    _rm_commit(repo, "leak.txt", "C remove secret on main")
    d = _commit(repo, "main2.txt", "maindata\n", "D more main")
    # PR branch from A
    _git(repo, "checkout", "-q", "-b", "pr", a)
    e = _commit(repo, "pr.txt", f"{PR_ADDED}\n", "E add pr value")
    # CI merge ref: first parent = main tip (D), second = PR head (E)
    _git(repo, "checkout", "-q", d)
    _git(repo, "merge", "-q", "--no-ff", "-m", "M merge ref", e)
    m = _git(repo, "rev-parse", "HEAD")
    return {"repo": repo, "A": a, "D": d, "E": e, "M": m}


def test_merge_ref_uses_head_first_parent(merge_ref_repo):
    repo = str(merge_ref_repo["repo"])
    spec = lsa.resolve_scan_spec("pull_request", "", "", cwd=repo)
    assert spec == ("range", "HEAD^1..HEAD")


def test_fix_excludes_main_addthenremove_includes_pr(merge_ref_repo):
    repo = str(merge_ref_repo["repo"])
    out = lsa.added_lines(("range", "HEAD^1..HEAD"), cwd=repo)
    assert PR_ADDED in out  # the PR's own addition is scanned
    assert MAIN_SECRET not in out  # main's add-then-removed value is NOT re-flagged


def test_control_old_stale_base_range_reflags_main_secret(merge_ref_repo):
    """Proves the bug: the OLD range base.sha..HEAD DID re-flag main's removed value."""
    repo = merge_ref_repo["repo"]
    base = merge_ref_repo["A"]
    old = subprocess.run(
        ["git", "log", "-p", "--no-merges", f"{base}..HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    old_added = "\n".join(x for x in old.splitlines() if x.startswith("+"))
    assert MAIN_SECRET in old_added  # the false positive the fix removes


def test_pr_authored_add_then_remove_still_caught(tmp_path: Path):
    """Gate not weakened: a value the PR adds then removes is still in range."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    a = _commit(repo, "base.txt", "hello\n", "A")
    d = _commit(repo, "main2.txt", "maindata\n", "D main only")
    _git(repo, "checkout", "-q", "-b", "pr", a)
    _commit(repo, "pr.txt", f"{PR_ADDED}\n", "E add pr value")
    e2 = _rm_commit(repo, "pr.txt", "F remove pr value within the PR")
    _git(repo, "checkout", "-q", d)
    _git(repo, "merge", "-q", "--no-ff", "-m", "M", e2)
    out = lsa.added_lines(("range", "HEAD^1..HEAD"), cwd=str(repo))
    assert PR_ADDED in out  # add-then-remove-within-PR is still scanned


def test_single_parent_head_falls_back_to_merge_base(tmp_path: Path):
    """Unmergeable PR (PR head checked out): anchor on live main via merge-base."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    a = _commit(repo, "base.txt", "hello\n", "A")
    d = _commit(repo, "main2.txt", "maindata\n", "D main")
    _git(repo, "checkout", "-q", "-b", "pr", a)
    _commit(repo, "pr.txt", f"{PR_ADDED}\n", "E pr value")
    # Simulate origin/main tracking ref at D; check out the PR head (1 parent).
    _git(repo, "update-ref", "refs/remotes/origin/main", d)
    _git(repo, "checkout", "-q", "pr")
    spec = lsa.resolve_scan_spec("pull_request", "", "", cwd=str(repo))
    assert spec[0] == "range" and spec[1].endswith("..HEAD")
    assert lsa.head_parent_count(cwd=str(repo)) == 1
    out = lsa.added_lines(spec, cwd=str(repo))
    assert PR_ADDED in out


def test_single_parent_head_no_main_fails_closed(tmp_path: Path):
    """No merge ref AND no reachable main → RangeError (fail closed, never empty)."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "base.txt", "hello\n", "A")
    _commit(repo, "pr.txt", f"{PR_ADDED}\n", "E")  # linear, 1-parent HEAD, no origin/main
    with pytest.raises(lsa.RangeError):
        lsa.resolve_scan_spec("pull_request", "", "", cwd=str(repo))


def test_push_path_scans_before_to_head(tmp_path: Path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    before = _commit(repo, "base.txt", "hello\n", "A before")
    head = _commit(repo, "pr.txt", f"{PR_ADDED}\n", "B pushed")
    spec = lsa.resolve_scan_spec("push", before, head, cwd=str(repo))
    assert spec == ("range", f"{before}..{head}")
    assert PR_ADDED in lsa.added_lines(spec, cwd=str(repo))


def test_new_branch_unknown_before_shows_tip(tmp_path: Path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    head = _commit(repo, "pr.txt", f"{PR_ADDED}\n", "A tip")
    zeros = "0000000000000000000000000000000000000000"
    spec = lsa.resolve_scan_spec("push", zeros, head, cwd=str(repo))
    assert spec == ("show", head)
    assert PR_ADDED in lsa.added_lines(spec, cwd=str(repo))


def test_main_maps_range_error_to_fail_closed(monkeypatch, capsys):
    def _boom(*a, **k):
        raise lsa.RangeError("simulated unresolvable")

    monkeypatch.setattr(lsa, "resolve_scan_spec", _boom)
    rc = lsa.main([])
    assert rc == lsa.EXIT_UNRESOLVABLE
    assert "Failing closed" in capsys.readouterr().err


# --- E2E: the two-script gate pipeline (range selector | pattern scan) --------


def _run_gate_pipeline(repo: Path, patterns_file: Path) -> int:
    """Compose the real CI gate: leak_scan_added_lines.py | private_pattern_scan.py."""
    import os
    import sys

    env = {**os.environ, "EVENT_NAME": "pull_request", "HOME": str(repo)}
    p1 = subprocess.run(
        [sys.executable, str(_MODULE_PATH)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    if p1.returncode != 0:
        return p1.returncode  # fail-closed range error propagates as the gate failure
    p2 = subprocess.run(
        [sys.executable, str(_PPS_PATH), "--patterns", str(patterns_file)],
        input=p1.stdout,
        capture_output=True,
        text=True,
    )
    return p2.returncode


def _merge_ref_with(repo: Path, pr_file: str | None) -> None:
    """main adds-then-removes a MAINLEAK value; optional PR file added on the branch."""
    _git(repo, "init", "-q", "-b", "main")
    a = _commit(repo, "base.txt", "hello\n", "A")
    _commit(repo, "m.txt", "cfg MAINLEAK_42 x\n", "B add mainleak on main")
    _rm_commit(repo, "m.txt", "C remove mainleak on main")
    d = _commit(repo, "main2.txt", "maindata\n", "D main")
    _git(repo, "checkout", "-q", "-b", "pr", a)
    if pr_file:
        _commit(repo, "pr.txt", pr_file, "E pr change")
    else:
        _commit(repo, "ok.txt", "harmless\n", "E clean")
    e = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", d)
    _git(repo, "merge", "-q", "--no-ff", "-m", "M", e)


def test_e2e_pr_introduced_leak_blocks(tmp_path: Path):
    repo = tmp_path / "r"
    repo.mkdir()
    pf = tmp_path / "patterns.txt"
    pf.write_text("MAINLEAK_[0-9]+\nPRLEAK_[0-9]+\n", encoding="utf-8")
    _merge_ref_with(repo, "token PRLEAK_99 here\n")
    assert _run_gate_pipeline(repo, pf) == 1  # EXIT_LEAK — PR's own leak is caught


def test_e2e_main_removed_value_stays_clean(tmp_path: Path):
    repo = tmp_path / "r"
    repo.mkdir()
    pf = tmp_path / "patterns.txt"
    pf.write_text("MAINLEAK_[0-9]+\nPRLEAK_[0-9]+\n", encoding="utf-8")
    _merge_ref_with(repo, None)  # PR clean; main added-then-removed MAINLEAK
    assert _run_gate_pipeline(repo, pf) == 0  # CLEAN — main history not re-flagged
