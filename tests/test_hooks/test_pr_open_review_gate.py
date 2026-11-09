"""`gh pr create` needs an independent review of the branch: the hook, run for real.

Every case runs the script as the harness does (stdin payload, exit code, stderr), in a
real git repository with a real linked worktree. The transcripts are synthetic but carry
the fields Claude Code writes (measured on 2.1.299).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import private_module

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "scripts" / "hooks" / "pr_open_review_gate.py"
PUBLIC = "acme/widgets"
CREATE = 'gh pr create --title "t" --body "b"'
SIGIL = "# pr-review-waived"


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def box(tmp_path):
    """A main checkout, a linked worktree on feat/x, and an empty session."""
    main = tmp_path / "genesis"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    _git(main, "remote", "add", "origin", f"https://github.com/{PUBLIC}.git")
    _git(main, "commit", "-q", "--allow-empty", "-m", "base")
    wt = tmp_path / "worktrees" / "gate-work"
    _git(main, "worktree", "add", "-q", "-b", "feat/x", str(wt))
    transcript = tmp_path / "sessions" / "s1.jsonl"
    transcript.parent.mkdir()
    transcript.write_text('{"type":"user"}\n')

    class Box:
        pass

    b = Box()
    b.main, b.wt, b.transcript, b.tmp = main, wt, transcript, tmp_path
    b.overrides = tmp_path / "overrides"
    return b


def _agent(
    box,
    agent_type="genesis-architect",
    prompt="Review the staged diff.",
    branch="feat/x",
    cwd=None,
    stop="end_turn",
    model="claude-opus-5-5",
    trailer=None,
    name="agent-a1",
):
    agents = box.transcript.with_suffix("") / "subagents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{name}.meta.json").write_text(json.dumps({"agentType": agent_type}))
    lines = [
        {
            "type": "user",
            "cwd": str(cwd if cwd is not None else box.tmp / "elsewhere"),
            "gitBranch": branch,
            "message": {"role": "user", "content": prompt},
        },
        {
            "type": "assistant",
            "message": {
                "stop_reason": stop,
                "model": model,
                "content": [{"type": "text", "text": "x"}],
            },
        },
    ]
    if trailer:
        lines.append(trailer)
    (agents / f"{name}.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))


def _run(
    box,
    cmd=CREATE,
    cwd=None,
    *,
    public=PUBLIC,
    dispatched=False,
    transcript="default",
    extra_env=None,
):
    env = {
        **os.environ,
        "_TEST_CANONICAL_PUBLIC_REPO": public,
        "GENESIS_MERGE_OVERRIDE_DIR": str(box.overrides),
    }
    env.pop("GENESIS_CC_SESSION", None)
    env.pop("GH_REPO", None)
    if dispatched:
        env["GENESIS_CC_SESSION"] = "1"
    env.update(extra_env or {})
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "cwd": str(cwd if cwd is not None else box.wt),
        "session_id": "s1",
    }
    if transcript == "default":
        payload["transcript_path"] = str(box.transcript)
    elif transcript is not None:
        payload["transcript_path"] = transcript
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        cwd=box.tmp,
    )


def _rows(box):
    return [
        json.loads(ln) for f in box.overrides.glob("*.jsonl") for ln in f.read_text().splitlines()
    ]


class TestEvidence:
    def test_no_review_refuses_and_names_the_branch_and_the_fix(self, box):
        done = _run(box)
        assert done.returncode == 2
        assert "feat/x" in done.stderr and "/deep-review" in done.stderr and SIGIL in done.stderr

    @pytest.mark.parametrize(
        "agent_type",
        [
            "genesis-architect",
            "genesis-security-reviewer",
            "feature-dev:code-reviewer",
            "pr-review-toolkit:code-reviewer",
        ],
    )
    def test_a_finished_reviewer_on_the_branch_passes(self, box, agent_type):
        _agent(box, agent_type=agent_type)
        assert _run(box).returncode == 0

    @pytest.mark.parametrize(
        "agent_type", ["general-purpose", "Explore", "Plan", "fork", "workflow-subagent"]
    )
    def test_other_agents_do_not_count(self, box, agent_type):
        _agent(box, agent_type=agent_type)
        assert _run(box).returncode == 2

    def test_the_builtin_code_review_counts_by_its_recipe_header(self, box):
        _agent(
            box,
            agent_type="general-purpose",
            prompt="`low effort → 1 diff pass → no verify → ≤4 findings`\n\n## Turn 1",
        )
        assert _run(box).returncode == 0

    def test_a_builder_that_mentions_the_recipe_later_does_not_count(self, box):
        _agent(box, agent_type="general-purpose", prompt="Build it. Then note `low effort → x`")
        assert _run(box).returncode == 2

    def test_an_old_reviewer_format_on_an_unrelated_record_does_not_break_the_scan(self, box):
        _agent(box, name="agent-good")
        _agent(box, name="agent-bad")
        agents = box.transcript.with_suffix("") / "subagents"
        (agents / "agent-bad.jsonl").write_text("not json\n")
        # Newest first, so the bad record is the one actually reached first.
        good, bad = agents / "agent-good.meta.json", agents / "agent-bad.meta.json"
        os.utime(good, (1_000, 1_000))
        os.utime(bad, (2_000, 2_000))
        assert _run(box).returncode == 0


class TestTiedToTheBranch:
    def test_a_review_of_another_branch_does_not_count(self, box):
        _agent(box, branch="feat/other")
        assert _run(box).returncode == 2

    def test_the_prompt_naming_the_branch_ties_it(self, box):
        _agent(box, branch="main", prompt="Review the staged diff on feat/x please.")
        assert _run(box).returncode == 0

    def test_the_prompt_naming_the_worktree_ties_it(self, box):
        _agent(box, branch="main", prompt=f"Review {box.wt} now.")
        assert _run(box).returncode == 0

    @pytest.mark.parametrize(
        "text", ["see feat/xy", "see feat/x-other", "see prefeat/x", "see feat/x/other"]
    )
    def test_a_longer_word_containing_the_branch_does_not_tie_it(self, box, text):
        _agent(box, branch="main", prompt=f"Review {text}.")
        assert _run(box).returncode == 2

    def test_running_in_the_worktree_ties_it(self, box):
        _agent(box, branch="main", cwd=box.wt)
        assert _run(box).returncode == 0

    def test_the_main_checkout_ties_nothing(self, box):
        # Every session starts in the main checkout, so a reviewer there says nothing
        # about which branch it read. Its directory name is no signal either.
        _agent(box, branch="main", cwd=box.main, prompt=f"Review something in {box.main}.")
        assert _run(box, f"{CREATE} --head feat/x", cwd=box.main).returncode == 2

    @pytest.mark.parametrize("via", ["cwd", "prompt"])
    def test_the_main_checkout_ties_nothing_for_its_own_branch(self, box, via):
        _git(box.main, "switch", "-q", "-c", "feat/m")
        if via == "cwd":
            _agent(box, branch="elsewhere", cwd=box.main)
        else:
            _agent(box, branch="elsewhere", prompt=f"Review {box.main}/a.py.")
        assert _run(box, cwd=box.main).returncode == 2

    def test_a_path_inside_the_worktree_names_it(self, box):
        _agent(box, branch="main", prompt=f"Review {box.wt}/a.py.")
        assert _run(box).returncode == 0

    def test_a_worktree_named_with_a_plain_word_needs_a_path(self, box):
        wt = box.tmp / "worktrees" / "review"
        _git(box.main, "worktree", "add", "-q", "-b", "feat/y", str(wt))
        _agent(box, branch="main", prompt="Please review this change.")
        assert _run(box, cwd=wt).returncode == 2
        _agent(box, branch="main", prompt=f"Please read {wt}/a.py.")
        assert _run(box, cwd=wt).returncode == 0

    def test_an_explicit_head_is_the_branch_judged(self, box):
        _agent(box, branch="feat/other")
        assert _run(box, f"{CREATE} --head feat/other").returncode == 0
        assert _run(box, f"{CREATE} --head me:feat/other").returncode == 0
        assert _run(box, f"{CREATE} --head feat/third").returncode == 2

    def test_a_worktree_directory_does_not_vouch_for_an_explicit_other_head(self, box):
        _agent(box, branch="main", cwd=box.wt)
        assert _run(box, f"{CREATE} --head feat/third").returncode == 2

    def test_a_prompt_naming_this_worktree_does_not_vouch_for_an_explicit_other_head(self, box):
        _agent(box, branch="main", prompt=f"Review {box.wt} now.")
        assert _run(box, f"{CREATE} --head feat/third").returncode == 2

    def test_an_explicit_head_of_this_very_branch_keeps_the_worktree_tie(self, box):
        _agent(box, branch="main", prompt=f"Review {box.wt} now.")
        assert _run(box, f"{CREATE} --head feat/x").returncode == 0
        _agent(box, branch="main", cwd=box.wt, prompt="Review the diff.")
        assert _run(box, f"{CREATE} --head feat/x").returncode == 0

    def test_a_cd_into_the_worktree_is_followed(self, box):
        _agent(box)
        assert _run(box, f"cd {box.wt} && {CREATE}", cwd=box.main).returncode == 0
        _agent(box, branch="feat/other")
        assert _run(box, f"cd {box.wt} && {CREATE}", cwd=box.main).returncode == 2


class TestFinished:
    @pytest.mark.parametrize("stop", ["tool_use", None, "max_tokens"])
    def test_a_run_that_did_not_end_normally_does_not_count(self, box, stop):
        _agent(box, stop=stop)
        assert _run(box).returncode == 2

    def test_a_usage_limit_ending_does_not_count(self, box):
        _agent(box, model="<synthetic>")
        assert _run(box).returncode == 2

    def test_a_line_after_the_final_turn_is_read_past(self, box):
        _agent(box, trailer={"type": "attachment", "attachment": {}})
        assert _run(box).returncode == 0

    def test_a_tool_call_after_end_of_text_means_still_running(self, box):
        _agent(
            box,
            trailer={
                "type": "assistant",
                "message": {"stop_reason": "tool_use", "model": "m", "content": []},
            },
        )
        assert _run(box).returncode == 2


class TestScope:
    def test_no_declared_public_repo_is_out_of_scope(self, box):
        assert _run(box, public="").returncode == 0

    def test_a_checkout_of_another_repo_is_out_of_scope(self, box):
        assert _run(box, public="acme/other").returncode == 0

    def test_explicit_repo_decides(self, box):
        assert _run(box, f"{CREATE} --repo acme/elsewhere").returncode == 0
        assert _run(box, f"{CREATE} --repo {PUBLIC}", cwd=box.tmp).returncode == 2
        assert _run(box, f"{CREATE} -R {PUBLIC.upper()}").returncode == 2

    def test_the_last_repo_flag_wins_as_it_does_in_gh(self, box):
        assert _run(box, f"{CREATE} --repo acme/elsewhere --repo {PUBLIC}").returncode == 2
        assert _run(box, f"{CREATE} --repo {PUBLIC} --repo acme/elsewhere").returncode == 0

    def test_a_remote_other_than_origin_counts(self, box):
        _git(box.main, "remote", "set-url", "origin", "https://github.com/me/fork.git")
        assert _run(box).returncode == 0
        _git(box.main, "remote", "add", "upstream", f"https://github.com/{PUBLIC}.git")
        assert _run(box).returncode == 2

    @pytest.mark.parametrize(
        "cmd",
        [
            "git status",
            "gh pr create --help",
            f"{CREATE} --dry-run",
            "gh pr view 1",
            "gh issue create --title t --body b",
            f"echo '{CREATE}'",
        ],
    )
    def test_other_commands_pass(self, box, cmd):
        assert _run(box, cmd).returncode == 0


class TestUnreadableCommand:
    CMD = "gh pr \\\ncreate --title t --body b"

    def test_this_hook_defers_on_a_command_the_parser_cannot_read(self, box):
        assert _run(box, self.CMD).returncode == 0

    def test_the_push_guard_refuses_that_command(self, box):
        # The deferral above is only sound if this holds: otherwise the shape opens a PR
        # past both hooks.
        env = {**os.environ, "_TEST_CANONICAL_PUBLIC_REPO": PUBLIC}
        env.pop("GENESIS_CC_SESSION", None)
        payload = {"tool_name": "Bash", "tool_input": {"command": self.CMD}, "cwd": str(box.wt)}
        done = subprocess.run(
            [sys.executable, str(HOOK.parent / "git_push_guard.py")],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            cwd=box.wt,
        )
        assert done.returncode == 2, done.stderr


class TestOverride:
    def test_it_lets_the_pr_open_and_is_logged_with_no_command_text(self, box):
        done = _run(box, f'gh pr create --title "secret-looking" --body "b" {SIGIL}')
        assert done.returncode == 0
        rows = _rows(box)
        assert [r["sigil"] for r in rows] == ["pr-review-waived"]
        assert rows[0]["outcome"] == "allowed" and "secret-looking" not in json.dumps(rows)
        assert len(rows[0]["head"]) == 40  # which commit the yes covered

    def test_it_works_on_the_required_first_push_shape(self, box):
        assert _run(box, f"git push -u origin HEAD && {CREATE} {SIGIL}").returncode == 0

    def test_that_shape_still_qualifies_for_the_push_guard(self, box):
        guard = private_module("git_push_guard_for_pr_open_gate", HOOK.parent / "git_push_guard.py")
        (box.wt / "body.md").write_text("b")
        cmd = f'git push -u origin HEAD && gh pr create --title "t" --body-file body.md {SIGIL}'
        segs, blind = guard.analyze_checked(cmd)
        assert blind is None
        assert guard._push_then_pr_create(segs, segs[0], cmd, str(box.wt)) is segs[1]

    def test_it_is_not_needed_and_not_logged_when_reviewed(self, box):
        _agent(box)
        assert _run(box, f"{CREATE} {SIGIL}").returncode == 0
        assert _rows(box) == []

    def test_the_word_inside_a_quoted_value_waives_nothing(self, box):
        assert _run(box, f'gh pr create --title "{SIGIL}" --body "b"').returncode == 2

    def test_on_another_segment_it_waives_nothing(self, box):
        assert _run(box, f"{CREATE} && echo ok {SIGIL}").returncode == 2

    def test_a_dispatched_session_cannot_use_it(self, box):
        done = _run(box, f"{CREATE} {SIGIL}", dispatched=True)
        assert done.returncode == 2 and "dispatched" in done.stderr
        assert _rows(box) == []

    def test_a_dispatched_session_with_a_review_passes(self, box):
        _agent(box)
        assert _run(box, dispatched=True).returncode == 0


class TestCouldNotVerify:
    @pytest.mark.parametrize(
        "transcript", [None, "", "/nonexistent/s.jsonl", "/tmp/not-a-transcript.txt"]
    )
    def test_unreadable_session_records_block_and_say_so(self, box, transcript):
        done = _run(box, transcript=transcript)
        assert done.returncode == 2 and "could not verify" in done.stderr

    def test_the_override_is_the_repair_path(self, box):
        assert _run(box, f"{CREATE} {SIGIL}", transcript=None).returncode == 0

    def test_an_unresolvable_directory_blocks_and_says_so(self, box):
        done = _run(box, f'cd "$UNSET_DIR" && {CREATE}')
        assert done.returncode == 2 and "could not verify" in done.stderr

    def test_an_unresolvable_directory_blocks_when_the_repo_is_named_too(self, box):
        done = _run(box, f'cd "$UNSET_DIR" && {CREATE} --repo {PUBLIC}')
        assert done.returncode == 2 and "could not verify" in done.stderr

    def test_an_unresolvable_directory_is_still_out_of_scope_with_no_public_repo(self, box):
        assert _run(box, f'cd "$UNSET_DIR" && {CREATE}', public="").returncode == 0

    def test_an_expanded_head_blocks_and_says_so(self, box):
        done = _run(box, f'{CREATE} --head "$BRANCH"')
        assert done.returncode == 2 and "could not verify" in done.stderr

    def test_a_detached_head_blocks_and_says_so(self, box):
        _git(box.wt, "checkout", "-q", "--detach")
        done = _run(box)
        assert done.returncode == 2 and "could not verify" in done.stderr


class TestFlagValuesAreNotFlags:
    """A flag's value is the caller's text, so it can spell anything."""

    @pytest.mark.parametrize(
        "extra",
        [
            '--title "-Rother/repo"',
            '-t "-Rother/repo"',
            '--reviewer "-Rother/repo"',
            '--title "--repo=other/repo"',
            "--title --dry-run",
            "--body --dry-run",
            '--title "--head=feat/reviewed"',
        ],
    )
    def test_they_change_nothing(self, box, extra):
        _agent(box, branch="feat/reviewed")
        done = _run(box, f"gh pr create --body b {extra}")
        assert done.returncode == 2
        assert "feat/x" in done.stderr

    def test_a_glued_head_is_the_head(self, box):
        _agent(box, branch="feat/reviewed")
        assert _run(box, f"{CREATE} -Hfeat/reviewed").returncode == 0
        assert _run(box, f"{CREATE} -Hfeat/other").returncode == 2

    def test_a_real_dry_run_still_passes(self, box):
        assert _run(box, f"{CREATE} --dry-run").returncode == 0

    def test_an_unknown_option_cannot_be_judged(self, box):
        _agent(box)
        done = _run(box, f"{CREATE} --brand-new-flag x")
        assert done.returncode == 2 and "could not verify" in done.stderr

    def test_the_new_spelling_of_create_is_gated(self, box):
        assert _run(box, 'gh pr new --title "t" --body "b"').returncode == 2


class TestRepoSpellings:
    @pytest.mark.parametrize(
        "repo",
        [
            f"https://github.com/{PUBLIC}.git",
            f"https://github.com/{PUBLIC}",
            f"git@github.com:{PUBLIC}.git",
            f"ssh://git@github.com/{PUBLIC}",
            f"github.com/{PUBLIC}",
            PUBLIC,
        ],
    )
    def test_each_names_the_public_repo_from_anywhere(self, box, repo):
        assert _run(box, f"{CREATE} --repo {repo}", cwd=box.tmp).returncode == 2

    @pytest.mark.parametrize(
        "repo", ["ghe.example.com/acme/widgets", "acme/other", "https://github.com/acme/other"]
    )
    def test_others_are_out_of_scope(self, box, repo):
        assert _run(box, f"{CREATE} --repo {repo}").returncode == 0

    @pytest.mark.parametrize(
        "cmd",
        [
            f'{CREATE} --repo "$R"',
            f'{CREATE} --repo "$OWNER/other"',
            f'{CREATE} --repo "$HOST/acme/other"',
            f"GH_REPO=other/repo {CREATE}",
            f"export GH_REPO=other/repo && {CREATE}",
        ],
    )
    def test_a_selector_this_cannot_read_cannot_be_judged(self, box, cmd):
        done = _run(box, cmd)
        assert done.returncode == 2 and "could not verify" in done.stderr


class TestGitFailure:
    def _shim(self, box, body):
        bin_dir = box.tmp / "bin"
        bin_dir.mkdir()
        shim = bin_dir / "git"
        shim.write_text(f"#!/bin/sh\n{body}\n")
        shim.chmod(0o755)
        return {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}

    def _run_env(self, box, extra):
        env = {**os.environ, **extra, "_TEST_CANONICAL_PUBLIC_REPO": PUBLIC}
        env.pop("GENESIS_CC_SESSION", None)
        env.pop("GH_REPO", None)
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": CREATE},
            "cwd": str(box.wt),
            "transcript_path": str(box.transcript),
        }
        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            cwd=box.tmp,
        )

    def test_git_that_hangs_cannot_be_judged(self, box):
        done = self._run_env(box, self._shim(box, "exec sleep 60"))
        assert done.returncode == 2 and "could not verify" in done.stderr

    def test_a_directory_git_says_is_no_repo_is_out_of_scope(self, box):
        plain = box.tmp / "plain"
        plain.mkdir()
        assert _run(box, cwd=plain).returncode == 0


class TestEarlierSteps:
    @pytest.mark.parametrize(
        "step", ["git switch feat/other", "git checkout feat/other", "pushd /tmp"]
    )
    def test_a_step_that_moves_the_branch_cannot_be_judged(self, box, step):
        _agent(box)
        done = _run(box, f"{step} && {CREATE}")
        assert done.returncode == 2 and "could not verify" in done.stderr

    def test_naming_the_head_resolves_it(self, box):
        _agent(box, branch="feat/other")
        assert _run(box, f"git switch feat/other && {CREATE} --head feat/other").returncode == 0


class TestFormatDrift:
    def _meta(self, box, payload):
        agents = box.transcript.with_suffix("") / "subagents"
        agents.mkdir(parents=True, exist_ok=True)
        (agents / "agent-a1.meta.json").write_text(payload)

    @pytest.mark.parametrize(
        "payload", ['{"renamed": "genesis-architect"}', '{"agentType": ["x"]}', "not json"]
    )
    def test_records_with_no_readable_type_cannot_be_judged(self, box, payload):
        self._meta(box, payload)
        done = _run(box)
        assert done.returncode == 2 and "could not verify" in done.stderr

    def test_a_typed_record_of_another_kind_is_simply_no_review(self, box):
        _agent(box, agent_type="Explore")
        done = _run(box)
        assert done.returncode == 2 and "no independent review" in done.stderr

    def test_a_prompt_block_with_null_text_does_not_cost_the_reviewer_its_tie(self, box):
        _agent(box, branch="main", prompt=[{"text": None}, {"text": f"Review {box.wt} now."}])
        assert _run(box).returncode == 0


class TestLoggedOnlyWhenItLetsTheCommandThrough:
    def test_a_later_create_that_refuses_logs_nothing(self, box):
        done = _run(box, f"{CREATE} {SIGIL}\n{CREATE}")
        assert done.returncode == 2
        assert _rows(box) == []


class TestDryRunValue:
    @pytest.mark.parametrize("value", ["false", "0", "FALSE", "f"])
    def test_a_false_value_is_a_real_create(self, box, value):
        assert _run(box, f"{CREATE} --dry-run={value}").returncode == 2

    @pytest.mark.parametrize("value", ["true", "1", "T"])
    def test_a_true_value_creates_nothing(self, box, value):
        assert _run(box, f"{CREATE} --dry-run={value}").returncode == 0


class TestShortBranchNames:
    def test_a_bare_word_branch_is_not_tied_by_a_prompt_using_the_word(self, box):
        _git(box.wt, "branch", "fix")
        _agent(box, branch="main", prompt="Review how to fix the voice repo.")
        assert _run(box, f"{CREATE} --head fix").returncode == 2

    def test_a_bare_word_branch_is_still_tied_by_the_branch_field(self, box):
        _agent(box, branch="fix")
        assert _run(box, f"{CREATE} --head fix").returncode == 0


class TestOddFiles:
    def test_a_fifo_cannot_hang_the_scan(self, box):
        _agent(box, name="agent-good")
        agents = box.transcript.with_suffix("") / "subagents"
        for name in ("agent-fifo.meta.json", "agent-fifo2.jsonl"):
            os.mkfifo(agents / name)
        (agents / "agent-fifo2.meta.json").write_text(
            json.dumps({"agentType": "genesis-architect"})
        )
        os.utime(agents / "agent-good.meta.json", (1_000, 1_000))
        assert _run(box).returncode == 0


def _could_not_verify(done):
    return done.returncode == 2 and "could not verify" in done.stderr


class TestOutOfScopeIsAPositiveAnswer:
    @pytest.mark.parametrize(
        "cmd",
        [
            f"git worktree add -b feat/z ../zz && cd ../zz && {CREATE}",
            f"git clone https://github.com/{PUBLIC} ../w && cd ../w && {CREATE}",
            f"mkdir -p sub && cd sub && {CREATE}",
        ],
    )
    def test_a_directory_an_earlier_step_has_yet_to_make_cannot_be_judged(self, box, cmd):
        assert _could_not_verify(_run(box, cmd))

    @pytest.mark.parametrize("step", ["git init -q", f"gh repo clone {PUBLIC} ."])
    def test_a_repository_an_earlier_step_makes_here_cannot_be_judged(self, box, step):
        empty = box.tmp / "empty"
        empty.mkdir()
        assert _could_not_verify(_run(box, f"{step} && {CREATE}", cwd=empty))

    def test_a_remote_an_earlier_step_adds_cannot_be_judged(self, box):
        bare = box.tmp / "bare"
        bare.mkdir()
        _git(bare, "init", "-q")
        cmd = f"git remote add origin https://github.com/{PUBLIC} && {CREATE}"
        assert _could_not_verify(_run(box, cmd, cwd=bare))

    def test_a_corrupt_config_cannot_be_judged(self, box):
        (box.main / ".git" / "config").write_text("[remote\n not a config")
        assert _could_not_verify(_run(box, cwd=box.main))

    def test_a_repository_with_no_remote_is_out_of_scope(self, box):
        bare = box.tmp / "bare"
        bare.mkdir()
        _git(bare, "init", "-q")
        assert _run(box, cwd=bare).returncode == 0

    def test_a_plain_directory_is_out_of_scope(self, box):
        plain = box.tmp / "plain"
        plain.mkdir()
        assert _run(box, cwd=plain).returncode == 0

    @pytest.mark.parametrize(
        "repo",
        [
            f"www.github.com/{PUBLIC}",
            f"https://www.github.com/{PUBLIC}",
            f"ssh.github.com/{PUBLIC}",
            f"ssh://git@ssh.github.com:443/{PUBLIC}.git",
        ],
    )
    def test_each_github_host_names_the_public_repo(self, box, repo):
        assert _run(box, f"{CREATE} --repo {repo}", cwd=box.tmp).returncode == 2

    @pytest.mark.parametrize(
        "repo", [f"github.com.example.org/{PUBLIC}", f"notgithub.com/{PUBLIC}"]
    )
    def test_a_look_alike_host_named_explicitly_is_out_of_scope(self, box, repo):
        assert _run(box, f"{CREATE} --repo {repo}").returncode == 0

    @pytest.mark.parametrize(
        ("url", "code"),
        [
            (f"git@ssh.github.com:{PUBLIC}.git", 2),
            (f"ssh://git@ssh.github.com:443/{PUBLIC}.git", 2),
            (f"git@github.com:{PUBLIC}.git", 2),
            (f"https://github.example.org/{PUBLIC}.git", 2),
            (f"git@gh-work:{PUBLIC}.git", 2),
            ("https://github.example.org/acme/elsewhere.git", 0),
        ],
    )
    def test_each_remote_spelling(self, box, url, code):
        _git(box.main, "remote", "set-url", "origin", url)
        assert _run(box).returncode == code

    def test_a_second_remote_can_name_it(self, box):
        _git(box.main, "remote", "set-url", "origin", "https://github.com/me/fork.git")
        _git(box.main, "remote", "add", "upstream", f"https://github.com/{PUBLIC}.git")
        assert _run(box).returncode == 2

    def test_gh_repo_in_the_environment_decides(self, box):
        assert _run(box, cwd=box.tmp, extra_env={"GH_REPO": PUBLIC}).returncode == 2
        assert _run(box, extra_env={"GH_REPO": "acme/elsewhere"}).returncode == 0


class TestSpellings:
    @pytest.mark.parametrize(
        "cmd",
        [
            "g''h pr create --title t --body b",
            "gh pr cr''eate --title t --body b",
            'sh -c "gh pr cr""eate --title t"',
        ],
    )
    def test_a_word_split_by_quotes_is_still_a_create(self, box, cmd):
        assert _run(box, cmd).returncode == 2

    def test_a_command_with_a_stray_apostrophe_is_still_judged(self, box):
        cmd = f"{CREATE} --head feat/x # don't"
        assert _run(box, cmd).returncode == 2
        _agent(box)
        assert _run(box, cmd).returncode == 0

    @pytest.mark.parametrize(
        "opts", [f"-dR{PUBLIC}", f"-dR {PUBLIC}", f"-wR{PUBLIC}", f"-dR={PUBLIC}", f"-zR{PUBLIC}"]
    )
    def test_a_repo_inside_a_short_cluster_is_read(self, box, opts):
        assert _run(box, f"{CREATE} {opts}", cwd=box.tmp).returncode == 2

    def test_a_cluster_naming_another_repo_is_out_of_scope(self, box):
        assert _run(box, f"{CREATE} -dRacme/elsewhere").returncode == 0

    def test_a_head_inside_a_cluster_is_read(self, box):
        _agent(box, branch="feat/other")
        assert _run(box, f"{CREATE} -dHfeat/other").returncode == 0
        assert _run(box, f"{CREATE} -dH feat/third").returncode == 2

    def test_a_value_glued_to_a_value_flag_is_not_a_cluster(self, box):
        # `-tR...` is a title, so this names no other repo.
        assert _run(box, "gh pr create -tRacme/elsewhere --body b").returncode == 2

    def test_a_flag_after_a_cluster_is_not_taken_as_its_value(self, box):
        _agent(box, branch="feat/other")
        assert _run(box, f"{CREATE} -dw --head feat/other").returncode == 0

    def test_the_same_create_twice_cannot_be_judged(self, box):
        other = box.tmp / "worktrees" / "second-work"
        _git(box.main, "worktree", "add", "-q", "-b", "feat/two", str(other))
        _agent(box)
        cmd = f"cd {box.wt} && {CREATE}; cd {other} && {CREATE}"
        assert _could_not_verify(_run(box, cmd))

    def test_a_title_mentioning_the_variable_does_not_set_it(self, box):
        _agent(box)
        assert _run(box, 'gh pr create --title "docs: GH_REPO=x is read" --body b').returncode == 0


class TestStepsBeforeTheCreate:
    @pytest.mark.parametrize(
        "step",
        [
            "gh pr checkout 5",
            "git branch -m feat/other",
            "git stash branch foo",
            "git bisect start",
            "git worktree add ../q",
            "git switch -c feat/q",
            "git checkout feat/q",
            "git remote add up x",
        ],
    )
    def test_a_step_that_may_move_the_branch_cannot_be_judged(self, box, step):
        _agent(box)
        assert _could_not_verify(_run(box, f"{step} && {CREATE}"))
        assert _run(box, f"{step} && {CREATE} --head feat/x").returncode == 0

    @pytest.mark.parametrize(
        "step",
        [
            "git add -A",
            "git commit -m x",
            "git push -u origin HEAD",
            "git status",
            "git pull",
            "git rev-parse HEAD",
            "gh pr list",
            "ruff check .",
            "cat > f <<'EOF'\nThe body says git is fine\nEOF\n",
        ],
    )
    def test_a_step_that_leaves_the_branch_alone_keeps_the_tie(self, box, step):
        _agent(box)
        joiner = "" if step.endswith("\n") else " && "
        assert _run(box, f"{step}{joiner}{CREATE}").returncode == 0


class TestRecordFormat:
    def _rewrite(self, box, first=None, final=None):
        path = box.transcript.with_suffix("") / "subagents" / "agent-a1.jsonl"
        lines = [json.loads(x) for x in path.read_text().splitlines()]
        if first:
            first(lines[0])
        if final:
            final(lines[-1]["message"])
        path.write_text("".join(json.dumps(x) + "\n" for x in lines))

    def test_a_record_that_is_not_json_says_could_not_verify(self, box):
        _agent(box)
        path = box.transcript.with_suffix("") / "subagents" / "agent-a1.jsonl"
        path.write_text("not json\n")
        assert _could_not_verify(_run(box))

    def test_renamed_branch_and_directory_fields_say_could_not_verify(self, box):
        _agent(box)

        def rename(line):
            line["branch"] = line.pop("gitBranch")
            line["dir"] = line.pop("cwd")

        self._rewrite(box, first=rename)
        assert _could_not_verify(_run(box))

    def test_a_renamed_stop_reason_says_could_not_verify(self, box):
        _agent(box)
        self._rewrite(box, final=lambda m: m.update(stopReason=m.pop("stop_reason")))
        assert _could_not_verify(_run(box))

    def test_one_good_record_settles_it_whatever_the_others_are(self, box):
        _agent(box, name="agent-good")
        _agent(box, name="agent-odd")
        path = box.transcript.with_suffix("") / "subagents" / "agent-odd.jsonl"
        path.write_text("not json\n")
        assert _run(box).returncode == 0

    def test_a_reviewer_still_running_is_no_review_not_drift(self, box):
        _agent(box, stop=None)
        done = _run(box)
        assert done.returncode == 2 and "no independent review" in done.stderr

    def test_a_reviewer_of_something_else_is_no_review_not_drift(self, box):
        _agent(box, branch="feat/other")
        done = _run(box)
        assert done.returncode == 2 and "no independent review" in done.stderr

    def test_a_long_run_is_still_read_from_its_end(self, box):
        _agent(box)
        path = box.transcript.with_suffix("") / "subagents" / "agent-a1.jsonl"
        first, final = path.read_text().splitlines()
        filler = json.dumps({"type": "user", "pad": "x" * (5 * 1024 * 1024)})
        path.write_text(f"{first}\n{filler}\n{final}\n")
        assert _run(box).returncode == 0

    @pytest.mark.parametrize(
        "agent_type",
        [
            "genesis-architect",
            "genesis-security-reviewer",
            "feature-dev:code-reviewer",
            "pr-review-toolkit:code-reviewer",
            "pr-review-toolkit:silent-failure-hunter",
            "pr-review-toolkit:pr-test-analyzer",
            "pr-review-toolkit:type-design-analyzer",
            "pr-review-toolkit:comment-analyzer",
        ],
    )
    def test_each_reviewer_type_counts(self, box, agent_type):
        _agent(box, agent_type=agent_type)
        assert _run(box).returncode == 0


class TestConfigThatDeclaresNothing:
    def _home(self, box, *, config):
        home = box.tmp / "home"
        (home / ".genesis" / "config").mkdir(parents=True)
        if config:
            (home / ".genesis" / "config" / "genesis.yaml").write_text("github: {publc_repo: x}\n")
        return {"HOME": str(home)}

    def test_it_tells_the_session_the_check_is_off(self, box):
        done = _run(box, public="", extra_env=self._home(box, config=True))
        assert done.returncode == 0
        note = json.loads(done.stdout)["hookSpecificOutput"]
        assert "OFF" in note["additionalContext"] and "permissionDecision" not in note

    def test_a_clone_with_no_config_stays_quiet(self, box):
        done = _run(box, public="", extra_env=self._home(box, config=False))
        assert done.returncode == 0 and done.stdout == ""


class TestRemotesAndBooleans:
    def test_a_remote_named_in_global_config_with_no_url_is_no_remote(self, box):
        cfg = box.tmp / "gitconfig"
        cfg.write_text('[remote "origin"]\n\tprune = true\n')
        bare = box.tmp / "bare"
        bare.mkdir()
        _git(bare, "init", "-q")
        env = {"GIT_CONFIG_GLOBAL": str(cfg)}
        assert _run(box, cwd=bare, extra_env=env).returncode == 0
        step = f"git remote add origin https://github.com/{PUBLIC} && {CREATE}"
        assert _could_not_verify(_run(box, step, cwd=bare, extra_env=env))

    def test_a_boolean_short_with_a_value_is_not_an_unknown_option(self, box):
        _agent(box)
        assert _run(box, f"{CREATE} -d=true").returncode == 0


class TestSecurityReview:
    @pytest.mark.parametrize("tail", ["?x=1", "#f", ".git?x=1"])
    def test_a_url_with_a_query_or_fragment_cannot_be_judged(self, box, tail):
        repo = f"https://github.com/{PUBLIC}{tail}"
        assert _could_not_verify(_run(box, f"{CREATE} -R '{repo}'", cwd=box.tmp))

    @pytest.mark.parametrize(
        "step",
        [
            f"git remote set-url origin git@github.com:{PUBLIC}.git",
            f"git remote add upstream https://github.com/{PUBLIC}.git",
            f"git config remote.origin.url git@github.com:{PUBLIC}.git",
            f"gh repo set-default {PUBLIC}",
        ],
    )
    def test_a_step_that_retargets_a_checkout_of_another_repo_cannot_be_judged(self, box, step):
        _git(box.main, "remote", "set-url", "origin", "https://github.com/me/fork.git")
        assert _could_not_verify(_run(box, f"{step} && {CREATE}"))

    def test_moving_the_branch_in_another_repo_stays_out_of_scope(self, box):
        _git(box.main, "remote", "set-url", "origin", "https://github.com/me/fork.git")
        assert _run(box, f"git switch -c feat/q && {CREATE}").returncode == 0

    def test_a_command_with_too_many_creates_cannot_be_judged(self, box):
        _agent(box)
        many = "; ".join(f"gh pr create -t t{i} -b b -R {PUBLIC}" for i in range(21))
        assert _could_not_verify(_run(box, many))

    def test_the_most_creates_allowed_are_still_judged(self, box):
        _agent(box)
        some = "; ".join(f"gh pr create -t t{i} -b b -R {PUBLIC}" for i in range(20))
        assert _run(box, some).returncode == 0

    def test_a_waiver_with_an_explicit_head_names_no_commit(self, box):
        assert _run(box, f"{CREATE} --head feat/other {SIGIL}").returncode == 0
        assert _rows(box)[0].get("head") in (None, "")

    @pytest.mark.parametrize(
        "cmd",
        [
            "gh pr c\\\nreate --title t --body b",
            "gh pr $(echo create) --title t --body b",
            'gh pr "$SUB" --title t --body b create',
            'eval "gh pr create --title t --body b"',
            "gh pr create --title t --body " + "x" * 50_000,
        ],
    )
    def test_each_form_left_to_the_push_guard_is_refused_by_it(self, box, cmd):
        env = {**os.environ, "_TEST_CANONICAL_PUBLIC_REPO": PUBLIC}
        env.pop("GENESIS_CC_SESSION", None)
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "cwd": str(box.wt),
            "session_id": "s1",
            "transcript_path": str(box.transcript),
        }
        gate = _run(box, cmd).returncode
        done = subprocess.run(
            [sys.executable, str(HOOK.with_name("git_push_guard.py"))],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            cwd=box.tmp,
        )
        assert gate == 2 or done.returncode == 2, (gate, done.returncode, done.stdout[:200])


class TestThirdReview:
    @pytest.mark.parametrize(
        "cmd",
        [
            "pushd {pub} && {c}",
            "builtin cd {pub} && {c}",
            "command cd {pub} && {c}",
            "if cd {pub}; then {c}; fi",
            "for d in {pub}; do cd $d; {c}; done",
            "! cd {pub} ; {c}",
            "time cd {pub} ; {c}",
            "env -C {pub} {c}",
            "env --chdir={pub} {c}",
        ],
    )
    def test_a_directory_change_it_cannot_follow_cannot_be_judged(self, box, cmd):
        _git(box.main, "remote", "set-url", "origin", "https://github.com/acme/elsewhere.git")
        pub = box.tmp / "pub"
        pub.mkdir()
        _git(pub, "init", "-q")
        _git(pub, "remote", "add", "origin", f"https://github.com/{PUBLIC}.git")
        assert _could_not_verify(_run(box, cmd.format(pub=pub, c=CREATE)))

    def test_a_plain_cd_is_still_followed(self, box):
        _git(box.main, "remote", "set-url", "origin", "https://github.com/acme/elsewhere.git")
        plain = box.tmp / "plain"
        plain.mkdir()
        assert _run(box, f"cd {plain} && {CREATE}").returncode == 0

    @pytest.mark.parametrize(
        "cmd",
        [
            f"env 'GH_REPO={PUBLIC}' {CREATE}",
            f'export "GH_REPO={PUBLIC}"; {CREATE}',
            f"unset GH_REPO; {CREATE}",
            f"env -u GH_REPO {CREATE}",
        ],
    )
    def test_each_way_of_changing_the_variable_cannot_be_judged(self, box, cmd):
        assert _could_not_verify(_run(box, cmd, extra_env={"GH_REPO": "acme/elsewhere"}))

    @pytest.mark.parametrize(
        "step",
        [
            "git branch --show-current",
            "git branch",
            "git remote -v",
            "git remote",
            "git config user.name",
            "git config --get user.name",
        ],
    )
    def test_a_git_step_that_only_reads_changes_nothing(self, box, step):
        _agent(box)
        assert _run(box, f"{step} && {CREATE}").returncode == 0
        _git(box.main, "remote", "set-url", "origin", "https://github.com/acme/elsewhere.git")
        assert _run(box, f"{step} && {CREATE}").returncode == 0

    @pytest.mark.parametrize(
        "cmd",
        [
            f'{CREATE} -H "$B"',
            f"{CREATE} --future",
            f"GH_REPO=x/y {CREATE}",
            f"git branch -m feat/z && {CREATE}",
        ],
    )
    def test_a_structural_refusal_does_not_send_the_session_to_a_review(self, box, cmd):
        _agent(box)
        done = _run(box, cmd)
        assert _could_not_verify(done) and "/deep-review" not in done.stderr
        assert SIGIL in done.stderr

    def test_a_no_review_refusal_names_the_write_tool(self, box):
        assert "Write tool" in _run(box).stderr

    def test_too_many_creates_offers_no_override_it_will_not_honour(self, box):
        # One per line: SIGIL starts a comment, so after "; " bash would run
        # only the first create.
        many = "\n".join(f"gh pr create -t t{i} -b b -R {PUBLIC} {SIGIL}" for i in range(21))
        done = _run(box, many)
        assert _could_not_verify(done) and "pr-review-waived" not in done.stderr

    def test_a_reviewer_in_a_subdirectory_of_the_worktree_ties_it(self, box):
        sub = box.wt / "src"
        sub.mkdir()
        _agent(box, branch="main", cwd=sub)
        assert _run(box).returncode == 0

    def test_a_directory_sharing_a_prefix_with_the_worktree_does_not(self, box):
        _agent(box, branch="main", cwd=f"{box.wt}-other")
        assert _run(box).returncode == 2

    def test_an_explicit_head_finds_the_worktree_holding_it(self, box):
        _agent(box, branch="main", cwd=box.wt)
        assert _run(box, f"{CREATE} -H feat/x", cwd=box.main).returncode == 0

    def test_an_explicit_head_held_by_the_main_checkout_ties_nothing(self, box):
        _agent(box, branch="elsewhere", cwd=box.main)
        assert _run(box, f"{CREATE} -H main", cwd=box.wt).returncode == 2

    @pytest.mark.parametrize("repo", ["{x/y,acme/widgets}", "acme/wid*", "~/x"])
    def test_a_selector_the_shell_will_rewrite_cannot_be_judged(self, box, repo):
        assert _could_not_verify(_run(box, f"{CREATE} -R {repo}"))

    def test_a_payload_with_no_directory_cannot_be_judged(self, box):
        env = {**os.environ, "_TEST_CANONICAL_PUBLIC_REPO": PUBLIC}
        env.pop("GH_REPO", None)
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": CREATE},
            "transcript_path": str(box.transcript),
        }
        done = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            cwd=box.wt,
        )
        assert _could_not_verify(done)

    def test_null_branch_and_directory_fields_say_could_not_verify(self, box):
        _agent(box)
        path = box.transcript.with_suffix("") / "subagents" / "agent-a1.jsonl"
        lines = [json.loads(x) for x in path.read_text().splitlines()]
        lines[0]["gitBranch"] = lines[0]["cwd"] = None
        path.write_text("".join(json.dumps(x) + "\n" for x in lines))
        assert _could_not_verify(_run(box))


class TestDirectoryWithANamedRepo:
    def test_an_untracked_move_still_leaves_the_branch_unknown(self, box):
        _agent(box)
        assert _could_not_verify(_run(box, f"pushd {box.main} && {CREATE} -R {PUBLIC}"))

    def test_naming_the_head_as_well_settles_it(self, box):
        _agent(box)
        assert _run(box, f"pushd {box.main} && {CREATE} -R {PUBLIC} -H feat/x").returncode == 0


class TestRefusalsSayWhatWorks:
    def test_unreadable_records_send_the_session_to_a_review(self, box):
        _agent(box)
        path = box.transcript.with_suffix("") / "subagents" / "agent-a1.jsonl"
        path.write_text("not json\n")
        done = _run(box)
        assert _could_not_verify(done)
        assert "/deep-review" in done.stderr and "A review alone" not in done.stderr
        _agent(box, name="agent-good")
        assert _run(box).returncode == 0

    @pytest.mark.parametrize(
        "cmd",
        [
            'gh pr create --title "fix env scrub" --body "run git -C foo status"',
            'gh pr create --title "env loader honours --chdir" --body b',
        ],
    )
    def test_a_title_mentioning_env_and_a_flag_is_not_a_directory_change(self, box, cmd):
        _agent(box)
        assert _run(box, cmd).returncode == 0

    def test_no_directory_with_a_named_repo_still_cannot_be_judged(self, box):
        env = {**os.environ, "_TEST_CANONICAL_PUBLIC_REPO": PUBLIC}
        env.pop("GH_REPO", None)
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": f"{CREATE} -R {PUBLIC}"},
            "transcript_path": str(box.transcript),
        }
        done = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            cwd=box.wt,
        )
        assert _could_not_verify(done)


class TestPromptTiesAreWholeWords:
    @pytest.mark.parametrize(
        "text",
        [
            "archive/feat/x",
            "../feat/x",
            "x.feat/x",
            "feat/x.py",
            "feat/x.bak",
            "feat/x+1",
        ],
    )
    def test_text_that_only_contains_the_branch_does_not_tie_it(self, box, text):
        _agent(box, branch="main", prompt=f"Review {text} please.")
        assert _run(box).returncode == 2

    @pytest.mark.parametrize(
        "text",
        [
            "feat/x",
            "`feat/x`",
            "'feat/x',",
            "feat/x.",
            "origin/feat/x",
            "refs/heads/feat/x",
            "main...feat/x",
            "origin/main..origin/feat/x",
            "feat/x~1",
        ],
    )
    def test_each_way_of_naming_the_branch_ties_it(self, box, text):
        _agent(box, branch="main", prompt=f"Review {text} now")
        assert _run(box).returncode == 0

    def test_a_longer_version_does_not_tie_the_shorter_branch(self, box):
        _git(box.main, "branch", "release/1.2")
        _agent(box, branch="main", prompt="Review release/1.2.1 please.")
        assert _run(box, f"{CREATE} --head release/1.2").returncode == 2

    @pytest.mark.parametrize(
        "text",
        [
            "/other/gate-work/a.py",
            "/other/gate-work",
            "~/other/gate-work",
            "gate-work.bak",
            "pre/gate-work",
            "../gate-work",
        ],
    )
    def test_another_directory_of_the_same_name_does_not_tie_the_worktree(self, box, text):
        _agent(box, branch="main", prompt=f"Review {text} please.")
        assert _run(box).returncode == 2

    def test_a_relative_path_ending_in_this_checkout_ties_it(self, box):
        _agent(box, branch="main", prompt="Review worktrees/gate-work/a.py please.")
        assert _run(box).returncode == 0

    @pytest.mark.parametrize("text", ["/review", "src/genesis/review.py", "/elsewhere/review"])
    def test_a_path_that_only_contains_a_plain_word_name_does_not_tie_it(self, box, text):
        wt = box.tmp / "worktrees" / "review"
        _git(box.main, "worktree", "add", "-q", "-b", "feat/y", str(wt))
        _agent(box, branch="main", prompt=f"Read {text} please.")
        assert _run(box, cwd=wt).returncode == 2


class TestAReusedWorktree:
    def test_a_review_of_the_branch_it_held_before_does_not_carry_over(self, box):
        _agent(box, branch="feat/old", cwd=box.wt)
        assert _run(box).returncode == 2

    def test_a_session_on_the_default_branch_still_ties_a_reviewer_run_there(self, box):
        _agent(box, branch="main", cwd=box.wt)
        assert _run(box).returncode == 0


class TestOneFlagTable:
    @pytest.mark.parametrize(
        "cmd",
        [
            "gh --title --repo=acme/elsewhere pr create --body b",
            "gh --body --repo=acme/elsewhere pr create --title t",
            "gh -t --repo=acme/elsewhere pr create --body b",
            "gh -tRacme/elsewhere pr create --body b",
            "gh -t -Racme/elsewhere pr create --body b",
            "gh pr --title --repo=acme/elsewhere create --body b",
            "gh pr -tRacme/elsewhere create --body b",
        ],
    )
    def test_a_value_taken_before_the_command_path_is_not_a_repo(self, box, cmd):
        assert _run(box, cmd).returncode == 2

    @pytest.mark.parametrize(
        "cmd",
        [
            "gh --head feat/x pr create --title t --body b",
            "gh -Hfeat/x pr create --title t --body b",
            "gh pr --head=feat/x create --title t --body b",
        ],
    )
    def test_a_head_named_before_the_command_path_is_read(self, box, cmd):
        _agent(box, branch="feat/x")
        assert _run(box, cmd).returncode == 0

    def test_dry_run_before_the_command_path_is_read(self, box):
        assert _run(box, "gh --dry-run pr create --title t --body b").returncode == 0

    @pytest.mark.parametrize(
        "cmd",
        [
            f"{CREATE} --brand-new --repo=acme/elsewhere",
            "gh --future -Racme/elsewhere pr create --title t --body b",
        ],
    )
    def test_an_unknown_option_cannot_be_trusted_about_the_repo_after_it(self, box, cmd):
        assert _could_not_verify(_run(box, cmd))

    @pytest.mark.parametrize(
        "cmd",
        [
            "gh pr create $ARGS --title t --body b",
            'gh pr create "${A[@]}" --title t --body b',
            "gh pr create $(cat args) --title t --body b",
            "gh $G pr create --title t --body b",
        ],
    )
    def test_a_word_a_create_takes_no_operand_for_cannot_be_judged(self, box, cmd):
        _agent(box)
        done = _run(box, cmd)
        assert done.returncode == 2

    def test_a_body_the_parser_split_into_loose_words_is_still_judged(self, box):
        _agent(box)
        cmd = "gh pr create --title t --body \"$(cat <<'EOF'\nA `quoted` word: it's fine\nEOF\n)\""
        assert _run(box, cmd).returncode == 0

    def test_a_create_with_only_known_options_is_judged(self, box):
        _agent(box)
        cmd = f"{CREATE} --base main --draft --label x --reviewer y --body-file f"
        assert _run(box, cmd).returncode == 0


class TestWhereGhWillSendIt:
    def _remote(self, box, name, repo, default=None):
        _git(box.main, "remote", "add", name, f"https://github.com/{repo}.git")
        if default:
            _git(box.main, "config", f"remote.{name}.gh-resolved", default)

    def test_a_stored_default_on_a_fork_is_out_of_scope(self, box):
        _git(box.main, "remote", "set-url", "origin", "https://github.com/me/fork.git")
        _git(box.main, "config", "remote.origin.gh-resolved", "base")
        self._remote(box, "upstream", PUBLIC)
        assert _run(box).returncode == 0

    def test_a_stored_default_naming_a_fork_from_the_public_remote_is_out_of_scope(self, box):
        _git(box.main, "config", "remote.origin.gh-resolved", "me/fork")
        assert _run(box).returncode == 0

    def test_a_stored_default_naming_the_public_repo_is_in_scope(self, box):
        _git(box.main, "remote", "set-url", "origin", "https://github.com/me/fork.git")
        _git(box.main, "config", "remote.origin.gh-resolved", PUBLIC)
        assert _run(box).returncode == 2

    def test_a_stored_base_on_the_public_remote_is_in_scope(self, box):
        _git(box.main, "config", "remote.origin.gh-resolved", "base")
        self._remote(box, "fork", "me/fork")
        assert _run(box).returncode == 2

    def test_no_stored_default_still_asks_whether_any_remote_names_it(self, box):
        _git(box.main, "remote", "set-url", "origin", "https://github.com/me/fork.git")
        self._remote(box, "upstream", PUBLIC)
        assert _run(box).returncode == 2

    def test_a_default_on_a_host_gh_may_skip_leaves_it_to_the_remotes(self, box):
        _git(box.main, "remote", "set-url", "origin", "https://ghe.example.org/me/fork.git")
        _git(box.main, "config", "remote.origin.gh-resolved", "base")
        self._remote(box, "upstream", PUBLIC)
        assert _run(box).returncode == 2

    def test_a_default_set_earlier_in_the_command_cannot_be_trusted(self, box):
        _git(box.main, "remote", "set-url", "origin", "https://github.com/me/fork.git")
        _git(box.main, "config", "remote.origin.gh-resolved", "base")
        assert _could_not_verify(_run(box, f"gh repo set-default {PUBLIC} && {CREATE}"))


class TestACdThatMayNotHaveRun:
    @pytest.mark.parametrize(
        "cmd",
        [
            "cat > f <<'EOF'\ncd {plain}\nEOF\n{c}",
            "false && cd {plain}; {c}",
            '[ -n "" ] && cd {plain}; {c}',
        ],
    )
    def test_it_cannot_put_a_create_out_of_scope(self, box, cmd):
        plain = box.tmp / "plain"
        plain.mkdir()
        assert _could_not_verify(_run(box, cmd.format(plain=plain, c=CREATE)))

    def test_a_cd_that_opens_the_command_still_can(self, box):
        plain = box.tmp / "plain"
        plain.mkdir()
        assert _run(box, f"cd {plain} && {CREATE}").returncode == 0

    def test_it_does_not_matter_when_the_repo_is_named(self, box):
        plain = box.tmp / "plain"
        plain.mkdir()
        assert _run(box, f"false && cd {plain}; {CREATE} --repo acme/elsewhere").returncode == 0


class TestSecondModelNotes:
    def test_a_waiver_tells_the_session_on_a_channel_it_can_read(self, box):
        done = _run(box, f"{CREATE} {SIGIL}")
        assert done.returncode == 0
        note = json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "unreviewed" in note and "permissionDecision" not in done.stdout

    def test_the_superpowers_reviewer_counts(self, box):
        _agent(box, agent_type="superpowers:code-reviewer")
        assert _run(box).returncode == 0

    def test_a_refusal_for_an_unresolved_directory_names_the_write_tool(self, box):
        _agent(box)
        done = _run(box, 'cd "$W" && gh pr create --title t --body b')
        assert _could_not_verify(done) and "Write tool" in done.stderr


class TestFixReviewNotes:
    def test_a_push_url_does_not_decide_the_stored_default(self, box):
        _git(box.main, "remote", "set-url", "--push", "origin", "git@github.com:me/fork.git")
        _git(box.main, "config", "remote.origin.gh-resolved", "base")
        assert _run(box).returncode == 2

    def test_a_push_url_alone_does_not_put_it_in_scope(self, box):
        _git(box.main, "remote", "set-url", "origin", "https://github.com/me/fork.git")
        _git(box.main, "remote", "set-url", "--push", "origin", f"git@github.com:{PUBLIC}.git")
        assert _run(box).returncode == 0

    def test_an_empty_stored_default_is_none(self, box):
        _git(box.main, "config", "remote.origin.gh-resolved", "")
        assert _run(box).returncode == 2

    def test_a_loose_expansion_in_a_heredoc_body_is_text(self, box):
        _agent(box)
        cmd = (
            "gh pr create --title t --body \"$(cat <<'EOF'\n"
            '`auth` is "set" and it\'s $ok `here\nEOF\n)"'
        )
        assert _run(box, cmd).returncode == 0

    def test_the_reused_worktree_refusal_says_how_to_clear_it(self, box):
        _agent(box, branch="feat/old", cwd=box.wt)
        assert "another feature branch" in _run(box).stderr

    @pytest.mark.parametrize(
        "text", ["gate-work/a.py", "./gate-work/a.py", "**feat/x**", "feat/x@{u}"]
    )
    def test_these_namings_tie_it(self, box, text):
        _agent(box, branch="main", prompt=f"Review {text} now")
        assert _run(box).returncode == 0

    def test_origin_head_names_the_default_branch(self, box):
        _git(box.main, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        _git(box.main, "switch", "-q", "-c", "feat/m")
        _agent(box, branch="main", cwd=box.wt)
        assert _run(box).returncode == 0
        _agent(box, branch="feat/m", cwd=box.wt)
        assert _run(box).returncode == 2


class TestLastSurvivors:
    def test_a_bare_hyphenated_worktree_name_ties_it(self, box):
        _agent(box, branch="main", prompt="Review gate-work now.")
        assert _run(box).returncode == 0

    def test_an_expansion_shaped_word_is_text_only_where_a_heredoc_can_split_a_body(self):
        spec = importlib.util.spec_from_file_location("gate_under_test", HOOK)
        gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(gate)
        argv = ["gh", "pr", "create", "--title", "t", "`auth_env`"]
        assert gate._create_flags(argv).unknown is True
        assert gate._create_flags(argv, has_heredoc=True).unknown is False


class TestABlockDiscardsTheWholeCommand:
    def test_a_refused_compound_says_so(self, box):
        done = _run(box, f"echo hi > note.txt && {CREATE}")
        assert done.returncode == 2 and "ENTIRE command was discarded" in done.stderr

    def test_a_refused_create_alone_does_not(self, box):
        done = _run(box)
        assert done.returncode == 2 and "ENTIRE command was discarded" not in done.stderr
