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


def test_the_dashboard_renders_the_SAFE_identity_not_the_ack_key():
    """The API splits the identity into three fields so this cannot be got
    wrong, and the first consumer got it wrong anyway.

    `zero_drop_status` emits `branch` (the VERBATIM ack key, which must
    round-trip unsanitised), `branch_display` (neutralised, safe to put in
    front of a person) and `identity_unrenderable` (the two differ).
    `git check-ref-format` accepts bidi overrides and zero-width characters, so
    a ref name can RENDER as something other than the key an operator is
    acknowledging. `x-text` is not the defence — it stops HTML injection, and
    this is not an injection problem.

    This is a source-level assertion because there is no browser in the suite;
    it is worth having anyway, since it fails the moment someone reverts to the
    shorter field name, which is the whole failure mode.
    """
    import pathlib

    tpl = (
        pathlib.Path(__file__).resolve().parents[2]
        / "src/genesis/dashboard/templates/partials/tabs/zero_drop.html"
    ).read_text()

    assert 'x-text="f.branch_display || f.branch"' in tpl, (
        "the finding row must render the NEUTRALISED identity"
    )
    assert "f.identity_unrenderable" in tpl, (
        "and must TELL the reader when what they see is not the ack key"
    )
    # The bare field in a text position is the regression to catch. It stays
    # legal as an x-for :key, which is never rendered.
    assert 'x-text="f.branch"' not in tpl, (
        "rendering the verbatim ack key is the defect this test exists for"
    )
