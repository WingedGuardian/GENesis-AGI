"""Tests for scripts/holdout_runner.py.

A real temporary git repository supplies the parent and merge commits, so the
worktree checkout, the diff and the per-arm runs are exercised for real; only
the two read-only ``gh`` queries are replaced. The scenarios are synthetic.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_DIR = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "holdout_runner_under_test", REPO_DIR / "scripts" / "holdout_runner.py"
)
hr = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = hr  # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(hr)
_REAL_SCRATCH_BASE = hr._scratch_base

STEP_MARKER = "steps-marker-never-printed"
HIDDEN_EXPECT = "hidden-expectation-never-printed"
ISSUE = 41


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _init(root: Path) -> None:
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A repo whose 'merge' commit changes calc.py's answer and src/pkgx."""
    root = tmp_path / "repo"
    _init(root)
    (root / "calc.py").write_text(
        "def f():\n    return 'alpha-one'\n\ndef g():\n    return 'gamma'\n"
    )
    (root / "other.py").write_text("X = 1\n")
    (root / "src" / "pkgx").mkdir(parents=True)
    (root / "src" / "pkgx" / "__init__.py").write_text("VALUE = 'before'\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "parent")
    parent = _git(root, "rev-parse", "HEAD")
    (root / "calc.py").write_text(
        "def f():\n    return 'beta-two'\n\ndef g():\n    return 'gamma'\n"
    )
    (root / "src" / "pkgx" / "__init__.py").write_text("VALUE = 'after'\n")
    _git(root, "commit", "-q", "-am", "merge")
    merge = _git(root, "rev-parse", "HEAD")
    return root, parent, merge


def _scenario(sid, *, touches="calc.py", before="PASS", steps, expect, extra=""):
    issue = f"Issue: {ISSUE}\n" if before == "FAIL" and "Issue:" not in extra else ""
    return (
        f"## {sid} title\n"
        f"Protects: protects {sid}\n"
        f"Touches: {touches}\n"
        f"Expect-before: {before}\n"
        f"{issue}{extra}"
        "Steps:\n```sh\n"
        f"# {STEP_MARKER}\n{steps}\n```\n"
        "Expect:\n```\n"
        f"{expect}\n```\n\n"
    )


def _call(name: str) -> str:
    return f"\"$HOLDOUT_PYTHON\" -c 'import calc; print(calc.{name}())'"


@pytest.fixture
def fake_gh(monkeypatch, repo):
    root, _, merge = repo
    state = {
        "state": "MERGED",
        "base": "main",
        "closing": [],
        "body": "",
        "cwds": [],
        "issue": "OPEN",
    }

    def gh_json(args, cwd):
        state["cwds"].append(Path(cwd))
        if args[:2] == ["pr", "view"]:
            return {
                "state": state["state"],
                "mergeCommit": {"oid": merge},
                "baseRefName": state["base"],
                "body": state["body"],
                "closingIssuesReferences": [
                    {"number": n, "repository": {"name": name, "owner": {"login": owner}}}
                    for owner, name, n in (
                        (c if isinstance(c, tuple) else ("acme", "widgets", c))
                        for c in state["closing"]
                    )
                ],
            }
        if args[:2] == ["repo", "view"]:
            return {"defaultBranchRef": {"name": "main"}, "nameWithOwner": "acme/widgets"}
        if args[:2] == ["issue", "view"]:
            return {"state": state["issue"]}
        raise AssertionError(f"unexpected gh call: {args}")

    monkeypatch.setattr(hr, "gh_json", gh_json)
    return state


@pytest.fixture(autouse=True)
def _scratch(monkeypatch, tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(hr, "_scratch_base", lambda: str(scratch))
    return scratch


def _run_pr(repo, holdout_text, tmp_path, capsys) -> tuple[int, str]:
    root, _, _ = repo
    holdout = tmp_path / "HOLDOUT.md"
    holdout.write_text(holdout_text)
    rc = hr.main(["--pr", "7", "--root", str(root), "--holdout", str(holdout)])
    return rc, capsys.readouterr().out


def _lines(out: str) -> dict[str, str]:
    return {ln.split()[0]: ln for ln in out.splitlines() if ln.startswith("H-")}


def _at_path(root, text, tmp_path, capsys, *ids) -> tuple[int, str]:
    holdout = tmp_path / "HOLDOUT.md"
    holdout.write_text(text)
    argv = ["--at-path", str(root), "--holdout", str(holdout)]
    for sid in ids:
        argv += ["--scenario", sid]
    rc = hr.main(argv)
    return rc, capsys.readouterr().out


def test_regression_verdicts(repo, fake_gh, tmp_path, capsys):
    text = (
        _scenario("H-01", steps=_call("f"), expect="alpha-one")  # regressed
        + _scenario("H-02", steps=_call("g"), expect="gamma")  # pass
        + _scenario("H-03", touches="other.py", steps=_call("g"), expect="gamma")  # untouched
        + _scenario("H-04", steps=_call("f"), expect="beta-two")  # fixed
        + _scenario("H-05", steps=_call("f"), expect="never")  # broken before
    )
    rc, out = _run_pr(repo, text, tmp_path, capsys)
    lines = _lines(out)
    assert lines["H-01"].startswith("H-01 REGRESSED — protects H-01 — observed exit=0")
    assert '"beta-two"' in lines["H-01"]
    assert lines["H-02"] == "H-02 PASS — protects H-02"
    assert "H-03" not in lines
    assert lines["H-04"] == "H-04 FIXED — protects H-04"
    assert lines["H-05"].startswith("H-05 BROKEN-BEFORE — protects H-05 — observed")
    assert "4 scenario(s) apply to it (2 changed file(s)), 1 do not" in out
    assert rc == 1


def _intent_set() -> str:
    return (
        _scenario("H-01", before="FAIL", steps=_call("f"), expect="beta-two")  # lands here
        + _scenario("H-02", before="FAIL", steps=_call("f"), expect="never")  # never lands
        + _scenario("H-03", before="FAIL", steps=_call("g"), expect="gamma")  # already true
    )


def test_intent_verdicts_when_the_pr_closes_the_issue(repo, fake_gh, tmp_path, capsys):
    fake_gh["closing"] = [ISSUE]
    rc, out = _run_pr(repo, _intent_set(), tmp_path, capsys)
    lines = _lines(out)
    assert lines["H-01"] == "H-01 DELIVERED — protects H-01"
    assert lines["H-02"].startswith("H-02 NOT-DELIVERED — protects H-02 — observed")
    assert lines["H-03"] == "H-03 STALE-INTENT — protects H-03"
    assert rc == 1


def test_a_body_mention_links_the_issue(repo, fake_gh, tmp_path, capsys):
    fake_gh["body"] = f"Part of #{ISSUE}."
    rc, out = _run_pr(repo, _intent_set(), tmp_path, capsys)
    assert _lines(out)["H-02"].startswith("H-02 NOT-DELIVERED")
    assert rc == 1


def test_an_unlinked_pr_is_never_charged_with_an_intent_scenario(repo, fake_gh, tmp_path, capsys):
    fake_gh["body"] = (
        f"Unrelated to #{ISSUE}; see owner/other#{ISSUE}, #{ISSUE}0 and Replaces: #{ISSUE}."
    )
    rc, out = _run_pr(repo, _intent_set(), tmp_path, capsys)
    lines = _lines(out)
    assert lines["H-01"] == "H-01 DELIVERED-UNLINKED — protects H-01"
    assert lines["H-02"] == "H-02 PENDING — protects H-02"
    assert lines["H-03"] == "H-03 STALE-INTENT — protects H-03"
    assert rc == 0


def test_no_steps_or_passing_expectation_is_ever_printed(repo, fake_gh, tmp_path, capsys):
    text = _scenario("H-01", steps=f"echo {HIDDEN_EXPECT}; echo leak >&2", expect=HIDDEN_EXPECT)
    rc, out = _run_pr(repo, text, tmp_path, capsys)
    assert "H-01 PASS" in out
    assert HIDDEN_EXPECT not in out
    assert STEP_MARKER not in out
    assert "leak" not in out
    assert rc == 0


def test_matching_stdout_with_wrong_exit_does_not_echo_stdout(repo, fake_gh, tmp_path, capsys):
    text = _scenario("H-01", steps=f"echo {HIDDEN_EXPECT}; exit 3", expect=HIDDEN_EXPECT)
    rc, out = _run_pr(repo, text, tmp_path, capsys)
    assert "BROKEN-BEFORE" in out
    assert "stdout as expected, exit=3 (wrong exit status)" in out
    assert HIDDEN_EXPECT not in out
    assert rc == 0  # broken before this PR: not attributable to it


def test_expect_exit_is_honoured(repo, fake_gh, tmp_path, capsys):
    text = _scenario("H-01", steps="echo out; exit 4", expect="out", extra="Expect-exit: 4\n")
    _, out = _run_pr(repo, text, tmp_path, capsys)
    assert "H-01 PASS" in out


def test_each_arm_imports_its_own_tree(repo, fake_gh, tmp_path, capsys):
    """PYTHONPATH puts the arm's src first, so an editable install elsewhere
    cannot hand both arms the same package."""
    step = "\"$HOLDOUT_PYTHON\" -c 'import pkgx; print(pkgx.VALUE)'"
    text = _scenario("H-01", touches="src/*", steps=step, expect="before")
    _, out = _run_pr(repo, text, tmp_path, capsys)
    assert _lines(out)["H-01"].startswith("H-01 REGRESSED")
    assert '"after"' in out


def test_gh_runs_in_the_root_checkout(repo, fake_gh, tmp_path, capsys):
    root, _, _ = repo
    _run_pr(repo, _scenario("H-01", steps=_call("g"), expect="gamma"), tmp_path, capsys)
    assert fake_gh["cwds"] and all(c == root.resolve() for c in fake_gh["cwds"])


def test_worktrees_and_scratch_are_removed(repo, fake_gh, tmp_path, capsys, _scratch):
    root, _, _ = repo
    _run_pr(repo, _scenario("H-01", steps=_call("g"), expect="gamma"), tmp_path, capsys)
    assert _git(root, "worktree", "list").count("\n") == 0  # only the main tree
    assert list(_scratch.iterdir()) == []


def test_steps_run_isolated_from_live_state(repo, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "token-value-never-visible")
    monkeypatch.setenv("SOME_API_KEY", "key-value-never-visible")
    root, _, _ = repo
    checks = {
        "home": f'[ "$HOME" != "{Path.home()}" ]',
        "gh": '[ -z "${GH_TOKEN:-}" ]',
        "key": '[ -z "${SOME_API_KEY:-}" ]',
        "root": f'[ "$HOLDOUT_ROOT" = "{root.resolve()}" ] && [ "$GENESIS_REPO_ROOT" = "$HOLDOUT_ROOT" ]',
        "pypath": '[ "$PYTHONPATH" = "$HOLDOUT_ROOT/src" ]',
        "db": '[ "${GENESIS_DB_PATH#$TMPDIR/}" != "$GENESIS_DB_PATH" ]',
        "qdrant": f'[ "$QDRANT_URL" = "{hr.UNROUTABLE_QDRANT}" ]',
        "python": f'[ "$HOLDOUT_PYTHON" = "{sys.executable}" ]',
    }
    step = "; ".join(f"{cmd} && echo {name}-ok || echo {name}-BAD" for name, cmd in checks.items())
    _, out = _at_path(root, _scenario("H-01", steps=step, expect="x"), tmp_path, capsys)
    assert "-BAD" not in out
    for name in checks:
        assert f"{name}-ok" in out


def test_no_holdout_file_is_a_clean_no_op(repo, tmp_path, capsys):
    root, _, _ = repo
    rc = hr.main(["--pr", "7", "--root", str(root), "--holdout", str(tmp_path / "absent.md")])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "no holdout on this install"


def test_unreadable_holdout_is_exit_3_not_1(repo, tmp_path, capsys):
    root, _, _ = repo
    holdout = tmp_path / "HOLDOUT.md"
    holdout.write_bytes(b"## H-01 \xff\xfe broken\n")
    rc = hr.main(["--at-path", str(root), "--holdout", str(holdout)])
    assert rc == 3
    assert "cannot run: UnicodeDecodeError" in capsys.readouterr().out


def test_unmerged_pr_cannot_run(repo, fake_gh, tmp_path, capsys):
    fake_gh["state"] = "OPEN"
    rc, out = _run_pr(repo, _scenario("H-01", steps="true", expect=""), tmp_path, capsys)
    assert rc == 3
    assert "cannot run: PR #7 is not merged" in out


def test_pr_merged_into_another_branch_cannot_run(repo, fake_gh, tmp_path, capsys):
    fake_gh["base"] = "feature-x"
    rc, out = _run_pr(repo, _scenario("H-01", steps="true", expect=""), tmp_path, capsys)
    assert rc == 3
    assert "not the default branch" in out


def test_a_moved_file_still_triggers_scenarios_on_its_old_path(tmp_path, monkeypatch, capsys):
    root = tmp_path / "mv"
    _init(root)
    (root / "old").mkdir()
    (root / "old" / "mod.py").write_text("X = 1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "parent")
    _git(root, "mv", "old/mod.py", "new_mod.py")
    _git(root, "commit", "-q", "-m", "move")
    merge = _git(root, "rev-parse", "HEAD")
    monkeypatch.setattr(
        hr,
        "gh_json",
        lambda args, cwd: (
            {"state": "MERGED", "mergeCommit": {"oid": merge}, "baseRefName": "main"}
            if args[0] == "pr"
            else {"defaultBranchRef": {"name": "main"}}
        ),
    )
    holdout = tmp_path / "HOLDOUT.md"
    holdout.write_text(
        _scenario("H-01", touches="old/mod.py", steps="test -f old/mod.py && echo y", expect="y")
    )
    rc = hr.main(["--pr", "7", "--root", str(root), "--holdout", str(holdout)])
    out = capsys.readouterr().out
    assert out.splitlines()[-1].startswith("H-01 REGRESSED")
    assert rc == 1


def test_malformed_scenarios_are_named_and_skipped(repo, fake_gh, tmp_path, capsys):
    text = (
        "## H-01 no expect\nProtects: p\nTouches: calc.py\nExpect-before: PASS\n"
        "Steps:\n```sh\ntrue\n```\n\n"
        "## H-02 bad before\nProtects: p\nTouches: calc.py\nExpect-before: MAYBE\n"
        "Steps:\n```sh\ntrue\n```\nExpect:\n```\n\n```\n\n"
        "## H-03 no before\nProtects: p\nTouches: calc.py\n"
        "Steps:\n```sh\ntrue\n```\nExpect:\n```\n\n```\n\n"
        "## H-04 intent without issue\nProtects: p\nTouches: calc.py\nExpect-before: FAIL\n"
        "Steps:\n```sh\ntrue\n```\nExpect:\n```\n\n```\n\n"
        + _scenario("H-05", steps=_call("g"), expect="gamma")
        + _scenario("H-05", steps=_call("g"), expect="gamma")
        + _scenario("H-06", steps=_call("g"), expect="gamma")
    )
    rc, out = _run_pr(repo, text, tmp_path, capsys)
    assert "H-01 MALFORMED — no Expect block" in out
    assert "H-02 MALFORMED — Expect-before must be PASS or FAIL" in out
    assert "H-03 MALFORMED — Expect-before must be PASS or FAIL" in out
    assert "H-04 MALFORMED — an intent scenario needs an Issue: line" in out
    assert out.count("H-05 MALFORMED — duplicate id") == 2
    assert "H-06 PASS" in out


def test_another_heading_ends_a_scenario():
    text = (
        _scenario("H-01", steps="echo a", expect="a")
        + "## Retired: H-00\nExpect-before: FAIL\nTouches: elsewhere.py\n"
        + "Steps:\n```sh\necho b\n```\n"
    )
    (sc,) = hr.parse_holdout(text)
    assert sc.problem is None
    assert sc.expect_before == "PASS" and sc.touches == ["calc.py"]
    assert sc.steps.endswith("echo a")


def test_fences_outside_a_scenario_are_not_parsed():
    text = "Notes:\n```\nTouches: x\nExpect-before: FAIL\n```\n\n" + _scenario(
        "H-01", steps="echo a", expect="a"
    )
    scs = hr.parse_holdout(text)
    assert [sc.sid for sc in scs] == ["H-01"]
    assert scs[0].problem is None and scs[0].expect_before == "PASS"


def test_a_scenario_heading_inside_a_notes_block_still_starts_a_scenario():
    """Failing toward a noisy MALFORMED line, never toward a lost scenario."""
    text = "Notes:\n```\n## H-09 fake\n```\n\n" + _scenario("H-01", steps="echo a", expect="a")
    scs = {sc.sid: sc for sc in hr.parse_holdout(text)}
    assert scs["H-09"].problem is not None
    assert scs["H-01"].problem is None and scs["H-01"].expect == "a"


def test_unclosed_block_is_malformed():
    scs = hr.parse_holdout(
        "## H-01 t\nProtects: p\nTouches: a\nExpect-before: PASS\nSteps:\n```sh\ntrue\n"
    )
    assert scs[0].problem == "unclosed code block"


def test_step_timeout_kills_everything_the_step_started(repo, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(hr, "STEP_TIMEOUT_S", 1)
    root, _, _ = repo
    pidfile = tmp_path / "child.pid"
    started = time.monotonic()
    _, out = _at_path(
        root,
        _scenario("H-01", steps=f"sleep 30 & echo $! > {pidfile}; wait", expect=""),
        tmp_path,
        capsys,
    )
    assert "H-01 FAIL — observed timed out after 1s" in out
    # A surviving child holds stdout open, so the run itself would stall for 30s.
    assert time.monotonic() - started < 15
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail(f"step child {pid} survived the timeout")


def test_at_path_reports_pass_fail_and_unknown(repo, tmp_path, capsys):
    root, _, _ = repo
    text = _scenario("H-01", steps=_call("f"), expect="beta-two") + _scenario(
        "H-02", steps=_call("f"), expect="alpha-one"
    )
    rc, out = _at_path(root, text, tmp_path, capsys, "H-01", "H-02", "H-09")
    assert "H-01 PASS" in out
    assert 'H-02 FAIL — observed exit=0, stdout="beta-two"' in out
    assert "H-09 UNKNOWN" in out
    assert rc == 2


def test_long_observed_output_states_the_omission(repo, tmp_path, capsys):
    root, _, _ = repo
    _, out = _at_path(
        root, _scenario("H-01", steps="printf 'y%.0s' $(seq 1 1000)", expect="n"), tmp_path, capsys
    )
    assert "[600 more chars omitted]" in out


@pytest.mark.parametrize(
    "body", [f"Closes #{ISSUE}", f"fixes #{ISSUE}.", f"Implements #{ISSUE}", f"Part of #{ISSUE}"]
)
def test_closing_keywords_link_the_issue(repo, fake_gh, tmp_path, capsys, body):
    fake_gh["body"] = body
    rc, out = _run_pr(repo, _intent_set(), tmp_path, capsys)
    assert _lines(out)["H-02"].startswith("H-02 NOT-DELIVERED")
    assert rc == 1


def test_a_linked_intent_scenario_runs_whatever_files_the_pr_touched(
    repo, fake_gh, tmp_path, capsys
):
    fake_gh["closing"] = [ISSUE]
    text = _scenario(
        "H-01", before="FAIL", touches="somewhere/else.py", steps=_call("f"), expect="never"
    )
    rc, out = _run_pr(repo, text, tmp_path, capsys)
    assert _lines(out)["H-01"].startswith("H-01 NOT-DELIVERED")
    assert rc == 1


def test_an_unlinked_intent_scenario_still_needs_a_touch(repo, fake_gh, tmp_path, capsys):
    text = _scenario(
        "H-01", before="FAIL", touches="somewhere/else.py", steps=_call("f"), expect="never"
    )
    rc, out = _run_pr(repo, text, tmp_path, capsys)
    assert "H-01" not in _lines(out)
    assert rc == 0


def test_a_stray_fence_cannot_swallow_the_next_scenario():
    text = (
        _scenario("H-01", steps="echo a", expect="a")
        + "```\n\n"
        + _scenario("H-02", steps="echo b", expect="b")
    )
    scs = {sc.sid: sc for sc in hr.parse_holdout(text)}
    assert set(scs) == {"H-01", "H-02"}
    assert scs["H-02"].problem is None and scs["H-02"].expect == "b"
    assert scs["H-01"].expect == "a"


def test_a_stray_fence_inside_a_scenario_marks_it():
    text = "## H-01 t\nProtects: p\nTouches: a\nExpect-before: PASS\n```\nnotes\n\n" + _scenario(
        "H-02", steps="echo b", expect="b"
    )
    scs = {sc.sid: sc for sc in hr.parse_holdout(text)}
    assert scs["H-01"].problem == "code block not closed"
    assert scs["H-02"].problem is None


def test_an_unclosed_stray_fence_at_the_end_marks_the_scenario():
    scs = hr.parse_holdout(_scenario("H-01", steps="echo a", expect="a") + "```\ntrailing\n")
    assert scs[0].problem == "unclosed code block"


@pytest.mark.parametrize("heading", ["## H-02a title", "## H-01-retired", "## H-"])
def test_headings_that_are_not_ids_are_reported(heading):
    text = _scenario("H-01", steps="echo a", expect="a") + f"{heading}\nTouches: x\n"
    scs = hr.parse_holdout(text)
    assert scs[0].problem is None
    assert scs[1].problem == "heading is not '## H-<digits> title'"


def test_children_left_by_a_finished_step_are_killed(repo, tmp_path, capsys):
    root, _, _ = repo
    pidfile = tmp_path / "left.pid"
    started = time.monotonic()
    _, out = _at_path(
        root,
        _scenario(
            "H-01",
            steps=f"sleep 30 >/dev/null 2>&1 & echo $! > {pidfile}; echo done",
            expect="done",
        ),
        tmp_path,
        capsys,
    )
    assert "H-01 PASS" in out
    assert time.monotonic() - started < 15
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    pytest.fail(f"step child {pid} survived its step")


def test_oversized_stdout_fails_without_being_read(repo, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(hr, "STDOUT_LIMIT_BYTES", 100)
    root, _, _ = repo
    _, out = _at_path(
        root,
        _scenario("H-01", steps="head -c 500 /dev/zero | tr '\\0' y", expect="y"),
        tmp_path,
        capsys,
    )
    assert "H-01 FAIL — observed stdout over 100 bytes" in out


def test_the_ssh_agent_and_unlisted_variables_are_not_inherited(
    repo, tmp_path, capsys, monkeypatch
):
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    monkeypatch.setenv("DATABASE_URL", "postgres://example.invalid/db")
    monkeypatch.setenv("GITHUB_PAT", "pat-value")
    root, _, _ = repo
    step = 'env | cut -d= -f1 | grep -E "^(SSH_AUTH_SOCK|DATABASE_URL|GITHUB_PAT)$" || echo clean'
    _, out = _at_path(root, _scenario("H-01", steps=step, expect="clean"), tmp_path, capsys)
    assert "H-01 PASS" in out


def test_a_closing_reference_to_another_repository_does_not_link(repo, fake_gh, tmp_path, capsys):
    fake_gh["closing"] = [("other", "project", ISSUE)]
    rc, out = _run_pr(repo, _intent_set(), tmp_path, capsys)
    assert _lines(out)["H-02"] == "H-02 PENDING — protects H-02"
    assert rc == 0


def test_a_closing_reference_matches_the_repository_case_insensitively(
    repo, fake_gh, tmp_path, capsys
):
    fake_gh["closing"] = [("ACME", "Widgets", ISSUE)]
    _, out = _run_pr(repo, _intent_set(), tmp_path, capsys)
    assert _lines(out)["H-02"].startswith("H-02 NOT-DELIVERED")


def test_a_non_ascii_file_name_still_triggers_its_scenario(tmp_path, monkeypatch, capsys):
    root = tmp_path / "uni"
    _init(root)
    (root / "café.py").write_text("X = 1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "parent")
    (root / "café.py").write_text("X = 2\n")
    _git(root, "commit", "-q", "-am", "merge")
    merge = _git(root, "rev-parse", "HEAD")
    monkeypatch.setattr(
        hr,
        "gh_json",
        lambda args, cwd: (
            {"state": "MERGED", "mergeCommit": {"oid": merge}, "baseRefName": "main"}
            if args[0] == "pr"
            else {"defaultBranchRef": {"name": "main"}, "nameWithOwner": "acme/widgets"}
        ),
    )
    holdout = tmp_path / "HOLDOUT.md"
    holdout.write_text(_scenario("H-01", touches="café.py", steps="echo y", expect="y"))
    hr.main(["--pr", "7", "--root", str(root), "--holdout", str(holdout)])
    assert "H-01 PASS" in capsys.readouterr().out


def test_control_characters_in_a_block_are_kept_literally(repo, tmp_path, capsys):
    root, _, _ = repo
    text = _scenario("H-01", steps="printf 'a\\fb\\n'", expect="a\fb")
    (sc,) = hr.parse_holdout(text)
    assert sc.expect == "a\fb"
    _, out = _at_path(root, text, tmp_path, capsys)
    assert "H-01 PASS" in out


def test_crlf_files_parse_like_lf_files():
    text = _scenario("H-01", steps="echo a", expect="a").replace("\n", "\r\n")
    (sc,) = hr.parse_holdout(text)
    assert sc.problem is None and sc.expect == "a" and sc.steps.endswith("echo a")


def test_a_descendant_that_starts_its_own_session_is_killed(repo, tmp_path, capsys):
    root, _, _ = repo
    pidfile = tmp_path / "escaped.pid"
    child = tmp_path / "spawn.py"
    child.write_text(
        "import subprocess\n"
        "p = subprocess.Popen(['sleep', '30'], start_new_session=True,\n"
        "                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
    )
    step = f'"$HOLDOUT_PYTHON" {child}; echo done'
    _, out = _at_path(root, _scenario("H-01", steps=step, expect="done"), tmp_path, capsys)
    assert "H-01 PASS" in out
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    pytest.fail(f"descendant {pid} in its own session survived its step")


def test_the_scratch_root_is_created_never_replaced_by_the_default_temp(tmp_path, monkeypatch):
    fresh = tmp_path / "fresh-home"
    fresh.mkdir()
    monkeypatch.setenv("HOME", str(fresh))
    assert _REAL_SCRATCH_BASE() == str(fresh / "tmp")
    assert (fresh / "tmp").is_dir()


def test_an_uncreatable_scratch_root_stops_the_run(tmp_path, monkeypatch):
    blocker = tmp_path / "home-file"
    blocker.write_text("not a directory")
    monkeypatch.setenv("HOME", str(blocker))
    with pytest.raises(hr.RunnerError, match="cannot create the scratch directory"):
        _REAL_SCRATCH_BASE()


@pytest.mark.parametrize(
    "fn,args", [("gh_json", (["pr", "view"], Path("/"))), ("_git", (Path("/"), "status"))]
)
def test_a_hung_gh_or_git_becomes_a_runner_error(monkeypatch, fn, args):
    def hang(*a, **kw):
        # A call with no timeout would hang for real: that is the defect.
        assert kw.get("timeout"), f"{fn} ran its command with no timeout"
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=kw["timeout"])

    monkeypatch.setattr(hr.subprocess, "run", hang)
    with pytest.raises(hr.RunnerError, match="timed out"):
        getattr(hr, fn)(*args)


def test_an_unlinked_intent_scenario_whose_issue_closed_is_flagged(repo, fake_gh, tmp_path, capsys):
    fake_gh["issue"] = "CLOSED"
    rc, out = _run_pr(repo, _intent_set(), tmp_path, capsys)
    assert _lines(out)["H-02"] == "H-02 CLOSED-NOT-DELIVERED — protects H-02"
    assert rc == 0  # not attributable to this PR


def test_an_unreadable_issue_state_stays_pending(repo, fake_gh, tmp_path, capsys, monkeypatch):
    real = hr.gh_json

    def flaky(args, cwd):
        if args[:2] == ["issue", "view"]:
            raise hr.RunnerError("network down")
        return real(args, cwd)

    monkeypatch.setattr(hr, "gh_json", flaky)
    _, out = _run_pr(repo, _intent_set(), tmp_path, capsys)
    assert _lines(out)["H-02"] == "H-02 PENDING — protects H-02"


@pytest.fixture
def install_holdout(tmp_path, monkeypatch):
    """Point the runner's install-level paths at a temporary Genesis home."""
    home = tmp_path / "ghome"
    (home / "holdout").mkdir(parents=True)
    monkeypatch.setattr(hr, "DEFAULT_HOLDOUT", home / "holdout" / "HOLDOUT.md")
    monkeypatch.setattr(hr, "EXPOSURE_LOG", home / "logs" / "holdout-exposures.jsonl")
    return home


def _run_install(repo, home, text, capsys):
    root, _, _ = repo
    holdout = home / "holdout" / "HOLDOUT.md"
    holdout.write_text(text)
    rc = hr.main(["--pr", "7", "--root", str(root), "--holdout", str(holdout)])
    return rc, capsys.readouterr().out


def test_the_install_holdout_gets_a_ripgrep_ignore_once(repo, fake_gh, install_holdout, capsys):
    ignore = install_holdout / ".ignore"
    ignore.write_text("other/")
    text = _scenario("H-01", steps=_call("g"), expect="gamma")
    _run_install(repo, install_holdout, text, capsys)
    _run_install(repo, install_holdout, text, capsys)
    assert ignore.read_text() == "other/\nholdout/\n"


def test_a_test_holdout_file_never_writes_an_ignore(repo, fake_gh, tmp_path, capsys):
    _run_pr(repo, _scenario("H-01", steps=_call("g"), expect="gamma"), tmp_path, capsys)
    assert not (tmp_path / ".ignore").exists() and not (tmp_path.parent / ".ignore").exists()


def test_exposures_are_counted_from_the_fence_log(repo, fake_gh, install_holdout, capsys):
    log = install_holdout / "logs" / "holdout-exposures.jsonl"
    log.parent.mkdir()
    rows = [
        {"at": "2026-11-02T00:00:00Z", "kind": "read"},
        {"at": "2026-11-01T00:00:00Z", "kind": "content"},
    ]
    log.write_text("".join(json.dumps(r) + "\n" for r in rows))
    _, out = _run_install(
        repo, install_holdout, _scenario("H-01", steps=_call("g"), expect="gamma"), capsys
    )
    assert (
        "holdout fence: 2 exposure(s), first 2026-11-01T00:00:00Z, latest 2026-11-02T00:00:00Z"
        in out
    )


def test_unreadable_lines_are_counted_not_fatal(repo, fake_gh, install_holdout, capsys):
    log = install_holdout / "logs" / "holdout-exposures.jsonl"
    log.parent.mkdir()
    log.write_text(
        json.dumps({"at": "2026-11-02T00:00:00Z", "kind": "read"}) + '\n{"at": "cut off\n[1]\n'
    )
    _, out = _run_install(
        repo, install_holdout, _scenario("H-01", steps=_call("g"), expect="gamma"), capsys
    )
    assert "1 exposure(s)" in out and "2 unreadable line(s)" in out


def test_an_empty_log_says_none(repo, fake_gh, install_holdout, capsys):
    log = install_holdout / "logs" / "holdout-exposures.jsonl"
    log.parent.mkdir()
    log.write_text("")
    _, out = _run_install(
        repo, install_holdout, _scenario("H-01", steps=_call("g"), expect="gamma"), capsys
    )
    assert "holdout fence: no exposures logged" in out


def test_a_missing_log_says_so(repo, fake_gh, install_holdout, capsys):
    _, out = _run_install(
        repo, install_holdout, _scenario("H-01", steps=_call("g"), expect="gamma"), capsys
    )
    assert "holdout fence: no exposures logged (no exposure log yet)" in out


def test_a_non_utf8_ignore_file_does_not_stop_the_run(repo, fake_gh, install_holdout, capsys):
    (install_holdout / ".ignore").write_bytes(b"\xff\xfe\n")
    rc, out = _run_install(
        repo, install_holdout, _scenario("H-01", steps=_call("g"), expect="gamma"), capsys
    )
    assert rc == 0 and "H-01 PASS" in out


def test_an_unexpected_error_exits_3_never_1(repo, monkeypatch, tmp_path, capsys):
    def broken(args, cwd):
        raise AttributeError("gh returned something odd")

    monkeypatch.setattr(hr, "gh_json", broken)
    rc, out = _run_pr(repo, _scenario("H-01", steps="true", expect=""), tmp_path, capsys)
    assert rc == 3 and "AttributeError" in out


def test_genesis_home_moves_the_default_holdout(monkeypatch, tmp_path):
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "gh"))
    assert hr.genesis_home() == tmp_path / "gh"
    monkeypatch.delenv("GENESIS_HOME")
    assert hr.genesis_home() == Path.home() / ".genesis"
