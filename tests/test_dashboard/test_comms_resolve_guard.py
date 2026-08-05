"""PR-6a: revision guard on the Chat/Comms-tab resolve surface.

The comms resolve route (`/api/genesis/comms/proposals/<id>/resolve`) is the
fourth user-facing resolve path; it gets the same optimistic-concurrency guard
as the ego-modal / Telegram / MCP surfaces.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from flask import Flask

# Importing the module registers its routes on the shared blueprint.
import genesis.dashboard.routes.comms  # noqa: F401
from genesis.dashboard._blueprint import blueprint


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    return app.test_client()


def _rt():
    rt = MagicMock()
    rt.is_bootstrapped = True
    rt.db = MagicMock()  # comms uses rt.db (not rt._db)
    return rt


def test_comms_resolve_passes_expected_revision(client):
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.db.crud.ego.resolve_proposal", new_callable=AsyncMock, return_value=True
        ) as mock_resolve,
        patch("genesis.db.crud.ego.get_proposal", new_callable=AsyncMock, return_value=None),
    ):
        MockRT.instance.return_value = _rt()
        resp = client.post(
            "/api/genesis/comms/proposals/p1/resolve",
            json={"status": "approved", "revision_num": 2},
        )
    assert resp.status_code == 200
    assert mock_resolve.call_args[1]["expected_revision"] == 2


def test_comms_resolve_without_revision_is_unguarded(client):
    """FOOTGUN GUARD: no revision_num → expected_revision None, never 1."""
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.db.crud.ego.resolve_proposal", new_callable=AsyncMock, return_value=True
        ) as mock_resolve,
        patch("genesis.db.crud.ego.get_proposal", new_callable=AsyncMock, return_value=None),
    ):
        MockRT.instance.return_value = _rt()
        client.post(
            "/api/genesis/comms/proposals/p1/resolve",
            json={"status": "approved"},
        )
    assert mock_resolve.call_args[1]["expected_revision"] is None


def test_comms_resolve_stale_revision_returns_409(client):
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.db.crud.ego.resolve_proposal", new_callable=AsyncMock, return_value=False),
        patch(
            "genesis.db.crud.ego.get_proposal",
            new_callable=AsyncMock,
            return_value={"id": "p1", "status": "pending", "revision_num": 5},
        ),
    ):
        MockRT.instance.return_value = _rt()
        resp = client.post(
            "/api/genesis/comms/proposals/p1/resolve",
            json={"status": "approved", "revision_num": 2},
        )
    assert resp.status_code == 409
    data = resp.get_json()
    assert data["error"] == "stale_revision"
    assert data["revision_num"] == 5


def test_comms_resolve_already_resolved_stays_404(client):
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.db.crud.ego.resolve_proposal", new_callable=AsyncMock, return_value=False),
        patch(
            "genesis.db.crud.ego.get_proposal",
            new_callable=AsyncMock,
            return_value={"id": "p1", "status": "approved", "revision_num": 2},
        ),
    ):
        MockRT.instance.return_value = _rt()
        resp = client.post(
            "/api/genesis/comms/proposals/p1/resolve",
            json={"status": "approved", "revision_num": 2},
        )
    assert resp.status_code == 404
