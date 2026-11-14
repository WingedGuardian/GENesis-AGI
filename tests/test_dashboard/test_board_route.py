"""Tests for /api/genesis/board — the read-only Board tab's endpoint (#4510).

The route WIRING (registration, the not-bootstrapped guard, the pass-through)
is pinned synchronously with a mocked runtime, because ``_async_route`` owns
its own event loop and cannot run inside an async test's loop (the zero-drop
route test's precedent). What the summary CONTAINS is tested against a real DB
in tests/test_board/test_board_tools.py, through the same function this route
returns unchanged.

The tab's registration points that test_tab_registry_parity.py does not check
(the route import, the store members, the fetch) are pinned here, so removing
any one of them fails a test.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from genesis.dashboard.api import blueprint

_DASH = Path(__file__).resolve().parents[2] / "src" / "genesis" / "dashboard"
_JS = (_DASH / "webui" / "js" / "dashboard.js").read_text()


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    return app.test_client()


def _rt(db=None, *, bootstrapped=True):
    rt = MagicMock()
    rt.is_bootstrapped = bootstrapped
    rt.db = db
    return rt


def test_the_route_is_registered_on_the_blueprint():
    """Registered through routes/__init__.py's import side effect: a module
    missing from that list registers nothing, silently."""
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    assert "/api/genesis/board" in {str(r) for r in app.url_map.iter_rules()}


@pytest.mark.parametrize("bootstrapped, db", [(False, object()), (True, None)])
def test_no_runtime_reports_unavailable_not_an_empty_board(client, bootstrapped, db):
    with patch(
        "genesis.runtime.GenesisRuntime.instance",
        return_value=_rt(db, bootstrapped=bootstrapped),
    ):
        resp = client.get("/api/genesis/board")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "unavailable"
    assert "unknown" in body["reason"]
    assert "summary" not in body and "latest" not in body


def test_the_summary_is_board_status_s_own_output_unchanged(client):
    """The tab and the board_status MCP tool can never disagree: the route
    returns that tool's function's output as it is."""
    sentinel = {
        "status": "ok",
        "heartbeat": {"status": "alive", "age_seconds": 12.5},
        "latest": {"mode": "propose_only", "paused": False, "last_error": None},
        "summary": {"items_total": 3, "writes": {"dry_run": True, "would_set_status": 1}},
    }
    db = object()
    seen = []

    async def fake(arg):
        seen.append(arg)
        return sentinel

    with (
        patch("genesis.runtime.GenesisRuntime.instance", return_value=_rt(db)),
        patch("genesis.mcp.health.board_tools._impl_board_status", fake),
    ):
        resp = client.get("/api/genesis/board")
    assert resp.status_code == 200
    assert resp.get_json() == sentinel
    assert seen == [db]


# ── registration points the parity test does not cover ─────────────────────


def _store_members() -> set[str]:
    # members of the Alpine store: 8-space-indented keys (the integrity test's rule)
    return set(re.findall(r"^        (?:async )?([A-Za-z_][A-Za-z0-9_]*)\s*[:(]", _JS, re.M))


def test_the_route_module_is_imported_and_exported():
    init = (_DASH / "routes" / "__init__.py").read_text()
    assert re.search(r"^\s+board,$", init, re.M), "board missing from the routes import list"
    assert re.search(r'^\s+"board",$', init, re.M), "board missing from routes.__all__"


@pytest.mark.parametrize(
    "member",
    [
        "boardView",
        "_boardInterval",
        "_boardFetchToken",
        "_boardSucceededToken",
        "fetchBoard",
        "boardNum",
        "boardWrites",
    ],
)
def test_the_store_declares_each_board_member(member):
    assert member in _store_members(), f"{member} is not declared on the store"


def test_fetch_board_reads_the_board_endpoint_and_guards_its_order():
    body = _JS[_JS.index("async fetchBoard()") :]
    body = body[: body.index("\n        },\n")]
    assert 'fetchApi("/api/genesis/board")' in body
    assert "_boardFetchToken" in body and "_boardNewerRequestSucceeded" in body
    assert 'this.startFetch("board")' in body and 'this.finishFetch("board")' in body


def test_the_board_tab_fetches_on_every_entry_and_every_minute():
    case = _JS[_JS.index('case "board":') :]
    case = case[: case.index("break;")]
    assert "if (first)" not in case, "the tab must fetch on every entry, not just the first"
    assert "this.fetchBoard();" in case
    assert re.search(r"_boardInterval = setInterval\(\(\) => this\.fetchBoard\(\), 60000\)", case)


def test_the_tab_template_never_shows_a_missing_number_as_zero():
    html = (_DASH / "templates" / "partials" / "tabs" / "board.html").read_text()
    assert "panelState('board')" in html and "refreshFailing('board')" in html
    assert "boardView?.status === 'unavailable'" in html
    # every count goes through the one helper that renders null as "unknown"
    assert "boardNum(" in html and "boardWrites(" in html
    assert not re.search(r"x-text=\"[^\"]*\|\| *0", html), "a missing value must not fall back to 0"
