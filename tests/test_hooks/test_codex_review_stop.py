"""Codex's boundary must deny instead of emitting unsupported native asks."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "scripts" / "hooks"


@pytest.fixture
def adapter():
    spec = importlib.util.spec_from_file_location("codex_review_stop", HOOKS / "codex_review_stop.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def payload(command):
    return {"tool_name": "Bash", "cwd": str(ROOT), "tool_input": {"command": command}}


@pytest.mark.parametrize("decision", ["ask", "deny", "unexpected"])
def test_request_decisions_never_ask_or_allow_unknown(adapter, monkeypatch, decision):
    monkeypatch.setattr(adapter.requests, "_check_codex_round_escalation", lambda *a: (decision, "limit"))
    assert adapter.decide(payload('gh pr comment 1 --body "@codex review"'))


def test_request_allow_is_preserved(adapter, monkeypatch):
    monkeypatch.setattr(adapter.requests, "_check_codex_round_escalation", lambda *a: ("allow", ""))
    assert adapter.decide(payload('gh pr comment 1 --body "@codex review"')) is None


@pytest.mark.parametrize("command", ["printf hello", "printf 'git commit'", "git status"])
def test_unrelated_commands_do_not_lookup_budget(adapter, monkeypatch, command):
    monkeypatch.setattr(adapter.requests, "_check_codex_round_escalation", lambda *a: ("allow", ""))
    monkeypatch.setattr(adapter.commits, "_branch_review_budget", lambda *a, **kw: pytest.fail("lookup"))
    assert adapter.decide(payload(command)) is None


@pytest.mark.parametrize("result", [None, {"status": "ok", "commit_approval_required": False}])
def test_commit_with_capacity(adapter, monkeypatch, result):
    monkeypatch.setattr(adapter.commits, "_branch_review_budget", lambda *a, **kw: result)
    assert adapter.decide(payload('git commit -m "test"')) is None


@pytest.mark.parametrize("result", [{"status": "unknown"}, {"status": "ok", "commit_approval_required": True}])
def test_commit_requires_handoff(adapter, monkeypatch, result):
    monkeypatch.setattr(adapter.commits, "_branch_review_budget", lambda *a, **kw: result)
    assert adapter.decide(payload('git commit -m "test"'))


@pytest.mark.parametrize("command", [
    'git switch other && git commit -m x',
    'git commit -m x; git commit -m y',
    'git commit -m x && gh pr comment 1 --body "@codex review"',
    'git --git-dir=/elsewhere commit -m x',
    'GIT_DIR=/elsewhere git commit -m x',
    'cd "$TARGET" && git commit -m x',
    'git "$VERB" -m x',
])
def test_unreadable_or_multiple_commit_targets_block_before_lookup(adapter, monkeypatch, command):
    monkeypatch.setattr(adapter.commits, "_branch_review_budget", lambda *a, **kw: pytest.fail("lookup"))
    assert adapter.decide(payload(command))


def test_literal_target_cwd_reaches_lookup(adapter, monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(adapter.state, "get_current_branch", lambda **kw: "feature")
    monkeypatch.setattr(adapter.commits, "_branch_review_budget", lambda cwd, *a, **kw: seen.append(cwd))
    assert adapter.decide(payload(f'git -C {tmp_path} commit -m x')) is None
    assert seen == [str(tmp_path)]


def test_inherited_repository_override_is_not_sampled_as_payload_cwd(adapter, monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/elsewhere")
    monkeypatch.setattr(adapter.commits, "_branch_review_budget", lambda *a, **kw: pytest.fail("lookup"))
    assert adapter.decide(payload("git commit -m x"))


@pytest.mark.parametrize("data", [{}, {"tool_input": {"command": 1}}, {"tool_name": "other"}])
def test_unreadable_hook_payload_is_not_allow(adapter, data):
    assert adapter.decide(data)


def test_launcher_denies_evaluator_failure(tmp_path):
    launcher = tmp_path / "codex-review-stop"
    launcher.write_bytes((HOOKS / "codex-review-stop").read_bytes())
    (tmp_path / "codex_review_stop.py").write_text("raise RuntimeError('probe')\n")
    run = subprocess.run(["bash", str(launcher)], input=json.dumps(payload("printf hello")), text=True, capture_output=True)
    assert run.returncode == 2
    assert not run.stdout
    assert "handoff" in run.stderr.lower()


def test_launcher_missing_evaluator_is_deny(tmp_path):
    launcher = tmp_path / "codex-review-stop"
    launcher.write_bytes((HOOKS / "codex-review-stop").read_bytes())
    run = subprocess.run(["bash", str(launcher)], input="{}", text=True, capture_output=True)
    assert run.returncode == 2


@pytest.mark.parametrize("output", ["", "unexpected", '{"permissionDecision":"ask"}'])
def test_launcher_requires_explicit_allow_result(tmp_path, output):
    launcher = tmp_path / "codex-review-stop"
    launcher.write_bytes((HOOKS / "codex-review-stop").read_bytes())
    (tmp_path / "codex_review_stop.py").write_text(f"print({output!r})\n")
    run = subprocess.run(["bash", str(launcher)], input="{}", text=True, capture_output=True)
    assert run.returncode == 2
    assert not run.stdout


def test_project_config_wires_fallback_launcher():
    import tomllib

    config = tomllib.loads((ROOT / ".codex" / "config.toml").read_text())
    entry = config["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == "^Bash$"
    assert "codex-review-stop" in entry["hooks"][0]["command"]
    assert "exit 2" in entry["hooks"][0]["command"]


def evidence(monkeypatch, adapter, count, gate=False):
    budget = adapter.requests._review_budget
    heads = [f"{n:x}" * 40 for n in range(1, 7)]
    rows = [{"login": budget.CODEX_REVIEW_BOT, "commit_id": head, "state": "COMMENTED"} for head in heads[:count]]
    values = {
        "_TEST_REVIEW_BUDGET_HEAD": heads[-1],
        "_TEST_REVIEW_BUDGET_COMMITS": "\n".join(json.dumps({"sha": h}) for h in heads),
        "_TEST_GH_CODEX_REVIEWS": "\n".join(json.dumps(r) for r in rows),
        "_TEST_GH_CODEX_COMMENTS": "",
        "_TEST_REVIEW_BUDGET_FILES": json.dumps({"filename": "scripts/hooks/x.py" if gate else "src/x.py"}),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return budget, heads[-1]


@pytest.mark.parametrize("count,blocked", [(0, False), (3, False), (4, True), (5, True)])
def test_real_request_policy_ordinary_boundary(adapter, monkeypatch, count, blocked):
    evidence(monkeypatch, adapter, count)
    reason = adapter.decide(payload('gh pr comment 1 --repo owner/repo --body "@codex review"'))
    assert bool(reason) == blocked


def test_real_gate_confirmation_policy_is_preserved(adapter, monkeypatch):
    budget, head = evidence(monkeypatch, adapter, 2, gate=True)
    prefix = 'gh pr comment 1 --repo owner/repo --body "@codex review'
    assert adapter.decide(payload(prefix + '"'))
    assert adapter.decide(payload(prefix + '\n' + budget.confirmation_marker(head) + '"')) is None


@pytest.mark.parametrize("count,gate,blocked", [(3, False, False), (4, False, True), (2, True, False), (3, True, True)])
def test_real_commit_policy_boundaries(adapter, monkeypatch, count, gate, blocked):
    evidence(monkeypatch, adapter, count, gate)
    repo = "owner/repo"
    monkeypatch.setattr(adapter.commits, "_canonical_public_repo", lambda: repo)
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_PR", "1")
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_REPO", repo)
    assert bool(adapter.decide(payload('git commit -m "test"'))) == blocked


def test_broken_evidence_is_deny(adapter, monkeypatch):
    evidence(monkeypatch, adapter, 0)
    monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "not JSON")
    assert adapter.decide(payload('gh pr comment 1 --repo owner/repo --body "@codex review"'))


def test_ordinary_exception_is_deny_not_exit_one(adapter, monkeypatch, capsys):
    monkeypatch.setattr(adapter, "decide", lambda _: 1 / 0)
    monkeypatch.setattr(adapter.sys, "stdin", __import__("io").StringIO("{}"))
    assert adapter.main() == 2
    assert not capsys.readouterr().out


@pytest.mark.parametrize("poisoned", [
    "git_push_guard", "review_enforcement_commit", "review_state",
    "git_repo_selection", "review_deadline", "shell_parse",
])
def test_launcher_denies_each_module_scope_import_failure(tmp_path, poisoned):
    """The configured shell entry point converts every sibling import crash to deny."""
    hooks = tmp_path / "scripts" / "hooks"
    hooks.mkdir(parents=True)
    for name in ("codex-review-stop", "codex_review_stop.py"):
        (hooks / name).write_bytes((HOOKS / name).read_bytes())
    stubs = {
        "git_push_guard": "",
        "review_enforcement_commit": "",
        "review_state": "",
        "git_repo_selection": "REPO_VARS = ()\nraw_sets_repo_env = seg_redirects_repo = None\n",
        "review_deadline": "Deadline = None\n",
        "shell_parse": "analyze_checked = gh_pr_subcommand = git_subcommand = git_subcommand_index = mentions = None\n"
            "_argv = _GH_ALL_VALUE_FLAGS = _GH_FLAG_TABLE = _gh_option = gh_command = None\n",
    }
    for name, text in stubs.items():
        if name == poisoned:
            text = "raise RuntimeError('poisoned sibling: " + name + "')\n"
        (hooks / (name + ".py")).write_text(text)
    run = subprocess.run(
        ["bash", str(hooks / "codex-review-stop")], input="{}",
        text=True, capture_output=True, timeout=10,
    )
    assert run.returncode == 2
    assert not run.stdout
    assert "poisoned sibling: " + poisoned in run.stderr
    assert "handoff" in run.stderr.lower()


@pytest.mark.parametrize("command", [
    "git commit -m x", 'gh pr comment 1 --repo owner/repo --body "@codex review"',
])
@pytest.mark.parametrize("variable", ["GH_REPO", "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"])
def test_inherited_overrides_block_both_budget_paths(adapter, monkeypatch, command, variable):
    monkeypatch.setenv(variable, "owner/other")
    monkeypatch.setattr(adapter.commits, "_branch_review_budget", lambda *a, **kw: pytest.fail("lookup"))
    monkeypatch.setattr(adapter.requests, "_check_codex_round_escalation", lambda *a: pytest.fail("lookup"))
    assert adapter.decide(payload(command))


@pytest.mark.parametrize("command", [
    "env -C /elsewhere git commit -m x",
    "pushd /elsewhere && git commit -m x",
    "false && cd /elsewhere; git commit -m x",
    "builtin cd /elsewhere && git commit -m x",
    "command cd /elsewhere && git commit -m x",
    "cd /elsewhere && git commit -m x",
    "git -C / -C /elsewhere commit -m x",
    "git -C/ -C/elsewhere commit -m x",
    "git -c core.worktree=/elsewhere commit -m x",
    "env GH_REPO=owner/other git commit -m x",
    'GH_REPO=owner/other gh pr comment 1 --body "@codex review"',
    'env GH_REPO=owner/other gh pr comment 1 --body "@codex review"',
    'export GH_REPO=owner/other; gh pr comment 1 --body "@codex review"',
    'GH_REPO=owner/other; gh pr comment 1 --body "@codex review"',
    'command gh pr comment 1 --body "@codex review"',
])
def test_unmodelled_actions_block_before_any_budget_lookup(adapter, monkeypatch, command):
    monkeypatch.setattr(adapter.commits, "_branch_review_budget", lambda *a, **kw: pytest.fail("lookup"))
    monkeypatch.setattr(adapter.requests, "_check_codex_round_escalation", lambda *a: pytest.fail("lookup"))
    assert adapter.decide(payload(command))


@pytest.mark.parametrize("command", [
    "git commit -h", "git commit --help", "git commit --dry-run",
    "git commit --dry-run -m x", "git commit -m x --dry-run",
    "git commit --no-dry-run --dry-run",
    "gh pr comment --help", "gh pr comment 1 --help",
    "gh pr comment -b x --help",
])
def test_definitive_non_mutating_modes_skip_lookups(adapter, monkeypatch, command):
    monkeypatch.setenv("GH_REPO", "owner/other")
    monkeypatch.setattr(adapter.commits, "_branch_review_budget", lambda *a, **kw: pytest.fail("lookup"))
    monkeypatch.setattr(adapter.requests, "_check_codex_round_escalation", lambda *a: pytest.fail("lookup"))
    assert adapter.decide(payload(command)) is None


@pytest.mark.parametrize("command", [
    "git commit --dry-run --no-dry-run",
    'git commit -m "--help"', 'git commit -m "--dry-run"',
    'git commit --message "--help"', 'git commit --message=--help',
    'git commit -m--help', 'git commit -- --help',
    'git commit --future-unknown-value --help',
    'gh pr comment 1 --body "--help"',
    'gh pr comment 1 --body-file=--help',
    'gh pr comment 1 -- --help',
])
def test_mode_like_values_cannot_skip_the_guard(adapter, monkeypatch, command):
    monkeypatch.setattr(adapter.commits, "_branch_review_budget", lambda *a, **kw: {"status": "unknown"})
    monkeypatch.setattr(adapter.requests, "_check_codex_round_escalation", lambda *a: ("ask", "limit"))
    segs, blind = adapter.analyze_checked(command)
    assert blind is None
    assert not adapter._non_mutating(segs[0])
    if segs[0].exe == "git":
        assert adapter.decide(payload(command))


@pytest.mark.parametrize("signing", ["--gpg-sign", "--gpg-sign=key", "-S", "-Skey"])
@pytest.mark.parametrize("modes,blocked", [
    ("--dry-run {signing} --no-dry-run", True),
    ("--no-dry-run {signing} --dry-run", False),
    ("{signing} --dry-run --no-dry-run", True),
])
def test_optional_signing_key_cannot_hide_mode_reversal(adapter, monkeypatch, signing, modes, blocked):
    monkeypatch.setattr(adapter.commits, "_branch_review_budget", lambda *a, **kw: {"status": "unknown"})
    command = "git commit " + modes.format(signing=signing) + " -m probe"
    assert bool(adapter.decide(payload(command))) == blocked
