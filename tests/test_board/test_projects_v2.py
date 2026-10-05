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
