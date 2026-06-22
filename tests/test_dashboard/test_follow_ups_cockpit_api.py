"""Tests for the dashboard follow-up cockpit routes (PR2).

Covers param handling, the soft/hard mutations, the batch dispatch, and the
503/404/400 guards. Runtime + CRUD layer are mocked (no real DB) — mirrors the
test_references_api.py pattern.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from flask import Flask

from genesis.dashboard.api import blueprint

_CRUD = "genesis.db.crud.follow_ups"
_RT = "genesis.runtime.GenesisRuntime"


@pytest.fixture()
def app():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    app.secret_key = "test-secret-key"
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


def _mock_rt(is_bootstrapped=True, has_db=True):
    rt = MagicMock()
    rt.is_bootstrapped = is_bootstrapped
    rt.db = MagicMock() if has_db else None
    return rt


def _row(**over):
    row = {
        "id": "fu-1", "content": "do the thing", "reason": "because",
        "status": "pending", "priority": "medium", "kind": "follow_up",
        "domain": None, "source": "foreground_session",
        "strategy": "ego_judgment", "created_at": "2026-06-01T00:00:00Z",
        "pinned": 0,
    }
    row.update(over)
    return row


# ── cockpit list ──────────────────────────────────────────────────────
def test_cockpit_returns_items_and_total(client):
    with (
        patch(_RT) as MockRT,
        patch(f"{_CRUD}.query_page", new=AsyncMock(return_value=[_row()])),
        patch(f"{_CRUD}.count_filtered", new=AsyncMock(return_value=1)),
    ):
        MockRT.instance.return_value = _mock_rt()
        resp = client.get(
            "/api/genesis/follow-ups/cockpit?kind=follow_up&page=1&page_size=50"
        )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total"] == 1 and data["page"] == 1 and len(data["items"]) == 1


def test_cockpit_empty_200_when_not_bootstrapped(client):
    with patch(_RT) as MockRT:
        MockRT.instance.return_value = _mock_rt(is_bootstrapped=False, has_db=False)
        resp = client.get("/api/genesis/follow-ups/cockpit")
    assert resp.status_code == 200
    assert resp.get_json()["items"] == []


def test_cockpit_passes_null_domain_sentinel(client):
    qp = AsyncMock(return_value=[])
    with (
        patch(_RT) as MockRT,
        patch(f"{_CRUD}.query_page", new=qp),
        patch(f"{_CRUD}.count_filtered", new=AsyncMock(return_value=0)),
    ):
        MockRT.instance.return_value = _mock_rt()
        client.get("/api/genesis/follow-ups/cockpit?domain=__null__")
    assert qp.await_args.kwargs["domain"] == "__null__"


def test_cockpit_all_tier_clears_kind_filter(client):
    qp = AsyncMock(return_value=[])
    with (
        patch(_RT) as MockRT,
        patch(f"{_CRUD}.query_page", new=qp),
        patch(f"{_CRUD}.count_filtered", new=AsyncMock(return_value=0)),
    ):
        MockRT.instance.return_value = _mock_rt()
        client.get("/api/genesis/follow-ups/cockpit?kind=all")
    assert qp.await_args.kwargs["kind"] is None


# ── filters ───────────────────────────────────────────────────────────
def test_filters_returns_sources_and_statuses(client):
    with (
        patch(_RT) as MockRT,
        patch(f"{_CRUD}.get_distinct_sources", new=AsyncMock(return_value=["ego_cycle"])),
    ):
        MockRT.instance.return_value = _mock_rt()
        resp = client.get("/api/genesis/follow-ups/filters")
    data = resp.get_json()
    assert data["sources"] == ["ego_cycle"]
    assert "pending" in data["statuses"] and "completed" in data["statuses"]


# ── single mutations ──────────────────────────────────────────────────
def test_done_marks_completed(client):
    us = AsyncMock(return_value=True)
    with patch(_RT) as MockRT, patch(f"{_CRUD}.update_status", new=us):
        MockRT.instance.return_value = _mock_rt()
        resp = client.post("/api/genesis/follow-ups/fu-1/done", json={"notes": "x"})
    assert resp.status_code == 200 and resp.get_json()["ok"] is True
    assert us.await_args.args[1:] == ("fu-1", "completed")


def test_done_404_when_missing(client):
    with (
        patch(_RT) as MockRT,
        patch(f"{_CRUD}.update_status", new=AsyncMock(return_value=False)),
    ):
        MockRT.instance.return_value = _mock_rt()
        resp = client.post("/api/genesis/follow-ups/nope/done", json={})
    assert resp.status_code == 404


def test_done_503_when_not_bootstrapped(client):
    with patch(_RT) as MockRT:
        MockRT.instance.return_value = _mock_rt(is_bootstrapped=False, has_db=False)
        resp = client.post("/api/genesis/follow-ups/fu-1/done", json={})
    assert resp.status_code == 503


def test_delete_ok(client):
    with patch(_RT) as MockRT, patch(f"{_CRUD}.delete", new=AsyncMock(return_value=True)):
        MockRT.instance.return_value = _mock_rt()
        resp = client.post("/api/genesis/follow-ups/fu-1/delete", json={})
    assert resp.status_code == 200


def test_pin_passes_bool(client):
    sp = AsyncMock(return_value=True)
    with patch(_RT) as MockRT, patch(f"{_CRUD}.set_pinned", new=sp):
        MockRT.instance.return_value = _mock_rt()
        resp = client.post("/api/genesis/follow-ups/fu-1/pin", json={"pinned": True})
    assert resp.status_code == 200 and resp.get_json()["pinned"] is True
    assert sp.await_args.args[2] is True


def test_priority_400_on_invalid(client):
    with (
        patch(_RT) as MockRT,
        patch(f"{_CRUD}.set_priority", new=AsyncMock(side_effect=ValueError("bad"))),
    ):
        MockRT.instance.return_value = _mock_rt()
        resp = client.post(
            "/api/genesis/follow-ups/fu-1/priority", json={"priority": "urgent"}
        )
    assert resp.status_code == 400


def test_kind_ok(client):
    with patch(_RT) as MockRT, patch(f"{_CRUD}.set_kind", new=AsyncMock(return_value=True)):
        MockRT.instance.return_value = _mock_rt()
        resp = client.post("/api/genesis/follow-ups/fu-1/kind", json={"kind": "tabled"})
    assert resp.status_code == 200 and resp.get_json()["kind"] == "tabled"


def test_domain_clear_passes_none(client):
    sd = AsyncMock(return_value=True)
    with patch(_RT) as MockRT, patch(f"{_CRUD}.set_domain", new=sd):
        MockRT.instance.return_value = _mock_rt()
        resp = client.post("/api/genesis/follow-ups/fu-1/domain", json={"domain": ""})
    assert resp.status_code == 200
    assert sd.await_args.args[2] is None  # empty string -> None (clear)


# ── batch ─────────────────────────────────────────────────────────────
def test_batch_done(client):
    usb = AsyncMock(return_value=3)
    with patch(_RT) as MockRT, patch(f"{_CRUD}.update_status_batch", new=usb):
        MockRT.instance.return_value = _mock_rt()
        resp = client.post(
            "/api/genesis/follow-ups/batch",
            json={"action": "done", "ids": ["a", "b", "c"]},
        )
    assert resp.status_code == 200 and resp.get_json()["count"] == 3


def test_batch_tabled_routes_to_set_kind_batch(client):
    skb = AsyncMock(return_value=2)
    with patch(_RT) as MockRT, patch(f"{_CRUD}.set_kind_batch", new=skb):
        MockRT.instance.return_value = _mock_rt()
        resp = client.post(
            "/api/genesis/follow-ups/batch",
            json={"action": "tabled", "ids": ["a", "b"]},
        )
    assert resp.status_code == 200
    assert skb.await_args.args[2] == "tabled"


def test_batch_400_on_bad_action(client):
    with patch(_RT) as MockRT:
        MockRT.instance.return_value = _mock_rt()
        resp = client.post(
            "/api/genesis/follow-ups/batch", json={"action": "nuke", "ids": ["a"]}
        )
    assert resp.status_code == 400


def test_batch_400_on_non_list_ids(client):
    with patch(_RT) as MockRT:
        MockRT.instance.return_value = _mock_rt()
        resp = client.post(
            "/api/genesis/follow-ups/batch", json={"action": "done", "ids": "abc"}
        )
    assert resp.status_code == 400


def test_batch_400_on_too_many_ids(client):
    with patch(_RT) as MockRT:
        MockRT.instance.return_value = _mock_rt()
        resp = client.post(
            "/api/genesis/follow-ups/batch",
            json={"action": "delete", "ids": [str(i) for i in range(201)]},
        )
    assert resp.status_code == 400


def test_batch_503_when_not_bootstrapped(client):
    with patch(_RT) as MockRT:
        MockRT.instance.return_value = _mock_rt(is_bootstrapped=False, has_db=False)
        resp = client.post(
            "/api/genesis/follow-ups/batch", json={"action": "done", "ids": ["a"]}
        )
    assert resp.status_code == 503
