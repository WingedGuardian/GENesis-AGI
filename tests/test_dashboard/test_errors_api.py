"""Tests for the unified errors API endpoint."""

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


def _async_db():
    """A db whose execute()/fetchall() are awaitable and return no rows.

    The plain-MagicMock db in _mock_rt makes the resolutions overlay query
    (``await rt.db.execute(...)``) raise, which the partial-tracking counts as a
    failed source. Tests that assert on ``sources_failed`` need the resolutions
    query to genuinely succeed so the ONE source under test is isolated.
    """
    db = MagicMock()
    cursor = MagicMock()
    cursor.fetchall = AsyncMock(return_value=[])
    db.execute = AsyncMock(return_value=cursor)
    return db


class TestUnifiedErrors:
    def test_not_bootstrapped(self, client):
        with patch("genesis.runtime.GenesisRuntime") as MockRT:
            MockRT.instance.return_value = _mock_rt(bootstrapped=False)
            resp = client.get("/api/genesis/unified-errors")
            data = resp.get_json()
            assert data["groups"] == []
            assert data["totals"]["events"] == 0
            # A not-ready backend loaded no error data — it must NOT read as clean.
            assert data["partial"] is True
            assert data["sources_failed"]

    def test_grouped_response(self, client):
        mock_groups = [
            {
                "subsystem": "routing",
                "event_type": "breaker.tripped",
                "msg_prefix": "Provider anthropic down",
                "worst_severity": "warning",
                "count": 5,
                "first_seen": "2026-03-14T08:00:00",
                "last_seen": "2026-03-14T09:50:00",
            },
        ]
        rt = _mock_rt()
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch("genesis.db.crud.events.query_grouped_errors", new_callable=AsyncMock, return_value=mock_groups),
            patch("genesis.db.crud.dead_letter.query_recent", new_callable=AsyncMock, return_value=[]),
            patch("genesis.db.crud.deferred_work.query_failed", new_callable=AsyncMock, return_value=[]),
            patch("genesis.mcp.health_mcp._impl_health_alerts", new_callable=AsyncMock, return_value=[]),
        ):
            MockRT.instance.return_value = rt
            resp = client.get("/api/genesis/unified-errors")
            data = resp.get_json()
            assert len(data["groups"]) == 1
            g = data["groups"][0]
            assert g["source"] == "events"
            assert g["count"] == 5
            assert data["totals"]["events"] == 5

    def test_dead_letters_included(self, client):
        mock_dl = [
            {
                "id": "dl1",
                "operation_type": "embed",
                "payload": "{}",
                "target_provider": "qdrant",
                "failure_reason": "connection refused",
                "created_at": "2026-03-14T10:00:00",
                "retry_count": 0,
                "last_retry_at": None,
                "status": "pending",
            },
        ]
        rt = _mock_rt()
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch("genesis.db.crud.events.query_grouped_errors", new_callable=AsyncMock, return_value=[]),
            patch("genesis.db.crud.dead_letter.query_recent", new_callable=AsyncMock, return_value=mock_dl),
            patch("genesis.db.crud.deferred_work.query_failed", new_callable=AsyncMock, return_value=[]),
            patch("genesis.mcp.health_mcp._impl_health_alerts", new_callable=AsyncMock, return_value=[]),
        ):
            MockRT.instance.return_value = rt
            resp = client.get("/api/genesis/unified-errors")
            data = resp.get_json()
            assert data["totals"]["dead_letters"] == 1
            assert any(g["source"] == "dead_letter" for g in data["groups"])

    def test_deferred_failures_included(self, client):
        mock_dw = [
            {
                "id": "dw1",
                "work_type": "reflection",
                "call_site_id": None,
                "priority": 30,
                "payload_json": "{}",
                "deferred_at": "2026-03-14T10:00:00",
                "deferred_reason": "cloud_down",
                "staleness_policy": "drain",
                "staleness_ttl_s": None,
                "status": "expired",
                "attempts": 1,
                "last_attempt_at": None,
                "completed_at": "2026-03-14T11:00:00",
                "error_message": "timeout",
                "created_at": "2026-03-14T10:00:00",
            },
        ]
        rt = _mock_rt()
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch("genesis.db.crud.events.query_grouped_errors", new_callable=AsyncMock, return_value=[]),
            patch("genesis.db.crud.dead_letter.query_recent", new_callable=AsyncMock, return_value=[]),
            patch("genesis.db.crud.deferred_work.query_failed", new_callable=AsyncMock, return_value=mock_dw),
            patch("genesis.mcp.health_mcp._impl_health_alerts", new_callable=AsyncMock, return_value=[]),
        ):
            MockRT.instance.return_value = rt
            resp = client.get("/api/genesis/unified-errors")
            data = resp.get_json()
            assert data["totals"]["deferred_failures"] == 1
            assert any(g["source"] == "deferred_work" for g in data["groups"])


class TestPartialDegradation:
    """A data-source outage must be SURFACED (partial/sources_failed), never a
    silent HTTP-200 '0 errors' that reads clean while the DB/FTS is down."""

    def test_partial_flag_on_source_failure(self, client):
        """One failing source → partial=True, that source named, endpoint still
        200 with the OTHER sources intact (a partial view beats none)."""
        rt = _mock_rt()
        rt.db = _async_db()
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch("genesis.db.crud.events.query_grouped_errors", new_callable=AsyncMock, side_effect=RuntimeError("FTS down")),
            patch("genesis.db.crud.dead_letter.query_recent", new_callable=AsyncMock, return_value=[]),
            patch("genesis.db.crud.deferred_work.query_failed", new_callable=AsyncMock, return_value=[]),
            patch("genesis.mcp.health_mcp._impl_health_alerts", new_callable=AsyncMock, return_value=[]),
        ):
            MockRT.instance.return_value = rt
            resp = client.get("/api/genesis/unified-errors")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["partial"] is True
            assert data["sources_failed"] == ["events"]

    def test_no_partial_when_all_sources_ok(self, client):
        """Healthy path → partial=False, sources_failed empty."""
        rt = _mock_rt()
        rt.db = _async_db()
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch("genesis.db.crud.events.query_grouped_errors", new_callable=AsyncMock, return_value=[]),
            patch("genesis.db.crud.dead_letter.query_recent", new_callable=AsyncMock, return_value=[]),
            patch("genesis.db.crud.deferred_work.query_failed", new_callable=AsyncMock, return_value=[]),
            patch("genesis.mcp.health_mcp._impl_health_alerts", new_callable=AsyncMock, return_value=[]),
        ):
            MockRT.instance.return_value = rt
            resp = client.get("/api/genesis/unified-errors")
            data = resp.get_json()
            assert data["partial"] is False
            assert data["sources_failed"] == []
