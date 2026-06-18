"""Tests for the dashboard reference-store browser routes.

Critical invariants:
- list / search / detail NEVER include the secret value (only reveal does).
- reveal is auth-gated; with DASHBOARD_PASSWORD set, no session → 403.
- delete routes through the shared reference delete helper and refuses
  non-reference units (ValueError → 400).

Fake creds only (no real secrets).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from flask import Flask

from genesis.dashboard.api import blueprint

# A canonical reference body with a recognizable fake secret + description.
_SECRET = "admin / Hunter2!xyz"
_BODY = (
    "[reference.credentials] Lab box login\n\n"
    "SSH login for the lab box\n\n"
    f"Value: {_SECRET}\n"
    "Tags: lab, ssh\n"
    "Captured: via=manual"
)


@pytest.fixture()
def app():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    app.secret_key = "test-secret-key"  # needed for session tests
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


def _mock_rt(**kw):
    rt = MagicMock()
    rt.is_bootstrapped = True
    rt.db = MagicMock()
    rt.memory_store = AsyncMock()
    for k, v in kw.items():
        setattr(rt, k, v)
    return rt


def _ref_row(**over):
    row = {
        "id": "ref-1",
        "concept": "Lab box login",
        "domain": "reference.credentials",
        "tags": '["reference", "credentials", "lab"]',
        "ingested_at": "2026-06-17T00:00:00Z",
        "source_pipeline": "reference_store",
        "confidence": 0.85,
        "body": _BODY,
        "project_type": "reference",
        "qdrant_id": "qid-1",
    }
    row.update(over)
    return row


# ── list ──────────────────────────────────────────────────────────────────


def test_list_groups_by_kind_and_hides_value(client):
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.db.crud.knowledge.list_by_domain",
            new=AsyncMock(return_value={"reference.credentials": [_ref_row()]}),
        ),
    ):
        MockRT.instance.return_value = _mock_rt()
        resp = client.get("/api/genesis/references/list")

    assert resp.status_code == 200
    raw = resp.get_data(as_text=True)
    assert _SECRET not in raw  # value must never appear in list
    data = resp.get_json()
    assert data["total"] == 1
    entry = data["by_kind"]["credentials"][0]
    assert entry["concept"] == "Lab box login"
    assert entry["kind"] == "credentials"
    assert entry["description"] == "SSH login for the lab box"
    assert entry["source_pipeline"] == "reference_store"
    assert "value" not in entry and "body" not in entry


def test_list_503_when_not_bootstrapped(client):
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = _mock_rt(is_bootstrapped=False, db=None)
        resp = client.get("/api/genesis/references/list")
    assert resp.status_code == 503


def test_credential_value_in_description_is_masked(client):
    # A credential whose secret also appears in the free-text description must
    # be masked in the non-reveal summary (the pre-existing mirror-redaction gap).
    body = (
        "[reference.credentials] Junk\n\n"
        "the password pw / secret was seen in prose\n\n"
        "Value: pw / secret\n"
        "Tags: x"
    )
    row = _ref_row(body=body, domain="reference.credentials")
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.db.crud.knowledge.list_by_domain",
            new=AsyncMock(return_value={"reference.credentials": [row]}),
        ),
    ):
        MockRT.instance.return_value = _mock_rt()
        resp = client.get("/api/genesis/references/list")
    assert resp.status_code == 200
    assert "pw / secret" not in resp.get_data(as_text=True)  # secret masked in description
    entry = resp.get_json()["by_kind"]["credentials"][0]
    assert "[hidden" in entry["description"]


def test_network_value_in_description_not_masked(client):
    # IPs/URLs are identifiers, not secrets — left readable in their own description.
    body = (
        "[reference.network] Host\n\n"
        "Lab host at 203.0.113.10 on the subnet\n\n"
        "Value: 203.0.113.10"
    )
    row = _ref_row(body=body, domain="reference.network", source_pipeline="extraction_job")
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.db.crud.knowledge.list_by_domain",
            new=AsyncMock(return_value={"reference.network": [row]}),
        ),
    ):
        MockRT.instance.return_value = _mock_rt()
        resp = client.get("/api/genesis/references/list")
    entry = resp.get_json()["by_kind"]["network"][0]
    assert "203.0.113.10" in entry["description"]


# ── search ──────────────────────────────────────────────────────────────────


def test_search_scopes_to_references_and_hides_value(client):
    fts_row = _ref_row(unit_id="ref-1")
    fts_row.pop("id")
    sf = AsyncMock(return_value=[fts_row])
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.db.crud.knowledge.search_fts", new=sf),
    ):
        MockRT.instance.return_value = _mock_rt()
        resp = client.get("/api/genesis/references/search?q=lab&kind=credentials")

    assert resp.status_code == 200
    assert _SECRET not in resp.get_data(as_text=True)
    # scoped to project=reference and domain=reference.credentials
    _, kwargs = sf.call_args
    assert kwargs["project"] == "reference"
    assert kwargs["domain"] == "reference.credentials"
    data = resp.get_json()
    assert data["count"] == 1
    assert data["results"][0]["id"] == "ref-1"


def test_search_requires_query(client):
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = _mock_rt()
        resp = client.get("/api/genesis/references/search?q=")
    assert resp.status_code == 400


# ── detail ──────────────────────────────────────────────────────────────────


def test_detail_returns_description_no_value(client):
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.db.crud.knowledge.get", new=AsyncMock(return_value=_ref_row())),
    ):
        MockRT.instance.return_value = _mock_rt()
        resp = client.get("/api/genesis/references/ref-1")

    assert resp.status_code == 200
    assert _SECRET not in resp.get_data(as_text=True)
    ref = resp.get_json()["reference"]
    assert ref["description"] == "SSH login for the lab box"
    assert "value" not in ref


def test_detail_404_for_non_reference(client):
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.db.crud.knowledge.get",
            new=AsyncMock(return_value=_ref_row(project_type="knowledge")),
        ),
    ):
        MockRT.instance.return_value = _mock_rt()
        resp = client.get("/api/genesis/references/ref-1")
    assert resp.status_code == 404


# ── reveal ──────────────────────────────────────────────────────────────────


def test_reveal_returns_value(client):
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.db.crud.knowledge.get", new=AsyncMock(return_value=_ref_row())),
    ):
        MockRT.instance.return_value = _mock_rt()
        resp = client.post("/api/genesis/references/ref-1/reveal")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["value"] == _SECRET
    assert data["id"] == "ref-1"


def test_reveal_404_for_non_reference(client):
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.db.crud.knowledge.get",
            new=AsyncMock(return_value=_ref_row(project_type="knowledge")),
        ),
    ):
        MockRT.instance.return_value = _mock_rt()
        resp = client.post("/api/genesis/references/ref-1/reveal")
    assert resp.status_code == 404


# ── delete ──────────────────────────────────────────────────────────────────


def test_delete_ok(client):
    dre = AsyncMock(return_value=True)
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.dashboard.routes.references.delete_reference_entry", new=dre),
    ):
        MockRT.instance.return_value = _mock_rt()
        resp = client.delete("/api/genesis/references/ref-1")

    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok", "deleted": True}
    args, _ = dre.call_args
    assert args[2] == "ref-1"  # (db, store, unit_id)


def test_delete_404_when_missing(client):
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.dashboard.routes.references.delete_reference_entry",
            new=AsyncMock(return_value=False),
        ),
    ):
        MockRT.instance.return_value = _mock_rt()
        resp = client.delete("/api/genesis/references/ref-1")
    assert resp.status_code == 404


def test_delete_400_for_non_reference(client):
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.dashboard.routes.references.delete_reference_entry",
            new=AsyncMock(side_effect=ValueError("not a reference entry")),
        ),
    ):
        MockRT.instance.return_value = _mock_rt()
        resp = client.delete("/api/genesis/references/ref-1")
    assert resp.status_code == 400


# ── stats + kinds ─────────────────────────────────────────────────────────


def test_stats_scopes_to_references(client):
    st = AsyncMock(return_value={"total": 5, "by_domain": {}, "by_tier": {}})
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.db.crud.knowledge.stats", new=st),
    ):
        MockRT.instance.return_value = _mock_rt()
        resp = client.get("/api/genesis/references/stats")
    assert resp.status_code == 200
    assert resp.get_json()["total"] == 5
    _, kwargs = st.call_args
    assert kwargs["project"] == "reference"


def test_kinds_returns_sorted_list(client):
    resp = client.get("/api/genesis/references/kinds")
    assert resp.status_code == 200
    kinds = resp.get_json()["kinds"]
    assert kinds == sorted(kinds)
    assert "credentials" in kinds and "url" in kinds


# ── auth gate (the deliberate exception to the /api bypass) ──────────────────


def test_reveal_403_when_password_set_no_session(client):
    # With a dashboard password configured and no authenticated session,
    # the explicit is_authenticated() gate must block reveal.
    with patch(
        "genesis.dashboard.auth.get_dashboard_password", return_value="secret",
    ):
        resp = client.post("/api/genesis/references/ref-1/reveal")
    assert resp.status_code == 403


def test_reveal_200_when_password_set_with_session(client):
    with client.session_transaction() as sess:
        sess["authenticated"] = True
    with (
        patch("genesis.dashboard.auth.get_dashboard_password", return_value="secret"),
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.db.crud.knowledge.get", new=AsyncMock(return_value=_ref_row())),
    ):
        MockRT.instance.return_value = _mock_rt()
        resp = client.post("/api/genesis/references/ref-1/reveal")
    assert resp.status_code == 200
    assert resp.get_json()["value"] == _SECRET


def test_list_403_when_password_set_no_session(client):
    with patch(
        "genesis.dashboard.auth.get_dashboard_password", return_value="secret",
    ):
        resp = client.get("/api/genesis/references/list")
    assert resp.status_code == 403
