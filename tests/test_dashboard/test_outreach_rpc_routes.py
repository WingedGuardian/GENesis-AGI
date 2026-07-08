"""Tests for the MCP-RPC bridge routes (send_and_wait, provision/grow).

These POST routes run the synchronous outreach ops in the server process (where
the live pipeline is) on behalf of the standalone MCP subprocess.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from flask import Flask

import genesis.dashboard.routes  # noqa: F401 — registers routes on the blueprint
from genesis.dashboard._blueprint import blueprint


@pytest.fixture
def client():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    return app.test_client()


def _rt(pipeline):
    rt = MagicMock()
    rt.is_bootstrapped = True
    rt.outreach_pipeline = pipeline
    return rt


def test_send_and_wait_route_503_when_pipeline_missing(client):
    with patch("genesis.runtime.GenesisRuntime.instance", return_value=_rt(None)):
        resp = client.post("/api/genesis/outreach/send_and_wait", json={"message": "hi"})
    assert resp.status_code == 503


def test_send_and_wait_route_delivers(client):
    with patch("genesis.runtime.GenesisRuntime.instance", return_value=_rt(MagicMock())), \
         patch("genesis.outreach.rpc.send_and_wait_via_pipeline", new_callable=AsyncMock,
               return_value={"outreach_id": "o1", "status": "delivered",
                             "reply": "yes", "timed_out": False}) as impl:
        resp = client.post("/api/genesis/outreach/send_and_wait",
                           json={"message": "approve?", "timeout_seconds": 10})
    assert resp.status_code == 200
    assert resp.get_json()["reply"] == "yes"
    impl.assert_awaited_once()


def test_provision_route_503_when_pipeline_missing(client):
    with patch("genesis.runtime.GenesisRuntime.instance", return_value=_rt(None)):
        resp = client.post("/api/genesis/provision/grow", json={"kind": "disk"})
    assert resp.status_code == 503


def test_provision_route_delivers(client):
    with patch("genesis.runtime.GenesisRuntime.instance", return_value=_rt(MagicMock())), \
         patch("genesis.outreach.rpc.grow_via_pipeline", new_callable=AsyncMock,
               return_value={"ok": True, "stage": "executed"}) as impl:
        resp = client.post("/api/genesis/provision/grow",
                           json={"kind": "disk", "disk": "scsi1", "gib": 1,
                                 "timeout_seconds": 10})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    impl.assert_awaited_once()
    # args threaded from the JSON body
    assert impl.call_args.kwargs["disk"] == "scsi1"
    assert impl.call_args.kwargs["gib"] == 1
