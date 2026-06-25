"""WS-7 / D12: the dashboard memory-search API exposes provenance.

Each result carries its collection + a first-party/external provenance label so
the UI can distinguish (and badge) external-world KB from Genesis's own memory.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from flask import Flask

from genesis.dashboard.api import blueprint
from genesis.memory.types import RetrievalResult


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    return app.test_client()


def _result(mid: str, *, collection: str, pipeline: str | None = None):
    return RetrievalResult(
        memory_id=mid, content="some content", source="api.pdf",
        memory_type="knowledge" if collection == "knowledge_base" else "episodic",
        score=0.9, vector_rank=1, fts_rank=None, activation_score=0.5,
        payload={}, source_pipeline=pipeline, collection=collection,
    )


def test_memory_search_exposes_provenance(client):
    rt = MagicMock()
    rt.is_bootstrapped = True
    rt.db = MagicMock()
    rt.hybrid_retriever = MagicMock()
    rt.hybrid_retriever.recall = AsyncMock(return_value=[
        _result("kb1", collection="knowledge_base", pipeline="curated"),
        _result("ep1", collection="episodic_memory"),
    ])

    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = rt
        resp = client.get("/api/genesis/memory/search?q=fastapi")

    assert resp.status_code == 200
    by_id = {it["memory_id"]: it for it in resp.get_json()["results"]}
    assert by_id["kb1"]["collection"] == "knowledge_base"
    assert by_id["kb1"]["provenance"].startswith("external-world knowledge")
    assert by_id["ep1"]["provenance"] == "first-party memory"
