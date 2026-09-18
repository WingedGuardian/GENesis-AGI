"""Distinct-reviewed-head authorization for ``@codex review`` requests."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_HOOKS = _ROOT / "scripts" / "hooks"
_spec = importlib.util.spec_from_file_location("round_request_guard", _HOOKS / "git_push_guard.py")
_mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(_mod)

CODEX = "chatgpt-codex-connector[bot]"
HEADS = tuple(f"{i:x}" * 40 for i in range(1, 7))
TRIGGER = 'gh pr comment 1372 --repo owner/repo --body "@codex review"'


def _jsonl(rows) -> str:
    return "\n".join(json.dumps(row) for row in rows)


def _evidence(monkeypatch, reviewed=(), *, head=HEADS[-1], files=("src/x.py",), comments=()):
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_HEAD", head)
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_COMMITS", _jsonl({"sha": sha} for sha in HEADS))
    monkeypatch.setenv(
        "_TEST_GH_CODEX_REVIEWS",
        _jsonl({"login": CODEX, "commit_id": sha, "state": state} for sha, state in reviewed),
    )
    monkeypatch.setenv(
        "_TEST_GH_CODEX_COMMENTS",
        _jsonl({"login": login, "type": kind, "body": body} for login, kind, body in comments),
    )
    monkeypatch.setenv(
        "_TEST_REVIEW_BUDGET_FILES",
        _jsonl({"filename": path, "previous_filename": None} for path in files),
    )


def _decision(cmd: str = TRIGGER):
    return _mod._check_codex_round_escalation(_mod.analyze(cmd), cmd, {"cwd": str(_ROOT)})


def _payload(monkeypatch, command: str):
    monkeypatch.setattr(_mod, "read_payload", lambda: {"tool_input": {"command": command}})


def _argv(command: str) -> list[str]:
    segments = _mod.analyze(command)
    assert len(segments) == 1
    return segments[0].argv


def test_ordinary_standing_authorization_ends_after_four_heads(monkeypatch):
    _evidence(monkeypatch, [(h, "COMMENTED") for h in HEADS[:3]])
    assert _decision() == ("allow", "")

    _evidence(monkeypatch, [(h, "COMMENTED") for h in HEADS[:4]])
    decision, reason = _decision()
    assert decision == "ask"
    assert "standing authorization ended after four" in reason


def test_round_six_and_later_are_strongly_discouraged(monkeypatch):
    _evidence(monkeypatch, [(h, "COMMENTED") for h in HEADS[:5]])
    decision, reason = _decision()
    assert decision == "ask"
    assert "strongly discouraged" in reason
    assert "narrow or redesign" in reason


def test_dismissed_reviews_count_but_duplicate_heads_count_once(monkeypatch):
    reviewed = [
        (HEADS[0], "DISMISSED"),
        (HEADS[0], "COMMENTED"),
        (HEADS[1], "DISMISSED"),
        (HEADS[2], "COMMENTED"),
    ]
    _evidence(monkeypatch, reviewed)
    assert _decision() == ("allow", "")
    _evidence(monkeypatch, reviewed + [(HEADS[3], "DISMISSED")])
    assert _decision()[0] == "ask"


@pytest.mark.parametrize("sigil", ["escalation-ack", "final-round-accept"])
def test_legacy_sigils_do_not_authorize_a_later_round(monkeypatch, sigil):
    _evidence(monkeypatch, [(h, "COMMENTED") for h in HEADS[:4]])
    assert _decision(f"{TRIGGER}  # {sigil}")[0] == "ask"


def test_unknown_evidence_asks_foreground_and_denies_dispatched(monkeypatch, capsys):
    _evidence(monkeypatch)
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_COMMITS", "not-json")
    assert _decision()[0] == "ask"

    _payload(monkeypatch, TRIGGER)
    monkeypatch.setenv("GENESIS_CC_SESSION", "1")
    assert _mod.main() == 2
    assert "could not be read reliably" in capsys.readouterr().err


def test_gate_surface_allows_one_exact_head_confirmation(monkeypatch):
    reviewed = [(HEADS[0], "COMMENTED"), (HEADS[1], "COMMENTED")]
    _evidence(
        monkeypatch,
        reviewed,
        head=HEADS[2],
        files=("scripts/hooks/git_push_guard.py",),
    )
    marker = _mod._review_budget.confirmation_marker(HEADS[2])
    without_marker = _decision()
    assert without_marker[0] == "ask"
    assert _decision(f"gh pr comment 1372 --repo owner/repo --body '@codex review\n{marker}'") == (
        "allow",
        "",
    )


def test_gate_confirmation_marker_prevents_a_repeat_exemption(monkeypatch):
    marker = _mod._review_budget.confirmation_marker(HEADS[2])
    _evidence(
        monkeypatch,
        [(HEADS[0], "COMMENTED"), (HEADS[1], "COMMENTED")],
        head=HEADS[2],
        files=("scripts/review_budget.py",),
        comments=(("owner", "User", marker),),
    )
    assert (
        _decision(f"gh pr comment 1372 --repo owner/repo --body '@codex review\n{marker}'")[0]
        == "ask"
    )


def test_multiple_requests_block_when_one_needs_approval(monkeypatch):
    _evidence(monkeypatch, [(h, "COMMENTED") for h in HEADS[:4]])
    decision, reason = _decision(f"{TRIGGER} && {TRIGGER}")
    assert decision == "deny"
    assert "multiple review requests" in reason


def test_multiple_requests_block_while_standing_authorization_remains(monkeypatch):
    _evidence(monkeypatch, [(h, "COMMENTED") for h in HEADS[:3]])
    decision, reason = _decision(f"{TRIGGER} && {TRIGGER}")
    assert decision == "deny"
    assert "multiple review requests" in reason


@pytest.mark.parametrize(
    "body",
    ["$BODY", "${BODY}", "$(cat request.txt)", "`cat request.txt`"],
)
def test_shell_expanded_inline_body_is_opaque_at_approval_boundary(monkeypatch, body):
    _evidence(monkeypatch, [(h, "COMMENTED") for h in HEADS[:4]])
    decision, _ = _decision(f'gh pr comment 1372 --repo owner/repo --body "{body}"')
    assert decision == "ask"


def test_shell_expanded_body_cannot_claim_confirmation_exemption(monkeypatch):
    _evidence(
        monkeypatch,
        [(HEADS[0], "COMMENTED"), (HEADS[1], "COMMENTED")],
        head=HEADS[2],
        files=("scripts/review_budget.py",),
    )
    marker = _mod._review_budget.confirmation_marker(HEADS[2])
    decision, _ = _decision(
        f'gh pr comment 1372 --repo owner/repo --body "$BODY {marker}"'
    )
    assert decision == "ask"


@pytest.mark.parametrize(
    "other",
    [
        "git push origin HEAD",
        'git commit -m "fix"',
        "gh pr create --head feat/x --title x --body x",
        "gh pr close 1372 --repo owner/repo",
    ],
)
def test_review_approval_cannot_cover_another_gated_action(monkeypatch, capsys, other):
    _evidence(monkeypatch, [(h, "COMMENTED") for h in HEADS[:4]])
    command = f"{TRIGGER} && {other}"
    _payload(monkeypatch, command)
    assert _mod.main() == 2
    assert "review request separately" in capsys.readouterr().err


@pytest.mark.parametrize(
    "target",
    ["$n", '"$PR"', "${!name}", "$(cat pr.txt)", "`cat pr.txt`", "pr-*"],
)
def test_unresolvable_identity_still_denies(monkeypatch, target):
    _evidence(monkeypatch)
    decision, reason = _decision(f'gh pr comment {target} --body "@codex review"')
    assert decision == "deny"
    assert "does not name a pull request" in reason


def test_literal_number_url_and_repo_flag_resolve(monkeypatch):
    _evidence(monkeypatch, [(h, "COMMENTED") for h in HEADS[:4]])
    forms = (
        TRIGGER,
        'gh pr comment https://github.com/owner/repo/pull/1372 --body "@codex review"',
        'gh pr -R owner/repo comment 1372 --body "@codex review"',
    )
    for command in forms:
        decision, reason = _decision(command)
        assert decision == "ask", (command, reason)


@pytest.mark.parametrize(
    ("command", "number", "repo"),
    [
        ('gh pr comment 1372 --body "@codex review"', "1372", None),
        ('gh pr comment "#1372" --body "@codex review"', "1372", None),
        (
            'gh pr comment https://github.com/owner/repo/pull/1372/files#x --body "@codex review"',
            "1372",
            "owner/repo",
        ),
        ('gh pr -R owner/repo comment 1372 --body "@codex review"', "1372", "owner/repo"),
        ('gh pr -Rgithub.com/owner/repo comment 1372 --body "@codex review"', "1372", "owner/repo"),
    ],
)
def test_comment_identity_parser_keeps_supported_literal_forms(command, number, repo):
    assert _mod._comment_target(_argv(command)) == (number, repo)


@pytest.mark.parametrize(
    "command",
    [
        'gh pr comment 1372 --body "see https://github.com/wrong/repo/pull/8 @codex review"',
        'gh pr comment 1372 -b"-R wrong/repo @codex review"',
        "gh pr comment 1372 --body-file ./review.txt -R owner/repo",
    ],
)
def test_comment_values_are_not_reparsed_as_target_or_repo(command):
    number, repo = _mod._comment_target(_argv(command))
    assert number == "1372"
    assert repo in {None, "owner/repo"}


def test_unknown_value_flag_keeps_target_identity_unreadable(monkeypatch):
    _evidence(monkeypatch)
    decision, reason = _decision('gh pr comment --future-flag value 1372 --body "@codex review"')
    assert decision == "deny"
    assert "does not name a pull request" in reason


def test_repo_resolution_uses_comment_effective_cwd(monkeypatch, tmp_path):
    _evidence(monkeypatch, [(h, "COMMENTED") for h in HEADS[:4]])
    target = tmp_path / "other-repo"
    target.mkdir()
    seen = []

    def derive(cwd):
        seen.append(cwd)
        return "owner/repo"

    monkeypatch.setattr(_mod, "_derive_repo_from_cwd", derive)
    command = f'cd {target} && gh pr comment 1372 --body "@codex review"'
    decision, _ = _mod._check_codex_round_escalation(
        _mod.analyze(command), command, {"cwd": str(_ROOT)}
    )
    assert decision == "ask"
    assert seen == [str(target)]


def test_non_request_commands_are_untouched_but_numberless_request_is_unknown(monkeypatch):
    _evidence(monkeypatch, [(h, "COMMENTED") for h in HEADS[:4]])
    for command in (
        "git status",
        "gh pr view 1372",
        'gh pr comment 1372 --body "great work"',
    ):
        assert _decision(command) == ("allow", "")
    decision, reason = _decision('gh pr comment --body "@codex review"')
    assert decision == "ask"
    assert "could not be resolved to a literal PR number" in reason


def test_opaque_comment_body_cannot_bypass_an_approval_boundary(monkeypatch):
    body_file = "gh pr comment 1372 --repo owner/repo --body-file review.txt"
    _evidence(monkeypatch, [(h, "COMMENTED") for h in HEADS[:3]])
    assert _decision(body_file) == ("allow", "")

    _evidence(monkeypatch, [(h, "COMMENTED") for h in HEADS[:4]])
    assert _decision(body_file)[0] == "ask"


def test_effective_comment_body_is_parsed_once(monkeypatch):
    """Request detection and exact-head confirmation share one argv parse."""
    _evidence(
        monkeypatch,
        [(HEADS[0], "COMMENTED"), (HEADS[1], "COMMENTED")],
        head=HEADS[2],
        files=("scripts/review_budget.py",),
    )
    marker = _mod._review_budget.confirmation_marker(HEADS[2])
    original = _mod._comment_body
    calls = 0

    def counted(argv):
        nonlocal calls
        calls += 1
        return original(argv)

    monkeypatch.setattr(_mod, "_comment_body", counted)
    assert _decision(f'gh pr comment 1372 --repo owner/repo --body "@codex review {marker}"') == (
        "allow",
        "",
    )
    assert calls == 1


def test_opaque_gate_confirmation_cannot_claim_exact_head_marker(monkeypatch):
    _evidence(
        monkeypatch,
        [(HEADS[0], "COMMENTED"), (HEADS[1], "COMMENTED")],
        head=HEADS[2],
        files=("scripts/review_budget.py",),
    )
    assert (
        _decision("gh pr comment 1372 --repo owner/repo --body-file confirmation.txt")[0] == "ask"
    )


def test_literal_branch_request_is_unknown_instead_of_bypassing_budget(monkeypatch):
    _evidence(monkeypatch, [(h, "COMMENTED") for h in HEADS[:4]])
    decision, reason = _decision('gh pr comment feat/review-policy --body "@codex review"')
    assert decision == "ask"
    assert "literal PR number" in reason


def test_shared_deadline_is_armed_and_not_reset(monkeypatch):
    import time

    _evidence(monkeypatch)
    monkeypatch.setattr(_mod, "_merge_deadline", None)
    _decision()
    assert _mod._merge_deadline is not None
    preset = time.monotonic() + 5
    monkeypatch.setattr(_mod, "_merge_deadline", preset)
    _decision()
    assert _mod._merge_deadline == preset


def test_main_emits_native_ask_and_dispatched_mode_denies(monkeypatch, capsys):
    _evidence(monkeypatch, [(h, "COMMENTED") for h in HEADS[:4]])
    _payload(monkeypatch, TRIGGER)
    assert _mod.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "ask"

    _payload(monkeypatch, TRIGGER)
    monkeypatch.setenv("GENESIS_CC_SESSION", "1")
    assert _mod.main() == 2


def test_hard_block_still_precedes_round_approval(monkeypatch, capsys):
    _evidence(monkeypatch, [(h, "COMMENTED") for h in HEADS[:4]])
    _payload(monkeypatch, f"{TRIGGER} && git commit --no-verify -m nope")
    assert _mod.main() == 2
    assert "--no-verify" in capsys.readouterr().err

    _payload(monkeypatch, f"{TRIGGER} && git commit --no-verify -m nope")
    monkeypatch.setenv("GENESIS_CC_SESSION", "1")
    assert _mod.main() == 2
    assert "--no-verify" in capsys.readouterr().err


def test_freshness_helpers_keep_dismissed_semantics(monkeypatch):
    monkeypatch.setenv(
        "_TEST_GH_CODEX_REVIEWS",
        _jsonl(
            (
                {"login": CODEX, "commit_id": HEADS[0], "state": "COMMENTED"},
                {"login": CODEX, "commit_id": HEADS[1], "state": "DISMISSED"},
            )
        ),
    )
    assert _mod._codex_review_commit_ids("1372") == [HEADS[0]]
    assert _mod._latest_codex_reviewed_sha("1372") == HEADS[0]
