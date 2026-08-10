"""DIAGNOSTIC REPRODUCTION for the secondary-worktree commit review-gate false-block.

NOT a fix, not a permanent regression suite — a throwaway evidence-gathering
harness for fix/commit-gate-worktree-falseblock. Do not merge as-is; the
findings belong in the design doc, this file is the raw proof.

Borrows the exact `_run_hook` / `_mark` / `repo` / `home` fixture shapes from
tests/test_hooks/test_review_enforcement_commit.py (duplicated here, not
imported, so this file runs standalone and never touches the real suite).

Every test prints a `[cellN]` diagnostic line (run with `-s` to see it) that
records: the resolved worktree key(s) involved, the hook's returncode, and
which Rule fired. Assertions lock in the OBSERVED behavior (this file was
run and adjusted to match reality — see the summary docstring at the bottom
of each cell for interpretation), not a hypothesis.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HOOK = _REPO_ROOT / "scripts" / "review_enforcement_commit.py"
_REVIEW_STATE = _REPO_ROOT / "scripts" / "review_state.py"

sys.path.insert(0, str(_REPO_ROOT / "scripts"))
from review_state import _worktree_key  # noqa: E402  (diagnostic read-only use)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo on a feature branch with a staged (unreviewed) change. == "W"."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "-c", "init.defaultBranch=main", "init", "-q")
    _git(r, "config", "user.email", "t@e.st")
    _git(r, "config", "user.name", "tester")
    (r / "f.py").write_text("base = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    _git(r, "checkout", "-q", "-b", "feature/x")
    (r / "f.py").write_text("base = 2\n")
    _git(r, "add", "-A")
    return r


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    (h / ".genesis").mkdir(parents=True)
    return h


def _second_repo(tmp_path: Path, name: str) -> Path:
    """A distinct git worktree root on its own feature branch, staged."""
    r = tmp_path / name
    r.mkdir()
    _git(r, "-c", "init.defaultBranch=main", "init", "-q")
    _git(r, "config", "user.email", "t@e.st")
    _git(r, "config", "user.name", "tester")
    (r / "g.py").write_text("base = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    _git(r, "checkout", "-q", "-b", "feature/y")
    (r / "g.py").write_text("base = 2\n")
    _git(r, "add", "-A")
    return r


def _run_hook(
    command: str,
    repo: Path,
    home: Path,
    *,
    payload_cwd: Path | str | None = None,
    run_from: Path | None = None,
) -> subprocess.CompletedProcess:
    body = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": "test",
    }
    if payload_cwd is not None:
        body["cwd"] = str(payload_cwd)
    payload = json.dumps(body)
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run(
        [sys.executable, str(_HOOK)],
        input=payload,
        cwd=str(run_from or repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _mark(repo: Path, home: Path) -> subprocess.CompletedProcess:
    """Run `review_state.py mark` with fresh agent-output evidence, no gstack.

    Marker is keyed to `review_state._worktree_key(cwd=repo)` because THIS
    subprocess's own cwd is `repo` — same mechanism the real /review workflow
    uses (mark runs with process cwd = wherever the shell happens to sit).
    """
    (home / ".genesis" / "last_code_review.txt").write_text("adversarial review: OK\n")
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run(
        [
            sys.executable,
            str(_REVIEW_STATE),
            "mark",
            "--agent-output",
            str(home / ".genesis" / "last_code_review.txt"),
        ],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _rule(stderr: str) -> str:
    """Classify which gate rule fired from the hook's stderr message."""
    if "Direct commits to main" in stderr:
        return "Rule1 (main)"
    if "review depth" in stderr.lower():
        return "Rule2.5 (depth)"
    if "without review" in stderr:
        return "Rule2 (marker missing/stale)"
    if "no-verify" in stderr.lower():
        return "Rule0 (no-verify)"
    if "escalation cap" in stderr:
        return "Rule3 (escalation)"
    if not stderr.strip():
        return "none/allow"
    return f"UNRECOGNIZED: {stderr[:120]!r}"


def _key(d: Path) -> str:
    """The worktree key review_state.py would compute for cwd=d (diagnostic)."""
    return _worktree_key(str(d))


def _markers(home: Path) -> list[Path]:
    return list((home / ".genesis" / "review_markers").glob("*.json"))


# ═══════════════════════════════════════════════════════════════════════
# Cell 1 — happy path: marked W, bare commit, payload_cwd == W
# ═══════════════════════════════════════════════════════════════════════


def test_cell1_marked_bare_commit_payload_eq_worktree(repo: Path, home: Path) -> None:
    assert _mark(repo, home).returncode == 0
    res = _run_hook('git commit -m "wip"', repo, home, payload_cwd=repo)
    print(f"[cell1] key(W)={_key(repo)} rc={res.returncode} rule={_rule(res.stderr)}")
    assert res.returncode == 0, res.stderr  # ALLOW — expected happy path


# ═══════════════════════════════════════════════════════════════════════
# Cell 2 — marked W, bare commit, payload_cwd points at a DIFFERENT repo
# ═══════════════════════════════════════════════════════════════════════


def test_cell2_marked_bare_commit_payload_ne_worktree(
    repo: Path, home: Path, tmp_path: Path
) -> None:
    other = _second_repo(tmp_path, "other_cell2")
    assert _mark(repo, home).returncode == 0  # marker keyed to W only
    res = _run_hook('git commit -m "wip"', repo, home, payload_cwd=other)
    print(
        f"[cell2] key(W)={_key(repo)} key(other)={_key(other)} "
        f"rc={res.returncode} rule={_rule(res.stderr)}"
    )
    # eff_cwd resolves to payload cwd (`other`, no cd/-C in the command) — a
    # DIFFERENT worktree key than the one that was marked. TRUE FALSE-BLOCK
    # if the reviewed code and the committed code are actually the same
    # change (the payload cwd is simply reporting a stale/wrong location).
    assert res.returncode == 2
    assert _rule(res.stderr) == "Rule2 (marker missing/stale)"


# ═══════════════════════════════════════════════════════════════════════
# Cell 3 — marked W, bare commit, payload_cwd absent from the JSON entirely
# ═══════════════════════════════════════════════════════════════════════


def test_cell3_marked_bare_commit_payload_cwd_none(repo: Path, home: Path) -> None:
    assert _mark(repo, home).returncode == 0
    res = _run_hook('git commit -m "wip"', repo, home, payload_cwd=None, run_from=repo)
    print(f"[cell3] key(W)={_key(repo)} rc={res.returncode} rule={_rule(res.stderr)}")
    # No payload cwd, no cd, no -C -> _effective_diff_cwd returns None (not
    # UNKNOWN) -> is_review_current(cwd=None) -> subprocess cwd=None ->
    # inherits the HOOK PROCESS's own cwd, which here is `run_from` (=W).
    assert res.returncode == 0, res.stderr


def test_cell3b_marked_bare_commit_payload_cwd_none_process_cwd_elsewhere(
    repo: Path, home: Path, tmp_path: Path
) -> None:
    """Same as cell 3 but the HOOK's own process cwd is NOT W either.

    This is the scenario data point (1) implies by contrast: when neither the
    payload cwd NOR the hook's own launch cwd is the worktree, a marked W
    still false-blocks. Demonstrates payload_cwd=None is not a safe substitute
    for an explicit worktree cwd — it silently falls through to whatever
    directory the hook process happened to be spawned in.
    """
    other = _second_repo(tmp_path, "other_cell3b")
    assert _mark(repo, home).returncode == 0
    res = _run_hook('git commit -m "wip"', repo, home, payload_cwd=None, run_from=other)
    print(
        f"[cell3b] key(W)={_key(repo)} key(other)={_key(other)} "
        f"rc={res.returncode} rule={_rule(res.stderr)}"
    )
    assert res.returncode == 2
    assert _rule(res.stderr) == "Rule2 (marker missing/stale)"


# ═══════════════════════════════════════════════════════════════════════
# Cell 4 — marked W, `git -C W commit`, payload_cwd = a DIFFERENT dir
# ═══════════════════════════════════════════════════════════════════════


def test_cell4_marked_w_dash_C_commit_payload_elsewhere(
    repo: Path, home: Path, tmp_path: Path
) -> None:
    other = _second_repo(tmp_path, "other_cell4")
    assert _mark(repo, home).returncode == 0
    res = _run_hook(f"git -C {repo} commit -m x", repo, home, payload_cwd=other)
    print(
        f"[cell4] key(W)={_key(repo)} key(other)={_key(other)} "
        f"rc={res.returncode} rule={_rule(res.stderr)}"
    )
    # `-C <abs path>` is resolved via `_resolve_against` which short-circuits
    # to the absolute target regardless of the base -> eff_cwd == W
    # regardless of payload cwd. OBSERVED: ALLOW. This REFUTES the "git -C
    # always blocks from elsewhere" memory as a general claim -- with an
    # ABSOLUTE -C target it resolves correctly.
    assert res.returncode == 0, res.stderr


# ═══════════════════════════════════════════════════════════════════════
# Cell 5 — marked W, `cd W && git commit`, payload_cwd = a DIFFERENT dir
# ═══════════════════════════════════════════════════════════════════════


def test_cell5_marked_w_sequential_cd_commit_payload_elsewhere(
    repo: Path, home: Path, tmp_path: Path
) -> None:
    other = _second_repo(tmp_path, "other_cell5")
    assert _mark(repo, home).returncode == 0
    res = _run_hook(f"cd {repo} && git commit -m x", repo, home, payload_cwd=other)
    print(
        f"[cell5] key(W)={_key(repo)} key(other)={_key(other)} "
        f"rc={res.returncode} rule={_rule(res.stderr)}"
    )
    # `_cd_target` returns W as an ABSOLUTE literal path (repo is already
    # absolute under tmp_path) -> `_resolve_against` short-circuits to W
    # regardless of the base (`other`). OBSERVED: ALLOW. REFUTES the "cd W
    # always blocks from elsewhere" memory as a general claim for an
    # ABSOLUTE cd target.
    assert res.returncode == 0, res.stderr


def test_cell5b_marked_w_relative_cd_commit_payload_elsewhere(
    repo: Path, home: Path, tmp_path: Path
) -> None:
    """Same as cell 5 but the `cd` target is RELATIVE, not absolute.

    A relative `cd ../repo && git commit` resolves against the PAYLOAD cwd
    (the base for `_resolve_against`), not the process cwd. If payload_cwd is
    a sibling of W under a different parent, the relative target resolves to
    the WRONG absolute path -- a TRUE FALSE-BLOCK class distinct from cell 5.
    """
    other = _second_repo(tmp_path, "other_cell5b")
    assert _mark(repo, home).returncode == 0
    # relative cd computed against `other` (the payload cwd), NOT against repo
    rel = os.path.relpath(repo, other)
    res = _run_hook(f"cd {rel} && git commit -m x", repo, home, payload_cwd=other)
    resolved = os.path.normpath(os.path.join(str(other), rel))
    print(
        f"[cell5b] key(W)={_key(repo)} key(resolved)={_key(Path(resolved))} "
        f"resolved_path={resolved} matches_W={resolved == str(repo)} "
        f"rc={res.returncode} rule={_rule(res.stderr)}"
    )
    # If the relative path genuinely lands back on W (sibling math correct),
    # this allows; the test documents the resolution rather than assuming.


# ═══════════════════════════════════════════════════════════════════════
# Cell 6 — marked W, message-only `git commit -F <file>` (pure/no -a/pathspec)
# ═══════════════════════════════════════════════════════════════════════


def test_cell6_marked_message_only_dash_F_commit(repo: Path, home: Path) -> None:
    msgfile = repo / "msg.txt"
    msgfile.write_text("commit via -F\n")
    assert _mark(repo, home).returncode == 0
    res = _run_hook(f"git commit -F {msgfile}", repo, home, payload_cwd=repo)
    print(f"[cell6] key(W)={_key(repo)} rc={res.returncode} rule={_rule(res.stderr)}")
    # `-F` is in _COMMIT_ARG_SHORT -> _commit_can_select_content returns False
    # -> commit_may_add_content=False -> takes the PURE path -> Rule 2 uses
    # is_review_current() (diff-hash bound, NO TTL), not has_valid_review_marker.
    assert res.returncode == 0, res.stderr


# ═══════════════════════════════════════════════════════════════════════
# Cell 7 — marked W FRESH, content-adding `git commit -am`
# ═══════════════════════════════════════════════════════════════════════


def test_cell7_marked_fresh_am_allowed(repo: Path, home: Path) -> None:
    _git(repo, "reset", "-q")  # unstage -> a tracked, unstaged edit remains for -a to pick up
    assert _mark(repo, home).returncode == 0
    res = _run_hook('git commit -am "x"', repo, home, payload_cwd=repo)
    print(f"[cell7] key(W)={_key(repo)} rc={res.returncode} rule={_rule(res.stderr)}")
    assert res.returncode == 0, res.stderr


# ═══════════════════════════════════════════════════════════════════════
# Cell 8 — marked W but AGED past the 30-min TTL, content-adding `-am`
# ═══════════════════════════════════════════════════════════════════════


def test_cell8_marked_am_ttl_expired_blocks(repo: Path, home: Path) -> None:
    _git(repo, "reset", "-q")
    assert _mark(repo, home).returncode == 0
    markers = _markers(home)
    assert len(markers) == 1
    state = json.loads(markers[0].read_text())
    old_ts = datetime.now(UTC) - timedelta(seconds=1900)  # > _MAX_EVIDENCE_AGE_SECONDS (1800)
    state["reviewed_at"] = old_ts.isoformat()
    markers[0].write_text(json.dumps(state))
    res = _run_hook('git commit -am "x"', repo, home, payload_cwd=repo)
    print(f"[cell8] key(W)={_key(repo)} age_s=1900 rc={res.returncode} rule={_rule(res.stderr)}")
    # has_valid_review_marker() checks age <= 1800s -> expired -> Rule 2 blocks
    # EVEN THOUGH the marker exists and content hasn't changed since review.
    # This is a TRUE (if narrow) false-block only if the reviewed diff is
    # still the one being committed 30+ min later -- otherwise it's the TTL
    # working as designed.
    assert res.returncode == 2
    assert _rule(res.stderr) == "Rule2 (marker missing/stale)"


def test_cell8b_marked_bare_commit_ttl_expired_still_allowed(repo: Path, home: Path) -> None:
    """Contrast case: the PURE/bare-commit path does NOT consult the TTL at
    all (is_review_current only compares diff_hash, no reviewed_at check),
    so an aged marker on a bare commit with an unchanged diff still ALLOWS.
    This is the TTL ASYMMETRY the task calls out: identical age, identical
    content, different verdict depending solely on -a/-am vs bare."""
    assert _mark(repo, home).returncode == 0  # staged f.py change, NOT reset this time
    markers = _markers(home)
    state = json.loads(markers[0].read_text())
    old_ts = datetime.now(UTC) - timedelta(seconds=1900)
    state["reviewed_at"] = old_ts.isoformat()
    markers[0].write_text(json.dumps(state))
    res = _run_hook('git commit -m "x"', repo, home, payload_cwd=repo)
    print(f"[cell8b] key(W)={_key(repo)} age_s=1900 rc={res.returncode} rule={_rule(res.stderr)}")
    assert res.returncode == 0, res.stderr  # bare commit: is_review_current, no TTL check


# ═══════════════════════════════════════════════════════════════════════
# Cell 9 — marked in the WRONG dir ("other"/MAIN-like), commit targets W via -C
# ═══════════════════════════════════════════════════════════════════════


def test_cell9_marked_wrong_dir_commit_via_dashC(repo: Path, home: Path, tmp_path: Path) -> None:
    other = _second_repo(tmp_path, "other_cell9")
    assert _mark(other, home).returncode == 0  # marker keyed to `other`, NOT W
    res = _run_hook(f"git -C {repo} commit -m x", repo, home, payload_cwd=other)
    print(
        f"[cell9] key(W)={_key(repo)} key(other)={_key(other)} "
        f"rc={res.returncode} rule={_rule(res.stderr)}"
    )
    # The shell's ambient cwd (`other`, where a prior /review + mark ran) is
    # NOT what the commit targets (-C redirects to W). eff_cwd correctly
    # resolves to W via -C, but W was never marked -> blocked. TRUE
    # FALSE-BLOCK if the review that ran in `other` was ACTUALLY reviewing
    # W's diff (e.g., a subagent ran /review with its own Bash cwd sitting
    # at the project root while the user's session cwd was already in W).
    assert res.returncode == 2
    assert _rule(res.stderr) == "Rule2 (marker missing/stale)"


# ═══════════════════════════════════════════════════════════════════════
# Cell 10 — marked W, staged set CHANGES before the bare commit (stat-drift)
# ═══════════════════════════════════════════════════════════════════════


def test_cell10_stat_drift_after_mark_blocks(repo: Path, home: Path) -> None:
    assert _mark(repo, home).returncode == 0
    (repo / "new_file.py").write_text("z = 1\n")
    _git(repo, "add", "-A")
    res = _run_hook('git commit -m "x"', repo, home, payload_cwd=repo)
    print(f"[cell10] key(W)={_key(repo)} rc={res.returncode} rule={_rule(res.stderr)}")
    # OBSERVED: fires Rule 2.5 (depth), not Rule 2 -- adding a second staged
    # .py file makes classify_change_substantiality() read "substantial"
    # (>1 code file), and the weak _mark() evidence in this harness was never
    # adversarial, so depth_is_adversarial=False blocks before Rule 2 is even
    # reached. Either way this is CORRECT behavior, not a false-block: the
    # committed diff genuinely differs from what was reviewed.
    assert res.returncode == 2
    assert _rule(res.stderr) == "Rule2.5 (depth)"


# ═══════════════════════════════════════════════════════════════════════
# Cell 11 — QUOTED heredoc commit-message body with a `cd`-like line must NOT
# false-block. Regression for the shell_parse heredoc fix: before it, the body
# line "…cd into the module…" was parsed as a real `cd` → eff_cwd UNKNOWN →
# false "Direct commits to main" (Rule 1) even with a current marker.
# ═══════════════════════════════════════════════════════════════════════


def test_cell11_heredoc_body_cd_line_not_false_blocked(repo: Path, home: Path) -> None:
    assert _mark(repo, home).returncode == 0
    cmd = (
        "git commit -q -F - <<'EOF'\n"
        "fix(x): change how we cd into the module then run it\n"
        "\n"
        "more body prose mentioning cd and other words\n"
        "EOF"
    )
    res = _run_hook(cmd, repo, home, payload_cwd=repo)
    print(f"[cell11] key(W)={_key(repo)} rc={res.returncode} rule={_rule(res.stderr)}")
    assert res.returncode == 0, f"false-block: {_rule(res.stderr)} :: {res.stderr[:200]}"
