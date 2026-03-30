"""Tests for the paginated events API and event detail endpoint."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from flask import Flask

from genesis.dashboard.api import blueprint


@pytest.fixture()
def app():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


def _mock_rt(*, bootstrapped=True, db=True):
    rt = MagicMock()
    rt.is_bootstrapped = bootstrapped
    rt.db = MagicMock() if db else None
    return rt


class TestEventsPaginated:
    def test_not_bootstrapped(self, client):
        with patch("genesis.runtime.GenesisRuntime") as MockRT:
            MockRT.instance.return_value = _mock_rt(bootstrapped=False)
            resp = client.get("/api/genesis/events")
            data = resp.get_json()
            assert data["events"] == []
            assert data["has_more"] is False

    def test_no_db(self, client):
        with patch("genesis.runtime.GenesisRuntime") as MockRT:
            MockRT.instance.return_value = _mock_rt(db=False)
            resp = client.get("/api/genesis/events")
            data = resp.get_json()
            assert data["events"] == []

    def test_basic_query(self, client):
        mock_events = [
            {"id": "e1", "timestamp": "2026-03-14T10:00:00", "subsystem": "routing",
             "severity": "info", "event_type": "test", "message": "hello",
             "details": None, "session_id": None, "created_at": "2026-03-14T10:00:00"},
        ]
        rt = _mock_rt()
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch("genesis.db.crud.events.query_paginated", new_callable=AsyncMock, return_value=(mock_events, False)),
            patch("genesis.db.crud.events.count_filtered", new_callable=AsyncMock, return_value=1),
        ):
            MockRT.instance.return_value = rt
            resp = client.get("/api/genesis/events")
            data = resp.get_json()
            assert len(data["events"]) == 1
            assert data["total_matching"] == 1
            assert data["has_more"] is False
            assert data["next_cursor"] is None

    def test_cursor_skips_count(self, client):
        rt = _mock_rt()
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch("genesis.db.crud.events.query_paginated", new_callable=AsyncMock, return_value=([], False)),
            patch("genesis.db.crud.events.count_filtered", new_callable=AsyncMock) as mock_count,
        ):
            MockRT.instance.return_value = rt
            resp = client.get("/api/genesis/events?cursor_ts=2026-03-14T10:00:00&cursor_id=e1")
            data = resp.get_json()
            mock_count.assert_not_called()
            assert data["total_matching"] == 0

    def test_has_more_returns_cursor(self, client):
        mock_events = [
            {"id": "e1", "timestamp": "2026-03-14T10:00:00", "subsystem": "routing",
             "severity": "info", "event_type": "test", "message": "hello",
             "details": None, "session_id": None, "created_at": "2026-03-14T10:00:00"},
        ]
        rt = _mock_rt()
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch("genesis.db.crud.events.query_paginated", new_callable=AsyncMock, return_value=(mock_events, True)),
            patch("genesis.db.crud.events.count_filtered", new_callable=AsyncMock, return_value=50),
        ):
            MockRT.instance.return_value = rt
            resp = client.get("/api/genesis/events")
            data = resp.get_json()
            assert data["has_more"] is True
            assert data["next_cursor"] == {"ts": "2026-03-14T10:00:00", "id": "e1"}


class TestEventDetail:
    def test_not_bootstrapped(self, client):
        with patch("genesis.runtime.GenesisRuntime") as MockRT:
            MockRT.instance.return_value = _mock_rt(bootstrapped=False)
            resp = client.get("/api/genesis/events/some-id")
            assert resp.status_code == 404

    def test_not_found(self, client):
        rt = _mock_rt()
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value=None)
        rt.db.execute = AsyncMock(return_value=mock_cursor)
        with patch("genesis.runtime.GenesisRuntime") as MockRT:
            MockRT.instance.return_value = rt
            resp = client.get("/api/genesis/events/nonexistent")
            assert resp.status_code == 404


class TestLogsPage:
    def test_logs_page_serves(self, client):
        resp = client.get("/genesis/logs")
        assert resp.status_code == 200

    def test_errors_page_serves(self, client):
        resp = client.get("/genesis/errors")
        assert resp.status_code == 200
