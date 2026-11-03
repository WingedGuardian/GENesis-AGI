"""The issue premise check: a foreground `gh issue create` carries the answers the server gate reads.

What the design promises, and so what is tested:

* it BLOCKS (exit 2, stderr) only a foreground create whose body it can read in full
  and whose target is the configured public repo, when
  ``premise_gate.owed_answers`` (the server gate's own rule) says answers are owed;
* every shape it cannot read passes in silence: a heredoc it did not excise, a
  substitution, an earlier ``cd``, a body file an earlier step writes, an unknown
  flag, another repo, an unreadable file;
* a dispatched session (``GENESIS_CC_SESSION=1``) is never blocked;
* it agrees with ``premise_gate.decide`` on an ``opened`` event, cell for cell;
* a command with no issue create never pays for the parser import.

Repos, paths and bodies are synthetic. No test touches GitHub: the hook makes no
network call, and the implicit target is read from a temporary git repository.
"""

from __future__ import annotations

import ast
import io
import itertools
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.conftest import private_module

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "scripts"
_HOOKS = _SCRIPTS / "hooks"
_HOOK = _HOOKS / "issue_premise_gate.py"
sys.path.insert(0, str(_HOOKS))

import shell_parse as sp  # noqa: E402

hook = private_module("issue_premise_gate_under_test", _HOOK)
gate = private_module("premise_gate_for_issue_hook", _SCRIPTS / "premise_gate.py")

CANON = "acme/widget"

SIX = """## Summary
Do the thing in `scripts/x.py`.

## Premise checks
1. n/a — no outside behaviour.
2. Searched scripts/ for x; nothing.
3. Scope reuses the shared reader.
4. Caller: the CI job, tracked by #42.
5. No numbers.
6. About 100 counted lines, one PR.
"""

PARTIAL = """## Summary
Do the thing.

## Premise checks
1. n/a — no outside behaviour.
2.
3. Scope reuses the shared reader.
4. Caller: the CI job, tracked by #42.
5. No numbers.
6. <counted lines, and the split>
"""

VERIFIED = """## Problem
It breaks.

## Verification
1. Reproduced on a fresh main.
2. The command and its output are below.
3. Ruled out local config and a stale tree.
4. Searched: no existing issue, no fix on main.
5. Expected a pass, got a crash.
"""

MISSING = "Just a short note about the thing."


def _bug_form_body() -> str:
    headings = gate._bug_form_headings()
    return "\n\n".join(f"{h}\n\nsome text" for h in headings) + "\n"


# -- harness -----------------------------------------------------------------


def _environ(monkeypatch, *, dispatched=False, extra=None):
    for name in ("GENESIS_CC_SESSION", "GH_REPO", "GH_HOST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", CANON)
    if dispatched:
        monkeypatch.setenv("GENESIS_CC_SESSION", "1")
    for k, v in (extra or {}).items():
        monkeypatch.setenv(k, v)


def _call(monkeypatch, capsys, command, *, cwd=None, dispatched=False, extra=None):
    """Run the hook in-process on one Bash payload. Returns (exit, stdout, stderr)."""
    _environ(monkeypatch, dispatched=dispatched, extra=extra)
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    if cwd is not None:
        payload["cwd"] = str(cwd)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    code = hook.main()
    out = capsys.readouterr()
    return code, out.out, out.err


def _sub_env(extra=None, *, dispatched=False):
    env = {
        k: v for k, v in os.environ.items() if k not in ("GENESIS_CC_SESSION", "GH_REPO", "GH_HOST")
    }
    env["_TEST_CANONICAL_PUBLIC_REPO"] = CANON
    if dispatched:
        env["GENESIS_CC_SESSION"] = "1"
    env.update(extra or {})
    return env


def _subprocess(script: Path, command: str, *, cwd=None, env=None, args=()):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    if cwd is not None:
        payload["cwd"] = str(cwd)
    return subprocess.run(
        [sys.executable, *args, str(script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env if env is not None else _sub_env(),
        timeout=120,
    )


def _file(tmp_path: Path, body: str, name: str = "issue.md") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _repo(tmp_path: Path, url: str, name: str = "repo") -> Path:
    path = tmp_path / name
    path.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", "init", "-q", str(path)], check=True, env=env)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", url], check=True, env=env)
    return path


def _create(*rest: str, repo: str | None = CANON, sub: str = "create") -> str:
    """A `gh issue create` command line; ``repo`` adds `-R <repo>` after the group."""
    words = ["gh", "issue", sub, "-t", "A title"]
    if repo:
        words += ["-R", repo]
    return " ".join([shlex.join(words), *rest])


def _b(body: str) -> str:
    return "-b " + shlex.quote(body)


# -- premise_gate.owed_answers: the server's own rule --------------------------

NOW = "2026-11-02T12:00:00Z"
PRE_EPOCH = "2026-10-01T00:00:00Z"


def test_owed_answers_names_the_section_and_the_missing_numbers():
    assert gate.owed_answers({"body": MISSING, "labels": [], "created_at": NOW}) == (
        "premise",
        [1, 2, 3, 4, 5, 6],
    )
    assert gate.owed_answers({"body": PARTIAL, "labels": [], "created_at": NOW}) == (
        "premise",
        [2, 6],
    )
    assert gate.owed_answers({"body": SIX, "labels": [], "created_at": NOW}) == ("", [])


def test_owed_answers_switches_to_verification_for_a_bug():
    assert gate.owed_answers({"body": SIX, "labels": ["bug"], "created_at": NOW}) == (
        "verification",
        [1, 2, 3, 4, 5],
    )
    assert gate.owed_answers({"body": VERIFIED, "labels": ["bug"], "created_at": NOW}) == ("", [])


def test_owed_answers_honours_both_exemptions():
    assert gate.owed_answers({"body": MISSING, "labels": [], "created_at": PRE_EPOCH}) == ("", [])
    assert gate.owed_answers({"body": _bug_form_body(), "labels": ["bug"], "created_at": NOW}) == (
        "",
        [],
    )


@pytest.mark.parametrize(
    ("body", "labels", "created", "section"),
    list(
        itertools.product(
            [MISSING, PARTIAL, SIX, VERIFIED, "bugform"],
            [[], ["bug"], [{"name": "bug"}, {"name": "x"}]],
            [PRE_EPOCH, NOW],
            [None],
        )
    ),
)
def test_owed_answers_agrees_with_the_server_gate_on_an_opened_event(
    body, labels, created, section
):
    """Parity: owed iff ``decide`` on an ``opened`` event adds ``premises-missing``.

    The hook decides through the server's own function, so this is the cell-by-cell
    check that the new wrapper did not drift from ``decide``.
    """
    text = _bug_form_body() if body == "bugform" else body
    issue = {"number": 1, "body": text, "labels": labels, "created_at": created}
    plan = gate.decide({"action": "opened", "issue": issue})
    owed = gate.owed_answers(issue)
    assert bool(owed[1]) == ("premises-missing" in plan.add), (owed, plan)
    assert bool(owed[0]) == bool(owed[1])


def test_owed_message_names_the_numbers_the_format_and_the_placeholder():
    msg = gate.owed_message({"body": PARTIAL, "labels": [], "created_at": NOW})
    assert "answer 2 (repo searched for existing code, result written down)" in msg
    assert "answer 6" in msg and "is only a <placeholder>" in msg
    assert "answer 1 " not in msg and "answer 3 " not in msg
    assert "## Premise checks" in msg and "n/a — <reason>" in msg
    msg = gate.owed_message({"body": MISSING, "labels": ["bug"], "created_at": NOW})
    assert "no `## Verification` section" in msg
    assert gate.owed_message({"body": SIX, "labels": [], "created_at": NOW}) == ""


def test_owed_answers_adds_no_module_import_to_the_action_script():
    """The Action sparse-checks out five files; a new import would break it there."""
    tree = ast.parse((_SCRIPTS / "premise_gate.py").read_text(encoding="utf-8"))
    modules = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            modules |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module)
    assert modules == {
        "__future__",
        "json",
        "os",
        "re",
        "subprocess",
        "sys",
        "tempfile",
        "collections.abc",
        "dataclasses",
        "pathlib",
        "plan_doc",
        "plan_premise_gate",
    }


# -- shell_parse additions -----------------------------------------------------


def test_issue_new_is_an_alias_of_the_create_row():
    assert sp._GH_FLAG_TABLE[("issue", "new")] == sp._GH_FLAG_TABLE[("issue", "create")]
    assert frozenset({"create", "new"}) == sp.GH_ISSUE_CREATE_SUBCOMMANDS


def test_gh_option_values_reads_every_spelling_in_order():
    argv = [
        "gh",
        "-R",
        "o/one",
        "issue",
        "create",
        "--repo=o/two",
        "-l",
        "a,b",
        "--label",
        "bug",
        "-b",
        "--help",
        "-Ro/three",
        "--web=true",
        "-F",
        "f.md",
    ]
    got = sp.gh_option_values(
        argv, frozenset({"-R", "--repo", "-l", "--label", "-b", "--web", "-F"})
    )
    assert got == [
        ("-R", "o/one"),
        ("--repo", "o/two"),
        ("-l", "a,b"),
        ("--label", "bug"),
        ("-b", "--help"),
        ("-R", "o/three"),
        ("--web", "true"),
        ("-F", "f.md"),
    ]


def test_gh_option_values_reports_a_boolean_without_a_value():
    got = sp.gh_option_values(["gh", "issue", "create", "--web", "-t", "x"], frozenset({"--web"}))
    assert got == [("--web", None)]


def test_the_dup_advisory_reads_the_shared_constants():
    adv = private_module("issue_dup_advisory_for_premise_test", _HOOKS / "issue_dup_advisory.py")
    assert adv._CREATE_SUBCOMMANDS is sp.GH_ISSUE_CREATE_SUBCOMMANDS
    assert adv._WEB_FLAGS is sp.GH_WEB_FLAGS


# -- the block: what it judges --------------------------------------------------


def test_1_a_literal_body_with_no_section_blocks(monkeypatch, capsys):
    code, out, err = _call(monkeypatch, capsys, _create(_b(MISSING)))
    assert code == 2 and not out
    assert "BLOCKED (issue premise check)" in err
    assert "no `## Premise checks` section" in err
    for n in range(1, 7):
        assert f"  {n}. <" in err
    assert "python3 scripts/premise_gate.py check" in err


def test_2_a_partial_body_names_exactly_the_missing_answers(monkeypatch, capsys, tmp_path):
    code, _, err = _call(monkeypatch, capsys, _create("-F", str(_file(tmp_path, PARTIAL))))
    assert code == 2
    assert "answer 2 (repo searched for existing code, result written down) is missing" in err
    assert "answer 6 (" in err and "is only a <placeholder>" in err
    for n in (1, 3, 4, 5):
        assert f"answer {n} (" not in err
    assert f"(body read from {tmp_path / 'issue.md'})" in err


def test_3_a_complete_body_passes_silently(monkeypatch, capsys):
    assert _call(monkeypatch, capsys, _create(_b(SIX))) == (0, "", "")
    na = SIX.replace("5. No numbers.", "5. n/a — there are no numbers here.")
    assert _call(monkeypatch, capsys, _create(_b(na))) == (0, "", "")


@pytest.mark.parametrize(
    "labels",
    [("--label", "bug"), ("-l", "docs,bug"), ("-l", "docs", "-l", "bug"), ("--label=bug",)],
)
def test_4_a_bug_label_owes_the_verification_section(monkeypatch, capsys, labels):
    code, _, err = _call(monkeypatch, capsys, _create(*labels, _b(SIX)))
    assert code == 2 and "## Verification" in err
    assert _call(monkeypatch, capsys, _create(*labels, _b(VERIFIED)))[0] == 0


def test_5_a_bug_form_body_passes(monkeypatch, capsys):
    assert _call(monkeypatch, capsys, _create("-l", "bug", _b(_bug_form_body())))[0] == 0


def test_6_body_file_paths(monkeypatch, capsys, tmp_path):
    f = _file(tmp_path, MISSING)
    assert _call(monkeypatch, capsys, _create("-F", str(f)))[0] == 2
    assert _call(monkeypatch, capsys, _create("-F", "issue.md"), cwd=tmp_path)[0] == 2
    assert _call(monkeypatch, capsys, "cd x && " + _create("-F", "issue.md"), cwd=tmp_path)[0] == 0
    # a relative path with no payload cwd cannot be resolved
    assert _call(monkeypatch, capsys, _create("-F", "issue.md"))[0] == 0
    assert _call(monkeypatch, capsys, _create("-F", str(tmp_path / "absent.md")))[0] == 0


def test_6b_unreadable_body_files_pass(monkeypatch, capsys, tmp_path):
    fifo = tmp_path / "fifo.md"
    os.mkfifo(fifo)
    assert _call(monkeypatch, capsys, _create("-F", str(fifo)))[0] == 0
    big = tmp_path / "big.md"
    with big.open("wb") as fh:
        fh.truncate(8 * 1024 * 1024 + 1)
    assert _call(monkeypatch, capsys, _create("-F", str(big)))[0] == 0
    bad = tmp_path / "bad.md"
    bad.write_bytes(b"\xff\xfe not utf-8")
    assert _call(monkeypatch, capsys, _create("-F", str(bad)))[0] == 0


def test_6c_a_body_over_the_hook_cap_passes_quickly_and_one_under_it_is_judged(
    monkeypatch, capsys, tmp_path
):
    """Security SF-1: markdown-it is super-linear on hostile input, so this hook reads
    at most its own cap (GitHub's body limit is 65,536 characters) and passes more."""
    cap = hook._MAX_BODY_BYTES
    assert cap <= 256 * 1024
    hostile = _file(tmp_path, "[" * (cap + 1), "hostile.md")
    started = time.monotonic()
    assert _call(monkeypatch, capsys, _create("-F", str(hostile)))[0] == 0
    assert time.monotonic() - started < 5
    under = _file(
        tmp_path, ("Some prose about the change.\n" * (cap // 32))[: cap - 10], "under.md"
    )
    assert under.stat().st_size < cap
    assert _call(monkeypatch, capsys, _create("-F", str(under)))[0] == 2


def test_6d_a_file_that_reports_size_zero_is_still_capped(monkeypatch, capsys, tmp_path):
    """Security NOTE 2: procfs files report st_size 0; the read itself is bounded."""
    big = _file(tmp_path, "x" * (hook._MAX_BODY_BYTES + 5), "big.md")
    real_fstat = os.fstat

    class _Zero:
        def __init__(self, st):
            self._st = st

        def __getattr__(self, name):
            return 0 if name == "st_size" else getattr(self._st, name)

    monkeypatch.setattr(hook.os, "fstat", lambda fd: _Zero(real_fstat(fd)))
    assert hook._read_body(str(big)) is None
    monkeypatch.undo()


def test_14f_a_hung_git_config_read_is_target_unknown(monkeypatch, capsys, tmp_path):
    """Security SF-2: an include.path naming a FIFO hangs `git config`; it must time out."""
    canon = _repo(tmp_path, "https://github.com/acme/widget.git", "canon")
    fifo = tmp_path / "include.fifo"
    os.mkfifo(fifo)
    _git("-C", str(canon), "config", "include.path", str(fifo))
    started = time.monotonic()
    assert _call(monkeypatch, capsys, _create(_b(MISSING), repo=None), cwd=canon)[0] == 0
    assert time.monotonic() - started < 8


def test_7_heredoc_bodies_the_parser_excised_are_judged(monkeypatch, capsys):
    plain = _create("-F", "- <<'EOF'") + "\n" + MISSING + "\nEOF"
    assert _call(monkeypatch, capsys, plain)[0] == 2
    assert _call(monkeypatch, capsys, _create("-F", "- <<'EOF'") + "\n" + SIX + "EOF")[0] == 0
    idiom = _create("--body \"$(cat <<'EOF'") + "\n" + MISSING + '\nEOF\n)"'
    assert _call(monkeypatch, capsys, idiom)[0] == 2
    piped = "cat f.md | " + _create("-F", "-")
    assert _call(monkeypatch, capsys, piped)[0] == 0


@pytest.mark.parametrize(
    "command",
    [
        "cat > /tmp/b.md <<'EOF'\n## Example\ngh issue create -R acme/widget -t x -b short\nEOF",
        "gh -R acme/widget issue create -t T -F - <<'EOF'\ngh issue create -R acme/widget -t x -b y\nEOF",
        "python3 - <<'EOF'\nprint('x')\ngh issue create -R acme/widget -t x -b y\nEOF",
        "cat <<EOF > /tmp/b.md\ngh issue create -R acme/widget -t x -b y\nEOF",
    ],
)
def test_8_heredoc_body_lines_are_never_judged_as_commands(monkeypatch, capsys, command):
    """F1: a heredoc the parser did not excise yields its body lines as segments."""
    assert _call(monkeypatch, capsys, command) == (0, "", "")


def test_8b_a_heredoc_after_the_create_does_not_hide_it(monkeypatch, capsys):
    cmd = _create(_b(MISSING)) + " && cat > /tmp/n.md <<'EOF'\nnotes\nEOF"
    assert _call(monkeypatch, capsys, cmd)[0] == 2
    # a here-string opens no heredoc, so it neither hides a create nor makes one
    assert _call(monkeypatch, capsys, _create(_b(MISSING)) + " <<< 'stdin'")[0] == 2


def test_8c_a_phantom_identical_to_an_earlier_quoted_mention_is_not_judged(monkeypatch, capsys):
    phantom = "gh issue create -R acme/widget -t x -b y"
    cmd = f"echo '{phantom}' <<'EOF'\n{phantom}\nEOF"
    assert _call(monkeypatch, capsys, cmd)[0] == 0


def test_9_substitutions_pass_and_single_quoted_literals_are_judged(monkeypatch, capsys):
    assert _call(monkeypatch, capsys, _create('--body "cost $(date)"'))[0] == 0
    assert _call(monkeypatch, capsys, _create('--body "cost `date`"'))[0] == 0
    assert _call(monkeypatch, capsys, _create('--body "$BODY"'))[0] == 0
    assert _call(monkeypatch, capsys, _create("--body 'has `code` and $HOME'"))[0] == 2


@pytest.mark.parametrize(
    "rest",
    [
        ("--web",),
        ("--web=true",),
        ("-w",),
        ("--help",),
        ("--template", "Bug"),
        ("--editor",),
        ("--recover", "x.json"),
        (),
    ],
)
def test_10_shapes_that_file_nothing_pass(monkeypatch, capsys, rest):
    """Without a TTY gh files nothing for these; a create with no body flag fails."""
    body = (_b(MISSING),) if rest else ()
    assert _call(monkeypatch, capsys, _create(*rest, *body))[0] == 0


def test_10b_a_body_whose_text_is_a_flag_is_judged(monkeypatch, capsys):
    assert _call(monkeypatch, capsys, _create("-b", "--help"))[0] == 2
    assert _call(monkeypatch, capsys, _create("--web=false", _b(MISSING)))[0] == 2


def test_11_the_body_file_wins_and_the_last_one_counts(monkeypatch, capsys, tmp_path):
    good = _file(tmp_path, SIX, "good.md")
    bad = _file(tmp_path, MISSING, "bad.md")
    assert _call(monkeypatch, capsys, _create(_b(MISSING), "-F", str(good)))[0] == 0
    assert _call(monkeypatch, capsys, _create("-F", str(good), _b(MISSING)))[0] == 0
    assert _call(monkeypatch, capsys, _create("-F", str(bad), _b(SIX)))[0] == 2
    assert _call(monkeypatch, capsys, _create("-F", str(good), "-F", str(bad)))[0] == 2
    assert _call(monkeypatch, capsys, _create("-F", str(bad), "-F", str(good)))[0] == 0
    assert _call(monkeypatch, capsys, _create(_b(SIX), _b(MISSING)))[0] == 2


def test_12_issue_new_is_judged(monkeypatch, capsys):
    code, _, err = _call(monkeypatch, capsys, _create(_b(MISSING), sub="new"))
    assert code == 2 and "`gh issue new`" in err


def test_13_a_dispatched_session_is_never_blocked(monkeypatch, capsys):
    assert hook._DISPATCH_ENV == "GENESIS_CC_SESSION"
    needs_user = private_module("needs_user_for_premise_test", _HOOKS / "needs_user.py")
    assert hook._DISPATCH_ENV == needs_user._DISPATCH_ENV
    assert _call(monkeypatch, capsys, _create(_b(MISSING)), dispatched=True) == (0, "", "")
    res = _subprocess(_HOOK, _create(_b(MISSING)), env=_sub_env(dispatched=True))
    assert (res.returncode, res.stdout, res.stderr) == (0, "", "")


def test_14_only_the_configured_public_repo_is_enforced(monkeypatch, capsys, tmp_path):
    canon = _repo(tmp_path, "https://github.com/acme/widget.git", "canon")
    ssh = _repo(tmp_path, "git@github.com:Acme/Widget.git", "ssh")
    foreign = _repo(tmp_path, "https://github.com/acme/other.git", "foreign")
    body = _b(MISSING)
    assert _call(monkeypatch, capsys, _create(body, repo=None), cwd=canon)[0] == 2
    assert _call(monkeypatch, capsys, _create(body, repo=None), cwd=ssh)[0] == 2
    assert _call(monkeypatch, capsys, _create(body, repo=None), cwd=foreign)[0] == 0
    assert _call(monkeypatch, capsys, "cd ../x && " + _create(body, repo=None), cwd=canon)[0] == 0
    assert _call(monkeypatch, capsys, _create(body, repo=None), cwd=tmp_path)[0] == 0
    assert _call(monkeypatch, capsys, _create(body, repo="ACME/widget"), cwd=foreign)[0] == 2
    assert _call(monkeypatch, capsys, _create(body, repo="https://github.com/acme/widget"))[0] == 2
    assert _call(monkeypatch, capsys, _create(body, repo="acme/other"), cwd=canon)[0] == 0
    assert _call(monkeypatch, capsys, _create(body, repo="ghe.example/acme/widget"))[0] == 0
    assert _call(monkeypatch, capsys, "GH_REPO=a/b " + _create(body, repo=None), cwd=canon)[0] == 0
    assert (
        _call(monkeypatch, capsys, _create(body, repo=None), cwd=canon, extra={"GH_REPO": "a/b"})[0]
        == 0
    )
    assert _call(monkeypatch, capsys, _create(body), extra={"GH_HOST": "ghe.example"})[0] == 0
    # no configured public repo: nothing is enforced
    assert (
        _call(monkeypatch, capsys, _create(body), extra={"_TEST_CANONICAL_PUBLIC_REPO": ""})[0] == 0
    )


def _git(*args):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    subprocess.run(["git", *args], check=True, env=env)


def test_14b_an_implicit_target_needs_every_remote_to_name_the_public_repo(
    monkeypatch, capsys, tmp_path
):
    """SF-2: gh files in ONE remote, and which one is not modelled, so enforce only
    when every candidate agrees (a fork clone's other remote makes it unknown)."""
    canon = _repo(tmp_path, "https://github.com/acme/widget.git", "canon")
    two = _repo(tmp_path, "https://github.com/acme/widget.git", "two")
    _git("-C", str(two), "remote", "add", "upstream", "git@github.com:acme/widget.git")
    assert _call(monkeypatch, capsys, _create(_b(MISSING), repo=None), cwd=canon)[0] == 2
    assert _call(monkeypatch, capsys, _create(_b(MISSING), repo=None), cwd=two)[0] == 2
    _git("-C", str(canon), "remote", "add", "fork", "https://github.com/me/widget.git")
    assert _call(monkeypatch, capsys, _create(_b(MISSING), repo=None), cwd=canon)[0] == 0
    # a `gh repo set-default` choice decides, in both directions
    _git("-C", str(canon), "config", "remote.origin.gh-resolved", "base")
    assert _call(monkeypatch, capsys, _create(_b(MISSING), repo=None), cwd=canon)[0] == 2
    _git("-C", str(canon), "config", "--unset", "remote.origin.gh-resolved")
    _git("-C", str(canon), "config", "remote.fork.gh-resolved", "base")
    assert _call(monkeypatch, capsys, _create(_b(MISSING), repo=None), cwd=canon)[0] == 0


@pytest.mark.parametrize(
    "prefix",
    [
        "GIT_DIR=/elsewhere/.git ",
        "GIT_WORK_TREE=/elsewhere ",
        "export GIT_DIR=/elsewhere/.git; ",
        "git -C . remote set-url origin https://github.com/x/y && ",
        "git --git-dir=/elsewhere/.git config remote.origin.url u && ",
        "gh -R x/y repo set-default x/y && ",
    ],
)
def test_14c_a_step_that_moves_the_implicit_target_makes_it_unknown(
    monkeypatch, capsys, tmp_path, prefix
):
    """SF-1: each of these can point gh at another repo than the cwd's remotes."""
    canon = _repo(tmp_path, "https://github.com/acme/widget.git", "canon")
    assert _call(monkeypatch, capsys, _create(_b(MISSING), repo=None), cwd=canon)[0] == 2
    assert _call(monkeypatch, capsys, prefix + _create(_b(MISSING), repo=None), cwd=canon)[0] == 0


def test_14d_a_git_dir_in_the_session_environment_is_honoured(monkeypatch, capsys, tmp_path):
    """SF-1: the remote read must see the repository gh will see, not strip GIT_DIR."""
    canon = _repo(tmp_path, "https://github.com/acme/widget.git", "canon")
    foreign = _repo(tmp_path, "https://github.com/acme/other.git", "foreign")
    extra = {"GIT_DIR": str(foreign / ".git")}
    assert (
        _call(monkeypatch, capsys, _create(_b(MISSING), repo=None), cwd=canon, extra=extra)[0] == 0
    )


@pytest.mark.parametrize(
    "prefix",
    ["gh() { echo fake; }; ", "function gh { echo; }; ", "alias gh=echo; "],
)
def test_14e_a_redefined_gh_is_never_judged(monkeypatch, capsys, prefix):
    """SF-1: an earlier function or alias may make `gh` something else entirely."""
    assert _call(monkeypatch, capsys, prefix + _create(_b(MISSING)))[0] == 0


def test_15_an_earlier_write_to_the_body_file_makes_it_unreadable(monkeypatch, capsys, tmp_path):
    f = _file(tmp_path, MISSING)
    create = _create("-F", str(f))
    assert _call(monkeypatch, capsys, f"grep -c x {f} && {create}")[0] == 2
    assert _call(monkeypatch, capsys, f"wc -l {f}; {create}")[0] == 2
    for writer in (
        f"sed -i s/a/b/ {f}",
        f"echo more >> {f}",
        f"echo more > {f}",
        f"cat a | tee {f}",
        f"cp a.md {f}",
        f"mv a.md {f}",
        f"grep x a.md > {f}",
    ):
        assert _call(monkeypatch, capsys, f"{writer} && {create}")[0] == 0, writer


def test_15b_any_earlier_step_that_is_not_read_only_makes_the_file_unknown(
    monkeypatch, capsys, tmp_path
):
    """LOW-1: a writer need not name the file (`make body`, a checkout)."""
    f = _file(tmp_path, MISSING)
    create = _create("-F", str(f))
    for step in ("make body", "git checkout other", "python3 gen.py"):
        assert _call(monkeypatch, capsys, f"{step} && {create}")[0] == 0, step
    for step in ("true", "echo starting", "ls", f"test -f {f}"):
        assert _call(monkeypatch, capsys, f"{step} && {create}")[0] == 2, step


def test_16_compound_and_nested_creates(monkeypatch, capsys):
    code, _, err = _call(monkeypatch, capsys, "true && " + _create(_b(MISSING)))
    assert code == 2 and "segment 2 of 2" in err
    assert "the ENTIRE command was discarded" in err  # the discarded-write note
    code, _, err = _call(monkeypatch, capsys, _create(_b(MISSING)))
    assert code == 2 and "the ENTIRE command was discarded" not in err  # one step: no note
    inner = _create(_b(MISSING))
    assert _call(monkeypatch, capsys, "bash -c " + shlex.quote(inner))[0] == 2
    two = _create(_b(SIX)) + " && " + _create(_b(MISSING))
    assert _call(monkeypatch, capsys, two)[0] == 2


def test_17_unreadable_commands_pass(monkeypatch, capsys):
    assert _call(monkeypatch, capsys, _create("--bogus-flag", _b(MISSING)))[0] == 0
    assert _call(monkeypatch, capsys, "$G issue create -R acme/widget -t x " + _b(MISSING))[0] == 0
    cont = "gh issue create -R acme/widget \\\n -t x " + _b(MISSING)
    assert _call(monkeypatch, capsys, cont)[0] == 0
    assert _call(monkeypatch, capsys, _create("positional", _b(MISSING)))[0] == 0
    assert _call(monkeypatch, capsys, _create('-l "$L"', _b(SIX)))[0] == 0
    assert _call(monkeypatch, capsys, _create('-F "$HOME/x.md"'))[0] == 0


def test_17b_a_substitution_inside_the_create_makes_its_body_file_unknown(
    monkeypatch, capsys, tmp_path
):
    f = _file(tmp_path, MISSING)
    assert _call(monkeypatch, capsys, _create("-F", str(f)))[0] == 2  # control
    cmd = _create(f'-a "$(echo me > {f})"', "-F", str(f))
    assert _call(monkeypatch, capsys, cmd)[0] == 0


def test_19_no_markdown_parser_or_an_internal_error_passes(monkeypatch, capsys):
    import plan_doc  # the module the hook's premise_gate reads through

    monkeypatch.setattr(plan_doc, "_parser", lambda: None)
    assert _call(monkeypatch, capsys, _create(_b(MISSING)))[0] == 0
    monkeypatch.undo()

    def boom(*_a, **_k):
        raise RuntimeError("injected")

    monkeypatch.setattr(hook, "_judge", boom)
    assert _call(monkeypatch, capsys, _create(_b(MISSING))) == (0, "", "")


def _parse_counter(monkeypatch):
    """Every text markdown-it parses during the hook, in order."""
    import plan_doc  # the module the hook's premise_gate reads through

    real = plan_doc._parser
    texts: list[str] = []

    def counting():
        md = real()
        if md is not None:
            parse = md.parse

            def counted(src, env=None):
                texts.append(src)
                return parse(src, env)

            md.parse = counted
        return md

    monkeypatch.setattr(plan_doc, "_parser", counting)
    return texts


def test_19b_a_parse_that_outlasts_the_hook_limit_passes_with_a_could_not_check_note(
    monkeypatch, capsys, tmp_path
):
    """Owner ruling 2026-11-03: no size cap keeps markdown-it inside the 10 s budget
    (64 KB of `*a ` MEASURED 9.7 s), so the parse has its own limit and, past it, the
    create passes and says it was not checked."""
    import signal

    monkeypatch.setattr(hook, "_PARSE_LIMIT_S", 0.2)
    hostile = _file(tmp_path, "*a " * (65536 // 3), "hostile.md")
    started = time.monotonic()
    code, out, err = _call(monkeypatch, capsys, _create("-F", str(hostile)))
    elapsed = time.monotonic() - started
    assert (code, err) == (0, "")
    assert elapsed < 2, elapsed
    context = json.loads(out)["hookSpecificOutput"]
    assert context["hookEventName"] == "PreToolUse"
    assert "did not run" in context["additionalContext"]
    assert "premise_gate.py check" in context["additionalContext"]
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_19c_a_normal_body_is_judged_under_the_limit_and_the_timer_is_cancelled(
    monkeypatch, capsys
):
    import signal

    seen = []
    hook._load()
    module_gate = hook.pg
    real = module_gate.owed_message

    def watched(issue):
        seen.append(signal.getitimer(signal.ITIMER_REAL)[0])
        return real(issue)

    monkeypatch.setattr(module_gate, "owed_message", watched)
    before = signal.getsignal(signal.SIGALRM)
    code, out, err = _call(monkeypatch, capsys, _create(_b(MISSING)))
    assert code == 2 and "BLOCKED (issue premise check)" in err and out == ""
    assert seen and 0 < seen[0] <= hook._PARSE_LIMIT_S  # armed during the parse
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)  # and cancelled after
    assert signal.getsignal(signal.SIGALRM) is before


def test_19d_the_hook_parses_each_text_once(monkeypatch, capsys):
    """cProfile MEASURED owed_message parsing the same body 4 times."""
    texts = _parse_counter(monkeypatch)
    assert _call(monkeypatch, capsys, _create(_b(MISSING)))[0] == 2
    assert len(texts) == 1
    texts.clear()
    fenced = PARTIAL + "\n```\n## Premise checks\n1. quoted\n```\n"
    assert _call(monkeypatch, capsys, _create(_b(fenced)))[0] == 2
    # The body, and its quote-stripped copy (a different text), each once.
    assert len(texts) == len(set(texts)) == 2


def test_unrelated_commands_pass_silently(monkeypatch, capsys):
    for cmd in (
        "echo hello",
        "gh issue list",
        "gh issue view 5",
        'git commit -m "issue: new create"',
    ):
        assert _call(monkeypatch, capsys, cmd) == (0, "", ""), cmd


def test_a_comment_or_edit_is_out_of_scope(monkeypatch, capsys):
    assert _call(monkeypatch, capsys, "gh issue edit 5 -R acme/widget " + _b(MISSING))[0] == 0
    assert _call(monkeypatch, capsys, "gh issue comment 5 -R acme/widget " + _b(MISSING))[0] == 0


# -- import cost and the degraded paths ----------------------------------------


def _imported(stderr: str) -> set[str]:
    names = set()
    for line in stderr.splitlines():
        if line.startswith("import time:") and "|" in line:
            names.add(line.rsplit("|", 1)[1].strip())
    return names


def test_the_prefilter_skips_the_parser_import(tmp_path):
    res = _subprocess(_HOOK, "echo hello", args=("-X", "importtime"))
    assert res.returncode == 0
    names = _imported(res.stderr)
    assert "hook_input" in names, "the importtime probe saw nothing; fix the probe"
    assert not {"shell_parse", "premise_gate", "plan_doc"} & names
    res = _subprocess(_HOOK, _create(_b(SIX)), args=("-X", "importtime"))
    assert {"shell_parse", "premise_gate"} <= _imported(res.stderr)


def _tree(tmp_path: Path, poison: str) -> Path:
    root = tmp_path / "tree"
    (root / "scripts" / "hooks").mkdir(parents=True)
    for f in _HOOKS.glob("*.py"):
        shutil.copy(f, root / "scripts" / "hooks" / f.name)
    for f in _SCRIPTS.glob("*.py"):
        shutil.copy(f, root / "scripts" / f.name)
    (root / "scripts" / "hooks" / poison).write_text('raise RuntimeError("poisoned")\n')
    return root


@pytest.mark.parametrize(
    ("command", "dispatched", "expected"),
    [
        ("gh issue create -R acme/widget -t x -b short", False, 2),
        ("echo hello", False, 0),
        ("gh issue create -R acme/widget -t x -b short", True, 0),
    ],
)
def test_a_poisoned_parser_blocks_only_a_named_create(tmp_path, command, dispatched, expected):
    root = _tree(tmp_path, "shell_parse.py")
    res = _subprocess(
        root / "scripts" / "hooks" / "issue_premise_gate.py",
        command,
        env=_sub_env(dispatched=dispatched),
    )
    assert res.returncode == expected, res.stderr
    if expected == 2:
        assert "GUARD DEGRADED (issue_premise_gate)" in res.stderr
        assert "names a gated operation" in res.stderr


def test_a_poisoned_hook_input_blocks_in_the_foreground_only(tmp_path):
    root = _tree(tmp_path, "hook_input.py")
    script = root / "scripts" / "hooks" / "issue_premise_gate.py"
    res = _subprocess(script, "echo hello")
    assert res.returncode == 2 and "GUARD DEGRADED (issue_premise_gate)" in res.stderr
    res = _subprocess(script, "echo hello", env=_sub_env(dispatched=True))
    assert res.returncode == 0 and not res.stderr


# -- wiring and the hook surface -------------------------------------------------


def test_the_hook_is_wired_on_bash_through_the_launcher():
    cfg = json.loads((_ROOT / ".claude" / "settings.json").read_text())
    want = "${CLAUDE_PROJECT_DIR}/.claude/hooks/genesis-hook hooks/issue_premise_gate.py"
    found = [
        (entry.get("matcher"), h.get("timeout"))
        for entry in cfg["hooks"]["PreToolUse"]
        for h in entry.get("hooks", [])
        if h.get("command") == want
    ]
    assert found == [("Bash", 10)]


def test_the_server_gate_is_on_the_hook_surface():
    rb = private_module("review_budget_for_premise_test", _SCRIPTS / "review_budget.py")
    assert "scripts/premise_gate.py" in rb.HOOK_SURFACE_FILES


# -- the generated matrix ---------------------------------------------------------


def _shape_command(sub, shape, rpos, wrap, body, path):
    """One cell's command line, or None when the cell cannot be spelled."""
    head = ["gh"]
    if rpos == "before":
        head += ["-R", CANON]
    head += ["issue", sub]
    if rpos == "after":
        head += ["-R", CANON]
    head += ["-t", "T"]
    base = shlex.join(head)
    if shape == "-b":
        cmd = f"{base} -b {shlex.quote(body)}"
    elif shape == "--body=":
        cmd = f"{base} --body={shlex.quote(body)}"
    elif shape == "-F file":
        path.write_text(body, encoding="utf-8")
        cmd = f"{base} -F {shlex.quote(str(path))}"
    elif shape == "-F -":
        cmd = f"{base} -F - <<'EOF'\n{body}\nEOF"
    else:  # the $(cat <<'EOF' idiom
        cmd = f"{base} --body \"$(cat <<'EOF'\n{body}\nEOF\n)\""
    if wrap == "single":
        return cmd
    if wrap == "&&":
        return "true && " + cmd
    if wrap == ";":
        return "true ; " + cmd
    if wrap == "bash -c":
        return "bash -c " + shlex.quote(cmd)
    return "cat > /tmp/notes.md <<'NOTES'\n" + cmd + "\nNOTES"


def _judged(sub, shape, rpos, wrap):
    """The declared model: which cells the hook can read in full."""
    if wrap == "heredoc":
        return False
    if shape in ("-F -", "idiom"):
        # Only a first-line `gh issue create … <<'EOF'` is excised by the parser.
        return wrap == "single" and rpos != "before" and sub == "create"
    return True


def test_the_generated_matrix(monkeypatch, capsys, tmp_path):
    repo = _repo(tmp_path, "https://github.com/acme/widget.git")
    cells = list(
        itertools.product(
            ["create", "new"],
            ["-b", "--body=", "-F file", "-F -", "idiom"],
            ["before", "after", "none"],
            ["single", "&&", ";", "bash -c", "heredoc"],
            ["complete", "missing"],
            [False, True],
        )
    )
    wrong = []
    blocks = 0
    for i, (sub, shape, rpos, wrap, kind, dispatched) in enumerate(cells):
        body = SIX if kind == "complete" else MISSING
        cmd = _shape_command(sub, shape, rpos, wrap, body, tmp_path / f"b{i}.md")
        expect = (
            2 if (not dispatched and kind == "missing" and _judged(sub, shape, rpos, wrap)) else 0
        )
        code, out, _err = _call(monkeypatch, capsys, cmd, cwd=repo, dispatched=dispatched)
        blocks += code == 2
        if code != expect or out:
            wrong.append((sub, shape, rpos, wrap, kind, dispatched, code, out[:80]))
    assert len(cells) == 600 and blocks > 50
    assert not wrong, f"{len(wrong)} cells disagree with the model: {wrong[:10]}"
