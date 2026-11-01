"""scripts/lib/update_state_guard.py: whether update_state.json still guards (#3139).

One rule, asked by update.sh, bootstrap.sh, deploy_code_only.sh, the live engine
and the dashboard. These tests run the real module (loaded per test, so a missing
module fails a test instead of collection) and its command line against scratch
repositories.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GUARD = REPO / "scripts" / "lib" / "update_state_guard.py"

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="git + bash")


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    return env


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=_git_env()
    ).stdout.strip()


@pytest.fixture
def guard():
    spec = importlib.util.spec_from_file_location("update_state_guard_under_test", GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repo(tmp_path: Path) -> dict:
    """A checkout at tag `pre` (HEAD), and `incoming`, a commit it has not merged."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    (root / "a.txt").write_text("a\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-qm", "base")
    _git(root, "tag", "pre")
    base = _git(root, "rev-parse", "HEAD")
    (root / "b.txt").write_text("b\n")
    _git(root, "add", "b.txt")
    _git(root, "commit", "-qm", "incoming")
    incoming = _git(root, "rev-parse", "HEAD")
    _git(root, "reset", "-q", "--hard", base)
    home = tmp_path / "home"
    home.mkdir()
    return {"root": root, "base": base, "incoming": incoming, "home": home}


def _state(r: dict, content) -> Path:
    path = r["home"] / "update_state.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    return path


def _verdict(guard, r: dict, content, conflicts=None) -> str:
    path = _state(r, content)
    if conflicts is not None:
        (r["home"] / "update_conflicts.json").write_text(json.dumps(conflicts))
    return guard.verdict(str(path), str(r["root"]))[0]


def test_no_file_guards_nothing(guard, repo):
    assert guard.verdict(str(repo["home"] / "update_state.json"), str(repo["root"]))[0] == "safe"


@pytest.mark.parametrize(
    "content",
    ["{not json", "[1, 2]", '"text"', "{}", '{"pid": 1}', '{"phase": 7}'],
    ids=["malformed", "array", "string", "empty-object", "no-phase", "non-string-phase"],
)
def test_a_state_that_cannot_be_judged_guards(guard, repo, content):
    assert _verdict(guard, repo, content) == "holds"


def test_done_guards_nothing(guard, repo):
    assert _verdict(guard, repo, {"phase": "done"}) == "safe"


def test_fetching_at_the_tag_guards_nothing(guard, repo):
    state = {
        "phase": "fetching",
        "rollback_tag": "pre",
        "deploy_head": repo["incoming"],
        "post_merge": False,
    }
    assert _verdict(guard, repo, state) == "safe"


@pytest.mark.parametrize(
    "state",
    [
        {"phase": "fetching", "rollback_tag": "pre", "deploy_head": "", "post_merge": False},
        {"phase": "fetching", "rollback_tag": "pre", "deploy_head": "x", "post_merge": True},
        {"phase": "fetching", "rollback_tag": "gone", "deploy_head": "x"},
        {"phase": "fetching", "deploy_head": "x"},
    ],
    ids=["no-deploy-head", "post-merge", "missing-tag", "no-tag"],
)
def test_a_fetching_state_that_may_cover_merged_code_guards(guard, repo, state):
    """SF1: a --post-merge writes "fetching" over code that is already merged,
    and may have made its fallback tag at that merged HEAD. A plain run's row
    naming no fetched commit merged nothing: it holds (Dismiss, run again)."""
    want = "holds" if state.get("post_merge") is False else "guards"
    assert _verdict(guard, repo, state) == want


def test_fetching_after_head_moved_guards(guard, repo):
    _git(repo["root"], "merge", "-q", "--ff-only", repo["incoming"])
    state = {
        "phase": "fetching",
        "rollback_tag": "pre",
        "deploy_head": repo["incoming"],
        "post_merge": False,
    }
    assert _verdict(guard, repo, state) == "holds"


def test_merging_after_the_merge_guards(guard, repo):
    _git(repo["root"], "merge", "-q", "--ff-only", repo["incoming"])
    state = {"phase": "merging", "rollback_tag": "pre", "deploy_head": repo["incoming"]}
    assert _verdict(guard, repo, state) == "guards"


def test_a_conflict_exit_is_conflict_pending(guard, repo):
    state = {"phase": "merging", "rollback_tag": "pre", "deploy_head": repo["incoming"]}
    assert _verdict(guard, repo, state, {"deploy_head": repo["incoming"]}) == "conflict_pending"


def test_a_kill_before_the_merge_is_not_a_conflict(guard, repo):
    """Review 4255463476: "merging" at the tag without a recorded conflict is a
    run killed between writing "merging" and its merge; nothing merged."""
    state = {"phase": "merging", "rollback_tag": "pre", "deploy_head": repo["incoming"]}
    assert _verdict(guard, repo, state) == "safe"


def test_a_conflict_file_for_another_commit_is_not_this_conflict(guard, repo):
    state = {"phase": "merging", "rollback_tag": "pre", "deploy_head": repo["incoming"]}
    assert _verdict(guard, repo, state, {"deploy_head": "0" * 40}) == "safe"


@pytest.mark.parametrize("path", ["a.txt", "AGENTS.md"], ids=["tracked", "ephemeral"])
def test_merging_at_the_tag_with_tracked_changes_guards(guard, repo, path):
    """The bootstrap residue check: every tracked change counts, an ephemeral
    path included (a half-written fast-forward writes those too)."""
    if path == "AGENTS.md":
        (repo["root"] / path).write_text("x\n")
        _git(repo["root"], "add", path)
        _git(repo["root"], "commit", "-qm", "agents")
        _git(repo["root"], "tag", "-f", "pre")
    (repo["root"] / path).write_text("edited\n")
    state = {"phase": "merging", "rollback_tag": "pre", "deploy_head": repo["incoming"]}
    # The merge (pre...incoming) writes only b.txt: an edit elsewhere is not
    # its residue. It still guards, as it did before #3492, without claiming a
    # half-written tree (--post-merge refuses only that).
    assert _verdict(guard, repo, state, {"deploy_head": repo["incoming"]}) == "holds"
    state["deploy_head"] = ""
    assert _verdict(guard, repo, state) == "mixed_tree", "no range: every tracked change"


def test_merging_at_the_tag_with_an_added_file_guards(guard, repo):
    """A fast-forward killed after writing only an added file leaves it untracked."""
    (repo["root"] / "b.txt").write_text("b\n")
    state = {"phase": "merging", "rollback_tag": "pre", "deploy_head": repo["incoming"]}
    assert _verdict(guard, repo, state) == "mixed_tree"


def test_a_merge_in_progress_guards(guard, repo):
    _git(repo["root"], "checkout", "-q", "-b", "side")
    (repo["root"] / "a.txt").write_text("side\n")
    _git(repo["root"], "commit", "-qam", "side")
    _git(repo["root"], "checkout", "-q", "main")
    (repo["root"] / "a.txt").write_text("main\n")
    _git(repo["root"], "commit", "-qam", "main")
    _git(repo["root"], "tag", "-f", "pre")
    # The conflicting merge exits non-zero; the identity env lets git start it on
    # any machine (no global identity needed), and MERGE_HEAD proves it stopped.
    r = subprocess.run(
        ["git", "-C", str(repo["root"]), "merge", "-q", "side"],
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    assert (repo["root"] / ".git" / "MERGE_HEAD").exists(), r.stdout + r.stderr
    state = {"phase": "merging", "rollback_tag": "pre", "deploy_head": repo["incoming"]}
    assert _verdict(guard, repo, state) == "holds"


@pytest.mark.parametrize("phase", ["bootstrap", "migrations", "health_check", "merged"])
def test_later_or_unknown_phases_guard(guard, repo, phase):
    want = "holds" if phase == "merged" else "guards"  # no update writes "merged"
    assert _verdict(guard, repo, {"phase": phase, "rollback_tag": "pre"}) == want


def test_an_inherited_git_dir_does_not_redirect_it(guard, repo, monkeypatch, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    state = {
        "phase": "fetching",
        "rollback_tag": "pre",
        "deploy_head": repo["incoming"],
        "post_merge": False,
    }
    assert _verdict(guard, repo, state) == "safe"


@pytest.mark.parametrize(
    "content,conflict,rc",
    [
        ({"phase": "done"}, False, 10),
        ({"phase": "bootstrap"}, False, 0),
        ({"phase": "merging", "rollback_tag": "pre"}, True, 3),
    ],
    ids=["safe", "guards", "conflict"],
)
def test_the_command_line(repo, content, conflict, rc):
    if "deploy_head" not in content and content["phase"] == "merging":
        content = {**content, "deploy_head": repo["incoming"]}
    path = _state(repo, content)
    if conflict:
        (repo["home"] / "update_conflicts.json").write_text(
            json.dumps({"deploy_head": repo["incoming"]})
        )
    r = subprocess.run(
        [sys.executable, "-I", "-S", str(GUARD), str(path), str(repo["root"])],
        capture_output=True,
        text=True,
    )
    assert r.returncode == rc, r.stdout + r.stderr
    if rc == 0:
        assert "--post-merge" in r.stdout and "Dismiss" in r.stdout
    if rc == 3:
        assert "merge conflicts" in r.stdout and "Resolve" in r.stdout


@pytest.mark.parametrize(
    "post_merge",
    ["absent", "true", None, 0],
    ids=["no-key", "string-true", "null", "zero"],
)
def test_only_an_explicit_plain_run_fetching_state_is_safe(guard, repo, post_merge):
    """SF-B: a state from before the key existed, or a hand edit, may be a
    --post-merge's "fetching" over merged code at a fallback tag made at HEAD."""
    state = {"phase": "fetching", "rollback_tag": "pre", "deploy_head": repo["incoming"]}
    if post_merge != "absent":
        state["post_merge"] = post_merge
    assert _verdict(guard, repo, state) == "guards"
    state["post_merge"] = False
    assert _verdict(guard, repo, state) == "safe", "control: an explicit plain run"


@pytest.mark.parametrize(
    "record", ["{not json", "[1, 2]", '"text"'], ids=["malformed", "array", "string"]
)
def test_a_conflict_record_that_cannot_be_read_keeps_the_conflict(guard, repo, record):
    """N1: doubt about the conflict record keeps the resolver's anchor."""
    state = {"phase": "merging", "rollback_tag": "pre", "deploy_head": repo["incoming"]}
    path = _state(repo, state)
    (repo["home"] / "update_conflicts.json").write_text(record)
    assert guard.verdict(str(path), str(repo["root"]))[0] == "conflict_pending"


def _cli(repo, path, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", "-S", str(GUARD), str(path), str(repo["root"])],
        capture_output=True,
        text=True,
        env=env,
    )


def test_a_check_that_cannot_run_git_could_not_run(repo, tmp_path):
    """SF-A: git not on PATH used to raise OSError, and Python's exit 1 for an
    uncaught exception was the old "safe" code. Now a check that fails partway
    exits 1, the "could not run" class every caller refuses: a guarding 0
    would let --post-merge (which finishes a guarding state) go past a
    half-written tree it never judged (#3492 N1)."""
    path = _state(
        repo, {"phase": "merging", "rollback_tag": "pre", "deploy_head": repo["incoming"]}
    )
    empty = tmp_path / "no-tools"
    empty.mkdir()
    r = _cli(repo, path, env={"PATH": str(empty), "HOME": str(repo["home"])})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "could not be checked" in r.stdout and "FileNotFoundError" in r.stdout
    assert not post_merge_advice(r.stdout), r.stdout


def test_a_state_nested_too_deeply_to_parse_guards(repo):
    path = repo["home"] / "update_state.json"
    path.write_text("[" * 200_000 + "]" * 200_000)
    r = _cli(repo, path)
    assert r.returncode == 6, r.stdout + r.stderr


def test_safe_is_a_code_no_default_failure_produces():
    """Python's uncaught exception (1), a usage error (2) and a shell's cannot-run
    codes (126, 127) must never mean safe."""
    text = GUARD.read_text()
    assert "EXIT_CODES = {GUARDS: 0, SAFE: 10, CONFLICT_PENDING: 3, MIXED_TREE: 4, HOLD: 6}" in text


def test_a_hung_git_guards_and_returns(guard, repo, tmp_path, monkeypatch):
    """A git that never returns (a wedged filesystem) must not hold the caller
    forever (the dashboard holds update.lock while it asks): the call times
    out, and a check that could not finish keeps the state."""
    path = _state(
        repo,
        {
            "phase": "fetching",
            "rollback_tag": "pre",
            "deploy_head": repo["incoming"],
            "post_merge": False,
        },
    )
    hang = tmp_path / "hang-bin"
    hang.mkdir()
    fake = hang / "git"
    fake.write_text("#!/bin/sh\nexec sleep 30\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{hang}:{os.environ['PATH']}")
    monkeypatch.setattr(guard, "_TIMEOUT_S", 0.5, raising=False)
    started = time.monotonic()
    assert guard.main([str(path), str(repo["root"])]) == 1
    assert time.monotonic() - started < 10, "the git call was not bounded"


# ── a live row: the engine's move record in update.sh's own row (#3492 gap 4) ──

LIB = REPO / "scripts" / "lib"


def diverged_live(root: Path, *, tag_only: bool = True) -> dict:
    """The shape of a normal `live` rebuild (each rebuild is a new merge chain on
    a new base, so the old and new tips diverge): main0 holds F and A; the old
    tip (tag `pre`, checked out on `live`) is main0 plus a candidate that deleted
    F and added O; the new tip is main1 (adds Z) with that candidate gone, so F
    comes back and O goes. The switch from old to new writes diff(old, new):
    F and Z; the symmetric old...new names only Z. ``tag_only`` False: no O,
    so a switch has nothing to delete (it deletes before it writes)."""
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    (root / "F").write_text("f\n")
    (root / "A").write_text("a\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "main0")
    main0 = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-qb", "cand")
    _git(root, "rm", "-q", "F")
    if tag_only:
        (root / "O").write_text("o\n")
        _git(root, "add", "O")
    _git(root, "commit", "-qm", "candidate deletes F, adds O")
    cand = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", "--detach", main0)
    (root / "Z").write_text("z\n")
    _git(root, "add", "Z")
    _git(root, "commit", "-qm", "main1")
    new = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", "--detach", main0)
    _git(root, "merge", "-q", "--no-ff", "--no-edit", cand)
    old = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", "-B", "live", old)
    _git(root, "tag", "pre", old)
    two_dot = _git(root, "diff", "--name-only", "--diff-filter=d", old, new).split()
    assert sorted(two_dot) == ["F", "Z"]
    assert _git(root, "diff", "--name-only", "--diff-filter=d", f"{old}...{new}").split() == ["Z"]
    return {"root": root, "old": old, "new": new, "main0": main0}


def live_row(d: dict, phase: str = "moving", **over) -> dict:
    """update.sh's own "merging" row on `live`, with the engine's record."""
    return {
        "phase": "merging",
        "rollback_tag": "pre",
        "original_branch": "live",
        "own_head": d["old"],
        "deploy_head": d["main0"],
        "post_merge": False,
        "pid": 2_147_483_647,
        "live_move": {"to": d["new"], "phase": phase},
        **over,
    }


@pytest.fixture
def live(tmp_path: Path) -> dict:
    d = diverged_live(tmp_path / "repo")
    d["home"] = tmp_path / "home"
    d["home"].mkdir()
    return d


def _live_verdict(guard, d: dict, state: dict) -> tuple[str, str]:
    return guard.verdict(str(_state(d, state)), str(d["root"]))


def kill_switch_after(root: Path, new: str, victim: str) -> None:
    """A real `git switch -C live <new>`, SIGKILLed by a smudge filter on
    ``victim`` (no sleep: deterministic): every file git writes before it is on
    disk, nothing after it is, and HEAD, `live` and the index stay put (git
    moves HEAD last). The stale index.lock a SIGKILL leaves is removed."""
    (root / ".git" / "info").mkdir(exist_ok=True)
    (root / ".git" / "info" / "attributes").write_text(f"{victim} filter=killer\n")
    _git(root, "config", "filter.killer.smudge", "kill -9 $PPID")
    _git(root, "config", "filter.killer.required", "true")
    r = subprocess.run(
        ["git", "-C", str(root), "switch", "--no-overwrite-ignore", "-C", "live", new],
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    assert r.returncode != 0, "the switch was not killed"
    (root / ".git" / "index.lock").unlink(missing_ok=True)
    (root / ".git" / "info" / "attributes").unlink()


def test_t4a_an_added_file_written_by_a_killed_switch_is_a_mixed_tree(guard, live):
    """T4a: HEAD at the tag, the record says the switch began, and a file the
    new tip adds is on disk untracked (git status shows nothing)."""
    (live["root"] / "Z").write_text("z\n")
    assert _git(live["root"], "status", "--porcelain", "--untracked-files=no") == ""
    verdict, reason = _live_verdict(guard, live, live_row(live))
    assert verdict == "mixed_tree", reason
    r = _cli(live, live["home"] / "update_state.json")
    assert r.returncode == 4, r.stdout + r.stderr
    # A live row's remedy is the repair, which saves before it restores (#3492 PR D1).
    assert "  ?? Z" in r.stdout and "scripts/deploy_candidates repair-move" in r.stdout, r.stdout


def test_t4g_a_path_a_dropped_candidate_had_deleted_is_seen(guard, live):
    """T4g (S-1): F, which the old tip's candidate deleted and the new tip has
    again, is what the switch writes; the symmetric range never names it."""
    (live["root"] / "F").write_text("f\n")
    verdict, reason = _live_verdict(guard, live, live_row(live))
    assert verdict == "mixed_tree", reason
    assert "  ?? F" in reason, reason


def test_t4g_a_real_switch_killed_after_its_first_file_is_a_mixed_tree(guard, tmp_path):
    """T4g, against a real switch: killed on Z, so F (which sorts first) is
    already written and nothing else is (no tag-only file: git deletes those
    first, which tracked dirt would show)."""
    live = diverged_live(tmp_path / "repo", tag_only=False)
    live["home"] = tmp_path / "home"
    live["home"].mkdir()
    root = live["root"]
    kill_switch_after(root, live["new"], "Z")
    assert _git(root, "rev-parse", "HEAD") == live["old"], "the kill came after HEAD moved"
    assert (root / "F").exists() and not (root / "Z").exists(), "the kill did not land after F"
    assert _git(root, "status", "--porcelain", "--untracked-files=no") == "", "not invisible"
    verdict, reason = _live_verdict(guard, live, live_row(live))
    assert verdict == "mixed_tree" and "  ?? F" in reason, reason


def test_t4b_a_partial_record_over_a_clean_tree_is_a_mixed_tree(guard, live):
    verdict, reason = _live_verdict(guard, live, live_row(live, "partial"))
    assert verdict == "mixed_tree", reason


@pytest.mark.parametrize("phase", ["moving", "ref", "clean"])
def test_t4c_a_record_over_an_untouched_tree_guards_nothing(guard, live, phase):
    """T4c: the switch began (or git refused cleanly) and wrote nothing."""
    assert _live_verdict(guard, live, live_row(live, phase))[0] == "safe"


def test_a_clean_refusal_is_not_blamed_for_a_file_that_was_already_there(guard, live):
    """ "clean": the engine compared the tree before and after and git wrote
    nothing, so an untracked file at a path the tip adds was there before (the
    reason git refused). A tracked change is then not the move's either: it
    guards, as a foreign edit always has, but is not a half-written tree."""
    (live["root"] / "Z").write_text("someone's\n")
    assert _live_verdict(guard, live, live_row(live, "clean"))[0] == "safe"
    (live["root"] / "A").write_text("edited\n")
    assert _live_verdict(guard, live, live_row(live, "clean"))[0] == "holds"


@pytest.mark.parametrize(
    "record",
    [
        "x",
        [],
        {"to": "e" * 40},
        {"to": "e" * 39, "phase": "moving"},
        {"to": "E" * 40, "phase": "moving"},
        {"to": "e" * 40, "phase": "halfway"},
        {"to": 7, "phase": "moving"},
    ],
    ids=["string", "list", "no-phase", "short", "uppercase", "unknown-phase", "non-string"],
)
def test_t4d_a_malformed_record_guards(guard, live, record):
    state = live_row(live)
    state["live_move"] = record
    assert _live_verdict(guard, live, state)[0] == "holds"


@pytest.mark.parametrize("phase", ["fetching", "bootstrap", "done"])
def test_t4e_a_record_under_any_other_phase_guards(guard, live, phase):
    """The engine writes its record only into the "merging" row, and update.sh's
    next row drops it: anywhere else it is a hand edit or a writer gone wrong."""
    state = live_row(live)
    state["phase"] = phase
    assert _live_verdict(guard, live, state)[0] == "holds"


def test_t4i_a_rollback_killed_midway_back_to_the_tag_is_a_mixed_tree(guard, live):
    """S18: the move finished (HEAD on `live` at its target), then the rollback's
    checkout back to the tag was killed after writing O, a file only the tag
    has: HEAD is still at the target."""
    root = live["root"]
    _git(root, "checkout", "-q", "-B", "live", live["new"])
    assert _live_verdict(guard, live, live_row(live, "done"))[0] == "guards", "control"
    (root / "O").write_text("o\n")
    verdict, reason = _live_verdict(guard, live, live_row(live, "done"))
    assert verdict == "mixed_tree" and "  ?? O" in reason, reason


def test_t4f_main_residue_is_a_mixed_tree_for_every_caller(guard, repo):
    """T4f: main keeps its range (rb...deploy_head), and what used to be the
    plain GUARDS verdict on it is the new verdict: exit 4, which every caller
    (exit 10 or not) still refuses."""
    (repo["root"] / "b.txt").write_text("b\n")
    state = {"phase": "merging", "rollback_tag": "pre", "deploy_head": repo["incoming"]}
    assert _verdict(guard, repo, state) == "mixed_tree"
    r = _cli(repo, repo["home"] / "update_state.json")
    assert r.returncode == 4 and "b.txt" in r.stdout, r.stdout + r.stderr


def post_merge_advice(text: str) -> list[str]:
    """Every sentence naming --post-merge that does not say it refuses (T4t)."""
    return [
        s
        for s in text.replace("\n", " ").split(". ")
        if "--post-merge" in s and "NOT" not in s and "refuses" not in s
    ]


def test_t4t_a_mixed_tree_message_never_recommends_post_merge(guard, live):
    (live["root"] / "Z").write_text("z\n")
    r = _cli(live, _state(live, live_row(live)))
    assert r.returncode == 4
    assert not post_merge_advice(r.stdout), r.stdout


def test_t4g_prime_the_two_dot_residue_names_what_the_switch_writes(live):
    """T4g': the probe, pinned: with F and Z on disk, the two-dot residue lists
    both, the symmetric (main's) lists only Z."""
    root = live["root"]
    (root / "F").write_text("f\n")
    (root / "Z").write_text("z\n")

    def residue(*extra: str) -> list[str]:
        r = subprocess.run(
            [
                "bash",
                "-c",
                '. "$1/deploy_checkout.sh" && genesis_merge_residue "$2" "$3" "$4" ${5:+"$5"}',
                "t",
                str(LIB),
                str(root),
                live["old"],
                live["new"],
                *extra,
            ],
            capture_output=True,
            text=True,
            env=_git_env(),
        )
        assert r.returncode == 0, r.stderr
        return sorted(rec.removeprefix("?? ") for rec in r.stdout.split("\0") if rec)

    assert residue() == ["Z"]
    assert residue("--two-dot") == ["F", "Z"]


# ── review fixes (#3492 PR C): direction, foreign edits, after adoption ──────


def _sentences(text: str) -> list[str]:
    return [s for s in text.replace("\n", " ").split(". ") if s.strip()]


def forward_message_is_true(text: str) -> None:
    """HEAD on `live` at the rebuilt tip: the remedy finishes FORWARD with
    --post-merge; it never says not to, and never offers Dismiss without saying
    the checkout then holds code bootstrap never ran on (SF1)."""
    assert "finish with scripts/update.sh --post-merge" in text, text
    assert "Do NOT run" not in text, text
    for s in _sentences(text):
        if "Dismiss" in s:
            assert "bootstrap never ran on" in s, s


def test_sf1_a_forward_mixed_tree_is_finished_with_post_merge(guard, live):
    """SF1: HEAD at the target, a tag-side file written by a rollback killed
    partway. Restoring to HEAD puts the FORWARD tree back, so the remedy is to
    finish with --post-merge; a bare Dismiss would drop the only guard against
    restarting onto code bootstrap never ran on."""
    _git(live["root"], "checkout", "-q", "-B", "live", live["new"])
    (live["root"] / "O").write_text("o\n")
    r = _cli(live, _state(live, live_row(live, "done")))
    assert r.returncode == 4, r.stdout + r.stderr
    forward_message_is_true(r.stdout)


def test_sf1_a_tag_side_mixed_tree_never_recommends_post_merge(guard, live):
    (live["root"] / "Z").write_text("z\n")
    r = _cli(live, _state(live, live_row(live)))
    assert r.returncode == 4
    assert not post_merge_advice(r.stdout), r.stdout
    assert "scripts/deploy_candidates repair-move" in r.stdout, r.stdout
    assert "bootstrap never ran on" not in r.stdout, r.stdout


@pytest.mark.parametrize("at", ["tag", "target"])
def test_sf2_a_foreign_edit_outside_the_switch_is_not_a_mixed_tree(guard, live, at):
    """SF2: A is the same in the tag and the target, so no switch between them
    writes it: an edit there is someone else's, and it guards (as a foreign
    tracked edit always has) without claiming the tree is half-written."""
    phase = "moving"
    if at == "target":
        _git(live["root"], "checkout", "-q", "-B", "live", live["new"])
        phase = "done"
    (live["root"] / "A").write_text("a session's edit\n")
    verdict, reason = _live_verdict(guard, live, live_row(live, phase))
    # At the tag the update moved nothing (HOLD: Dismiss, run it again); at the
    # target it moved and the code awaits bootstrap (GUARDS: --post-merge).
    assert verdict == ("holds" if at == "tag" else "guards"), reason


@pytest.mark.parametrize("at", ["tag", "target"])
def test_sf2_an_edit_on_a_switch_path_is_still_a_mixed_tree(guard, live, at):
    """Control: O (tag only) and Z (target only) are paths the switch writes."""
    phase, path = "moving", "O"
    if at == "target":
        _git(live["root"], "checkout", "-q", "-B", "live", live["new"])
        phase, path = "done", "Z"
    (live["root"] / path).write_text("half\n")
    verdict, reason = _live_verdict(guard, live, live_row(live, phase))
    assert verdict == "mixed_tree" and f"  M {path}" in reason, reason


def _residue(root: Path, frm: str, to: str, *extra: str) -> str:
    r = subprocess.run(
        [
            "bash",
            "-c",
            '. "$1/deploy_checkout.sh" && genesis_merge_residue "$2" "$3" "$4" ${5:+"$5"}',
            "t",
            str(LIB),
            str(root),
            frm,
            to,
            *extra,
        ],
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_sf2_the_two_dot_residue_keeps_only_the_switchs_paths(live):
    """--two-dot keeps tracked dirt on the switch's own paths only. Main's mode
    filters the same way over its own range (old...new writes neither A nor O
    here), and with no range at all every tracked change is listed."""
    root = live["root"]
    (root / "A").write_text("foreign\n")
    (root / "O").write_text("half\n")
    two_dot = _residue(root, live["old"], live["new"], "--two-dot")
    assert " M O" in two_dot and " M A" not in two_dot, two_dot
    main = _residue(root, live["old"], live["new"])
    assert " M A" not in main and " M O" not in main, main
    whole = _residue(root, live["old"], "")
    assert " M A" in whole and " M O" in whole, whole


def _adopted_row(d: dict, **over) -> dict:
    """update.sh's "merging" row after it adopted the rebuilt tip: the record is
    gone, own_head is the tip (SF4)."""
    row = live_row(d, **over)
    row.pop("live_move")
    row["own_head"] = d["new"]
    return row


def test_sf4_a_rollback_killed_after_adoption_is_a_mixed_tree(guard, live):
    """SF4: the rollback after adoption (a units refusal, a failed bootstrap,
    migrations or health check) was killed partway back to the tag: HEAD at the
    adopted tip, O (tag only) written. Without the record the row is still a
    live row, judged toward own_head."""
    _git(live["root"], "checkout", "-q", "-B", "live", live["new"])
    (live["root"] / "O").write_text("o\n")
    verdict, reason = _live_verdict(guard, live, _adopted_row(live))
    assert verdict == "mixed_tree" and "  ?? O" in reason, reason
    r = _cli(live, live["home"] / "update_state.json")
    assert r.returncode == 4
    forward_message_is_true(r.stdout)


def test_sf4_a_clean_adopted_row_reads_as_before(guard, live):
    """Control: clean at the adopted tip it guards (moved, not bootstrapped);
    back at the tag, clean, it guards nothing."""
    _git(live["root"], "checkout", "-q", "-B", "live", live["new"])
    verdict, reason = _live_verdict(guard, live, _adopted_row(live))
    assert verdict == "guards" and "bootstrap has not run on" in reason, reason
    _git(live["root"], "checkout", "-q", "-B", "live", live["old"])
    assert _live_verdict(guard, live, _adopted_row(live))[0] == "safe"


@pytest.mark.parametrize("tag", ["pre~0", "pre^{commit}", "pre@{0}", "-pre"])
def test_n2_a_rollback_tag_that_is_not_a_tag_name_guards(guard, repo, tag):
    """N2: the rollback tag is a NAME, never a revision expression: "pre~0" or
    "pre^{commit}" would resolve to HEAD and clear a state no real tag clears."""
    state = {
        "phase": "fetching",
        "rollback_tag": tag,
        "deploy_head": repo["incoming"],
        "post_merge": False,
    }
    assert _verdict(guard, repo, state) == "holds"
    state["rollback_tag"] = "pre"
    assert _verdict(guard, repo, state) == "safe", "control: the tag by its name"


def test_n5_control_characters_in_a_path_never_reach_the_terminal(guard, repo):
    """N5: residue paths are printed to terminals and into the supervising
    session's summary; an ESC or a tab in a file name is shown as "?"."""
    name = "e\x1bvil\tname.txt"
    root = repo["root"]
    (root / name).write_text("x\n")
    _git(root, "add", "--", name)
    _git(root, "commit", "-qm", "incoming with an odd name")
    incoming = _git(root, "rev-parse", "HEAD")
    _git(root, "reset", "-q", "--hard", "pre")
    (root / name).write_text("x\n")
    state = {"phase": "merging", "rollback_tag": "pre", "deploy_head": incoming}
    verdict, reason = guard.verdict(str(_state(repo, state)), str(root))
    assert verdict == "mixed_tree", reason
    # One shell token, in bash's $'...' form (#3532 round 3): one path, one line.
    assert "$'e\\x1bvil\\tname.txt'" in reason, reason
    bad = [c for c in reason if (ord(c) < 0x20 and c != "\n") or ord(c) == 0x7F]
    assert not bad, repr(reason)


def _incoming_editing(repo: dict, path: str) -> str:
    """A commit off the tag that changes ``path`` (HEAD stays at the tag)."""
    root = repo["root"]
    (root / path).write_text("incoming\n")
    _git(root, "add", "--", path)
    _git(root, "commit", "-qm", f"incoming edits {path}")
    sha = _git(root, "rev-parse", "HEAD")
    _git(root, "reset", "-q", "--hard", "pre")
    return sha


def test_main_a_foreign_edit_outside_the_merge_guards_not_mixed(guard, repo):
    """Main's residue counts only the paths its merge writes (pre...deploy_head),
    as a live row's does: an indexer's or another session's edit elsewhere
    guards (as before #3492) and is not a half-written tree."""
    incoming = _incoming_editing(repo, "b.txt")
    (repo["root"] / "a.txt").write_text("a session's edit\n")
    state = {"phase": "merging", "rollback_tag": "pre", "deploy_head": incoming}
    verdict, reason = guard.verdict(str(_state(repo, state)), str(repo["root"]))
    assert verdict == "holds", reason


def test_main_an_edit_on_a_path_the_merge_writes_is_mixed(guard, repo):
    incoming = _incoming_editing(repo, "a.txt")
    (repo["root"] / "a.txt").write_text("half\n")
    state = {"phase": "merging", "rollback_tag": "pre", "deploy_head": incoming}
    verdict, reason = guard.verdict(str(_state(repo, state)), str(repo["root"]))
    assert verdict == "mixed_tree" and "a.txt" in reason, reason


# ── #3532 round 1 ───────────────────────────────────────────────────────────


def _failing_git(tmp_path: Path, monkeypatch, *needles: str) -> None:
    """Put a git on PATH that fails (exit 128) for a call whose arguments hold
    every one of ``needles`` and runs the real git for every other call: a read
    that fails inside a process substitution, whose status nothing checked."""
    real = shutil.which("git")
    bin_ = tmp_path / "failing-git-bin"
    bin_.mkdir()
    test = " && ".join(f'case " $* " in *" {n} "*) ;; *) false ;; esac' for n in needles)
    (bin_ / "git").write_text(f'#!/bin/sh\nif {test}; then exit 128; fi\nexec "{real}" "$@"\n')
    (bin_ / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")


def _main_half_written(repo: dict, left: str) -> dict:
    """A main merge killed with its own residue on disk: the merge
    (pre...incoming) edits a.txt and adds b.txt. ``left`` "tracked": a.txt
    half-edited (seen only through status); "added": b.txt written (seen only
    through the collision scan)."""
    root = repo["root"]
    (root / "a.txt").write_text("incoming\n")
    (root / "b.txt").write_text("b\n")
    _git(root, "add", "a.txt", "b.txt")
    _git(root, "commit", "-qm", "incoming")
    incoming = _git(root, "rev-parse", "HEAD")
    _git(root, "reset", "-q", "--hard", "pre")
    if left == "tracked":
        (root / "a.txt").write_text("half\n")
    else:
        (root / "b.txt").write_text("b\n")
    return {"phase": "merging", "rollback_tag": "pre", "deploy_head": incoming}


@pytest.mark.parametrize(
    ("needles", "left"),
    [
        (("status", "-z"), "tracked"),
        (("diff", "-z", "--name-only"), "tracked"),
        (("diff", "--name-only"), "tracked"),
        (("diff", "-z", "--diff-filter=d"), "added"),
    ],
    ids=["status", "diff-names", "both-diff-listings", "collision-diff"],
)
def test_r1_a_failed_listing_never_reads_as_no_residue(
    guard, repo, tmp_path, monkeypatch, needles, left
):
    """Item 1: a read inside a process substitution whose status nothing
    checked turned a failure into an empty list, so residue vanished and the
    tree read as not half-written. Every such read now fails closed (the lines
    are kept, or the range is reported unreadable). Each case leaves only the
    residue that one read sees."""
    state = _main_half_written(repo, left)
    assert _verdict(guard, repo, state) == "mixed_tree", "control"
    _failing_git(tmp_path, monkeypatch, *needles)
    verdict, reason = guard.verdict(str(_state(repo, state)), str(repo["root"]))
    assert verdict == "mixed_tree", reason


def test_r1_a_path_with_a_newline_is_one_quoted_line(guard, repo):
    """Item 4: NUL-separated records joined with newlines showed a file name
    holding a newline as two paths. Every displayed path (tracked change,
    collision) is git's C-style quoted form."""
    root = repo["root"]
    tracked, added = "m\nn.txt", "a\nb.txt"
    (root / tracked).write_text("m\n")
    _git(root, "add", "--", tracked)
    _git(root, "commit", "-qm", "a tracked file with a newline")
    _git(root, "tag", "-f", "pre")
    (root / tracked).write_text("incoming\n")
    (root / added).write_text("x\n")
    _git(root, "add", "--", tracked, added)
    _git(root, "commit", "-qm", "incoming")
    incoming = _git(root, "rev-parse", "HEAD")
    _git(root, "reset", "-q", "--hard", "pre")
    (root / tracked).write_text("half\n")
    (root / added).write_text("x\n")
    state = {"phase": "merging", "rollback_tag": "pre", "deploy_head": incoming}
    verdict, reason = guard.verdict(str(_state(repo, state)), str(root))
    assert verdict == "mixed_tree", reason
    assert "$'m\\nn.txt'" in reason and "$'a\\nb.txt'" in reason, reason
    lines = [ln.strip() for ln in reason.split("\n")]
    assert "n.txt" not in lines and "b.txt" not in lines and "a" not in lines, reason


def test_r1_an_adopted_row_at_the_tag_is_judged_only_as_a_live_row(guard, live):
    """Item 5: after adoption (no record, own_head the tip), HEAD back at the
    tag and clean as far as a switch between the two tips goes: a file at a
    path only the upstream base range names, which neither live tip has, is not
    this update's residue. It used to fall through to main's rule and read
    mixed_tree."""
    root = live["root"]
    _git(root, "checkout", "-q", "--detach", live["main0"])
    (root / "P").write_text("p\n")
    _git(root, "add", "P")
    _git(root, "commit", "-qm", "an upstream base with P")
    base = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", "-B", "live", live["old"])
    (root / "P").write_text("p\n")
    row = live_row(live)
    row.pop("live_move")
    row.update(own_head=live["new"], deploy_head=base)
    verdict, reason = _live_verdict(guard, live, row)
    assert verdict == "safe", reason


# ── #3532 round 2: ACTIVATE (GUARDS, exit 0) vs HOLD (exit 6) ──────────────────


def _merged(repo: dict) -> str:
    """A main update that merged: HEAD (on main) at its merge, which edits a.txt
    and adds b.txt over the tag. Returns the merge (the row's own_head)."""
    root = repo["root"]
    (root / "a.txt").write_text("merged\n")
    (root / "b.txt").write_text("b\n")
    _git(root, "add", "a.txt", "b.txt")
    _git(root, "commit", "-qm", "the update's merge")
    return _git(root, "rev-parse", "HEAD")


def _merged_row(own: str, phase: str = "merging") -> dict:
    return {
        "phase": phase,
        "rollback_tag": "pre",
        "original_branch": "main",
        "own_head": own,
        "deploy_head": own,
        "post_merge": False,
    }


@pytest.mark.parametrize("phase", ["merging", "bootstrap", "migrations", "health_check"])
def test_p1_a_main_rollback_killed_partway_is_a_forward_mixed_tree(guard, repo, phase):
    """P1: the merge was adopted (HEAD at own_head), then the rollback's
    checkout back to the tag was killed after writing a.txt's tag version.
    --post-merge (which finishes exit 0) would bootstrap that tree."""
    own = _merged(repo)
    (repo["root"] / "a.txt").write_text("a\n")
    r = _cli(repo, _state(repo, _merged_row(own, phase)))
    assert r.returncode == 4, r.stdout + r.stderr
    assert "a.txt" in r.stdout
    forward_message_is_true(r.stdout)


@pytest.mark.parametrize("phase", ["merging", "bootstrap"])
def test_activate_a_clean_merge_awaiting_bootstrap_still_guards(guard, repo, phase):
    """Control: merged code complete on disk, bootstrap not run: exit 0, the
    state --post-merge finishes."""
    own = _merged(repo)
    r = _cli(repo, _state(repo, _merged_row(own, phase)))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Finish it with scripts/update.sh --post-merge" in r.stdout, r.stdout


def test_p2_a_live_row_with_only_a_foreign_edit_holds(guard, live):
    """P2: the rebuild never moved the checkout (HEAD at the tag, no residue on
    the switch's paths) and A carries someone's edit. Finishing forward would
    bootstrap the OLD tip and clear a state whose target never deployed."""
    (live["root"] / "A").write_text("a session's edit\n")
    r = _cli(live, _state(live, live_row(live)))
    assert r.returncode == 6, r.stdout + r.stderr
    assert "never changed the checkout" in r.stdout and "Dismiss" in r.stdout, r.stdout
    assert "scripts/update.sh again" in r.stdout, r.stdout
    assert not post_merge_advice(r.stdout), r.stdout


def _hold(guard, r: dict, state) -> str:
    v, reason = guard.verdict(str(_state(r, state)), str(r["root"]))
    assert v == "holds", (v, reason)
    msg = guard.message("state", v, reason)
    assert not post_merge_advice(msg), msg
    return reason


def test_hold_main_foreign_edit_at_the_tag(guard, repo):
    incoming = _git(repo["root"], "rev-parse", "HEAD")
    (repo["root"] / "a.txt").write_text("a session's edit\n")
    state = {"phase": "merging", "rollback_tag": "pre", "deploy_head": repo["incoming"]}
    assert incoming
    assert "never changed the checkout" in _hold(guard, repo, state)


def test_hold_a_merging_row_whose_checkout_moved_elsewhere(guard, repo):
    own = _merged(repo)
    _git(repo["root"], "commit", "-q", "--allow-empty", "-m", "someone else's")
    assert "not where the update left it" in _hold(guard, repo, _merged_row(own))


def test_hold_a_later_phase_whose_checkout_moved(guard, repo):
    own = _merged(repo)
    _git(repo["root"], "commit", "-q", "--allow-empty", "-m", "someone else's")
    _hold(guard, repo, _merged_row(own, "bootstrap"))


def test_hold_a_plain_fetching_row_off_its_tag(guard, repo):
    _merged(repo)
    state = {"phase": "fetching", "rollback_tag": "pre", "deploy_head": "x", "post_merge": False}
    _hold(guard, repo, state)


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", {"pid": 1}, {"phase": "merged"}])
def test_hold_a_record_that_cannot_be_trusted(guard, repo, content):
    assert "Nothing can be told" in _hold(guard, repo, content)


def test_hold_a_merge_in_progress(guard, repo):
    root = repo["root"]
    _git(root, "checkout", "-q", "-b", "side")
    (root / "a.txt").write_text("side\n")
    _git(root, "commit", "-qam", "side")
    _git(root, "checkout", "-q", "main")
    (root / "a.txt").write_text("main\n")
    _git(root, "commit", "-qam", "main")
    _git(root, "tag", "-f", "pre")
    subprocess.run(
        ["git", "-C", str(root), "merge", "-q", "side"], capture_output=True, env=_git_env()
    )
    state = {"phase": "merging", "rollback_tag": "pre", "deploy_head": repo["incoming"]}
    assert "merge is in progress" in _hold(guard, repo, state)


def test_hold_a_live_checkout_neither_at_the_tag_nor_at_the_tip(guard, live):
    _git(live["root"], "checkout", "-q", "-B", "live", live["main0"])
    _hold(guard, live, live_row(live, "done"))


def test_activate_a_post_merge_fetching_row(guard, repo):
    """A --post-merge that stopped before bootstrap: merged code it was
    finishing, which a new --post-merge finishes."""
    state = {"phase": "fetching", "rollback_tag": "pre", "deploy_head": "x", "post_merge": True}
    assert _verdict(guard, repo, state) == "guards"


def test_activate_resolved_conflicts_still_finish(guard, repo):
    """The conflict resolver merged deploy_head onto the tag and committed:
    own_head is still the tag (the update never adopted a merge), and
    --post-merge is what finishes it."""
    root = repo["root"]
    tag = _git(root, "rev-parse", "pre")
    _git(root, "merge", "-q", "--no-ff", "-m", "resolved", repo["incoming"])
    state = {
        "phase": "merging",
        "rollback_tag": "pre",
        "own_head": tag,
        "deploy_head": repo["incoming"],
    }
    path = _state(repo, state)
    (repo["home"] / "update_conflicts.json").write_text(
        json.dumps({"deploy_head": repo["incoming"]})
    )
    assert guard.verdict(str(path), str(root))[0] == "guards"


def test_conflict_pending_is_unchanged(guard, repo):
    state = {"phase": "merging", "rollback_tag": "pre", "deploy_head": repo["incoming"]}
    assert _verdict(guard, repo, state, {"deploy_head": repo["incoming"]}) == "conflict_pending"


# ── #3532 round-2 class audit ───────────────────────────────────────────────


def _post_merge_fetching(own: str) -> dict:
    return {
        "phase": "fetching",
        "rollback_tag": "pre",
        "original_branch": "main",
        "own_head": own,
        "deploy_head": own,
        "post_merge": True,
    }


def test_audit1_a_post_merge_fetching_row_whose_head_moved_holds(guard, repo):
    """Audit 1: a --post-merge wrote "fetching" naming its own_head and stopped;
    the checkout then moved. Finishing would activate code it never checked."""
    own = _merged(repo)
    assert _verdict(guard, repo, _post_merge_fetching(own)) == "guards", "control"
    _git(repo["root"], "commit", "-q", "--allow-empty", "-m", "someone else's")
    assert "not where the update left it" in _hold(guard, repo, _post_merge_fetching(own))


def test_audit1_a_post_merge_fetching_row_without_its_tag_holds(guard, repo):
    own = _merged(repo)
    _git(repo["root"], "tag", "-d", "pre")
    _hold(guard, repo, _post_merge_fetching(own))


def _conflicted_at(repo: dict) -> str:
    """HEAD at the update's merge (own_head) with a conflicting merge started on
    top of it: MERGE_HEAD present, markers in a.txt. Returns own_head."""
    root = repo["root"]
    own = _merged(repo)
    _git(root, "checkout", "-q", "-b", "side", "pre")
    (root / "a.txt").write_text("side\n")
    _git(root, "commit", "-qam", "side")
    _git(root, "checkout", "-q", "main")
    subprocess.run(
        ["git", "-C", str(root), "merge", "-q", "side"], capture_output=True, env=_git_env()
    )
    assert (root / ".git" / "MERGE_HEAD").exists(), "the fixture built no merge in progress"
    assert _git(root, "rev-parse", "HEAD") == own
    return own


@pytest.mark.parametrize("phase", ["fetching", "bootstrap", "migrations", "health_check"])
def test_audit2_a_merge_in_progress_holds_in_every_phase(guard, repo, phase):
    """Audit 2: MERGE_HEAD was checked only in "merging"; in any other phase a
    conflicted merge over the update's code read exit 0, and --post-merge would
    bootstrap over the markers."""
    own = _conflicted_at(repo)
    row = _merged_row(own, phase)
    row["post_merge"] = phase == "fetching"
    reason = _hold(guard, repo, row)
    assert "merge is in progress" in reason, reason
    assert "update_in_progress.pid" in reason, "N10: the remedy must not abort a resolver's merge"


def _landed_before_adoption(repo: dict, shape: str) -> dict:
    """update.sh's merge landed but the run died before it recorded own_head
    (still the tag's commit). "ff": HEAD is deploy_head; "merge": HEAD is a
    two-parent merge of the tag and deploy_head, as `git merge` makes it."""
    root = repo["root"]
    if shape == "merge":
        (root / "local.txt").write_text("local\n")
        _git(root, "add", "local.txt")
        _git(root, "commit", "-qm", "local work under the tag")
        _git(root, "tag", "-f", "pre")
        tag = _git(root, "rev-parse", "HEAD")
        _git(root, "merge", "-q", "--no-ff", "--no-edit", repo["incoming"])
    else:
        tag = _git(root, "rev-parse", "pre")
        _git(root, "merge", "-q", "--ff-only", repo["incoming"])
    return {
        "phase": "merging",
        "rollback_tag": "pre",
        "original_branch": "main",
        "own_head": tag,
        "deploy_head": repo["incoming"],
        "post_merge": False,
    }


@pytest.mark.parametrize("shape", ["ff", "merge"])
def test_audit3_a_merge_that_landed_before_adoption_guards(guard, repo, shape):
    """Audit 3 (a regression in round 2): the merge landed, the run died before
    adopting it (own_head is still the tag's commit). HEAD is exactly what
    update.sh's own adoption rule accepts, so --post-merge finishes it (exit 0);
    "Dismiss and run again" would leave it never bootstrapped (the rerun finds
    nothing new)."""
    state = _landed_before_adoption(repo, shape)
    r = _cli(repo, _state(repo, state))
    assert r.returncode == 0, r.stdout + r.stderr


def test_audit3_any_other_commit_over_the_tag_still_holds(guard, repo):
    """Control: a commit on top of the tag that is not the update's merge shape."""
    state = _landed_before_adoption(repo, "ff")
    _git(repo["root"], "commit", "-q", "--allow-empty", "-m", "someone else's")
    _hold(guard, repo, state)


# ── #3532 round 3 ───────────────────────────────────────────────────────────

#: name on disk -> (the added file's token, the tracked "<name>.t" file's token):
#: every residue path is ONE shell token, rendered from its raw name by one
#: rule (shlex.quote, or $'...' when it holds a control character, so the line
#: stays one line and pastes back as the same name).
DISPLAY = {
    "trail ": ("'trail '", "'trail .t'"),
    " lead": ("' lead'", "' lead.t'"),
    "tab\tx": ("$'tab\\tx'", "$'tab\\tx.t'"),
    "nl\nx": ("$'nl\\nx'", "$'nl\\nx.t'"),
    "cr\rx": ("$'cr\\x0dx'", "$'cr\\x0dx.t'"),
    "crlf\r\nx": ("$'crlf\\x0d\\nx'", "$'crlf\\x0d\\nx.t'"),
    'q"x': ("'q\"x'", "'q\"x.t'"),
    "\u00fcn": ("'\u00fcn'", "'\u00fcn.t'"),
    "back\\slash": ("'back\\slash'", "'back\\slash.t'"),
    "it's": ("'it'\"'\"'s'", "'it'\"'\"'s.t'"),
    "plain.txt": ("plain.txt", "plain.txt.t"),
}


@pytest.mark.parametrize("name", list(DISPLAY), ids=lambda n: repr(n))
def test_r3_every_residue_path_is_shown_by_one_quoting_rule(guard, repo, name):
    """P2 (the second display-quoting finding): git's own listing left a
    trailing space unquoted, so the shown name was not the name on disk. Every
    residue path (a tracked change and an added file alike) is now rendered from
    its raw name, carried NUL-separated from the shell, as one shell token."""
    root = repo["root"]
    tracked = name + ".t"
    (root / tracked).write_text("t\n")
    _git(root, "add", "--", tracked)
    _git(root, "commit", "-qm", "a tracked file")
    _git(root, "tag", "-f", "pre")
    (root / tracked).write_text("incoming\n")
    (root / name).write_text("x\n")
    _git(root, "add", "--", tracked, name)
    _git(root, "commit", "-qm", "incoming")
    incoming = _git(root, "rev-parse", "HEAD")
    _git(root, "reset", "-q", "--hard", "pre")
    (root / tracked).write_text("half\n")
    (root / name).write_text("x\n")
    state = {"phase": "merging", "rollback_tag": "pre", "deploy_head": incoming}
    verdict, reason = guard.verdict(str(_state(repo, state)), str(root))
    assert verdict == "mixed_tree", reason
    lines = reason.split("\n")[1:]
    want_added, want_tracked = DISPLAY[name]
    assert f"  ?? {want_added}" in lines, (want_added, lines)
    assert f"   M {want_tracked}" in lines, (want_tracked, lines)
    assert len(lines) == 2, lines
    for token in (want_added, want_tracked):
        cmd = f"printf %s {token}"
        out = subprocess.run(["bash", "-c", cmd], capture_output=True).stdout
        got = out.decode("utf-8", "surrogateescape")  # bytes: no newline translation
        assert got in (name, tracked), (token, got)


@pytest.mark.parametrize("name", ["\x9b31m", "a\u2028b", "a\u2029b", "c\x85d"])
def test_review_n2_c1_controls_and_line_separators_are_escaped(guard, name):
    """#3532 round-3 review N2: an 8-bit CSI (U+009B) or a line separator in a
    name never reaches a terminal raw; the token still pastes back exactly
    (escaped as its UTF-8 bytes, which needs no locale)."""
    token = guard._shell_token(name)
    assert all(not (0x80 <= ord(c) <= 0x9F) and c not in "\u2028\u2029" for c in token), token
    got = subprocess.run(["bash", "-c", f"printf %s {token}"], capture_output=True).stdout
    assert got == name.encode(), (token, got)


@pytest.mark.parametrize(
    ("path", "verdict"),
    [
        ("AGENTS.md", "guards"),
        ("config/procedure_triggers.yaml", "guards"),
        ("docs/AGENTS.md", "mixed_tree"),
        ("d/x AGENTS.md", "mixed_tree"),
    ],
)
def test_r4_a_runtime_rewrite_of_an_ephemeral_path_is_not_a_forward_mixed_tree(
    guard, live, path, verdict
):
    """Round 4 (CodeRabbit): after adoption the runtime rewrites the known
    ephemeral tracked paths (EPHEMERAL_DIRTY_RE, deploy_marker.sh: GitNexus
    rewrites AGENTS.md's stat block), so a change there on a path the update
    brought in is not a rollback killed partway; it must not refuse recovery or
    --post-merge. The list matches exact paths only: docs/AGENTS.md still counts."""
    root = live["root"]
    _git(root, "checkout", "-q", "-B", "live", live["new"])
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text("generated\n")
    _git(root, "add", "--", path)
    _git(root, "commit", "-qm", "the update brings the path in")
    tip = _git(root, "rev-parse", "HEAD")
    (root / path).write_text("regenerated at runtime\n")
    got, reason = _live_verdict(guard, live, {**_adopted_row(live), "own_head": tip})
    assert got == verdict, reason
