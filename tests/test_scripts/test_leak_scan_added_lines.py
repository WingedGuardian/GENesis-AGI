"""Tests for scripts/ci/leak_scan_added_lines.py — the CI leak-scan range selector.

The range is the security-critical half of the private-pattern gate. These tests
build scratch git repos that reproduce the CI topology (a PR **merge ref** =
merge(main@ci, PR-head), and its awkward cousins) and prove:
  * the stale-base range re-flags a value main added-then-removed (the bug), and
  * anchoring on merge-base(origin/main, HEAD) scans the PR's own commits only,
  * a PR-authored add-then-remove is STILL caught (gate not weakened),
  * an unmergeable PR whose HEAD is itself a merge commit is STILL fully scanned
    (parent-count is not a "is this the merge ref" signal — Codex P1),
  * an unmergeable PR with no reachable main fails CLOSED,
  * content after a Unicode line separator (U+0085) is preserved (Codex P1), and
  * a non-UTF-8 byte in a diff does not crash the gate (Codex P2).

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
        "GIT_AUTHOR_EMAIL": "ci@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "ci@example.com",
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


def _set_origin_main(repo: Path, sha: str) -> None:
    """Simulate the origin/main remote-tracking ref a real checkout provides."""
    _git(repo, "update-ref", "refs/remotes/origin/main", sha)


@pytest.fixture
def merge_ref_repo(tmp_path: Path) -> dict:
    """Build main (add-then-remove a secret) + a PR branch, then the CI merge ref.

    main:  A ─ B(+secret) ─ C(-secret) ─ D(+maindata)
    pr:    A ─ E(+pr_value)
    HEAD = M = merge(D, E)   (parent1 = D = main@ci, parent2 = E = PR head)
    origin/main = D
    """
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    a = _commit(repo, "base.txt", "hello\n", "A base")
    _commit(repo, "leak.txt", f"{MAIN_SECRET}\n", "B add secret on main")
    _rm_commit(repo, "leak.txt", "C remove secret on main")
    d = _commit(repo, "main2.txt", "maindata\n", "D more main")
    _set_origin_main(repo, d)
    _git(repo, "checkout", "-q", "-b", "pr", a)
    e = _commit(repo, "pr.txt", f"{PR_ADDED}\n", "E add pr value")
    _git(repo, "checkout", "-q", d)
    _git(repo, "merge", "-q", "--no-ff", "-m", "M merge ref", e)
    m = _git(repo, "rev-parse", "HEAD")
    return {"repo": repo, "A": a, "D": d, "E": e, "M": m}


def test_pull_request_resolves_to_merge_base_range(merge_ref_repo):
    repo = str(merge_ref_repo["repo"])
    spec = lsa.resolve_scan_spec("pull_request", "", "", cwd=repo)
    # merge-base(origin/main=D, HEAD=M) == D (M's first parent).
    assert spec == ("range", f"{merge_ref_repo['D']}..HEAD")


def test_fix_excludes_main_addthenremove_includes_pr(merge_ref_repo):
    repo = str(merge_ref_repo["repo"])
    spec = lsa.resolve_scan_spec("pull_request", "", "", cwd=repo)
    out = lsa.added_lines(spec, cwd=repo)
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
    _set_origin_main(repo, d)
    _git(repo, "checkout", "-q", "-b", "pr", a)
    _commit(repo, "pr.txt", f"{PR_ADDED}\n", "E add pr value")
    _rm_commit(repo, "pr.txt", "F remove pr value within the PR")
    e2 = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", d)
    _git(repo, "merge", "-q", "--no-ff", "-m", "M", e2)
    spec = lsa.resolve_scan_spec("pull_request", "", "", cwd=str(repo))
    out = lsa.added_lines(spec, cwd=str(repo))
    assert PR_ADDED in out  # add-then-remove-within-PR is still scanned


def test_merge_headed_unmergeable_pr_still_fully_scanned(tmp_path: Path):
    """Codex P1: an unmergeable PR whose HEAD is itself a merge commit.

    Parent-count is NOT a "this is the CI merge ref" signal. The old HEAD^1..HEAD
    heuristic would exclude the PR's first-parent history and MISS a secret there;
    merge-base(origin/main, HEAD) scans all of the PR's own commits.
    """
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    a = _commit(repo, "base.txt", "hello\n", "A")
    d = _commit(repo, "main2.txt", "maindata\n", "D main tip")
    _set_origin_main(repo, d)
    # PR branch from A: first-parent history carries the secret, then the author
    # merges a side branch, so the PR HEAD is itself a merge commit (2 parents).
    _git(repo, "checkout", "-q", "-b", "feature", a)
    _commit(repo, "leak.txt", f"{PR_ADDED}\n", "E1 secret in first-parent history")
    _git(repo, "checkout", "-q", "-b", "side", a)
    _commit(repo, "side.txt", "side\n", "S1 side branch")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "merge", "-q", "--no-ff", "-m", "H author merge", "side")
    # HEAD = feature (a 2-parent merge), checked out as the PR head (unmergeable).
    spec = lsa.resolve_scan_spec("pull_request", "", "", cwd=str(repo))
    out = lsa.added_lines(spec, cwd=str(repo))
    assert PR_ADDED in out  # merge-base catches the first-parent-history secret
    # Control: the old HEAD^1..HEAD heuristic would have MISSED it.
    old = lsa.added_lines(("range", "HEAD^1..HEAD"), cwd=str(repo))
    assert PR_ADDED not in old


def test_single_parent_head_uses_merge_base(tmp_path: Path):
    """Unmergeable PR (linear PR head): merge-base still bounds to PR commits."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    a = _commit(repo, "base.txt", "hello\n", "A")
    d = _commit(repo, "main2.txt", "maindata\n", "D main")
    _set_origin_main(repo, d)
    _git(repo, "checkout", "-q", "-b", "pr", a)
    _commit(repo, "pr.txt", f"{PR_ADDED}\n", "E pr value")
    spec = lsa.resolve_scan_spec("pull_request", "", "", cwd=str(repo))
    assert spec == ("range", f"{a}..HEAD")  # merge-base(D, E) == A (fork point)
    assert PR_ADDED in lsa.added_lines(spec, cwd=str(repo))


def test_no_reachable_main_fails_closed(tmp_path: Path):
    """No origin/main ref → merge-base fails → RangeError (fail closed, never empty)."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "base.txt", "hello\n", "A")
    _commit(repo, "pr.txt", f"{PR_ADDED}\n", "E")  # no origin/main ref set
    with pytest.raises(lsa.RangeError):
        lsa.resolve_scan_spec("pull_request", "", "", cwd=str(repo))


def test_unicode_line_separator_content_preserved(tmp_path: Path):
    """Codex P1: content after U+0085 must NOT be dropped (str.splitlines would)."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    a = _commit(repo, "base.txt", "hello\n", "A")
    d = _commit(repo, "main2.txt", "x\n", "D")
    _set_origin_main(repo, d)
    _git(repo, "checkout", "-q", "-b", "pr", a)
    # An added line with a U+0085 (NEL) between benign text and the secret token.
    (repo / "f.txt").write_text(f"safe{PR_ADDED}\n", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "E nel line")
    spec = lsa.resolve_scan_spec("pull_request", "", "", cwd=str(repo))
    out = lsa.added_lines(spec, cwd=str(repo))
    assert PR_ADDED in out  # content after U+0085 preserved (split on b"\n" only)


def test_non_utf8_byte_does_not_crash(tmp_path: Path):
    """Codex P2: a non-UTF-8 byte in an added line must not crash the gate."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    a = _commit(repo, "base.txt", "hello\n", "A")
    d = _commit(repo, "main2.txt", "x\n", "D")
    _set_origin_main(repo, d)
    _git(repo, "checkout", "-q", "-b", "pr", a)
    # Raw 0xff byte (not a NUL, so git treats the blob as text) + ASCII token.
    (repo / "f.bin").write_bytes(b"prefix\xff " + PR_ADDED.encode() + b"\n")
    _git(repo, "add", "f.bin")
    _git(repo, "commit", "-q", "-m", "E non-utf8")
    spec = lsa.resolve_scan_spec("pull_request", "", "", cwd=str(repo))
    out = lsa.added_lines(spec, cwd=str(repo))  # must not raise
    assert PR_ADDED in out  # ASCII content around the bad byte preserved


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
    _set_origin_main(repo, d)
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
