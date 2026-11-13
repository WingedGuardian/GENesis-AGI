"""Projects v2 adapter: transport failures never read as data, field kinds
come from dataType, option merges never clear a card's value, and a paged
read that comes up short raises instead of reporting a partial board."""

from __future__ import annotations

import json

import pytest

from genesis.board import projects_v2 as pv


def _runner(*responses):
    """A fake runner returning *responses* in order; records each payload."""
    calls = []

    async def run(payload: str):
        calls.append(json.loads(payload))
        resp = responses[min(len(calls), len(responses)) - 1]
        if isinstance(resp, tuple):
            return resp
        return 0, json.dumps(resp), ""

    run.calls = calls
    return run


async def test_graphql_errors_raise_never_read_as_empty():
    with pytest.raises(pv.ProjectsError, match="GraphQL error: nope"):
        await pv.graphql(
            "query { x }", runner=_runner({"errors": [{"message": "nope"}], "data": None})
        )


@pytest.mark.parametrize(
    "resp",
    [
        (1, "", "auth failed"),
        (0, "not json", ""),
        (0, json.dumps({"no": "data"}), ""),
        (124, "", "timeout"),
    ],
)
async def test_transport_failures_raise(resp):
    with pytest.raises(pv.ProjectsError):
        await pv.graphql("query { x }", runner=_runner(resp))


async def test_variables_travel_in_the_payload_not_the_query():
    run = _runner({"data": {"viewer": {"login": "me", "id": "U1"}}})
    await pv.viewer(runner=run)
    assert run.calls[0]["variables"] == {}
    run2 = _runner({"data": {"repository": {"id": "R1"}}})
    await pv.repository_id("own3r", "rep0", runner=run2)
    assert run2.calls[0]["variables"] == {"o": "own3r", "n": "rep0"}
    assert "own3r" not in run2.calls[0]["query"] and "rep0" not in run2.calls[0]["query"]


def _project_node(fields, total=None):
    return {
        "id": "P1",
        "number": 7,
        "title": "Board",
        "public": False,
        "fields": {"totalCount": total if total is not None else len(fields), "nodes": fields},
    }


async def test_field_kinds_come_from_datatype():
    fields = [
        {"__typename": "ProjectV2Field", "id": "F1", "name": "Title", "dataType": "TITLE"},
        {"__typename": "ProjectV2Field", "id": "F2", "name": "Genesis note", "dataType": "TEXT"},
        {
            "__typename": "ProjectV2SingleSelectField",
            "id": "F3",
            "name": "Status",
            "dataType": "SINGLE_SELECT",
            "options": [{"id": "o1", "name": "Done", "color": "GREEN", "description": ""}],
        },
    ]
    proj = await pv.get_project(
        "me", 7, runner=_runner({"data": {"user": {"projectV2": _project_node(fields)}}})
    )
    assert proj.fields["Genesis note"].kind == "text"
    assert proj.fields["Title"].kind == "other", (
        "a built-in is never mistaken for a writable text field"
    )
    assert proj.fields["Status"].options == {"Done": "o1"}


async def test_a_partial_field_map_is_refused():
    node = _project_node([], total=60)
    with pytest.raises(pv.ProjectsError, match="more than 50 fields"):
        await pv.get_project("me", 7, runner=_runner({"data": {"user": {"projectV2": node}}}))


async def test_missing_project_raises():
    with pytest.raises(pv.ProjectsError, match="no project"):
        await pv.get_project("me", 9, runner=_runner({"data": {"user": {"projectV2": None}}}))


def test_merge_options_keeps_ids_so_no_card_loses_its_value():
    existing = [
        {"id": "t", "name": "Todo", "color": "GRAY", "description": ""},
        {"id": "p", "name": "In Progress", "color": "YELLOW", "description": "x"},
        {"id": "d", "name": "Done", "color": "GREEN", "description": ""},
    ]
    kept = pv.merge_options(existing, pv.STATUS_OPTIONS, keep_unlisted=True)
    assert [o["name"] for o in kept] == [*pv.STATUS_OPTIONS, "Todo"]
    by_name = {o["name"]: o for o in kept}
    assert (
        by_name["In Progress"]["id"] == "p"
        and by_name["Done"]["id"] == "d"
        and by_name["Todo"]["id"] == "t"
    )
    assert "id" not in by_name["Proposed"], "a new option is sent without an id"
    dropped = pv.merge_options(existing, pv.STATUS_OPTIONS, keep_unlisted=False)
    assert "Todo" not in [o["name"] for o in dropped]


def test_options_literal_escapes_strings_and_rejects_bad_colors():
    lit = pv._options_literal([{"name": 'x" } evil: {', "color": "RED", "description": "d\n"}])
    assert '"x\\" } evil: {"' in lit, "a quote cannot break out of its string slot"
    with pytest.raises(pv.ProjectsError, match="invalid option color"):
        pv._options_literal([{"name": "x", "color": "RED } }", "description": ""}])


def _items_page(nodes, *, total, has_next, cursor=None):
    return {
        "data": {
            "node": {
                "items": {
                    "totalCount": total,
                    "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                    "nodes": nodes,
                }
            }
        }
    }


async def test_list_items_paginates_to_the_end():
    run = _runner(
        _items_page([{"id": "a"}], total=2, has_next=True, cursor="C1"),
        _items_page([{"id": "b"}], total=2, has_next=False),
    )
    out = await pv.list_items("P1", runner=run)
    assert [i["id"] for i in out["items"]] == ["a", "b"] and out["total"] == 2
    assert run.calls[1]["variables"]["c"] == "C1"


async def test_list_items_short_read_raises():
    run = _runner(_items_page([{"id": "a"}], total=3, has_next=False))
    with pytest.raises(pv.ProjectsError, match="read 1 items but the project reports 3"):
        await pv.list_items("P1", runner=run)


async def test_find_projects_by_title_reads_every_page_and_skips_closed():
    run = _runner(
        {
            "data": {
                "user": {
                    "projectsV2": {
                        "nodes": [{"id": "1", "number": 1, "title": "B", "closed": True}],
                        "pageInfo": {"hasNextPage": True, "endCursor": "c"},
                    }
                }
            }
        },
        {
            "data": {
                "user": {
                    "projectsV2": {
                        "nodes": [{"id": "2", "number": 2, "title": "B", "closed": False}],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        },
    )
    assert [p["number"] for p in await pv.find_projects_by_title("me", "B", runner=run)] == [2]


# ─── round 2 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("node", [None, {}, {"id": "x"}])
async def test_an_unreadable_item_raises_rather_than_reading_as_no_status(node):
    with pytest.raises(pv.ProjectsError):
        await pv.item_status("I1", runner=_runner({"data": {"node": node}}))


async def test_an_item_with_no_status_reads_as_none():
    assert await pv.item_status("I1", runner=_runner({"data": {"node": {"status": None}}})) is None
    run = _runner({"data": {"node": {"status": {"name": "Ready"}}}})
    assert await pv.item_status("I1", runner=run) == "Ready"


async def test_set_view_layout_sends_the_layout_as_a_variable():
    run = _runner({"data": {"updateProjectV2View": {"projectV2View": {"id": "V1"}}}})
    await pv.set_view_layout("V1", pv.BOARD_LAYOUT, runner=run)
    assert run.calls[0]["variables"] == {"v": "V1", "l": "BOARD_LAYOUT"}
    assert "layout: $l" in run.calls[0]["query"]


# ─── round 3 ────────────────────────────────────────────────────────────────


def _repos_page(names, *, has_next, cursor=None):
    return {
        "data": {
            "node": {
                "repositories": {
                    "nodes": [{"nameWithOwner": n} for n in names],
                    "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                }
            }
        }
    }


async def test_project_repositories_reads_every_page():
    run = _runner(
        _repos_page(["a/one"], has_next=True, cursor="C1"),
        _repos_page(["b/two"], has_next=False),
    )
    assert await pv.project_repositories("P1", runner=run) == ["a/one", "b/two"]
    assert run.calls[1]["variables"] == {"p": "P1", "c": "C1"}


@pytest.mark.parametrize("node", [None, {}])
async def test_an_unreadable_project_is_never_read_as_linked_to_nothing(node):
    with pytest.raises(pv.ProjectsError):
        await pv.project_repositories("P1", runner=_runner({"data": {"node": node}}))


async def test_link_repository_sends_ids_as_variables():
    run = _runner({"data": {"linkProjectV2ToRepository": {"repository": {"id": "R1"}}}})
    await pv.link_repository("P1", "R1", runner=run)
    assert run.calls[0]["variables"] == {"p": "P1", "r": "R1"}
    assert "linkProjectV2ToRepository" in run.calls[0]["query"]


async def test_get_project_reads_closed():
    node = {**_project_node([]), "closed": True}
    proj = await pv.get_project("me", 7, runner=_runner({"data": {"user": {"projectV2": node}}}))
    assert proj.closed is True


# ─── reconciler reads ───────────────────────────────────────────────────────


async def test_repo_open_counts_reports_both_totals():
    run = _runner(
        {
            "data": {
                "repository": {
                    "issues": {"totalCount": 662},
                    "pullRequests": {"totalCount": 44},
                }
            }
        }
    )
    assert await pv.repo_open_counts("o", "r", runner=run) == {"issues": 662, "pull_requests": 44}
    assert run.calls[0]["variables"] == {"o": "o", "n": "r"}


async def test_repo_open_counts_raises_for_a_missing_repo():
    with pytest.raises(pv.ProjectsError):
        await pv.repo_open_counts("o", "r", runner=_runner({"data": {"repository": None}}))


def _card_node(**over):
    node = {
        "__typename": "Issue",
        "number": 1,
        "state": "OPEN",
        "projectItems": {
            "totalCount": 1,
            "nodes": [
                {
                    "project": {"number": 2, "owner": {"login": "o"}},
                    "status": {"name": "In Review", "updatedAt": "2026-10-04T05:12:52Z"},
                }
            ],
        },
        "blockedBy": {
            "totalCount": 1,
            "nodes": [{"number": 2, "state": "OPEN", "repository": {"nameWithOwner": "o/r"}}],
        },
    }
    node.update(over)
    return {"data": {"repository": {"issueOrPullRequest": node}}}


async def test_card_for_issue_shape():
    card = await pv.card_for_issue("o", "r", 1, runner=_runner(_card_node()))
    assert card["kind"] == "Issue" and card["state"] == "OPEN"
    assert card["cards"] == [
        {
            "project_owner": "o",
            "project_number": 2,
            "status": "In Review",
            "status_updated_at": "2026-10-04T05:12:52Z",
            "genesis": None,
        }
    ]
    assert card["cards_truncated"] is False
    assert card["blocked_by"] == [{"number": 2, "state": "OPEN", "repo": "o/r"}]
    assert card["blocked_by_total"] == 1 and card["blockers_truncated"] is False


async def test_card_for_issue_flags_truncated_lists_rather_than_hiding_them():
    node = _card_node(
        blockedBy={
            "totalCount": 25,
            "nodes": [
                {"number": i, "state": "OPEN", "repository": {"nameWithOwner": "o/r"}}
                for i in range(20)
            ],
        },
        projectItems={"totalCount": 11, "nodes": [{"project": {"number": 2}, "status": None}] * 10},
    )
    card = await pv.card_for_issue("o", "r", 1, runner=_runner(node))
    assert card["blocked_by_total"] == 25 and card["blockers_truncated"] is True
    assert card["cards_truncated"] is True
    assert card["cards"][0]["status"] is None


async def test_card_for_issue_reads_genesis_and_drops_archived_cards():
    run = _runner(
            _card_node(
                projectItems={
                    "totalCount": 2,
                    "nodes": [
                        {
                            "isArchived": True,
                            "project": {"number": 2, "owner": {"login": "o"}},
                            "status": {"name": "Done"},
                        },
                        {
                            "isArchived": False,
                            "project": {"number": 2, "owner": {"login": "o"}},
                            "status": {"name": "Ready"},
                            "genesis": {"name": "Blocked"},
                        },
                    ],
                }
            )
    )

    card = await pv.card_for_issue("o", "r", 1, runner=run)
    assert [c["status"] for c in card["cards"]] == ["Ready"]
    assert card["cards"][0]["genesis"] == "Blocked"
    # includeArchived defaults to TRUE on projectItems; the query must say false.
    assert "includeArchived: false" in run.calls[0]["query"]


async def test_card_for_a_pull_request_has_no_blockers_field():
    node = _card_node(__typename="PullRequest")
    node["data"]["repository"]["issueOrPullRequest"].pop("blockedBy")
    card = await pv.card_for_issue("o", "r", 1, runner=_runner(node))
    assert card["kind"] == "PullRequest"
    assert card["blocked_by"] == [] and card["blocked_by_total"] == 0


async def test_card_for_issue_raises_when_the_number_does_not_exist():
    with pytest.raises(pv.ProjectsError):
        await pv.card_for_issue(
            "o", "r", 9, runner=_runner({"data": {"repository": {"issueOrPullRequest": None}}})
        )


# ─── reconciler writes (#4400) ──────────────────────────────────────────────

_STATUS_OPTS = {name: f"opt-{i}" for i, name in enumerate(pv.STATUS_OPTIONS)}


def _board_project():
    status = pv.Field("F-STATUS", pv.STATUS_FIELD, "single_select", dict(_STATUS_OPTS))
    return pv.Project("P1", 2, "Board", False, {pv.STATUS_FIELD: status})


@pytest.mark.parametrize("to_name", ["Proposed", "In Review"])
async def test_set_status_fills_an_empty_column_with_the_option_id(to_name):
    run = _runner({"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "I1"}}}})
    await pv.set_status(_board_project(), "I1", None, to_name, runner=run)
    assert len(run.calls) == 1
    assert run.calls[0]["variables"] == {
        "p": "P1",
        "i": "I1",
        "f": "F-STATUS",
        "o": _STATUS_OPTS[to_name],
    }


@pytest.mark.parametrize("from_name", [None, *pv.STATUS_OPTIONS])
async def test_set_status_never_writes_in_progress(from_name):
    run = _runner({"data": {}})
    with pytest.raises(pv.ProjectsError, match="In Progress"):
        await pv.set_status(_board_project(), "I1", from_name, "In Progress", runner=run)
    assert run.calls == []


@pytest.mark.parametrize(
    ("from_name", "to_name"),
    [
        (None, "Ready"),
        (None, "Done"),
        ("Proposed", "Ready"),
        ("Ready", "Proposed"),
        ("In Review", "Proposed"),
        ("Done", "In Review"),
        # Part 2's moves into In Review are not allowed until Part 2 lands.
        ("Proposed", "In Review"),
    ],
)
async def test_set_status_refuses_every_transition_outside_the_allowlist(from_name, to_name):
    run = _runner({"data": {}})
    with pytest.raises(pv.ProjectsError, match="not an allowed Status change"):
        await pv.set_status(_board_project(), "I1", from_name, to_name, runner=run)
    assert run.calls == []


async def test_set_status_refuses_when_the_board_lacks_the_option():
    proj = _board_project()
    proj.fields[pv.STATUS_FIELD].options.pop("Proposed")
    run = _runner({"data": {}})
    with pytest.raises(pv.ProjectsError, match="no 'Proposed' option"):
        await pv.set_status(proj, "I1", None, "Proposed", runner=run)
    proj.fields.pop(pv.STATUS_FIELD)
    with pytest.raises(pv.ProjectsError, match="no Status field"):
        await pv.set_status(proj, "I1", None, "In Review", runner=run)
    assert run.calls == []


# GitHub GraphQL docs ("Rate limits and query limits for the GraphQL API",
# consulted 2026-11-13): a primary limit is an error, a secondary limit comes
# back as HTTP 200 or 403 "with an error message that indicates that you hit a
# secondary rate limit".
@pytest.mark.parametrize(
    "resp",
    [
        {"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}], "data": None},
        {"errors": [{"message": "You have exceeded a secondary rate limit."}]},
        (1, json.dumps({"message": "You have exceeded a secondary rate limit"}), "HTTP 403"),
        (1, "", "gh: You have triggered an abuse detection mechanism (HTTP 403)"),
        (1, "", "gh: API rate limit exceeded for user ID 1. (HTTP 403)"),
    ],
)
async def test_a_rate_limit_raises_its_own_error(resp):
    with pytest.raises(pv.RateLimited):
        await pv.graphql("mutation { x }", runner=_runner(resp))


@pytest.mark.parametrize(
    "resp",
    [
        {"errors": [{"type": "NOT_FOUND", "message": "Could not resolve to a node"}]},
        (1, "", "gh: Bad credentials (HTTP 401)"),
    ],
)
async def test_other_errors_are_not_read_as_a_rate_limit(resp):
    with pytest.raises(pv.ProjectsError) as caught:
        await pv.graphql("query { x }", runner=_runner(resp))
    assert not isinstance(caught.value, pv.RateLimited)


# MEASURED 2026-11-13 (gh 2.101): a GraphQL error exits 1 with the errors JSON
# on stdout; an HTTP failure exits 1 with {"message": ...} and no data/errors.
_GRAPHQL_ERROR = (
    1,
    json.dumps({"data": {"node": None}, "errors": [{"type": "NOT_FOUND", "message": "nope"}]}),
    "gh: nope",
)


@pytest.mark.parametrize(
    "resp",
    [
        (1, json.dumps({"message": "Bad credentials", "status": "401"}), "gh: Bad credentials (HTTP 401)"),
        (1, json.dumps({"message": "Server Error"}), "gh: Server Error (HTTP 502)"),
        (124, "", "gh api graphql timed out after 60s"),
        (0, "not json", ""),
        (0, json.dumps({"no": "data"}), ""),
    ],
)
async def test_a_failed_request_is_told_apart_from_an_error_about_one_item(resp):
    with pytest.raises(pv.RequestFailed):
        await pv.graphql("mutation { x }", runner=_runner(resp))


async def test_an_error_about_one_item_is_not_a_failed_request():
    with pytest.raises(pv.ProjectsError) as caught:
        await pv.graphql("mutation { x }", runner=_runner(_GRAPHQL_ERROR))
    assert not isinstance(caught.value, pv.RequestFailed)


def test_a_rate_limit_is_a_failed_request():
    assert issubclass(pv.RateLimited, pv.RequestFailed)


def test_status_option_resolves_without_any_request():
    assert pv.status_option(_board_project(), None, "Proposed") == ("F-STATUS", _STATUS_OPTS["Proposed"])
    with pytest.raises(pv.ProjectsError, match="In Progress"):
        pv.status_option(_board_project(), None, "In Progress")


def test_the_items_read_carries_each_issue_s_open_closing_prs():
    _, found, rest = pv._ITEMS_QUERY.partition("closedByPullRequestsReferences(")
    assert found, "the items read must carry each issue's closing PRs"
    block = rest[: rest.index("} }") + 3]
    for name in ("totalCount", "number", "state", "createdAt", "isCrossRepository"):
        assert name in block, name


def _content_page(kind, nodes, *, total, has_next=False, cursor=None):
    key = "issues" if kind == "Issue" else "pullRequests"
    return {
        "data": {
            "repository": {
                key: {
                    "totalCount": total,
                    "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                    "nodes": nodes,
                }
            }
        }
    }


async def test_open_content_reads_every_open_issue_and_pr_to_the_end():
    run = _runner(
        _content_page("Issue", [{"id": "I_a", "number": 1}], total=2, has_next=True, cursor="c"),
        _content_page("Issue", [{"id": "I_b", "number": 2}], total=2),
        _content_page("PullRequest", [{"id": "PR_c", "number": 3}], total=1),
    )
    out = await pv.open_content("o", "r", runner=run)
    assert out == [
        {"id": "I_a", "number": 1, "kind": "Issue"},
        {"id": "I_b", "number": 2, "kind": "Issue"},
        {"id": "PR_c", "number": 3, "kind": "PullRequest"},
    ]
    assert run.calls[1]["variables"]["c"] == "c"
    assert all(c["variables"]["o"] == "o" and c["variables"]["n"] == "r" for c in run.calls)


async def test_open_content_short_read_raises():
    run = _runner(_content_page("Issue", [{"id": "I_a", "number": 1}], total=4))
    with pytest.raises(pv.ProjectsError, match="read 1 open issues but the repository reports 4"):
        await pv.open_content("o", "r", runner=run)
