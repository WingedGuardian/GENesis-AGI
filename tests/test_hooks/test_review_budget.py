from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))

import review_budget as rb  # noqa: E402
import review_state  # noqa: E402

H1 = "1" * 40
H2 = "2" * 40
H3 = "3" * 40
H4 = "4" * 40
H5 = "5" * 40


def test_legacy_constant_import_surface_matches_shared_policy():
    assert review_state.STANDING_REVIEWED_HEAD_LIMIT == rb.STANDING_REVIEWED_HEAD_LIMIT
    assert review_state.GATE_DISCOVERY_ROUND_LIMIT == rb.GATE_DISCOVERY_ROUND_LIMIT
    assert (
        review_state.STRONGLY_DISCOURAGED_REVIEWED_HEADS == rb.STRONGLY_DISCOURAGED_REVIEWED_HEADS
    )


def _review(head: str, *, state: str = "APPROVED") -> dict:
    return {"login": rb.CODEX_REVIEW_BOT, "commit_id": head, "state": state}


def _comment(body: str, *, login: str = rb.CODEX_REVIEW_BOT, kind: str = "Bot") -> dict:
    return {"login": login, "type": kind, "body": body}


def _eval(*, head=H5, reviews=(), comments=(), files=("src/x.py",), commits=None, templates=()):
    return rb.evaluate_evidence(
        current_head=head,
        commit_heads=commits or (H1, H2, H3, H4, H5),
        codex_reviews=reviews,
        issue_comments=comments,
        changed_files=files,
        external_identity_templates=templates,
    )


def test_distinct_heads_include_dismissed_and_deduplicate_sources():
    comments = (
        _comment(f"Codex Review: Didn't find any major issues.\n**Reviewed commit:** `{H1[:10]}`"),
        _comment(f"external report head={H2}"),
    )
    got = _eval(
        reviews=(_review(H1), _review(H2, state="DISMISSED")),
        comments=comments,
        templates=("external report head={head}",),
    )
    assert got["status"] == "ok"
    assert got["reviewed_heads"] == [H1, H2]
    assert got["count"] == 2


def test_ordinary_four_requires_fresh_approval_and_five_discourages():
    four = _eval(reviews=tuple(_review(h) for h in (H1, H2, H3, H4)))
    assert four["approval_required"] is True
    assert four["commit_approval_required"] is True
    assert four["strongly_discouraged"] is False
    five = _eval(reviews=tuple(_review(h) for h in (H1, H2, H3, H4, H5)))
    assert five["strongly_discouraged"] is True


def test_gate_second_round_fix_and_one_marked_confirmation_are_exempt():
    gate_file = "scripts/hooks/git_push_guard.py"
    second_fix = _eval(
        head=H3,
        reviews=(_review(H1), _review(H2)),
        files=(gate_file,),
    )
    assert second_fix["gate_surface"] is True
    assert second_fix["confirmation_exempt"] is True
    assert second_fix["approval_required"] is False
    assert second_fix["commit_approval_required"] is False

    already_requested = _eval(
        head=H3,
        reviews=(_review(H1), _review(H2)),
        comments=(_comment(rb.confirmation_marker(H3), login="owner", kind="User"),),
        files=(gate_file,),
    )
    assert already_requested["confirmation_requested"] is True
    assert already_requested["approval_required"] is True


def test_gate_confirmation_result_makes_next_fix_require_approval():
    got = _eval(
        reviews=(_review(H1), _review(H2), _review(H3)),
        files=("scripts/review_budget.py",),
    )
    assert got["count"] == 3
    assert got["approval_required"] is True
    assert got["commit_approval_required"] is True
    assert got["strongly_discouraged"] is True


def test_rename_source_and_destination_both_classify_gate_surface():
    renamed_from_gate = _eval(
        files=({"filename": "docs/old.py", "previous_filename": "scripts/hooks/old.py"},)
    )
    renamed_to_gate = _eval(
        files=({"filename": "scripts/hooks/new.py", "previous_filename": "docs/old.py"},)
    )
    assert renamed_from_gate["gate_surface"] is True
    assert renamed_to_gate["gate_surface"] is True


def test_ambiguous_prefix_and_malformed_relevant_records_are_unknown():
    shared = "abcdef0"
    a = shared + "1" * 33
    b = shared + "2" * 33
    body = f"Codex Review: Didn't find any major issues.\nReviewed commit: `{shared}`"
    ambiguous = _eval(
        head=a,
        comments=(_comment(body),),
        commits=(a, b),
    )
    assert ambiguous["status"] == "unknown"
    assert ambiguous["approval_required"] is True
    malformed = _eval(reviews=({"login": rb.CODEX_REVIEW_BOT, "commit_id": "bad"},))
    assert malformed["status"] == "unknown"


def test_full_review_head_must_belong_to_pr_commit_list():
    outside = "f" * 40
    got = _eval(reviews=(_review(outside),))
    assert got["status"] == "unknown"
    assert "unresolved_review_head" in got["errors"]


def test_current_head_must_be_present_in_pr_commit_list():
    got = _eval(head=H5, commits=(H1, H2, H3, H4))
    assert got["status"] == "unknown"
    assert "current_head_missing_from_commits" in got["errors"]


def test_external_template_requires_exactly_one_full_head_placeholder():
    missing = _eval(templates=("external report",))
    repeated = _eval(templates=("{head}:{head}",))
    assert missing["status"] == repeated["status"] == "unknown"
    partial = _eval(
        comments=(_comment(f"external report {H1[:10]}", login="reviewer", kind="Bot"),),
        templates=("external report {head}",),
    )
    assert partial["count"] == 0


def test_confirmation_marker_requires_full_sha():
    try:
        rb.confirmation_marker(H1[:10])
    except ValueError:
        pass
    else:
        raise AssertionError("abbreviated confirmation marker was accepted")


def test_external_identity_environment_is_exact_and_optional(monkeypatch):
    monkeypatch.setenv(
        "GENESIS_EXTERNAL_REVIEW_IDENTITY_TEMPLATE",
        "<!-- external-review head={head} -->",
    )
    templates, error = rb.configured_external_identity_templates()
    assert error is None
    assert templates == ("<!-- external-review head={head} -->",)


def test_evaluate_pr_rejects_documented_endpoint_ceilings(monkeypatch):
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_HEAD", H5)
    monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "")
    monkeypatch.setenv("_TEST_GH_CODEX_COMMENTS", "")
    monkeypatch.setenv(
        "_TEST_REVIEW_BUDGET_COMMITS",
        "\n".join(json.dumps({"sha": f"{i:040x}"}) for i in range(rb.MAX_PR_COMMITS_RESPONSE)),
    )
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_FILES", json.dumps({"filename": "src/x.py"}))
    commits = rb.evaluate_pr("owner/repo", 1, external_identity_templates=())
    assert commits["status"] == "unknown"
    assert "commits_response_truncated" in commits["errors"]

    monkeypatch.setenv("_TEST_REVIEW_BUDGET_COMMITS", json.dumps({"sha": H5}))
    monkeypatch.setenv(
        "_TEST_REVIEW_BUDGET_FILES",
        "\n".join(json.dumps({"filename": f"src/{i}.py"}) for i in range(rb.MAX_PR_FILES_RESPONSE)),
    )
    files = rb.evaluate_pr("owner/repo", 1, external_identity_templates=())
    assert files["status"] == "unknown"
    assert "files_response_truncated" in files["errors"]


def test_evaluate_pr_rejects_head_change_during_fetch(monkeypatch):
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_HEAD", H4)
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_HEAD_AFTER", H5)
    monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "")
    monkeypatch.setenv("_TEST_GH_CODEX_COMMENTS", "")
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_FILES", json.dumps({"filename": "src/x.py"}))
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_COMMITS", json.dumps({"sha": H4}))
    got = rb.evaluate_pr("owner/repo", 1, external_identity_templates=())
    assert got["status"] == "unknown"
    assert "head_changed_during_evaluation" in got["errors"]
