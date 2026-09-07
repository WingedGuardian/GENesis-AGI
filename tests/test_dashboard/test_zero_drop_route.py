"""Tests for /api/genesis/zero-drop — the accounting view's dashboard surface.

Split per the calibration/cc-sessions route-test precedent: the route WIRING
(registration, the not-bootstrapped guard, the limit parameter) is pinned
synchronously with a mocked runtime, because ``_async_route`` owns its own
event loop and cannot run inside an async test's loop. The view's CONTENT is
tested against a real DB in tests/test_session_awareness/test_zero_drop_view.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from genesis.dashboard.api import blueprint


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
    """Level-3 wiring: the endpoint exists on the app, not merely in a module.

    A route module that is never imported registers nothing, and the import
    happens by side effect through routes/__init__.py — so a module added to
    the package but missing from that list is silently absent.
    """
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    rules = {str(r) for r in app.url_map.iter_rules()}
    assert "/api/genesis/zero-drop" in rules


def test_a_not_bootstrapped_runtime_reports_UNAVAILABLE_not_an_empty_board(client):
    """The distinction this whole surface exists to preserve.

    Returning empty parts here would render as a clean board — zero stranded,
    zero pending — which is exactly the false-clean the detector was built to
    prevent. The runtime being down is unknown, never zero.
    """
    with patch("genesis.runtime.GenesisRuntime.instance", return_value=_rt(bootstrapped=False)):
        resp = client.get("/api/genesis/zero-drop")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "unavailable"
    assert "not zero" in body["reason"], "the reason must say WHY an empty answer is wrong"
    assert "gaps" not in body, "no part may be rendered as a count when nothing was read"


def test_the_view_is_returned_whole(client):
    """The route does no assembly of its own — it returns what build_view says.

    Pinned because a route that reshapes the view is how two surfaces start
    disagreeing about the same board.
    """
    sentinel = {"computed_at": "2026-09-07T00:00:00+00:00", "gaps": {"status": "ok", "open": 3}}

    async def _fake(db, *, now, findings_limit):
        return sentinel

    with (
        patch("genesis.runtime.GenesisRuntime.instance", return_value=_rt(db=object())),
        patch("genesis.session_awareness.zero_drop_view.build_view", _fake),
    ):
        resp = client.get("/api/genesis/zero-drop")

    assert resp.get_json() == sentinel


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("", 20),  # the default page size
        ("?limit=5", 5),
        ("?limit=99999", 200),  # clamped to _MAX_LIMIT
        ("?limit=0", 1),  # clamped up: a zero-row page is not a listing
        ("?limit=notanumber", 20),  # unparseable falls back, never raises
    ],
)
def test_the_limit_parameter_is_clamped_rather_than_trusted(client, query, expected):
    """The page size is bounded; the DENOMINATOR beside it is not.

    Clamping is safe here only because this bounds a page rather than a total —
    the counts come from full COUNTs, so a clamped page still renders "n of N".
    """
    seen = {}

    async def _fake(db, *, now, findings_limit):
        seen["limit"] = findings_limit
        return {"computed_at": "x"}

    with (
        patch("genesis.runtime.GenesisRuntime.instance", return_value=_rt(db=object())),
        patch("genesis.session_awareness.zero_drop_view.build_view", _fake),
    ):
        client.get(f"/api/genesis/zero-drop{query}")

    assert seen["limit"] == expected
