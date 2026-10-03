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
    await pv.repository_id("o", "r", runner=_runner({"data": {"repository": {"id": "R1"}}}))


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
