"""Tests for the observations API routes (filters collapse + sentinel expansion)."""

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


class TestObservationsFilters:
    def test_not_bootstrapped(self, client):
        with patch("genesis.runtime.GenesisRuntime") as MockRT:
            MockRT.instance.return_value = _mock_rt(bootstrapped=False)
            resp = client.get("/api/genesis/observations/filters")
            assert resp.get_json() == {"types": [], "sources": []}

    def test_session_sources_collapse_to_sentinel(self, client):
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch(
                "genesis.db.crud.observations.distinct_unresolved_types",
                new_callable=AsyncMock, return_value=["metric"],
            ),
            patch(
                "genesis.db.crud.observations.distinct_unresolved_sources",
                new_callable=AsyncMock,
                return_value=["routing", "session:aaa-111", "session:bbb-222"],
            ),
        ):
            MockRT.instance.return_value = _mock_rt()
            data = client.get("/api/genesis/observations/filters").get_json()
            assert data["sources"] == ["routing", "session:*"]

    def test_no_session_sources_no_sentinel(self, client):
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch(
                "genesis.db.crud.observations.distinct_unresolved_types",
                new_callable=AsyncMock, return_value=[],
            ),
            patch(
                "genesis.db.crud.observations.distinct_unresolved_sources",
                new_callable=AsyncMock, return_value=["routing", "sensor"],
            ),
        ):
            MockRT.instance.return_value = _mock_rt()
            data = client.get("/api/genesis/observations/filters").get_json()
            assert data["sources"] == ["routing", "sensor"]


class TestObservationsListSourceFilter:
    def _query_kwargs(self, client, url):
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch(
                "genesis.db.crud.observations.query",
                new_callable=AsyncMock, return_value=[],
            ) as mock_query,
        ):
            MockRT.instance.return_value = _mock_rt()
            resp = client.get(url)
            assert resp.status_code == 200
            return mock_query.call_args.kwargs

    def test_sentinel_expands_to_prefix_match(self, client):
        kwargs = self._query_kwargs(
            client, "/api/genesis/observations?source=session:*"
        )
        assert kwargs["source_prefix"] == "session:"
        assert "source" not in kwargs

    def test_exact_source_still_exact(self, client):
        kwargs = self._query_kwargs(client, "/api/genesis/observations?source=routing")
        assert kwargs["source"] == "routing"
        assert "source_prefix" not in kwargs
