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
    ("command", "redirects", "expected"),
    [
        ("gh pr merge 5 2>&1", True, None),
        ("gh pr merge 5", True, None),
        ("gh pr merge 5 > /dev/null", True, ">"),
        ("gh pr merge 5 <<< x", True, "<"),
        ("gh pr merge 5 < f", True, "<"),
        ("gh pr merge 5 {--body,x}", False, "{"),
        ("gh pr merge 5 --body-fil?", False, "?"),
        ("gh pr merge 5 x*", False, "*"),
        ("gh pr merge 5 [ab]", False, "["),
        ("gh pr merge 5 $F", False, "$"),
        ("gh pr merge 5 `x`", False, "`"),
        ("gh pr merge 5 > f", False, None),
        ("gh pr merge 5  # review-override: accepted P2s? see [codex]", False, None),
        ("gh pr merge 5 --body-file /p  # note: a<b?", True, None),
    ],
)
def test_shell_rewrites_names_the_first_character_that_changes_argv_or_the_file(
    command, redirects, expected
):
    words = gh_merge.command_words(command)
    assert gh_merge.shell_rewrites(words, redirects=redirects) == expected


def test_an_untokenizable_command_has_no_words():
    assert gh_merge.command_words("gh pr merge 5 'unclosed") is None


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


def _drive(monkeypatch, command, *, body=BODY, commits=None, inline=None):
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
        inline or (lambda n, force=False, repo=None, uncounted_out=None, **_kw: (False, "")),
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
    assert "redirect or expansion" in capsys.readouterr().err


def test_body_text_under_stale_review_override_is_now_refused(monkeypatch, capsys):
    """The sibling hole: the belt ran only inside the binding, which this sigil skips."""
    rc = _drive(
        monkeypatch,
        "gh pr merge 5 --repo o/r --squash --admin --body x  # stale-review-override",
    )
    assert rc == 2
    assert "shadow the" in capsys.readouterr().err


def test_a_merge_without_a_body_file_still_merges(monkeypatch, capsys):
    """Optional (owner ruling, #5027 round 1): a merge without the body file is
    allowed and carries no receipt, which reads as 'no gate record'."""
    assert _drive(monkeypatch, _merge()) == 0, capsys.readouterr().err


def test_a_dispatched_session_without_a_body_file_still_merges(monkeypatch, capsys):
    monkeypatch.setenv("GENESIS_CC_SESSION", "1")
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
    assert "body-file      : none here (" in captured.out


def test_check_pr_with_a_shell_active_home_falls_back_and_says_why(monkeypatch, capsys, tmp_path):
    """GLM P2: a home path with a space would print a command the shell splits."""
    home = tmp_path / "My Home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _report_env(monkeypatch)
    rc = _mod.check_pr_report("5", repo="o/r")
    captured = capsys.readouterr()
    assert rc == 0
    assert "--body-file" not in _merge_with_line(captured.out)
    assert "is unusable" in captured.out and "carries no receipt" in captured.out
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


# ── round 1 (#2988): what gh consumes must be what the hook judged ──────────


@pytest.mark.parametrize(
    "extra",
    ["{--body,evil}", "{--body-file,/etc/hostname}", "--body-fil?"],
    ids=["brace-body", "brace-body-file", "glob-flag"],
)
def test_a_merge_word_the_shell_would_expand_is_refused_on_every_merge(monkeypatch, capsys, extra):
    """`{--body,evil}` is ONE word to the hook and `--body evil` to gh: refused
    before any flag check reads the hook's argv, overrides included."""
    rc = _drive(monkeypatch, _merge(extra) + "  # stale-review-override")
    assert rc == 2
    assert "shell character" in capsys.readouterr().err


@pytest.mark.parametrize("redirect", ["<<< x", "< /dev/null"], ids=["here-string", "input"])
def test_an_input_redirect_on_a_body_file_merge_is_refused(monkeypatch, capsys, redirect):
    rc = _drive(monkeypatch, _merge("--body-file", _gate_file(), redirect))
    assert rc == 2
    assert "redirect or expansion" in capsys.readouterr().err


def test_a_short_read_is_completed_not_trusted(monkeypatch):
    """os.read may return less than asked: a read stopping where the expected
    body ends must not pass for the whole file."""
    path = _gate_file(EXPECTED + "appended text gh would read\n")
    real_read = gh_merge.os.read
    state = {"calls": 0}

    def one_chunk_then_eof(fd, n):
        state["calls"] += 1
        return real_read(fd, len(EXPECTED.encode())) if state["calls"] == 1 else b""

    monkeypatch.setattr(gh_merge.os, "read", one_chunk_then_eof)
    text, why = gh_merge.read_body_file(path)
    assert text is None and "changed while it was being read" in why


def test_a_byte_at_a_time_read_still_reads_the_whole_file(monkeypatch):
    path = _gate_file()
    real_read = gh_merge.os.read
    monkeypatch.setattr(gh_merge.os, "read", lambda fd, n: real_read(fd, 1))
    assert gh_merge.read_body_file(path) == (EXPECTED, "")


def test_a_hard_linked_body_file_is_refused(tmp_path):
    path = _gate_file()
    os.link(path, tmp_path / "second-name.md")
    text, why = gh_merge.read_body_file(path)
    assert text is None and "hard link" in why


def test_a_body_file_changed_after_the_check_is_refused_last(monkeypatch, capsys):
    """The early check runs right after the binding; a writer that changes the
    file while the later gates run is caught by the re-read before allow."""
    path = _gate_file()
    real_sched = _mod._check_scheduled_claude_reviewed_head

    def sched_then_tamper(*a, **kw):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("late text\n")
        return real_sched(*a, **kw)

    monkeypatch.setattr(_mod, "_check_scheduled_claude_reviewed_head", sched_then_tamper)
    rc = _drive(monkeypatch, _merge("--body-file", path))
    assert rc == 2
    assert "changed after it was checked" in capsys.readouterr().err


@pytest.mark.parametrize("word", ["$F", "`printf x`"], ids=["variable", "backtick"])
def test_a_variable_or_substitution_in_a_merge_word_is_refused(monkeypatch, capsys, word):
    """A variable is the brace bypass by another spelling: gh gets what F holds."""
    rc = _drive(monkeypatch, _merge(word))
    assert rc == 2
    assert "shell character" in capsys.readouterr().err


def test_a_sigil_reason_holding_glob_characters_still_merges(monkeypatch, capsys):
    """The scan reads words, not the raw text: a trailing comment is prose."""
    rc = _drive(
        monkeypatch,
        _merge("--body-file", _gate_file()) + "  # review-override: accepted P2s? see [codex]",
    )
    assert rc == 0, capsys.readouterr().err


# ── the merge receipt (#5027) ───────────────────────────────────────────────


@pytest.fixture
def ids(monkeypatch):
    """Deterministic identity: the session and install ids the receipt reads."""
    for var in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GENESIS_SESSION_ID", "abcdef12-0000-4000-8000-000000000000")
    monkeypatch.setenv("GENESIS_INSTALL_ID", "1234abcd-0000-4000-8000-000000000000")


def _receipt_of(path: str) -> dict[str, str]:
    text = Path(path).read_text()
    assert text.startswith(EXPECTED.rstrip("\n") + "\n")
    lines = text[len(EXPECTED.rstrip("\n")) + 1 :].rstrip("\n").split("\n")
    return dict(line.split(": ", 1) for line in lines)


def test_an_allowed_merge_leaves_its_receipt_in_the_body_file(monkeypatch, capsys, ids):
    path = _gate_file()
    assert _drive(monkeypatch, _merge("--body-file", path)) == 0, capsys.readouterr().err
    receipt = _receipt_of(path)
    assert receipt == {
        "Gate-verdict": "PASSED",
        # pin-receipts and rework are out of scope for this PR (n/a), not ok.
        "Gate-rows": "mergeable=ok ci=ok base-branch=ok pin-receipts=n/a codex-at-head=ok body-file=ok review-body=ok inline-findings=ok scheduled-claude=ok rework=n/a",
        "Gate-sigils": "none",
        "Gate-session": "abcdef12",
        "Gate-install": "1234abcd",
    }


def test_a_waived_row_and_its_sigil_are_on_the_receipt(monkeypatch, capsys, ids):
    path = _gate_file()
    monkeypatch.setenv(
        "_TEST_GH_CI_ROLLUP",
        json.dumps([{"name": "t", "workflowName": "CI", "conclusion": "FAILURE"}]),
    )
    real_drive_ci = _mod._pr_ci_status
    monkeypatch.setattr(_mod, "_pr_ci_status", lambda n, repo=None: ("red", ["t"]))
    rc = _drive(monkeypatch, _merge("--body-file", path) + "  # ci-override")
    assert rc == 0, capsys.readouterr().err
    receipt = _receipt_of(path)
    assert receipt["Gate-verdict"] == "OVERRIDDEN"
    assert "ci=waived" in receipt["Gate-rows"].split()
    assert receipt["Gate-sigils"] == "ci-override=ci:red"
    assert real_drive_ci is not None


def test_a_sigil_present_but_unused_does_not_mark_a_row_waived(monkeypatch, capsys, ids):
    path = _gate_file()
    rc = _drive(monkeypatch, _merge("--body-file", path) + "  # ci-override")
    assert rc == 0, capsys.readouterr().err
    receipt = _receipt_of(path)
    assert receipt["Gate-verdict"] == "PASSED"
    assert receipt["Gate-sigils"] == "ci-override=ci:green"


def test_a_retry_after_another_hook_stopped_the_merge_is_accepted(monkeypatch, capsys, ids):
    """The receipt is written before gh runs; if another PreToolUse hook then
    stops the command, the next attempt finds the file with that receipt."""
    path = _gate_file()
    assert _drive(monkeypatch, _merge("--body-file", path)) == 0
    first = Path(path).read_text()
    assert _drive(monkeypatch, _merge("--body-file", path)) == 0, capsys.readouterr().err
    assert Path(path).read_text() == first  # rewritten to the same receipt, not stacked


def test_a_hand_edited_receipt_is_refused(monkeypatch, capsys, ids):
    path = _gate_file(EXPECTED.rstrip("\n") + "\nGate-verdict: PASSED\nGate-note: trust me\n")
    rc = _drive(monkeypatch, _merge("--body-file", path))
    assert rc == 2
    assert "is not the body this gate computes" in capsys.readouterr().err


def test_a_receipt_that_cannot_be_written_blocks_the_merge(monkeypatch, capsys, ids):
    path = _gate_file()

    def refuse(*_a, **_k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(_mod, "write_body_file", refuse)
    rc = _drive(monkeypatch, _merge("--body-file", path))
    assert rc == 2
    assert "merge receipt could not be written" in capsys.readouterr().err


def test_the_install_id_comes_from_install_json_and_is_never_created(monkeypatch, tmp_path):
    monkeypatch.delenv("GENESIS_INSTALL_ID", raising=False)
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path))
    assert _mod._receipt_install() is None
    assert not (tmp_path / "install.json").exists()
    (tmp_path / "install.json").write_text(
        json.dumps({"install_id": "9F8E7D6C-1111-4111-8111-111111111111"})
    )
    assert _mod._receipt_install() == "9f8e7d6c"


def test_the_session_id_prefers_the_payload_then_prepare_commit_msgs_order(monkeypatch):
    """The payload names the session running THIS merge; an inherited env var
    can name a parent session."""
    for var in ("GENESIS_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GENESIS_SESSION_ID", "f00df00d-y")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cafe0001-x")
    assert _mod._receipt_session({"session_id": "deadbeef-1"}) == "deadbeef"
    assert _mod._receipt_session({}) == "f00df00d"
    monkeypatch.delenv("GENESIS_SESSION_ID")
    assert _mod._receipt_session({}) == "cafe0001"
    monkeypatch.setenv("GENESIS_SESSION_ID", "not-hex")
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    assert _mod._receipt_session({}) is None
    monkeypatch.setenv("CLAUDE_SESSION_ID", "abcd1234-dead")  # a dead payload variable
    assert _mod._receipt_session({}) is None


@pytest.mark.parametrize(
    "key,value",
    [
        ("Gate-sigils", "/home/someone/.genesis/x"),
        ("Gate-stand-in", "bot@host.example.invalid"),
        ("Gate-session", "abcdef12 extra"),
        ("Gate-rows", "ci=ok\nGate-verdict: MERGEABLE"),
        ("Gate-verdict", "MERGEABLE; rm -rf"),
        ("Gate-verdict", "MERGEABLE"),
        ("Gate-rows", "ci=skipped"),
    ],
    ids=[
        "path",
        "hostname",
        "trailing-text",
        "newline-injection",
        "verdict-text",
        "retired-verdict-word",
        "unknown-row-value",
    ],
)
def test_no_value_outside_the_grammar_reaches_the_receipt(key, value):
    receipt = gh_merge.compose_receipt({"Gate-verdict": "PASSED", key: value})
    assert value not in receipt
    assert f"Gate-incomplete: {key}" in receipt or key == "Gate-verdict"


def test_the_real_state_directory_path_cannot_ride_any_field():
    real = os.path.realpath(os.path.expanduser("~/.genesis"))
    for key in gh_merge.RECEIPT_GRAMMAR:
        assert real not in gh_merge.compose_receipt({key: real})


def test_the_receipt_round_trips_through_git_interpret_trailers(tmp_path):
    import subprocess

    receipt = gh_merge.compose_receipt(
        {
            "Gate-verdict": "OVERRIDDEN",
            "Gate-rows": "ci=waived codex-at-head=ok",
            "Gate-sigils": "ci-override=ci:red,substitute-review=codex-freshness:used",
            "Gate-stand-in": "coderabbitai[bot],devin-ai-integration[bot]@" + HEAD[:12],
            "Gate-session": "abcdef12",
            "Gate-install": "1234abcd",
        }
    )
    message = "subject\n\n" + gh_merge.with_receipt(EXPECTED, receipt)
    out = subprocess.run(
        ["git", "interpret-trailers", "--parse"],
        input=message,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    parsed = dict(line.split(": ", 1) for line in out.strip().splitlines())
    assert parsed["Squashed-From"] == HEAD
    assert parsed["Gate-verdict"] == "OVERRIDDEN"
    assert parsed["Gate-stand-in"].startswith("coderabbitai[bot],devin-ai-integration[bot]@")
    assert parsed["Gate-install"] == "1234abcd"


def test_check_pr_prints_the_body_file_row_when_a_gate_fails(monkeypatch, capsys):
    _report_env(monkeypatch)
    monkeypatch.setattr(_mod, "_pr_ci_status", lambda n, repo=None: ("red", ["t"]))
    rc = _mod.check_pr_report("5", repo="o/r")
    captured = capsys.readouterr()
    out = captured.out
    assert rc == 1
    assert "merge-with" not in out
    path = gh_merge.body_file_path("o/r", "5", HEAD)
    row = next(ln for ln in out.splitlines() if ln.startswith("body-file"))
    assert row.endswith(
        f"--match-head-commit {HEAD} --body-file {path}"
    )  # nothing a sigil lands behind
    assert "only the approved sigils appended" in captured.err
    assert Path(path).read_text() == EXPECTED


def test_check_pr_leaves_a_file_holding_this_body_and_a_receipt_alone(monkeypatch, capsys):
    _report_env(monkeypatch)
    path = _gate_file(EXPECTED.rstrip("\n") + "\nGate-verdict: PASSED\n")
    _mod.check_pr_report("5", repo="o/r")
    assert Path(path).read_text().endswith("Gate-verdict: PASSED\n")


def test_stacked_earlier_receipts_are_cleared_and_replaced_by_the_gates_own(
    monkeypatch, capsys, ids
):
    once = "Gate-verdict: OVERRIDDEN\nGate-sigils: none"
    path = _gate_file(EXPECTED.rstrip("\n") + "\n" + once + "\n" + once + "\n")
    assert _drive(monkeypatch, _merge("--body-file", path)) == 0, capsys.readouterr().err
    receipt = _receipt_of(path)
    assert receipt["Gate-verdict"] == "PASSED" and receipt["Gate-session"] == "abcdef12"


def test_strip_receipt_leaves_anything_that_is_not_a_clean_receipt_alone():
    bad = EXPECTED.rstrip("\n") + "\nGate-verdict: PASSED\nGate-note: trust me\n"
    assert gh_merge.strip_receipt(bad) == bad
    good = EXPECTED.rstrip("\n") + "\nGate-verdict: PASSED\n"
    assert gh_merge.strip_receipt(good) == EXPECTED
    assert gh_merge.strip_receipt(EXPECTED) == EXPECTED


def test_a_pr_body_holding_receipt_shaped_lines_is_refused(monkeypatch, capsys):
    """Security W1: a PR body paragraph of Gate-* lines could pass for the
    receipt to a reader that takes the first trailer-looking block."""
    path = _gate_file()
    forged = BODY + "\nGate-verdict: MERGEABLE\nGate-rows: ci=ok\n"
    rc = _drive(monkeypatch, _merge("--body-file", path), body=forged)
    assert rc == 2
    assert "trailer key only the gate writes" in capsys.readouterr().err


@pytest.mark.parametrize(
    "line", ["gate-surface: hooks", "Gate-surface: hooks", "Gate-rowsy: x", "Gate rows: x"]
)
def test_a_pr_body_line_that_only_resembles_a_receipt_key_composes(line):
    """Audit A2: only the exact receipt keys are reserved."""
    assert gh_merge.compose_squash_body(BODY + "\n" + line, HEAD, [])


@pytest.mark.parametrize(
    "line",
    ["GATE-VERDICT: PASSED", "gate-rows: ci=ok", "  Gate-rows: x", "Gate-verdict : PASSED"],
)
def test_a_case_or_spacing_variant_of_a_receipt_key_is_refused(line):
    """Architect S1 (MEASURED): git's trailer readers match keys in any case, so a
    variant spelling in the PR body could still pose as the receipt."""
    with pytest.raises(ValueError, match="trailer key only the gate writes"):
        gh_merge.compose_squash_body(BODY + "\n" + line, HEAD, [])


# ── round-1 review fixes (#5027) ────────────────────────────────────────────


def test_an_earlier_receipt_is_stripped_before_the_slow_gates_run(monkeypatch, capsys, ids):
    """A hook killed after the early check must leave gh the bare body, never an
    earlier attempt's decision: the early check clears it at once."""
    forged = EXPECTED.rstrip("\n") + "\nGate-verdict: PASSED\nGate-sigils: none\n"
    path = _gate_file(forged)
    seen = {}
    real_sched = _mod._check_scheduled_claude_reviewed_head

    def peek(*a, **kw):
        seen["mid"] = Path(path).read_text()
        return real_sched(*a, **kw)

    monkeypatch.setattr(_mod, "_check_scheduled_claude_reviewed_head", peek)
    assert _drive(monkeypatch, _merge("--body-file", path)) == 0, capsys.readouterr().err
    assert seen["mid"] == EXPECTED
    assert _receipt_of(path)["Gate-session"] == "abcdef12"  # the gate's own, not the forged one


def test_a_ci_read_that_passed_fail_open_is_reported_unverified(monkeypatch, capsys, ids):
    path = _gate_file()
    monkeypatch.setattr(_mod, "_pr_ci_status", lambda n, repo=None: ("unknown", []))
    assert _drive(monkeypatch, _merge("--body-file", path)) == 0, capsys.readouterr().err
    rows = dict(r.split("=") for r in _receipt_of(path)["Gate-rows"].split())
    assert rows["ci"] == "unverified"
    assert _receipt_of(path)["Gate-verdict"] == "PASSED-UNVERIFIED"


def test_an_unchecked_rework_contract_is_reported_unverified(monkeypatch, capsys, ids):
    path = _gate_file()
    monkeypatch.setattr(
        _mod, "_check_rework", lambda *a, **k: (_mod.REWORK_UNCHECKED, "could not read the PR")
    )
    assert _drive(monkeypatch, _merge("--body-file", path)) == 0, capsys.readouterr().err
    rows = dict(r.split("=") for r in _receipt_of(path)["Gate-rows"].split())
    assert rows["rework"] == "unverified"


def test_an_unverified_pin_gate_and_an_out_of_scope_scheduled_gate(monkeypatch, capsys, ids):
    """Security W4 and audit B9: a pin read that failed open is unverified, never
    `ok`; a scheduled gate out of scope here is n/a, not unverified."""
    path = _gate_file()
    monkeypatch.setattr(
        _mod, "_check_pin_receipts", lambda n, repo=None: (False, "NOTE: pin receipts not verified")
    )
    monkeypatch.setattr(_mod, "_scheduled_gate_applies", lambda repo: False)
    assert _drive(monkeypatch, _merge("--body-file", path)) == 0, capsys.readouterr().err
    rows = dict(r.split("=") for r in _receipt_of(path)["Gate-rows"].split())
    assert rows["pin-receipts"] == "unverified"
    assert rows["scheduled-claude"] == "n/a"
    assert _receipt_of(path)["Gate-verdict"] == "PASSED-UNVERIFIED"


# ── per-gate outcomes (#5027 round 1: owner rulings) ────────────────────────


def _rows_of(path: str) -> dict[str, str]:
    return dict(r.split("=") for r in _receipt_of(path)["Gate-rows"].split())


@pytest.mark.parametrize(
    "state,canonical,override,expected",
    [
        ("green", True, False, "ok"),
        ("green", True, True, "ok"),
        ("green", False, True, "ok"),
        ("red", True, True, "waived"),
        ("red", False, True, "waived"),
        ("pending", True, True, "waived"),
        ("pending", False, True, "waived"),
        ("absent", True, True, "waived"),
        ("absent", False, False, "n/a"),
        ("absent", False, True, "n/a"),
        ("incomplete", True, True, "waived"),
        ("incomplete", False, False, "n/a"),
        ("incomplete", False, True, "n/a"),
        ("unknown", True, False, "unverified"),
        ("unknown", True, True, "unverified"),
        ("unknown", False, False, "unverified"),
    ],
)
def test_the_ci_row_is_waived_only_where_the_state_would_have_blocked(
    state, canonical, override, expected
):
    """Audit B2: every CI state the arm lets through, on and off the canonical
    repo, with and without # ci-override (the blocked cells never reach it)."""
    assert _mod._ci_receipt_outcome(state, canonical, override) == expected


@pytest.mark.parametrize(
    "state,canonical,override,row",
    [
        ("red", True, True, "waived"),
        ("absent", False, False, "n/a"),
        ("absent", True, True, "waived"),
        ("unknown", True, True, "unverified"),
    ],
)
def test_the_arm_writes_the_ci_row_it_decided(
    monkeypatch, capsys, ids, state, canonical, override, row
):
    path = _gate_file()
    monkeypatch.setattr(_mod, "_pr_ci_status", lambda n, repo=None: (state, ["t"]))
    monkeypatch.setattr(_mod, "_scheduled_gate_applies", lambda repo: canonical)
    cmd = _merge("--body-file", path) + ("  # ci-override" if override else "")
    assert _drive(monkeypatch, cmd) == 0, capsys.readouterr().err
    assert _rows_of(path)["ci"] == row


@pytest.mark.parametrize(
    "sigil,skipped",
    [
        ("stale-review-override", {"codex-at-head", "base-branch"}),
        ("review-override", {"review-body"}),
        ("scheduled-review-override", {"scheduled-claude"}),
        ("rework-override", {"rework"}),
    ],
)
def test_a_sigil_that_skips_a_gate_reads_unverified_and_overridden(
    monkeypatch, capsys, ids, sigil, skipped
):
    """Owner ruling 49a93304: a gate the sigil skipped unread is `unverified`, never
    `waived` (nobody knows it would have blocked), and the verdict is OVERRIDDEN."""
    path = _gate_file()
    rc = _drive(monkeypatch, _merge("--body-file", path) + f"  # {sigil}")
    assert rc == 0, capsys.readouterr().err
    receipt = _receipt_of(path)
    rows = _rows_of(path)
    assert {r for r, v in rows.items() if v == "unverified"} == skipped
    assert "waived" not in rows.values()
    assert receipt["Gate-verdict"] == "OVERRIDDEN"
    assert receipt["Gate-sigils"].startswith(sigil + "=")


@pytest.mark.parametrize("found,row", [([], "ok"), (["score"], "waived"), (["unread"], "waived")])
def test_review_override_waives_inline_only_when_it_let_a_block_through(
    monkeypatch, capsys, ids, found, row
):
    """Audit B8: the inline scan still runs under # review-override, so the gate
    knows whether the sigil changed its answer."""
    path = _gate_file()

    def inline(n, force=False, repo=None, uncounted_out=None, force_waived=None, **_kw):
        assert force and force_waived is not None
        force_waived.extend(found)
        return False, ""

    rc = _drive(monkeypatch, _merge("--body-file", path) + "  # review-override", inline=inline)
    assert rc == 0, capsys.readouterr().err
    assert _rows_of(path)["inline-findings"] == row
    assert _rows_of(path)["review-body"] == "unverified"


@pytest.mark.parametrize("used,row", [(True, "waived"), (False, "ok")])
def test_floor_accept_waives_the_inline_row_only_when_used(monkeypatch, capsys, ids, used, row):
    path = _gate_file()

    def inline(
        n,
        force=False,
        repo=None,
        uncounted_out=None,
        floor_accept=(),
        floor_accept_used=None,
        **_kw,
    ):
        if used:
            floor_accept_used.add(7)
        return False, ""

    rc = _drive(monkeypatch, _merge("--body-file", path) + "  # floor-accept=7", inline=inline)
    assert rc == 0, capsys.readouterr().err
    receipt = _receipt_of(path)
    assert _rows_of(path)["inline-findings"] == row
    assert receipt["Gate-verdict"] == ("OVERRIDDEN" if used else "PASSED")


def test_a_used_substitute_waives_codex_and_names_the_stand_in(monkeypatch, capsys, ids):
    path = _gate_file()

    def fresh(n, force=False, repo=None, substitute=False, substitute_out=None):
        assert substitute and not force
        substitute_out.append({"reviewers": ["coderabbitai[bot]"]})
        return False, "", HEAD

    monkeypatch.setattr(_mod, "_check_codex_reviewed_head", fresh)
    rc = _drive(monkeypatch, _merge("--body-file", path) + "  # substitute-review")
    assert rc == 0, capsys.readouterr().err
    receipt = _receipt_of(path)
    assert _rows_of(path)["codex-at-head"] == "waived"
    assert receipt["Gate-stand-in"] == "coderabbitai[bot]@" + HEAD[:12]
    assert receipt["Gate-verdict"] == "OVERRIDDEN"


def test_a_present_but_unused_substitute_leaves_codex_ok(monkeypatch, capsys, ids):
    path = _gate_file()
    rc = _drive(monkeypatch, _merge("--body-file", path) + "  # substitute-review")
    assert rc == 0, capsys.readouterr().err
    receipt = _receipt_of(path)
    assert _rows_of(path)["codex-at-head"] == "ok"
    assert "Gate-stand-in" not in receipt
    assert receipt["Gate-verdict"] == "PASSED"


@pytest.mark.parametrize(
    "outcomes,skipped,verdict",
    [
        ({}, set(), "PASSED"),
        ({"pin-receipts": "n/a", "rework": "n/a"}, set(), "PASSED"),
        ({"ci": "unverified"}, set(), "PASSED-UNVERIFIED"),
        ({"ci": "waived", "rework": "unverified"}, set(), "OVERRIDDEN"),
        ({"rework": "unverified"}, {"rework"}, "OVERRIDDEN"),
    ],
)
def test_the_verdict_follows_from_the_recorded_rows(monkeypatch, ids, outcomes, skipped, verdict):
    full = {r: "ok" for r in _mod._RECEIPT_ROWS} | outcomes
    monkeypatch.setattr(_mod, "_RECEIPT_OUTCOMES", full)
    monkeypatch.setattr(_mod, "_RECEIPT_SKIPPED", set(skipped))
    receipt = dict(ln.split(": ", 1) for ln in _mod._merge_receipt({}, HEAD, []).splitlines())
    assert receipt["Gate-verdict"] == verdict


def test_a_row_no_gate_recorded_is_named_incomplete_never_ok(monkeypatch, ids):
    partial = {r: "ok" for r in _mod._RECEIPT_ROWS if r != "rework"}
    monkeypatch.setattr(_mod, "_RECEIPT_OUTCOMES", partial)
    monkeypatch.setattr(_mod, "_RECEIPT_SKIPPED", set())
    receipt = _mod._merge_receipt({}, HEAD, [])
    assert "Gate-rows:" not in receipt and "Gate-verdict:" not in receipt
    assert "Gate-incomplete: Gate-verdict,Gate-rows" in receipt


def test_every_allowed_merge_records_every_row(monkeypatch, capsys, ids):
    path = _gate_file()
    assert _drive(monkeypatch, _merge("--body-file", path)) == 0, capsys.readouterr().err
    assert set(_mod._RECEIPT_OUTCOMES) == set(_mod._RECEIPT_ROWS)
    assert "Gate-incomplete" not in _receipt_of(path)


def test_check_pr_in_ci_says_why_no_body_file_is_written(monkeypatch, capsys):
    """Kimi P3: in CI the head is readable; the row must not claim otherwise."""
    _report_env(monkeypatch)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(_mod, "_check_codex_reviewed_head", lambda *a, **k: (True, "x", None))
    _mod.check_pr_report("5", repo="o/r")
    row = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("body-file"))
    assert "not written in CI" in row and "could not be read" not in row


# ── override merges carry the receipt where it can be produced (ruling 1101933a)


@pytest.mark.parametrize("sigil", ["ci-override", "review-override", "floor-accept=7"])
def test_an_override_merge_without_a_body_file_is_sent_to_the_body_file_command(
    monkeypatch, capsys, sigil
):
    rc = _drive(monkeypatch, _merge() + f"  # {sigil}")
    assert rc == 2
    err = capsys.readouterr().err
    path = gh_merge.body_file_path("o/r", "5", HEAD)
    assert f"--match-head-commit {HEAD} --body-file {path}" in err
    assert Path(path).read_text() == EXPECTED  # written, ready to use


def test_an_unbound_override_merge_is_sent_to_a_bound_body_file_command(monkeypatch, capsys):
    rc = _drive(monkeypatch, "gh pr merge 5 --repo o/r --squash --admin  # stale-review-override")
    assert rc == 2
    assert f"--match-head-commit {HEAD} --body-file " in capsys.readouterr().err


@pytest.mark.parametrize("line", ["Gate-verdict: PASSED", "gate-rows: ci=ok"])
def test_an_override_merge_with_a_reserved_line_in_the_body_is_told_to_reword_it(
    monkeypatch, capsys, line
):
    """Owner ruling cfdd23b2: the one cannot-produce cause the author fixes in
    seconds is refused, not silently merged without a receipt."""
    rc = _drive(monkeypatch, _merge() + "  # ci-override", body=BODY + "\n" + line)
    assert rc == 2
    err = capsys.readouterr().err
    assert "Reword that line" in err and line.split(":")[0] in err
    assert line not in err  # the key only, never the rest of the PR text


def test_a_plain_merge_with_a_reserved_line_in_the_body_still_merges(monkeypatch, capsys):
    assert _drive(monkeypatch, _merge(), body=BODY + "\nGate-verdict: PASSED") == 0


def test_another_gates_refusal_comes_before_the_body_file_one(monkeypatch, capsys):
    """Architect S1: the override check runs last, so a merge another gate refuses
    gets that refusal, never a body-file round trip first."""
    monkeypatch.setattr(_mod, "_pr_ci_status", lambda n, repo=None: ("red", ["t"]))
    rc = _drive(monkeypatch, _merge() + "  # stale-review-override")
    assert rc == 2
    err = capsys.readouterr().err
    assert "CI is RED" in err and "needs --body-file" not in err


def test_an_override_merge_goes_on_without_a_receipt_where_none_can_be_produced(
    monkeypatch, capsys
):
    """A commit list the gate cannot read (the 250-commit cap reads the same way)
    means no body file can be composed: the merge runs, recording nothing."""
    rc = _drive(monkeypatch, _merge() + "  # ci-override", commits="__error__")
    assert rc == 0, capsys.readouterr().err
    assert not Path(gh_merge.body_file_path("o/r", "5", HEAD)).exists()


def test_the_test_pin_means_no_body_file_so_no_requirement(monkeypatch, capsys):
    monkeypatch.setenv("_TEST_SQUASH_BODY_FILE", "off")
    assert _drive(monkeypatch, _merge() + "  # ci-override") == 0, capsys.readouterr().err


def test_a_plain_merge_without_a_body_file_pays_no_probe(monkeypatch, capsys):
    calls = []
    real = _mod._prepare_squash_body_file
    monkeypatch.setattr(_mod, "_prepare_squash_body_file", lambda *a: calls.append(a) or real(*a))
    assert _drive(monkeypatch, _merge()) == 0, capsys.readouterr().err
    assert calls == []


def test_the_override_merge_with_the_printed_command_gets_its_receipt(monkeypatch, capsys, ids):
    assert _drive(monkeypatch, _merge() + "  # ci-override") == 2
    path = gh_merge.body_file_path("o/r", "5", HEAD)
    rc = _drive(monkeypatch, _merge("--body-file", path) + "  # ci-override")
    assert rc == 0, capsys.readouterr().err
    assert _receipt_of(path)["Gate-sigils"] == "ci-override=ci:green"
