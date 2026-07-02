"""Tests for the dashboard knowledge-browser DELETE route.

Regression focus: the DELETE route must remove the Qdrant vector via
``rt.memory_store.qdrant_client`` — NOT the non-existent ``rt.qdrant_client``
(a bug present since #83 that made every delete 500) — and must then drop the
unit from the ingestion manifest (tombstone) so a fully-deleted source can be
re-ingested.

These tests use a faithful ``SimpleNamespace`` runtime stand-in rather than a
bare ``MagicMock``: a MagicMock would auto-vivify ``rt.qdrant_client`` and
silently hide the bug. SimpleNamespace raises ``AttributeError`` on attributes
the real ``GenesisRuntime`` lacks, exactly as production does.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from flask import Flask

from genesis.dashboard.api import blueprint


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


_SENTINEL = object()


def _fake_rt(*, memory_store=_SENTINEL, is_bootstrapped=True, db=_SENTINEL):
    """A faithful runtime stand-in that genuinely lacks ``qdrant_client``."""
    if memory_store is _SENTINEL:
        memory_store = MagicMock()  # .qdrant_client.delete is an auto-mock
    if db is _SENTINEL:
        db = MagicMock()
    return SimpleNamespace(
        is_bootstrapped=is_bootstrapped, db=db, memory_store=memory_store
    )


@patch("genesis.knowledge.manifest.ManifestManager")
@patch("genesis.db.crud.knowledge.delete", new_callable=AsyncMock)
@patch("genesis.db.crud.knowledge.get", new_callable=AsyncMock)
@patch("genesis.runtime.GenesisRuntime")
def test_delete_removes_qdrant_via_memory_store_and_tombstones(
    MockRT, mock_get, mock_delete, MockManifest, client
):
    """Happy path: SQLite + Qdrant (via memory_store) + manifest all handled."""
    rt = _fake_rt()
    MockRT.instance.return_value = rt
    mock_get.return_value = {"id": "u-1", "qdrant_id": "qid-1"}
    mock_delete.return_value = True
    MockManifest.return_value.remove_unit.return_value = True

    resp = client.delete("/api/genesis/knowledge/u-1")

    assert resp.status_code == 200  # buggy rt.qdrant_client code 500s here
    body = resp.get_json()
    assert body["sqlite_deleted"] is True
    assert body["qdrant_deleted"] is True
    assert body["manifest_removed"] is True
    # Qdrant deletion routed through the memory store's client + right collection.
    rt.memory_store.qdrant_client.delete.assert_called_once()
    _, kwargs = rt.memory_store.qdrant_client.delete.call_args
    assert kwargs["collection_name"] == "knowledge_base"
    # The RIGHT vector is deleted (the crux of the original bug).
    assert list(kwargs["points_selector"].points) == ["qid-1"]
    MockManifest.return_value.remove_unit.assert_called_once_with("u-1")


@patch("genesis.knowledge.manifest.ManifestManager")
@patch("genesis.db.crud.knowledge.delete", new_callable=AsyncMock)
@patch("genesis.db.crud.knowledge.get", new_callable=AsyncMock)
@patch("genesis.runtime.GenesisRuntime")
def test_delete_ok_when_memory_store_absent(
    MockRT, mock_get, mock_delete, MockManifest, client
):
    """No memory store → Qdrant delete is skipped gracefully (no 500); the
    manifest tombstone still runs."""
    MockRT.instance.return_value = _fake_rt(memory_store=None)
    mock_get.return_value = {"id": "u-1", "qdrant_id": "qid-1"}
    mock_delete.return_value = True
    MockManifest.return_value.remove_unit.return_value = True

    resp = client.delete("/api/genesis/knowledge/u-1")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["qdrant_deleted"] is False
    assert body["manifest_removed"] is True


@patch("genesis.knowledge.manifest.ManifestManager")
@patch("genesis.db.crud.knowledge.delete", new_callable=AsyncMock)
@patch("genesis.db.crud.knowledge.get", new_callable=AsyncMock)
@patch("genesis.runtime.GenesisRuntime")
def test_delete_404_when_unit_missing(
    MockRT, mock_get, mock_delete, MockManifest, client
):
    MockRT.instance.return_value = _fake_rt()
    mock_get.return_value = None
    mock_delete.return_value = False  # nothing deleted

    resp = client.delete("/api/genesis/knowledge/missing")

    assert resp.status_code == 404
    MockManifest.return_value.remove_unit.assert_not_called()


@patch("genesis.runtime.GenesisRuntime")
def test_delete_503_when_not_bootstrapped(MockRT, client):
    MockRT.instance.return_value = _fake_rt(is_bootstrapped=False, db=None)
    resp = client.delete("/api/genesis/knowledge/u-1")
    assert resp.status_code == 503
