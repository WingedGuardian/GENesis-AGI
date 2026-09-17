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


# ── aggregate lookup deadline ───────────────────────────────────────
# The per-call caps are 6-8s each and run SERIALLY, so a degraded-but-not-dead
# GitHub keeps every individual call inside its own cap while the total runs to
# ~36s. The PreToolUse commit hook is registered for 10s and an overrun SIGKILLs
# it -- which fails OPEN, letting the commit through with neither the budget
# check nor the review-current and depth checks that follow it. These bind the
# aggregate bound that prevents that.


class _FakeClock:
    """A monotonic clock the calls themselves advance. No sleeping."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _slow_runner(clock: _FakeClock, calls: list[tuple[str, float]]):
    """A runner that consumes its FULL allotted timeout, as a stalled call does.

    It SUCCEEDS. A failing runner is the wrong probe here: the first non-zero
    return short-circuits to `unknown` after one call, so the serial sum this
    deadline exists to bound is never exercised and the test passes with the
    clamp deleted. VERIFY-RED caught exactly that. Each endpoint therefore
    returns the minimal well-formed payload its parser accepts, so the lookup
    runs the whole way through and the cost is the SUM of the calls.
    """

    def run(argv, *, timeout):
        joined = " ".join(argv)
        calls.append((" ".join(argv[:3]), timeout))
        clock.now += timeout
        if "headRefOid" in joined:
            return 0, H5 + "\n", ""
        if joined.endswith("/files") or "/files" in joined:
            return 0, json.dumps({"path": "src/x.py"}) + "\n", ""
        if "/commits" in joined:
            return 0, "\n".join(json.dumps({"sha": h}) for h in (H1, H2, H3, H4, H5)) + "\n", ""
        if "/reviews" in joined or "/comments" in joined:
            return 0, "", ""
        return 0, "", ""

    return run


def test_the_lookup_stops_at_its_aggregate_deadline():
    """A stalled GitHub must not run the hook past its harness window.

    Each call consumes its whole timeout here, which is what a hung connection
    does. Without an aggregate bound the serial caps sum far past the 10s the
    hook is registered for; with one, the lookup stops inside its budget and
    reports `unknown` -- which asks a human and denies autonomous sessions,
    the same as any other unreadable endpoint. Slow and unreadable are the same
    outcome on purpose: in both the evidence did not arrive.
    """
    clock = _FakeClock()
    calls: list[tuple[str, float]] = []
    start = clock.now
    result = rb.evaluate_pr(
        "owner/repo",
        "1",
        runner=_slow_runner(clock, calls),
        external_identity_templates=(),
        budget_seconds=7.5,
        monotonic=clock,
    )
    elapsed = clock.now - start
    assert elapsed <= 7.5, f"the lookup ran {elapsed}s past a 7.5s budget: {calls}"
    assert result["status"] == "unknown", result
    # Denies an autonomous session and asks an interactive one.
    assert result["approval_required"] is True
    assert result["commit_approval_required"] is True
    # No call may be issued with a timeout that would itself cross the deadline.
    for argv, timeout in calls:
        assert timeout <= 7.5, (argv, timeout)


def test_no_deadline_leaves_every_call_at_its_own_cap():
    """The control. `budget_seconds=None` is the default, and every non-hook
    caller keeps the unbounded behaviour it had before -- a CLI or a merge gate
    is not living under a 10s harness timeout, and clamping them would trade a
    real answer for `unknown` at no benefit."""
    clock = _FakeClock()
    calls: list[tuple[str, float]] = []
    rb.evaluate_pr(
        "owner/repo",
        "1",
        runner=_slow_runner(clock, calls),
        external_identity_templates=(),
        monotonic=clock,
    )
    assert calls, "no call was issued at all"
    # The first call keeps its own full cap rather than a clamped remainder.
    assert calls[0][1] >= 6.0, calls


def test_the_budget_is_not_spent_on_a_call_too_small_to_finish():
    """Below the floor the lookup stops rather than issuing a doomed call.

    A sub-second timeout cannot complete a TLS handshake plus a GitHub round
    trip, so issuing it burns the rest of the budget to reach the same
    `unknown` -- with the timeout landing INSIDE the caller's harness window
    instead of before it.
    """
    clock = _FakeClock()
    calls: list[tuple[str, float]] = []
    rb.evaluate_pr(
        "owner/repo",
        "1",
        runner=_slow_runner(clock, calls),
        external_identity_templates=(),
        budget_seconds=6.2,  # one 6s call, then 0.2s left -- below the floor
        monotonic=clock,
    )
    assert len(calls) == 1, f"a doomed sub-floor call was issued: {calls}"


def test_the_commit_hook_budget_fits_inside_its_registered_timeout():
    """The number is not free-floating: it is derived from the registration.

    `.claude/settings.json` is the authority for how long the harness allows
    this hook, and the lookup budget must leave room for the local work that
    follows it. Bumping one without the other is exactly how the overrun
    appeared, so this reads BOTH and relates them.
    """
    import json
    import sys as _sys

    _sys.path.insert(0, str(_ROOT / "scripts"))
    import review_enforcement_commit as rec

    settings = json.loads((_ROOT / ".claude" / "settings.json").read_text())
    registered = [
        hook["timeout"]
        for matcher in settings["hooks"]["PreToolUse"]
        for hook in matcher.get("hooks", [])
        if "review_enforcement_commit.py" in hook.get("command", "")
    ]
    assert registered, "the commit hook is not wired in settings.json"
    assert float(registered[0]) == rec._COMMIT_HOOK_REGISTERED_TIMEOUT, (
        f"the constant says {rec._COMMIT_HOOK_REGISTERED_TIMEOUT}s but settings.json "
        f"registers {registered[0]}s -- one of them moved without the other"
    )
    assert rec._COMMIT_BUDGET_LOOKUP_SECONDS < rec._COMMIT_HOOK_REGISTERED_TIMEOUT, (
        "the network budget must leave headroom for the local work after it"
    )
    headroom = rec._COMMIT_HOOK_REGISTERED_TIMEOUT - rec._COMMIT_BUDGET_LOOKUP_SECONDS
    assert headroom >= 2.0, f"only {headroom}s left for diff classification and rendering"


def test_every_paginated_read_asks_for_a_full_page_in_the_path():
    """The page size must ride in the PATH, never as a `gh api -f` field.

    Two separate defects, and the second is why this test exists rather than a
    comment. First, gh defaults to 30 per page while the caps above tolerate 250
    commits and 3000 files, so the default makes a large PR dozens of SERIAL
    round trips under the commit hook's aggregate deadline.

    Second, the obvious fix is wrong in a way that hides itself. MEASURED:
    `gh api -f per_page=100` sends the value as a POST BODY field, which flips
    the HTTP method -- every one of these reads returns
    `{"message": "Not Found"}` and the whole lookup degrades to `unknown`,
    FASTER than the healthy path. A gate that silently stops reading evidence
    and returns sooner is the exact shape nobody notices, so the shape is
    asserted here instead of trusted.
    """
    import ast

    source = (_ROOT / "scripts" / "review_budget.py").read_text()
    tree = ast.parse(source)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_evaluate_pr_inner"
    )
    paginated = 0
    for node in ast.walk(fn):
        if not isinstance(node, ast.List):
            continue
        argv = [ast.unparse(e).strip("\"'") for e in node.elts]
        if "--paginate" not in argv:
            continue
        paginated += 1
        # -f / --raw-field / --field would flip the method to POST.
        assert not any(a in ("-f", "--raw-field", "-F", "--field") for a in argv), (
            f"a paginated read passes a body field, which makes it a POST: {argv}"
        )
        path = next((a for a in argv if "repos/" in a), "")
        assert "per_page=" in path, (
            f"a paginated read does not request a full page in its path: {path}"
        )
    assert paginated == 4, f"expected 4 paginated reads, found {paginated}"
    assert rb._PAGE_SIZE == 100, "100 is the GitHub API maximum page size"
