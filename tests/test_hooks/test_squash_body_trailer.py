"""The squash body a gated merge may carry, and why the gate never trusts it.

``--check-pr`` writes the PR body plus a ``Squashed-From: <reviewed head>`` trailer
and the branch's ``Genesis-Session:`` trailers to a file, and prints
``--body-file <path>`` in its merge-with line. Text a merge writes into the squash
commit is outside the scheduled leaks review, so the merge arm:

1. allows ``--body-file`` only as one long-form flag naming a file in the gate's
   own directory, by an absolute path that resolves to itself (the hook and gh
   are different processes, and the hook sees argv before the shell expands it);
2. refuses it on an unbound merge, inside a compound command, or with an output
   redirect (each could change the file between the hook's read and gh's);
3. recomputes the body from the live PR and requires byte equality.

The shadow-flag belt (``--body``/``--subject``/``--author-email``/``-F``) used to
run only inside the head binding, so ``# stale-review-override`` let their text
through; it now runs on every merge.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_HOOKS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"
sys.path.insert(0, str(_HOOKS))
_spec = importlib.util.spec_from_file_location("git_push_guard", _HOOKS / "git_push_guard.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_gm_spec = importlib.util.spec_from_file_location("gh_merge", _HOOKS / "gh_merge.py")
gh_merge = importlib.util.module_from_spec(_gm_spec)
_gm_spec.loader.exec_module(gh_merge)

HEAD = "0cd13afeb51025af5dc7bd24df1ffa57cd2babab"
OTHER = "1111111111111111111111111111111111111111"
BODY = "## Summary\r\nDoes a thing.  \r\n\r\nE2E: none — test\r\n"
MESSAGES = [
    "feat: one\n\nGenesis-Session: aaaa1111\nInstall: 12345678\n",
    "fix: two\n\nWe write\nGenesis-Session: example here.\n\nGenesis-Session: bbbb2222\n",
    "fix: three\n\nGenesis-Session: aaaa1111\n",
    # Not in the final paragraph, so not a trailer: never collected.
    "fix: four\n\nGenesis-Session: cccc3333\n\nA closing paragraph of prose.\n",
]
EXPECTED = (
    "## Summary\nDoes a thing.  \n\nE2E: none — test\n\n"
    f"Squashed-From: {HEAD}\nGenesis-Session: aaaa1111\nGenesis-Session: bbbb2222\n"
)


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    """The gate's directory lives under HOME; a temp HOME isolates it."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("_TEST_SQUASH_BODY_FILE", raising=False)
    return home


def _bodies_dir() -> Path:
    return Path(gh_merge.merge_bodies_dir())


def _commits(head=HEAD):
    rows = [{"sha": OTHER, "parents": 1, "message": MESSAGES[0]}]
    rows += [{"sha": f"{i}" * 40, "parents": 1, "message": m} for i, m in ((2, MESSAGES[1]),)]
    rows.append({"sha": head, "parents": 1, "message": MESSAGES[2]})
    return "\n".join(json.dumps(r) for r in rows)


# ── composing the body ──────────────────────────────────────────────────────


def test_compose_appends_one_trailer_block_after_the_body():
    assert gh_merge.compose_squash_body(BODY, HEAD, MESSAGES) == EXPECTED


def test_compose_with_an_empty_body_is_the_trailers_alone():
    assert gh_merge.compose_squash_body("", HEAD, [None]) == f"Squashed-From: {HEAD}\n"


@pytest.mark.parametrize("head", ["", HEAD[:12], HEAD.upper(), "x" * 40])
def test_compose_refuses_anything_but_a_full_lowercase_sha(head):
    with pytest.raises(ValueError):
        gh_merge.compose_squash_body(BODY, head, MESSAGES)


# ── which paths the hook and gh agree on ────────────────────────────────────


def test_the_gate_path_is_accepted():
    path = gh_merge.body_file_path("o/r", "5", HEAD)
    assert gh_merge.body_file_path_problem(path) is None
    assert os.path.dirname(path) == str(_bodies_dir())


@pytest.mark.parametrize(
    "make",
    [
        lambda d: "relative.md",
        lambda d: "~/tmp/merge-bodies/x.md",
        lambda d: "$HOME/tmp/merge-bodies/x.md",
        lambda d: f"{d}/../merge-bodies/x.md",
        lambda d: f"/proc/self/cwd/{d.name}/x.md",
        lambda d: "/etc/hostname",
        lambda d: f"{d}/sub/x.md",
        # Absolute, in the right directory, resolving to itself: only the
        # character allowlist stands between these and bash handing gh a
        # different file (a variable's value, a glob's match, a brace's split).
        lambda d: f"{d}/$X.md",
        lambda d: f"{d}/[a].md",
        lambda d: f"{d}/x*.md",
        lambda d: f"{d}/x?.md",
        lambda d: f"{d}/{{a,b}}.md",
        lambda d: f"{d}/a b.md",
    ],
    ids=[
        "relative",
        "tilde",
        "variable",
        "dotdot",
        "proc-self",
        "other-dir",
        "subdir",
        "inner-var",
        "glob-class",
        "glob-star",
        "glob-qmark",
        "brace",
        "space",
    ],
)
def test_paths_the_hook_and_gh_could_read_differently_are_refused(make):
    assert gh_merge.body_file_path_problem(make(_bodies_dir())) is not None


def test_a_symlink_in_the_gate_directory_is_refused(tmp_path):
    d = _bodies_dir()
    d.mkdir(parents=True)
    target = tmp_path / "decoy.md"
    target.write_text("anything")
    link = d / "link.md"
    link.symlink_to(target)
    assert gh_merge.body_file_path_problem(str(link)) is not None
    assert gh_merge.read_body_file(str(link))[0] is None


def test_read_refuses_a_fifo_and_an_oversized_file():
    d = _bodies_dir()
    d.mkdir(parents=True)
    fifo = d / "pipe.md"
    os.mkfifo(fifo)
    text, why = gh_merge.read_body_file(str(fifo))
    assert text is None and "regular" in why
    big = d / "big.md"
    big.write_bytes(b"a" * (gh_merge.MERGE_BODY_MAX_BYTES + 1))
    assert gh_merge.read_body_file(str(big))[0] is None


def test_write_then_read_round_trips_owner_only():
    path = gh_merge.body_file_path(None, "5", HEAD)
    gh_merge.write_body_file(path, EXPECTED)
    assert gh_merge.read_body_file(path) == (EXPECTED, "")
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert [p.name for p in _bodies_dir().iterdir()] == [os.path.basename(path)]


@pytest.mark.parametrize(
    ("command", "expected"),
    [("gh pr merge 5 2>&1", False), ("gh pr merge 5 > /dev/null", True), ("gh pr merge 5", False)],
)
def test_output_redirects_are_seen_in_the_raw_text(command, expected):
    assert gh_merge.redirects_output(command) is expected


# ── reading the flag off argv ───────────────────────────────────────────────


def _argv(*extra):
    return ["gh", "pr", "merge", "5", "--squash", "--admin", *extra]


def test_one_long_body_file_naming_the_gate_file_is_read_and_is_not_a_shadow():
    path = gh_merge.body_file_path("o/r", "5", HEAD)
    for argv in (_argv("--body-file", path), _argv(f"--body-file={path}")):
        assert _mod._merge_body_file(argv) == (path, None)
        assert _mod._merge_has_shadow_flag(argv) is False


@pytest.mark.parametrize(
    "extra",
    [
        ("--body-file", "-"),
        ("--body-file", f"--match-head-commit={HEAD}"),
        ("--body-file",),
        ("--body-file", "relative.md"),
    ],
    ids=["stdin", "shadowing-value", "missing-value", "relative"],
)
def test_a_body_file_the_gate_cannot_vouch_for_is_a_problem_and_a_shadow(extra):
    argv = _argv(*extra)
    path, problem = _mod._merge_body_file(argv)
    assert path is None and problem
    assert _mod._merge_has_shadow_flag(argv) is True


def test_two_body_files_are_a_problem():
    path = gh_merge.body_file_path("o/r", "5", HEAD)
    _path, problem = _mod._merge_body_file(_argv("--body-file", path, "--body-file", path))
    assert _path is None and "more than once" in problem


def test_the_short_form_stays_a_shadow():
    path = gh_merge.body_file_path("o/r", "5", HEAD)
    assert _mod._merge_has_shadow_flag(_argv("-F", path)) is True


def test_a_body_file_consumed_as_another_flags_value_is_not_read():
    """`-R --body-file` makes `--body-file` the repo VALUE: nothing to read."""
    assert _mod._merge_body_file(_argv("-R", "--body-file")) == (None, None)


# ── the merge arm ───────────────────────────────────────────────────────────


def _scheduled_marker(head=HEAD):
    markers = "\n".join(
        f"<!-- genesis-scheduled-review: head={head} kind={k} -->" for k in ("code-review", "leaks")
    )
    return json.dumps(
        {"login": "owner", "author_association": "OWNER", "body": "Review done.\n" + markers}
    )


def _drive(monkeypatch, command, *, body=BODY, commits=None):
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
    monkeypatch.setenv(
        "_TEST_GH_CODEX_REVIEWS",
        json.dumps({"login": "chatgpt-codex-connector[bot]", "commit_id": HEAD}),
    )
    monkeypatch.setenv("_TEST_GH_CODEX_COMMENTS", "")
    monkeypatch.setenv("_TEST_GH_BASE_REF", "main")
    monkeypatch.setenv("_TEST_GH_DEFAULT_BRANCH", "main")
    monkeypatch.setenv(
        "_TEST_GH_CI_ROLLUP",
        json.dumps([{"name": "t", "workflowName": "CI", "conclusion": "SUCCESS"}]),
    )
    monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", _scheduled_marker())
    monkeypatch.setenv(
        "_TEST_GH_PR_FILES", '{"filename": "src/benign.py", "previous_filename": null}'
    )
    monkeypatch.setenv("_TEST_GH_PR_BODY", body)
    monkeypatch.setenv("_TEST_GH_PR_COMMITS", _commits() if commits is None else commits)
    monkeypatch.setattr(_mod, "_check_mergeable", lambda n, repo=None: "MERGEABLE")
    monkeypatch.setattr(
        _mod, "_check_pr_review_findings", lambda n, force=False, repo=None: (False, "")
    )
    monkeypatch.setattr(
        _mod,
        "_check_inline_review_findings",
        lambda n, force=False, repo=None, uncounted_out=None: (False, ""),
    )
    monkeypatch.setattr(
        _mod,
        "read_payload",
        lambda: {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
        },
    )
    return _mod.main()


def _merge(*extra):
    return " ".join(["gh pr merge 5 --repo o/r --squash --admin --match-head-commit", HEAD, *extra])


def _gate_file(text=EXPECTED):
    path = gh_merge.body_file_path("o/r", "5", HEAD)
    gh_merge.write_body_file(path, text)
    return path


def test_a_merge_carrying_the_recomputed_body_is_allowed(monkeypatch, capsys):
    rc = _drive(monkeypatch, _merge("--body-file", _gate_file()))
    assert rc == 0, capsys.readouterr().err
    assert "no --body-file" not in capsys.readouterr().err


def test_an_edited_body_file_is_refused(monkeypatch, capsys):
    rc = _drive(monkeypatch, _merge("--body-file", _gate_file(EXPECTED + "extra\n")))
    assert rc == 2
    assert "is not the body this gate computes" in capsys.readouterr().err


def test_a_pr_body_edited_after_check_pr_is_refused(monkeypatch, capsys):
    path = _gate_file()
    rc = _drive(monkeypatch, _merge("--body-file", path), body=BODY + "\nlate edit\n")
    assert rc == 2
    assert "is not the body this gate computes" in capsys.readouterr().err


def test_a_missing_body_file_is_refused(monkeypatch, capsys):
    path = gh_merge.body_file_path("o/r", "5", HEAD)
    rc = _drive(monkeypatch, _merge("--body-file", path))
    assert rc == 2
    assert "cannot be used" in capsys.readouterr().err


def test_a_head_outside_the_prs_commits_is_refused(monkeypatch, capsys):
    rc = _drive(monkeypatch, _merge("--body-file", _gate_file()), commits=_commits(head=OTHER))
    assert rc == 2
    assert "not one of the PR's commits" in capsys.readouterr().err


def test_an_unbound_merge_with_a_body_file_is_refused(monkeypatch, capsys):
    path = _gate_file()
    rc = _drive(monkeypatch, f"gh pr merge 5 --repo o/r --squash --admin --body-file {path}")
    assert rc == 2
    assert "not bound with --match-head-commit" in capsys.readouterr().err


def test_a_body_file_merge_inside_a_compound_is_refused(monkeypatch, capsys):
    rc = _drive(monkeypatch, "true && " + _merge("--body-file", _gate_file()))
    assert rc == 2
    assert "not a command of its own" in capsys.readouterr().err


def test_a_body_file_merge_with_an_output_redirect_is_refused(monkeypatch, capsys):
    rc = _drive(monkeypatch, _merge("--body-file", _gate_file(), "> /dev/null"))
    assert rc == 2
    assert "redirects output" in capsys.readouterr().err


def test_body_text_under_stale_review_override_is_now_refused(monkeypatch, capsys):
    """The sibling hole: the belt ran only inside the binding, which this sigil skips."""
    rc = _drive(
        monkeypatch,
        "gh pr merge 5 --repo o/r --squash --admin --body x  # stale-review-override",
    )
    assert rc == 2
    assert "shadow the" in capsys.readouterr().err


def test_a_merge_without_a_body_file_still_merges(monkeypatch, capsys):
    """Advisory only (owner, 2026-10-01): the plain bound merge is unaffected."""
    assert _drive(monkeypatch, _merge()) == 0, capsys.readouterr().err


@pytest.mark.parametrize(
    "extra",
    [("--body-file=",), ("-sF", "x")],
    ids=["empty-attached-value", "cluster-short-F"],
)
def test_an_empty_value_or_the_short_form_in_a_cluster_is_refused(monkeypatch, capsys, extra):
    rc = _drive(monkeypatch, _merge(*extra))
    assert rc == 2
    assert "shadow the" in capsys.readouterr().err


def test_gh_help_with_another_word_is_not_exempt():
    assert gh_merge.is_help_only(["gh", "help", "pr", "merge"]) is True
    assert gh_merge.is_help_only(["gh", "help", "pr", "merge", "--x"]) is False
    assert gh_merge.is_help_only(["gh", "pr", "help", "merge"]) is False


# ── --check-pr ──────────────────────────────────────────────────────────────


def _report_env(monkeypatch):
    monkeypatch.setenv("_TEST_GH_HEAD_SHA", HEAD)
    monkeypatch.setenv(
        "_TEST_GH_CODEX_REVIEWS",
        json.dumps({"login": "chatgpt-codex-connector[bot]", "commit_id": HEAD}),
    )
    monkeypatch.setenv("_TEST_GH_CODEX_COMMENTS", "")
    monkeypatch.setenv("_TEST_GH_SCHEDULED_COMMENTS", _scheduled_marker())
    monkeypatch.setenv("_TEST_GH_PR_BODY", BODY)
    monkeypatch.setenv("_TEST_GH_PR_COMMITS", _commits())
    monkeypatch.setenv(
        "_TEST_GH_PR_FILES", '{"filename": "src/benign.py", "previous_filename": null}'
    )
    monkeypatch.setattr(_mod, "_check_mergeable", lambda n, repo=None: "MERGEABLE")
    monkeypatch.setattr(_mod, "_pr_ci_status", lambda n, repo=None: ("green", []))
    monkeypatch.setattr(_mod, "_check_base_is_default", lambda n, repo=None: (False, ""))
    monkeypatch.setattr(
        _mod, "_check_pr_review_findings", lambda n, repo=None, force=False: (False, "")
    )
    monkeypatch.setattr(
        _mod,
        "_check_inline_review_findings",
        lambda n, repo=None, force=False, uncounted_out=None: (False, ""),
    )


def _merge_with_line(out):
    return next(ln for ln in out.splitlines() if ln.startswith("merge-with"))


def test_check_pr_writes_the_body_and_prints_its_path(monkeypatch, capsys):
    _report_env(monkeypatch)
    rc = _mod.check_pr_report("5", repo="o/r")
    line = _merge_with_line(capsys.readouterr().out)
    assert rc == 0
    path = gh_merge.body_file_path("o/r", "5", HEAD)
    assert line.endswith(f"--match-head-commit {HEAD} --body-file {path}")
    assert Path(path).read_text() == EXPECTED


def test_check_pr_in_ci_writes_nothing_and_prints_the_plain_command(monkeypatch, capsys):
    _report_env(monkeypatch)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    rc = _mod.check_pr_report("5", repo="o/r")
    line = _merge_with_line(capsys.readouterr().out)
    assert rc == 0
    assert "--body-file" not in line
    assert not _bodies_dir().exists()


def test_check_pr_without_a_body_prints_the_plain_command_and_says_why(monkeypatch, capsys):
    _report_env(monkeypatch)
    monkeypatch.setenv("_TEST_GH_PR_COMMITS", "__error__")
    rc = _mod.check_pr_report("5", repo="o/r")
    captured = capsys.readouterr()
    assert rc == 0
    assert "--body-file" not in _merge_with_line(captured.out)
    assert "no squash body file" in captured.err


def test_check_pr_with_a_shell_active_home_falls_back_and_says_why(
    monkeypatch, capsys, tmp_path
):
    """GLM P2: a home path with a space would print a command the shell splits."""
    home = tmp_path / "My Home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _report_env(monkeypatch)
    rc = _mod.check_pr_report("5", repo="o/r")
    captured = capsys.readouterr()
    assert rc == 0
    assert "--body-file" not in _merge_with_line(captured.out)
    assert "is unusable" in captured.err
    assert not (home / "tmp").exists()


def test_a_body_directory_others_can_write_is_refused():
    """GLM P3: with ~/tmp symlinked into a shared /tmp, someone else may own it."""
    d = _bodies_dir()
    d.mkdir(parents=True)
    path = gh_merge.body_file_path("o/r", "5", HEAD)
    assert gh_merge.body_file_path_problem(path) is None
    d.chmod(0o777)
    assert "writable by nobody else" in gh_merge.body_file_path_problem(path)
    with pytest.raises(PermissionError):
        gh_merge.write_body_file(path, EXPECTED)
    d.chmod(0o700)


def test_an_uppercase_session_id_is_collected_in_lowercase():
    """GLM P3: prepare-commit-msg keeps the case it found."""
    body = gh_merge.compose_squash_body("", HEAD, ["x\n\nGenesis-Session: ABCD1234\n"])
    assert body == f"Squashed-From: {HEAD}\nGenesis-Session: abcd1234\n"
