"""deploy_candidates: the manifest, `add`, and how a candidate is pinned to a commit.

The scratch world is tests/test_scripts/_deploy_candidates_world.py. Each refusal
has a control that must succeed, so an engine that refuses everything (or
nothing) fails the suite.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.test_scripts._deploy_candidates_world import SCRIPTS

# ── add, list and the manifest ────────────────────────────────────────────


def test_add_pins_the_exact_head(dc, dc_ready, capsys):
    w = dc_ready
    head = w.candidate("feat/x", {"x.txt": "x\n"})
    w.pr(11, "feat/x")
    assert w.add(dc, "feat/x", "--pr", "11") == 0, capsys.readouterr()
    data = w.manifest()
    assert data["version"] == 3
    assert data["repo"] == os.path.realpath(w.root / ".git")
    [e] = data["candidates"]
    assert e == {
        "branch": "feat/x",
        "pr": 11,
        "owner_session": "s1",
        "added_at": e["added_at"],
        "verified_head": head,
        "hook_approval": None,
    }
    capsys.readouterr()
    assert w.run(dc, "list") == 0
    out = capsys.readouterr().out
    assert "feat/x" in out and "#11" in out and f"pinned at {head[:12]}" in out


@pytest.mark.parametrize("flag", ["--owner-approved", "--approved-in-chat"])
def test_there_is_no_approval_flag(dc, dc_ready, flag):
    """Owner ruling 2026-10-01: running an unmerged branch on this install's own
    server needs no approval step, so no flag claims one. The one approval that
    exists is --approve-hooks (owner ruling, #2978), and it covers hook changes
    only: see test_deploy_candidates_hooks.py."""
    w = dc_ready
    w.candidate("feat/x", {"x.txt": "x\n"})
    assert w.run(dc, "add", "feat/x", "--owner", "s1", flag) == 2
    assert not w.manifest_path.exists()


@pytest.mark.parametrize(
    "env",
    [
        {},  # no Claude Code marker at all: another agent, a script, `env -i`
        {"CLAUDECODE": "1"},  # a Claude Code session
        {"CLAUDECODE": "1", "GENESIS_CC_SESSION": "1"},  # a dispatched session
    ],
)
def test_add_needs_no_approval_from_any_caller(dc, dc_ready, env):
    w = dc_ready
    head = w.candidate("feat/x", {"x.txt": "x\n"})
    assert w.run(dc, "add", "feat/x", "--owner", "s1", extra_env=env) == 0
    assert w.manifest()["candidates"][0]["verified_head"] == head


@pytest.mark.parametrize(
    ("argv", "why"),
    [
        (("--owner", "  "), "--owner is empty"),
        (("--owner", "s1", "--pr", "0"), "not a PR number"),
        (("--owner", "s1", "--pr", "-3"), "not a PR number"),
    ],
)
def test_add_refuses_values_no_reader_could_use(dc, dc_ready, capsys, argv, why):
    w = dc_ready
    w.candidate("feat/x", {"x.txt": "x\n"})
    assert w.run(dc, "add", "feat/x", *argv) == 1
    assert why in capsys.readouterr().err
    assert not w.manifest_path.exists()


@pytest.mark.parametrize("bad", ["live/x", "main/x", "live", "main", "a..b"])
def test_add_refuses_a_name_a_candidate_cannot_have(dc, dc_ready, capsys, bad):
    w = dc_ready
    assert w.add(dc, bad) == 1
    assert "cannot be a candidate" in capsys.readouterr().err


def test_re_adding_moves_the_pin_to_the_new_head_and_keeps_added_at(dc, dc_ready, capsys):
    w = dc_ready
    w.candidate("feat/x", {"x.txt": "x\n"})
    assert w.add(dc, "feat/x") == 0
    first = w.manifest()["candidates"][0]
    h2 = w.candidate("feat/x", {"x.txt": "x2\n"})
    assert w.add(dc, "feat/x") == 0
    [e] = w.manifest()["candidates"]
    assert e["verified_head"] == h2
    assert e["added_at"] == first["added_at"]
    assert "re-verified" in capsys.readouterr().out


def test_add_refuses_a_branch_sharing_an_unmerged_commit_with_a_listed_one(dc, dc_ready, capsys):
    """One candidate per commit: a stack goes in as its top branch."""
    w = dc_ready
    s1 = w.candidate("feat/s", {"s.txt": "s\n"})
    w.candidate("feat/t", {"t.txt": "t\n"}, base=s1)
    assert w.add(dc, "feat/s") == 0, capsys.readouterr()
    before = w.manifest_path.read_text()
    capsys.readouterr()
    assert w.add(dc, "feat/t") == 1
    err = capsys.readouterr().err
    assert "feat/t shares unmerged commits with feat/s" in err and "top branch" in err, err
    assert "drop feat/s, then add feat/t" in err, err
    assert w.manifest_path.read_text() == before
    # Controls: an independent branch is added, and re-adding feat/s after it
    # moved on does not count feat/s against itself.
    w.candidate("feat/c", {"c.txt": "c\n"})
    assert w.add(dc, "feat/c") == 0, capsys.readouterr()
    # A re-add is judged at the branch's NEW head: feat/c, listed while it was
    # independent, then merges feat/s. Judged at its listed head it would pass.
    w.git(w.tmp / "wt-feat-c", "merge", "-q", "--no-edit", "feat/s")
    before = w.manifest_path.read_text()
    capsys.readouterr()
    assert w.add(dc, "feat/c") == 1
    err = capsys.readouterr().err
    assert "feat/c shares unmerged commits with feat/s" in err, err
    assert w.manifest_path.read_text() == before
    h2 = w.candidate("feat/s", {"s2.txt": "s2\n"})
    assert w.add(dc, "feat/s") == 0, capsys.readouterr()
    assert {c["branch"]: c["verified_head"] for c in w.manifest()["candidates"]}["feat/s"] == h2


def test_add_refusal_names_the_remedy_for_each_way_two_branches_share_commits(dc, dc_ready, capsys):
    """Three shapes share commits, and each has a different remedy: the top is
    already listed (nothing to add), and two siblings cut from one branch (no
    top: rebase or combine). The stacked-top-added-last shape is in the test
    above."""
    w = dc_ready
    s1 = w.candidate("feat/s", {"s.txt": "s\n"})
    w.candidate("feat/t", {"t.txt": "t\n"}, base=s1)
    w.candidate("feat/u", {"u.txt": "u\n"}, base=s1)
    assert w.add(dc, "feat/t") == 0, capsys.readouterr()
    capsys.readouterr()
    assert w.add(dc, "feat/s") == 1
    err = capsys.readouterr().err
    assert "feat/t, already listed, carries feat/s's commits" in err, err
    assert w.add(dc, "feat/u") == 1
    err = capsys.readouterr().err
    assert "cut from one branch" in err and "freshly fetched origin/main" in err, err
    assert "top branch" not in err, err
    assert [c["branch"] for c in w.manifest()["candidates"]] == ["feat/t"]


def test_add_refuses_a_pr_that_is_not_open_against_main_with_this_head(dc, dc_ready, capsys):
    w = dc_ready
    head = w.candidate("feat/x", {"x.txt": "x\n"})
    for n, kw, why in [
        (5, {"headRefName": "feat/other"}, "head branch is 'feat/other'"),
        (6, {"headRefOid": "1" * 40}, "not the pinned"),
        (7, {"baseRefName": "release"}, "targets 'release'"),
        (8, {"state": "CLOSED"}, "closed without merging"),
        (9, {"state": "MERGED"}, "has merged"),
    ]:
        w.pr(n, "feat/x", head, **kw)
        assert w.add(dc, "feat/x", "--pr", str(n)) == 1, kw
        assert why in capsys.readouterr().err, kw
    assert not w.manifest_path.exists()
    w.pr(10, "feat/x", head)
    assert w.add(dc, "feat/x", "--pr", "10") == 0


def test_add_and_drop_need_the_update_lock(dc, dc_ready, capsys):
    w = dc_ready
    w.candidate("feat/x", {"x.txt": "x\n"})
    assert w.run(dc, "add", "feat/x", "--owner", "s1", lock=False) == 1
    assert "update.lock" in capsys.readouterr().err
    assert w.add(dc, "feat/x") == 0
    assert w.run(dc, "drop", "feat/x", "--no-rebuild", lock=False) == 1
    assert "update.lock" in capsys.readouterr().err
    assert w.run(dc, "drop", "feat/x", "--no-rebuild") == 0


# ── the manifest's shape ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        json.dumps(
            {"version": 1, "repo": "/x", "candidates": []}
        ),  # v1 carried a self-supplied flag
        json.dumps({"version": 2, "repo": "/x", "candidates": []}),  # v2 had no hook_approval
        json.dumps({"version": 3, "repo": "rel/path", "candidates": []}),
        json.dumps({"version": 3, "repo": "/x", "candidates": [{"branch": "b"}]}),
    ],
)
def test_a_malformed_manifest_refuses_every_mutating_command(dc, dc_ready, capsys, text):
    w = dc_ready
    w.candidate("feat/x", {"x.txt": "x\n"})
    w.manifest_path.write_text(text)
    for argv in (
        ("add", "feat/x", "--owner", "s1"),
        ("drop", "feat/x"),
        ("rebuild",),
    ):
        assert w.run(dc, *argv) == 1, argv
        assert "manifest" in capsys.readouterr().err
    assert w.manifest_path.read_text() == text


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.update(owner_session=""),
        lambda e: e.update(pr=0),
        lambda e: e.update(branch="live/x"),
        lambda e: e.update(verified_head="abc"),
        lambda e: e.update(owner_approved=True),
        lambda e: e.update(approval={"kind": "chat"}),
        lambda e: e.pop("added_at"),
        lambda e: e.pop("hook_approval"),
        lambda e: e.update(hook_approval=True),
        lambda e: e.update(hook_approval={"head": e["verified_head"], "approved_by": "o"}),
        lambda e: e.update(
            hook_approval={"head": "0" * 40, "approved_by": "o", "approved_at": "t"}
        ),
        lambda e: e.update(
            hook_approval={"head": e["verified_head"], "approved_by": " ", "approved_at": "t"}
        ),
    ],
)
def test_the_validator_rejects_entries_no_command_would_write(dc, dc_ready, mutate):
    w = dc_ready
    w.candidate("feat/x", {"x.txt": "x\n"})
    e = w.entry("feat/x")
    assert dc.manifest.manifest_problem({"version": 3, "repo": "/x", "candidates": [e]}) == ""
    mutate(e)
    assert dc.manifest.manifest_problem({"version": 3, "repo": "/x", "candidates": [e]}) != ""


def test_a_change_that_would_write_a_malformed_manifest_writes_nothing(dc, dc_ready):
    w = dc_ready
    w.candidate("feat/x", {"x.txt": "x\n"})
    w.write_manifest([w.entry("feat/x")])
    before = w.manifest_path.read_text()
    store = dc.manifest.ManifestStore(w.home, lambda: os.path.realpath(w.root / ".git"))

    def bad(data):
        data["candidates"][0]["owner_session"] = ""
        return data

    with pytest.raises(dc.core.Refusal, match="malformed"):
        store.update(bad)
    assert w.manifest_path.read_text() == before


def test_a_manifest_for_another_repository_is_refused(dc, dc_ready, capsys):
    w = dc_ready
    w.candidate("feat/x", {"x.txt": "x\n"})
    e = w.entry("feat/x")
    w.manifest_path.write_text(
        json.dumps({"version": 3, "repo": "/some/other/.git", "candidates": [e]})
    )
    assert w.run(dc, "list") == 1
    assert "another repository" in capsys.readouterr().err


def test_git_location_variables_from_a_hook_context_do_not_redirect_the_engine(
    dc, dc_ready, tmp_path
):
    w = dc_ready
    w.candidate("feat/x", {"x.txt": "x\n"})
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", str(other)], env=w.env, check=True)
    extra = {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other), "GIT_INDEX_FILE": "/nope"}
    assert (
        w.run(
            dc,
            "add",
            "feat/x",
            "--owner",
            "s1",
            extra_env=extra,
        )
        == 0
    )
    assert w.manifest()["repo"] == os.path.realpath(w.root / ".git")


def test_a_linked_worktree_reports_the_main_checkout_and_refuses_changes(dc, dc_ready, capsys):
    w = dc_ready
    w.candidate("feat/x", {"x.txt": "x\n"})
    assert w.add(dc, "feat/x") == 0
    wt = w.tmp / "wt-feat-x"
    capsys.readouterr()
    env = dict(w.env)
    assert dc.main(["list"], env=env, gh=w.gh, serving=w.serving, root=wt) == 0
    out = capsys.readouterr().out
    assert "reporting the main checkout" in out and "feat/x" in out
    assert dc.main(["rebuild"], env=env, gh=w.gh, serving=w.serving, root=wt) == 1
    assert "linked worktree" in capsys.readouterr().err


def test_the_manifest_the_engine_writes_arms_the_hooks_for_this_repository_only(
    dc, dc_ready, tmp_path
):
    """The real pre-commit hook refuses a commit on `live` in the repository the
    engine's (version 2) manifest names, and leaves another clone's `live` alone:
    every reader of the manifest on main reads only its `repo` key."""
    w = dc_ready
    w.candidate("feat/x", {"x.txt": "x\n"})
    assert w.add(dc, "feat/x") == 0
    hook = SCRIPTS / "hooks" / "pre-commit"

    def commit_on_live(repo: Path) -> subprocess.CompletedProcess:
        w.git(repo, "checkout", "-q", "-B", "live")
        (repo / "z.txt").write_text("z\n")
        w.git(repo, "add", "z.txt")
        return subprocess.run(
            ["bash", str(hook)], cwd=repo, env=w.env, capture_output=True, text=True
        )

    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", "-q", str(w.origin), str(other)],
        env=w.env,
        check=True,
        capture_output=True,
    )
    assert commit_on_live(w.root).returncode == 1
    assert commit_on_live(other).returncode == 0


def _keys_read(source: str, func: str | None) -> set[str]:
    """Every key a reader takes from the parsed manifest, inside one function (or
    a whole script): ``m["k"]``, ``m.get("k")`` and ``"k" in m``, where ``m`` is a
    name bound from ``json.load(...)``."""
    tree = ast.parse(source)
    if func is not None:
        tree = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == func)
    loaded = {
        t.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Attribute)
        and n.value.func.attr == "load"
        for t in n.targets
        if isinstance(t, ast.Name)
    }
    assert loaded, "no json.load binding found: the reader changed shape"

    def is_manifest(node) -> bool:
        return isinstance(node, ast.Name) and node.id in loaded

    keys = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Subscript) and is_manifest(n.value):
            keys.add(getattr(n.slice, "value", "<computed>"))
        elif (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "get"
            and is_manifest(n.func.value)
        ):
            keys.add(getattr(n.args[0], "value", "<computed>") if n.args else "<none>")
        elif isinstance(n, ast.Compare) and any(is_manifest(c) for c in n.comparators):
            keys.add(getattr(n.left, "value", "<computed>"))
    return keys


def test_every_manifest_reader_on_this_tree_reads_only_the_repo_key():
    """The git hooks and the commit, merge and push guards read the manifest; a
    reader that consulted `version` or a candidate field would change behaviour
    now that the engine writes version 2. Every file that names the manifest is
    enumerated, and each must be one of the known readers."""
    root = SCRIPTS.parent
    found = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "grep",
            "-l",
            "deploy_manifest.json",
            "--",
            "scripts",
            "src",
            ".claude",
        ],
        capture_output=True,
        text=True,
    ).stdout.split()
    readers = {f for f in found if not Path(f).name.startswith("deploy_candidates")}
    known = {
        "scripts/hooks/git_push_guard.py": "_live_manifest_binding",
        "scripts/review_enforcement_commit.py": "_live_manifest_binding",
        "scripts/lib/live_checkout.py": "state",
        "scripts/hooks/pre-commit": None,
        "scripts/hooks/pre-merge-commit": None,
    }
    assert readers == set(known), f"a new manifest reader appeared: {sorted(readers - set(known))}"
    for f, func in known.items():
        text = (root / f).read_text()
        if func is None:  # a shell hook: its embedded python reads the manifest
            text = text.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        assert _keys_read(text, func) == {"repo"}, f


@pytest.mark.parametrize("bad", ["-x", "live/x", "main/y", "master/z", "live", "", "a..b", None, 3])
def test_a_candidate_name_must_be_a_branch_outside_live_and_main(dc, bad):
    assert not dc.core.valid_candidate_name(bad)
    assert dc.core.valid_candidate_name("feat/live-x")
