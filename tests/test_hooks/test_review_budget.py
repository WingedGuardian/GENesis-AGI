from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

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
        reviews=reviews,
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


def test_a_review_on_a_force_pushed_away_head_still_counts():
    """A review object names its FULL commit, so a head a force-push removed from
    the PR is still a round. It used to read `unknown`, which wedged every commit
    and review request on 5 of the 300 most recent merged PRs (MEASURED
    2026-10-01, every one a Codex review on a rewritten commit)."""
    outside = "f" * 40
    got = _eval(reviews=(_review(outside),))
    assert got["status"] == "ok", got
    assert got["reviewed_heads"] == [outside]
    assert got["count"] == 1


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


def test_quoted_external_identity_allows_an_inline_yaml_comment(monkeypatch, tmp_path):
    local = tmp_path / ".genesis" / "config" / "external_review.local.yaml"
    local.parent.mkdir(parents=True)
    local.write_text(
        'report_identity_template: "external report head={head}" # marker\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("GENESIS_EXTERNAL_REVIEW_IDENTITY_TEMPLATE", raising=False)
    monkeypatch.setattr(rb.Path, "home", classmethod(lambda cls: tmp_path))

    assert rb.configured_external_identity_templates() == (
        ("external report head={head}",),
        None,
    )

    local.write_text(
        "report_identity_template: 'reviewer''s report head={head}' # marker\n",
        encoding="utf-8",
    )
    assert rb.configured_external_identity_templates() == (
        ("reviewer's report head={head}",),
        None,
    )


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

    serve = _graphql_server(
        head=H5,
        files=[{"path": "src/x.py", "changeType": "MODIFIED"}],
        commits=[{"commit": {"oid": h}} for h in (H1, H2, H3, H4, H5)],
    )

    def run(argv, *, timeout):
        calls.append((" ".join(argv[:3]), timeout))
        clock.now += timeout
        return serve(argv, timeout=timeout)

    return run


def _graphql_server(*, head=H5, page_size=100, heads=None, **connections):
    """A fake `gh api graphql` that honours the query's connections and cursors.

    It reads WHICH connections the query selects from the query text and each
    one's `after_<name>` cursor from argv, and serves `page_size` nodes per page,
    so pagination, per-connection cursors and re-reads are exercised against the
    real argv the module builds rather than a canned reply. `heads`, when given,
    is consumed one per call (a head that moves between reads). Anything that is
    not a GraphQL call fails, so an unexpected REST call is loud.
    """
    head_seq = list(heads or [])
    seen: list[list[str]] = []

    def run(argv, *, timeout):
        if argv[:3] != ["gh", "api", "graphql"]:
            return 1, "", "unexpected non-graphql call"
        seen.append(list(argv))
        query = next(a for a in argv if a.startswith("query="))
        fields = dict(a.split("=", 1) for a in argv if "=" in a and not a.startswith("query="))
        pr: dict = {"headRefOid": head_seq.pop(0) if head_seq else head}
        for name in ("reviews", "comments", "files", "commits"):
            if f" {name}(first: 100" not in query:
                continue
            nodes = connections.get(name, [])
            offset = int(fields.get(f"after_{name}", "0"))
            page = nodes[offset : offset + page_size]
            more = offset + page_size < len(nodes)
            pr[name] = {
                "pageInfo": {"hasNextPage": more, "endCursor": str(offset + page_size)},
                "nodes": page,
            }
        return 0, json.dumps({"data": {"repository": {"pullRequest": pr}}}), ""

    run.seen = seen  # type: ignore[attr-defined]
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
    assert calls[0][1] >= 8.0, calls


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
        budget_seconds=8.2,  # one 8s call, then 0.2s left -- below the floor
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
    """A REST read's page size must ride in the PATH, never as a `gh api -f` field.

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
    # One REST read survives: the files fallback for a PR carrying a rename,
    # because GraphQL does not expose a renamed file's earlier path.
    assert paginated == 1, f"expected 1 paginated REST read, found {paginated}"
    assert rb._PAGE_SIZE == 100, "100 is the GitHub API maximum page size"
    graphql_sites = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.List)
        and [ast.unparse(e).strip("\"'") for e in node.elts][:3] == ["gh", "api", "graphql"]
    ]
    assert len(graphql_sites) == 1, "the PR evidence is read by exactly one GraphQL query site"


# -- the GraphQL read (A1: one query instead of five REST reads) ----------
# MEASURED 2026-09-29 over all 70 open PRs: the old and new `evaluate_pr` gave
# identical results on every one, p90 5.98s -> 1.80s (full figures in the
# `snapshot` docstring). These pin the read's own
# mechanics, which that replay cannot: pagination, the bot-login mapping, the
# rename fallback, and every way the read can fail closed.


def _no_seams(monkeypatch):
    for name in (
        "_TEST_REVIEW_BUDGET_HEAD",
        "_TEST_REVIEW_BUDGET_HEAD_AFTER",
        "_TEST_GH_CODEX_REVIEWS",
        "_TEST_GH_CODEX_COMMENTS",
        "_TEST_REVIEW_BUDGET_FILES",
        "_TEST_REVIEW_BUDGET_COMMITS",
    ):
        monkeypatch.delenv(name, raising=False)


def _gql_review(head, *, login="chatgpt-codex-connector", kind="Bot", state="COMMENTED"):
    return {
        "state": state,
        "submittedAt": "2026-01-01T00:00:00Z",
        "body": "",
        "author": {"login": login, "__typename": kind},
        "commit": {"oid": head},
        "comments": {"pageInfo": {"hasNextPage": False}, "nodes": []},
    }


_COMMITS = [{"commit": {"oid": h}} for h in (H1, H2, H3, H4, H5)]
_FILES = [{"path": "src/x.py", "changeType": "MODIFIED"}]


def test_graphql_read_counts_bot_reviews_under_their_rest_login(monkeypatch):
    """GraphQL names an App `chatgpt-codex-connector`; REST and every hook
    constant say `chatgpt-codex-connector[bot]`. Without the mapping every Codex
    review would read as a stranger's and the count would silently drop to 0."""
    _no_seams(monkeypatch)
    serve = _graphql_server(
        reviews=[_gql_review(H3), _gql_review(H4)], files=_FILES, commits=_COMMITS
    )
    got = rb.evaluate_pr("owner/repo", 7, runner=serve, external_identity_templates=())
    assert got["status"] == "ok", got
    assert got["reviewed_heads"] == [H3, H4], got
    # Two reads: the snapshot, then the head + mutable-evidence re-read.
    assert len(serve.seen) == 2, serve.seen


def test_graphql_read_never_promotes_a_human_to_a_bot(monkeypatch):
    """The suffix is keyed on `__typename`, not on the login's spelling."""
    _no_seams(monkeypatch)
    serve = _graphql_server(
        reviews=[_gql_review(H4, login="chatgpt-codex-connector", kind="User")],
        files=_FILES,
        commits=_COMMITS,
    )
    got = rb.evaluate_pr("owner/repo", 7, runner=serve, external_identity_templates=())
    assert got["status"] == "ok", got
    assert got["count"] == 0, got


def test_graphql_read_follows_every_page_of_every_connection(monkeypatch):
    """Page size 1 forces a cursor walk; the review on the LAST page must count."""
    _no_seams(monkeypatch)
    comment = {
        "body": "Codex Review: Didn't find any major issues.\nReviewed commit: `" + H2[:10] + "`",
        "author": {"login": "chatgpt-codex-connector", "__typename": "Bot"},
    }
    serve = _graphql_server(
        page_size=1,
        reviews=[_gql_review(H1), _gql_review(H3), _gql_review(H5)],
        comments=[{"body": "hello", "author": {"login": "someone", "__typename": "User"}}, comment],
        files=_FILES,
        commits=_COMMITS,
    )
    got = rb.evaluate_pr("owner/repo", 7, runner=serve, external_identity_templates=())
    assert got["status"] == "ok", got
    assert got["reviewed_heads"] == [H1, H2, H3, H5], got
    # A follow-up page queries only the connections that still have pages.
    later = [a for a in serve.seen[1] if a.startswith("query=")][0]
    assert " files(" not in later, later


def test_graphql_read_that_never_ends_is_unknown(monkeypatch):
    """Past the page bound the evidence is incomplete, never partially counted."""
    _no_seams(monkeypatch)
    monkeypatch.setattr(rb, "_GRAPHQL_MAX_PAGES", 2)
    serve = _graphql_server(
        page_size=1, reviews=[_gql_review(h) for h in (H1, H2, H3)], files=_FILES, commits=_COMMITS
    )
    got = rb.evaluate_pr("owner/repo", 7, runner=serve, external_identity_templates=())
    assert got["status"] == "unknown", got
    assert "reviews_response_truncated" in got["errors"], got


def test_graphql_head_moving_between_pages_is_unknown(monkeypatch):
    _no_seams(monkeypatch)
    serve = _graphql_server(
        page_size=1,
        # The head moves on page 2 and moves BACK for the final read, so the
        # final head check cannot see it: only the per-page check can.
        # Exactly two pages per read (two reviews, page size 1): H4 then H5 on
        # the first read, H4 twice on the re-read.
        heads=[H4, H5, H4, H4],
        reviews=[_gql_review(H4), _gql_review(H4)],
        files=_FILES,
        commits=[{"commit": {"oid": H4}}],
    )
    got = rb.evaluate_pr("owner/repo", 7, runner=serve, external_identity_templates=())
    assert got["status"] == "unknown", got
    assert "head_changed_during_evaluation" in got["errors"], got


def test_graphql_evidence_changing_between_reads_is_unknown(monkeypatch):
    """A review landing between the snapshot and the re-read is a race, not a count."""
    _no_seams(monkeypatch)
    base = _graphql_server(reviews=[_gql_review(H3)], files=_FILES, commits=_COMMITS)
    grown = _graphql_server(
        reviews=[_gql_review(H3), _gql_review(H4)], files=_FILES, commits=_COMMITS
    )
    calls = []

    def serve(argv, *, timeout):
        calls.append(argv)
        return (base if len(calls) == 1 else grown)(argv, timeout=timeout)

    got = rb.evaluate_pr("owner/repo", 7, runner=serve, external_identity_templates=())
    assert got["status"] == "unknown", got
    assert "evidence_changed_during_evaluation" in got["errors"], got


def test_graphql_failures_fail_closed(monkeypatch):
    """An error exit, a missing pull request and a malformed node all read as
    unknown -- never as an empty, zero-round PR."""
    _no_seams(monkeypatch)

    def erroring(argv, *, timeout):
        return 1, "", "gh: Could not resolve to a PullRequest"

    def missing(argv, *, timeout):
        return 0, json.dumps({"data": {"repository": {"pullRequest": None}}}), ""

    bad_node = _graphql_server(reviews=[{"author": 17}], files=_FILES, commits=_COMMITS)
    for runner, error in (
        (erroring, "graphql_unreadable"),
        (missing, "graphql_malformed"),
        (bad_node, "reviews_malformed"),
    ):
        got = rb.evaluate_pr("owner/repo", 7, runner=runner, external_identity_templates=())
        assert got["status"] == "unknown", (error, got)
        assert error in got["errors"], (error, got)
        assert got["approval_required"] is True and got["commit_approval_required"] is True


def test_graphql_deleted_author_is_skipped_like_rest_ghost(monkeypatch):
    _no_seams(monkeypatch)
    ghost = {**_gql_review(H2), "author": None}
    serve = _graphql_server(reviews=[ghost, _gql_review(H5)], files=_FILES, commits=_COMMITS)
    got = rb.evaluate_pr("owner/repo", 7, runner=serve, external_identity_templates=())
    assert got["status"] == "ok", got
    assert got["reviewed_heads"] == [H5], got


def test_a_renamed_file_falls_back_to_rest_for_its_earlier_path(monkeypatch):
    """GraphQL has no earlier path for a renamed file, and a file renamed OUT of
    the hook surface is still a gate change. The fallback is what keeps it one."""
    _no_seams(monkeypatch)
    serve = _graphql_server(
        files=[{"path": "scripts/moved.py", "changeType": "RENAMED"}], commits=_COMMITS
    )
    rest_calls = []

    def runner(argv, *, timeout):
        if argv[:3] == ["gh", "api", "graphql"]:
            return serve(argv, timeout=timeout)
        rest_calls.append(argv)
        row = {"filename": "scripts/moved.py", "previous_filename": "scripts/hooks/guard.py"}
        return 0, json.dumps(row) + "\n", ""

    got = rb.evaluate_pr("owner/repo", 7, runner=runner, external_identity_templates=())
    assert got["status"] == "ok", got
    assert got["gate_surface"] is True, got
    assert len(rest_calls) == 1 and "/files?per_page=100" in rest_calls[0][2], rest_calls

    def runner_fails(argv, *, timeout):
        if argv[:3] == ["gh", "api", "graphql"]:
            return serve(argv, timeout=timeout)
        return 1, "", "boom"

    got = rb.evaluate_pr("owner/repo", 7, runner=runner_fails, external_identity_templates=())
    assert got["status"] == "unknown", got
    assert "files_unreadable" in got["errors"], got


def test_graphql_read_queries_the_base_repository(monkeypatch):
    """Owner, name and number come from the PR identity (the BASE repository,
    so a fork PR is read where it lives), sent as GraphQL variables."""
    _no_seams(monkeypatch)
    serve = _graphql_server(files=_FILES, commits=_COMMITS)
    rb.evaluate_pr("acme/widgets", 42, runner=serve, external_identity_templates=())
    argv = serve.seen[0]
    assert "owner=acme" in argv and "name=widgets" in argv and "number=42" in argv, argv
    assert argv[argv.index("number=42") - 1] == "-F", "number must be sent typed (-F) as an Int"


# ── records whose author account is gone ────────────────────────────
# GitHub returns `user: null` once an author account is deleted, so the jq
# yields `login: null` while the body and commit stay readable. Rejecting that
# as malformed makes the WHOLE budget permanently `unknown` -- every foreground
# request prompts, every autonomous session is denied -- over a record that has
# nothing to do with Codex. Verified by execution against jq before fixing.


def test_a_deleted_author_does_not_wedge_the_whole_budget():
    """A null-author review is skipped, not treated as unreadable evidence."""
    ghost = {"login": None, "commit_id": H4, "state": "COMMENTED"}
    result = _eval(reviews=(ghost, _review(H5)))
    assert result["status"] == "ok", result
    assert result["reviewed_heads"] == [H5], result
    # CONTROL: a record that is unreadable in a way we cannot attribute at all
    # still fails closed. The fix narrows strictness, it does not remove it.
    broken = {"login": 17, "commit_id": H4, "state": "COMMENTED"}
    assert _eval(reviews=(broken,))["status"] == "unknown"


def test_a_deleted_author_comment_still_counts_for_the_confirmation_marker():
    """The marker is matched on TEXT, so authorship does not gate it.

    Skipping the record entirely would silently drop a confirmation request
    whose author later deleted their account -- turning a satisfied gate back
    into an unsatisfied one.
    """
    marker = rb.confirmation_marker(H5)
    ghost = {"login": None, "type": None, "body": f"please re-review {marker}"}
    result = _eval(comments=(ghost,))
    assert result["status"] == "ok", result
    assert result["confirmation_requested"] is True, result
    # CONTROL: an unreadable BODY is still malformed — there is nothing to match.
    assert _eval(comments=({"login": None, "type": None, "body": None},))["status"] == "unknown"


def test_the_cli_loads_configured_reviewer_identities():
    """No flag must mean `None` (load the config), not `[]` (no reviewers).

    An empty list is a real value, so passing it suppressed the branch that
    reads `report_identity_template`. On an install with a configured secondary
    reviewer the CLI then omitted those reviewed heads and could report standing
    authorization while the hook callers reported approval required -- two
    answers to the same question from one module.
    """
    import argparse
    import inspect

    src = inspect.getsource(rb.main)
    assert 'action="append", default=None' in src, (
        "the CLI must default to None so evaluate_pr loads the configured identity"
    )
    # And the parser really does yield None rather than [].
    parser = argparse.ArgumentParser()
    parser.add_argument("--external-identity-template", action="append", default=None)
    assert parser.parse_args([]).external_identity_template is None
    assert parser.parse_args(
        ["--external-identity-template", "x{head}"]
    ).external_identity_template == ["x{head}"]


def _seams(monkeypatch, reviews):
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_HEAD", H5)
    monkeypatch.setenv(
        "_TEST_REVIEW_BUDGET_COMMITS",
        "\n".join(json.dumps({"sha": h}) for h in (H1, H2, H3, H4, H5)),
    )
    monkeypatch.setenv("_TEST_GH_CODEX_REVIEWS", "\n".join(json.dumps(r) for r in reviews))
    monkeypatch.setenv("_TEST_GH_CODEX_COMMENTS", "")
    monkeypatch.setenv("_TEST_REVIEW_BUDGET_FILES", json.dumps({"filename": "src/x.py"}))


def test_the_round_counter_counts_the_primary(monkeypatch):
    """A2a round 1 (Devin 🔴): the round counter counts THE primary's reviewed heads,
    read from `review_findings` at call time, never another reviewer's."""
    import review_findings as rf

    devin = "devin-ai-integration[bot]"
    # Before the round-rule cutover only the primary's heads count; another
    # reviewer's review there is not a round, findings or not.
    before = "2026-01-01T00:00:00Z"
    _seams(
        monkeypatch,
        [
            {"login": devin, "commit_id": h, "submitted_at": before, "top_level": ["x"]}
            for h in (H1, H2, H3, H4)
        ],
    )
    assert rb.evaluate_pr("o/r", 1, external_identity_templates=())["count"] == 0
    # A non-primary review that cannot be placed either side of it is unknown,
    # never dropped: dropping it would undercount after the cutover.
    _seams(monkeypatch, [{"login": devin, "commit_id": H1}])
    assert rb.evaluate_pr("o/r", 1, external_identity_templates=())["status"] == "unknown"
    _seams(monkeypatch, [{"login": rf.CODEX_LOGIN, "commit_id": h} for h in (H1, H2, H3, H4)])
    got = rb.evaluate_pr("o/r", 1, external_identity_templates=())
    assert got["count"] == 4 and got["commit_approval_required"] is True
    # Bound to the accessor, not a copy: whatever it names is what is counted.
    monkeypatch.setattr(rf, "primary_reviewer_login", lambda: devin)
    _seams(monkeypatch, [{"login": devin, "commit_id": h} for h in (H1, H2, H3, H4)])
    assert rb.evaluate_pr("o/r", 1, external_identity_templates=())["count"] == 4


def test_an_unimportable_reviewer_list_makes_the_budget_unknown(monkeypatch):
    _seams(monkeypatch, [_review(H4)])
    monkeypatch.setitem(sys.modules, "review_findings", None)  # import now raises
    got = rb.evaluate_pr("o/r", 1, external_identity_templates=())
    assert got["status"] == "unknown" and "review_findings_unimportable" in got["errors"]


def test_graphql_comment_landing_between_reads_is_unknown(monkeypatch):
    """The re-read covers issue comments as well as reviews: a clean-review or
    confirmation comment is budget evidence too."""
    _no_seams(monkeypatch)
    late = {"body": "late", "author": {"login": "someone", "__typename": "User"}}
    base = _graphql_server(files=_FILES, commits=_COMMITS)
    grown = _graphql_server(comments=[late], files=_FILES, commits=_COMMITS)
    calls = []

    def serve(argv, *, timeout):
        calls.append(argv)
        return (base if len(calls) == 1 else grown)(argv, timeout=timeout)

    got = rb.evaluate_pr("owner/repo", 7, runner=serve, external_identity_templates=())
    assert got["status"] == "unknown", got
    assert "evidence_changed_during_evaluation" in got["errors"], got


def test_a_copied_file_on_a_later_page_still_triggers_the_rest_fallback(monkeypatch):
    """COPIED carries an earlier path too, and the flag must survive paging."""
    _no_seams(monkeypatch)
    serve = _graphql_server(
        page_size=1,
        files=[
            # COPIED on page 1, a plain change on page 2: the flag must survive.
            {"path": "src/b.py", "changeType": "COPIED"},
            {"path": "src/a.py", "changeType": "MODIFIED"},
        ],
        commits=[{"commit": {"oid": H5}}],
    )
    rest_calls = []

    def runner(argv, *, timeout):
        if argv[:3] == ["gh", "api", "graphql"]:
            return serve(argv, timeout=timeout)
        rest_calls.append(argv)
        return 0, json.dumps({"filename": "src/b.py", "previous_filename": "src/a.py"}) + "\n", ""

    got = rb.evaluate_pr("owner/repo", 7, runner=runner, external_identity_templates=())
    assert got["status"] == "ok", got
    assert len(rest_calls) == 1, rest_calls


def test_cursors_are_sent_raw_never_typed(monkeypatch):
    """`-F` would read a cursor starting with `@` as a FILE and turn an
    all-digit one into a number; only `-f` sends the string as written."""
    _no_seams(monkeypatch)
    serve = _graphql_server(
        page_size=1, reviews=[_gql_review(H1), _gql_review(H2)], files=_FILES, commits=_COMMITS
    )
    rb.evaluate_pr("owner/repo", 7, runner=serve, external_identity_templates=())
    later = serve.seen[1]
    cursor_args = [i for i, a in enumerate(later) if a.startswith("after_")]
    assert cursor_args, later
    assert all(later[i - 1] == "-f" for i in cursor_args), later


def test_one_read_is_bounded_even_without_a_caller_budget(monkeypatch):
    """Without `budget_seconds` each page still gets 8s, so a many-page read
    needs its own ceiling or it runs to 50 x 8s -- past the push guard's
    registered hook timeout, where a SIGKILL lets the command through."""
    _no_seams(monkeypatch)
    clock = _FakeClock()
    serve = _graphql_server(
        page_size=1, reviews=[_gql_review(H5)] * 40, files=_FILES, commits=_COMMITS
    )

    def slow(argv, *, timeout):
        clock.now += timeout
        return serve(argv, timeout=timeout)

    start = clock.now
    got = rb.evaluate_pr(
        "owner/repo", 7, runner=slow, external_identity_templates=(), monotonic=clock
    )
    assert got["status"] == "unknown", got
    assert "graphql_read_timeout" in got["errors"], got
    assert clock.now - start <= rb._GRAPHQL_READ_SECONDS, clock.now - start


def test_an_errors_payload_or_a_missing_page_flag_is_never_evidence(monkeypatch):
    _no_seams(monkeypatch)
    ok_pr = {"headRefOid": H5, "reviews": {"pageInfo": {}, "nodes": []}}

    def with_errors(argv, *, timeout):
        body = {"data": {"repository": {"pullRequest": {"headRefOid": H5}}}, "errors": [{}]}
        return 0, json.dumps(body), ""

    def no_flag(argv, *, timeout):
        return 0, json.dumps({"data": {"repository": {"pullRequest": ok_pr}}}), ""

    for runner, error in ((with_errors, "graphql_errors"), (no_flag, "reviews_malformed")):
        got = rb.evaluate_pr("owner/repo", 7, runner=runner, external_identity_templates=())
        assert got["status"] == "unknown", (error, got)
        assert error in got["errors"], (error, got)


@pytest.mark.parametrize("stalled_to", [100.0, 19.9], ids=["past-deadline", "just-short"])
def test_a_stall_before_the_call_is_a_timeout_never_a_traceback_or_a_doomed_call(
    monkeypatch, stalled_to
):
    """Codex P2 on #2594, and the case next to it. The read's deadline is taken,
    then the process stalls before the call is issued. Past the deadline that
    raised out of `evaluate_pr` (which catches only the aggregate-budget stop,
    and the CLI calls it with no handler); just short of it, a call too small to
    finish went out. Both must read as a timed-out read, with no call issued.
    """
    _no_seams(monkeypatch)
    ticks = {"n": 0}

    def clock():
        # Call 1 creates the read deadline at t=0; call 2 is the one reading
        # that decides the call, after the stall. (No caller budget, so the
        # aggregate deadline never reads the clock.)
        ticks["n"] += 1
        return 0.0 if ticks["n"] == 1 else stalled_to

    calls = []

    def runner(argv, *, timeout):
        calls.append((argv, timeout))
        return 1, "", "must not be reached"

    got = rb.evaluate_pr(
        "owner/repo", 7, runner=runner, external_identity_templates=(), monotonic=clock
    )
    assert got["status"] == "unknown", got
    assert "graphql_read_timeout" in got["errors"], got
    assert calls == [], calls
    assert ticks["n"] == 2, "the call decision must rest on exactly one clock reading"
