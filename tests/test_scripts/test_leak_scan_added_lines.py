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


# --- Branch pushes (LEAK_SCAN_RANGE=branch, branch-leak-scan.yml) -------------


def _branch_repo(tmp_path: Path, n_commits: int = 3) -> dict:
    """main: A ─ B(-leak added then removed) ─ D;  branch: A ─ E1..En (each adds a token).

    The branch's FIRST commit carries PR_ADDED, so a tip-only scan misses it.
    """
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    a = _commit(repo, "base.txt", "hello\n", "A")
    _commit(repo, "leak.txt", f"{MAIN_SECRET}\n", "B main adds")
    _rm_commit(repo, "leak.txt", "C main removes")
    d = _commit(repo, "main2.txt", "maindata\n", "D")
    _set_origin_main(repo, d)
    _git(repo, "checkout", "-q", "-b", "feature", a)
    _commit(repo, "first.txt", f"{PR_ADDED}\n", "E1 first branch commit")
    for i in range(2, n_commits + 1):
        _commit(repo, f"f{i}.txt", f"benign {i}\n", f"E{i}")
    return {"repo": repo, "A": a, "D": d, "tip": _git(repo, "rev-parse", "HEAD")}


def test_branch_push_scans_every_branch_commit_not_just_tip(tmp_path: Path):
    """A new branch (before = zeros) with several commits: the old push path
    scanned the tip only and missed the first commit's token (control), the
    branch path scans merge-base..HEAD and catches it, without re-flagging
    main's own added-then-removed value."""
    r = _branch_repo(tmp_path)
    repo = str(r["repo"])
    zeros = "0" * 40
    control = lsa.added_lines(lsa.resolve_scan_spec("push", zeros, r["tip"], cwd=repo), cwd=repo)
    assert PR_ADDED not in control  # control: tip-only scan is blind to commit 1
    spec = lsa.resolve_scan_spec("push", zeros, r["tip"], cwd=repo, branch_push=True)
    assert spec == ("range", f"{r['A']}..HEAD")
    out = lsa.added_lines(spec, cwd=repo)
    assert PR_ADDED in out
    assert MAIN_SECRET not in out


def test_branch_push_ignores_push_before(tmp_path: Path):
    """A known `before` must NOT narrow the branch scan (a cancelled earlier
    run would otherwise leave its commits unscanned)."""
    r = _branch_repo(tmp_path)
    repo = str(r["repo"])
    before = _git(r["repo"], "rev-parse", "HEAD~1")
    spec = lsa.resolve_scan_spec("push", before, r["tip"], cwd=repo, branch_push=True)
    assert spec == ("range", f"{r['A']}..HEAD")
    assert PR_ADDED in lsa.added_lines(spec, cwd=repo)


def test_branch_push_branch_at_main_is_empty_not_error(tmp_path: Path):
    """A branch created at main's tip with no commits of its own resolves to an
    empty range — nothing was authored, so nothing to scan."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    d = _commit(repo, "base.txt", "hello\n", "A")
    _set_origin_main(repo, d)
    _git(repo, "checkout", "-q", "-b", "feature")
    spec = lsa.resolve_scan_spec("push", "0" * 40, d, cwd=str(repo), branch_push=True)
    assert spec == ("range", f"{d}..HEAD")
    assert lsa.added_lines(spec, cwd=str(repo)) == ""


def test_branch_push_orphan_branch_scans_full_history(tmp_path: Path):
    """No common ancestor with main: every commit is branch-authored."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    d = _commit(repo, "base.txt", "hello\n", "A")
    _set_origin_main(repo, d)
    _git(repo, "checkout", "-q", "--orphan", "orphan")
    _git(repo, "rm", "-rq", "--cached", ".")
    (repo / "base.txt").unlink()
    _commit(repo, "o1.txt", f"{PR_ADDED}\n", "O1")
    _commit(repo, "o2.txt", "benign\n", "O2")
    spec = lsa.resolve_scan_spec("push", "0" * 40, "", cwd=str(repo), branch_push=True)
    assert spec == ("range", "HEAD")
    out = lsa.added_lines(spec, cwd=str(repo))
    assert PR_ADDED in out


def test_branch_push_without_origin_main_fails_closed(tmp_path: Path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "pr.txt", f"{PR_ADDED}\n", "A")  # no origin/main ref
    with pytest.raises(lsa.RangeError):
        lsa.resolve_scan_spec("push", "0" * 40, "", cwd=str(repo), branch_push=True)


def test_branch_flag_without_push_event_is_ignored_by_resolver(tmp_path: Path):
    """branch_push only applies to push events; a PR keeps its own path."""
    r = _branch_repo(tmp_path)
    spec = lsa.resolve_scan_spec("pull_request", "", "", cwd=str(r["repo"]), branch_push=True)
    assert spec == ("range", f"{r['A']}..HEAD")


@pytest.mark.parametrize(
    ("scope", "event"),
    [("branches", "push"), ("BRANCH", "push"), ("branch", "pull_request"), ("branch", "")],
)
def test_main_rejects_bad_scope_fail_closed(monkeypatch, capsys, scope, event):
    """An unrecognised LEAK_SCAN_RANGE, or `branch` on a non-push event, must
    fail closed rather than silently fall back to a narrower range."""
    called = []
    monkeypatch.setattr(lsa, "resolve_scan_spec", lambda *a, **k: called.append(1))
    monkeypatch.setenv("LEAK_SCAN_RANGE", scope)
    monkeypatch.setenv("EVENT_NAME", event)
    assert lsa.main([]) == lsa.EXIT_UNRESOLVABLE
    assert "Failing closed" in capsys.readouterr().err
    assert not called


def test_main_passes_branch_scope(monkeypatch):
    seen = {}

    def _spy(event_name, push_before, head_sha, cwd=None, *, branch_push=False):
        seen["branch_push"] = branch_push
        return ("show", "HEAD")

    monkeypatch.setattr(lsa, "resolve_scan_spec", _spy)
    monkeypatch.setattr(lsa, "added_lines", lambda spec, cwd=None: "")
    monkeypatch.setenv("LEAK_SCAN_RANGE", "branch")
    monkeypatch.setenv("EVENT_NAME", "push")
    assert lsa.main([]) == lsa.EXIT_OK
    assert seen == {"branch_push": True}


def test_e2e_branch_push_leak_in_first_commit_blocks(tmp_path: Path):
    """The real two-script pipeline, as branch-leak-scan.yml runs it."""
    import os
    import sys

    r = _branch_repo(tmp_path)
    pf = tmp_path / "patterns.txt"
    pf.write_text("PRADDED_TOKEN_[a-z0-9]+\n", encoding="utf-8")
    env = {
        **os.environ,
        "EVENT_NAME": "push",
        "LEAK_SCAN_RANGE": "branch",
        "PUSH_BEFORE": "0" * 40,
        "HEAD_SHA": r["tip"],
        "HOME": str(r["repo"]),
    }
    p1 = subprocess.run(
        [sys.executable, str(_MODULE_PATH)], cwd=r["repo"], env=env, capture_output=True, text=True
    )
    assert p1.returncode == 0, p1.stderr
    p2 = subprocess.run(
        [sys.executable, str(_PPS_PATH), "--patterns", str(pf)],
        input=p1.stdout,
        capture_output=True,
        text=True,
    )
    assert p2.returncode == 1  # EXIT_LEAK


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


def test_range_mode_prints_the_branch_range_for_the_history_scan(
    tmp_path: Path, monkeypatch, capsys
):
    """--range prints the git revision range instead of added lines, so the
    gitleaks history step scans exactly the commits the private step scans."""
    r = _branch_repo(tmp_path)
    monkeypatch.chdir(r["repo"])
    monkeypatch.setenv("EVENT_NAME", "push")
    monkeypatch.setenv("LEAK_SCAN_RANGE", "branch")
    monkeypatch.setenv("HEAD_SHA", r["tip"])
    assert lsa.main(["--range"]) == lsa.EXIT_OK
    assert capsys.readouterr().out.strip() == f"{r['A']}..HEAD"


def test_range_mode_turns_a_single_commit_spec_into_that_commit_alone(
    tmp_path: Path, monkeypatch, capsys
):
    """A new-branch push with no known base scans one commit; its history form
    is `<sha>^!`, which git log reads as that commit alone."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    tip = _commit(repo, "base.txt", "hello\n", "A")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("EVENT_NAME", "push")
    monkeypatch.delenv("LEAK_SCAN_RANGE", raising=False)
    monkeypatch.setenv("HEAD_SHA", tip)
    monkeypatch.setenv("PUSH_BEFORE", "0" * 40)
    assert lsa.main(["--range"]) == lsa.EXIT_OK
    assert capsys.readouterr().out.strip() == f"{tip}^!"


def test_range_mode_still_fails_closed(tmp_path: Path, monkeypatch, capsys):
    """An unresolvable range is an error in --range mode too, never empty."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "base.txt", "hello\n", "A")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("EVENT_NAME", "push")
    monkeypatch.setenv("LEAK_SCAN_RANGE", "branch")
    assert lsa.main(["--range"]) == lsa.EXIT_UNRESOLVABLE
    assert capsys.readouterr().out == ""


# --- Merge commits: a conflict resolution's additions are scanned ------------


def _resolved_merge_repo(tmp_path: Path) -> dict:
    """main and a branch edit the same line; the branch merges main and its
    conflict resolution introduces PR_ADDED. main also adds a file the merge
    brings in cleanly, carrying MAIN_SECRET."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    a = _commit(repo, "f.txt", "a\nb\nc\n", "A")
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, "f.txt", "a\nFEAT\nc\n", "branch edit")
    _git(repo, "checkout", "-q", "main")
    _commit(repo, "f.txt", "a\nMAIN\nc\n", "main edit")
    d = _commit(repo, "m.txt", f"{MAIN_SECRET}\n", "main adds a file")
    _set_origin_main(repo, d)
    _git(repo, "checkout", "-q", "feature")
    subprocess.run(
        ["git", "merge", "-q", "main"],
        cwd=repo,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "ci@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "ci@example.com",
            "GIT_CONFIG_NOSYSTEM": "1",
            "HOME": str(repo),
        },
    )
    (repo / "f.txt").write_text(f"a\n{PR_ADDED}\nc\n", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "--no-edit")
    return {"repo": repo, "A": a, "D": d, "tip": _git(repo, "rev-parse", "HEAD")}


def test_a_merge_resolution_value_is_scanned(tmp_path: Path):
    """Codex P1 (round 2): `--no-merges` skipped every merge commit, so a value
    introduced while resolving a conflict was never read. The control proves
    the old command misses it; the scan now reads it."""
    r = _resolved_merge_repo(tmp_path)
    repo = str(r["repo"])
    spec = lsa.resolve_scan_spec("push", "0" * 40, r["tip"], cwd=repo, branch_push=True)
    control = _git(r["repo"], "log", "-p", "--no-merges", spec[1])
    assert PR_ADDED not in control
    assert PR_ADDED in lsa.added_lines(spec, cwd=repo)


def test_a_clean_merge_does_not_rescan_main(tmp_path: Path):
    """What a merge brings in from main without a conflict is main's content,
    scanned by main's own run; the remerge diff of the merge does not repeat it."""
    r = _resolved_merge_repo(tmp_path)
    repo = str(r["repo"])
    spec = lsa.resolve_scan_spec("push", "0" * 40, r["tip"], cwd=repo, branch_push=True)
    assert MAIN_SECRET not in lsa.added_lines(spec, cwd=repo)


def test_with_paths_names_the_file_and_commit(tmp_path: Path):
    """--with-paths emits path:commit12:content for every added line, a merge
    resolution included, and keeps added content that itself starts with '++'."""
    r = _branch_repo(tmp_path)
    _commit(r["repo"], "plus.txt", "++ not a header\n", "E plus")
    repo = str(r["repo"])
    spec = lsa.resolve_scan_spec("push", "0" * 40, "HEAD", cwd=repo, branch_push=True)
    rows = lsa.added_lines_with_paths(spec, cwd=repo)
    first = _git(r["repo"], "rev-list", "--reverse", f"{r['A']}..HEAD").splitlines()[0]
    assert f"first.txt:{first[:12]}:{PR_ADDED}" in rows
    assert any(row.startswith("plus.txt:") and row.endswith(":++ not a header") for row in rows)

    (tmp_path / "m").mkdir()
    m = _resolved_merge_repo(tmp_path / "m")
    mrepo = str(m["repo"])
    mspec = lsa.resolve_scan_spec("push", "0" * 40, m["tip"], cwd=mrepo, branch_push=True)
    assert f"f.txt:{m['tip'][:12]}:{PR_ADDED}" in lsa.added_lines_with_paths(mspec, cwd=mrepo)


def test_with_paths_mode_from_the_command_line(tmp_path: Path, monkeypatch, capsys):
    r = _branch_repo(tmp_path)
    monkeypatch.chdir(r["repo"])
    monkeypatch.setenv("EVENT_NAME", "push")
    monkeypatch.setenv("LEAK_SCAN_RANGE", "branch")
    monkeypatch.setenv("HEAD_SHA", r["tip"])
    assert lsa.main(["--with-paths"]) == lsa.EXIT_OK
    out = capsys.readouterr().out
    assert f":{PR_ADDED}" in out
    assert all(line.count(":") >= 2 for line in out.splitlines() if line)
