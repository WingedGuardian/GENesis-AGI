"""Tests for the attention shadow-review dashboard routes.

Critical invariants:
- list / stats NEVER include transcript text (only reveal-text does, from the snapshot).
- reveal-text is auth-gated; with DASHBOARD_PASSWORD set, no session -> 403.
- reveal-text degrades to 410 when the snapshot is purged, 404 when the event is missing.
- label validates the signal (bad -> 400) and 404s a missing event.
"""

from __future__ import annotations

import json
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


def _mock_rt(**kw):
    rt = MagicMock()
    rt.is_bootstrapped = True
    rt.db = MagicMock()
    for k, v in kw.items():
        setattr(rt, k, v)
    return rt


def _evrow(**over):
    row = {
        "id": "20260701T013412Z:0.1.0-default:12",
        "ts": "2026-07-01T00:00:03+00:00",
        "session_id": "s12",
        "activation": "soft",
        "score": 0.62,
        "clarity": 0.9,
        "mode_state": "unknown",
        "triggers_fired": json.dumps([{"name": "multi_speaker", "kind": "soft", "contribution": 0.4}]),
        "suppressors": json.dumps([]),
        "window_ref": json.dumps({
            "snapshot_id": "20260701T013412Z", "session_id": "s12",
            "utt_ids": [10, 11, 12], "ts_start": 1.0, "ts_end": 3.0,
        }),
        "l15_verdict": None,
        "acceptance_signal": None,
        "snapshot_id": "20260701T013412Z",
        "config_version": "0.1.0-default",
        "created_at": "2026-07-01T00:00:04+00:00",
    }
    row.update(over)
    return row


# ── list ──────────────────────────────────────────────────────────────────

def test_list_returns_parsed_events_no_text(client):
    le = AsyncMock(return_value=[_evrow()])
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.db.crud.attention.list_events", new=le),
    ):
        MockRT.instance.return_value = _mock_rt()
        resp = client.get("/api/genesis/attention/list?unlabeled=true&trigger=multi_speaker&is_user=true")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["count"] == 1
    ev = data["events"][0]
    assert ev["activation"] == "soft"
    assert ev["triggers_fired"][0]["name"] == "multi_speaker"   # JSON parsed
    assert ev["window_ref"]["utt_ids"] == [10, 11, 12]          # refs only
    assert "text" not in json.dumps(ev)                          # no transcript text anywhere
    # filters threaded through to the crud call
    _, kwargs = le.call_args
    assert kwargs["unlabeled"] is True and kwargs["trigger"] == "multi_speaker" and kwargs["is_user"] is True


def test_list_503_when_not_bootstrapped(client):
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = _mock_rt(is_bootstrapped=False, db=None)
        resp = client.get("/api/genesis/attention/list")
    assert resp.status_code == 503


# ── stats ─────────────────────────────────────────────────────────────────

def test_stats_aggregates(client):
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.db.crud.attention.label_counts",
              new=AsyncMock(return_value={"total": 3, "labeled": 1, "unlabeled": 2, "by_signal": {"should": 1}})),
        patch("genesis.db.crud.attention.activation_stats", new=AsyncMock(return_value={"soft": 2, "hard": 1})),
        patch("genesis.db.crud.attention.trigger_stats", new=AsyncMock(return_value={"multi_speaker": 2})),
        patch("genesis.db.crud.attention.suppressor_stats", new=AsyncMock(return_value={})),
    ):
        MockRT.instance.return_value = _mock_rt()
        resp = client.get("/api/genesis/attention/stats")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["labels"]["unlabeled"] == 2
    assert data["by_trigger"]["multi_speaker"] == 2


# ── reveal-text ───────────────────────────────────────────────────────────

def test_reveal_returns_window_text(client):
    window = [{"id": 12, "ts": "2026-07-01T00:00:03+00:00", "text": "what do you think?",
               "speaker_label": "w1:1/2", "is_user": 1, "is_trigger": True}]
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.db.crud.attention.get_event", new=AsyncMock(return_value=_evrow())),
        patch("genesis.attention.sources.resolve_window_text", new=AsyncMock(return_value=window)),
    ):
        MockRT.instance.return_value = _mock_rt()
        resp = client.post("/api/genesis/attention/evt-1/reveal-text")
    assert resp.status_code == 200
    assert resp.get_json()["window"][0]["text"] == "what do you think?"


def test_reveal_410_when_snapshot_purged(client):
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.db.crud.attention.get_event", new=AsyncMock(return_value=_evrow())),
        patch("genesis.attention.sources.resolve_window_text", new=AsyncMock(return_value=None)),
    ):
        MockRT.instance.return_value = _mock_rt()
        resp = client.post("/api/genesis/attention/evt-1/reveal-text")
    assert resp.status_code == 410


def test_reveal_404_when_event_missing(client):
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.db.crud.attention.get_event", new=AsyncMock(return_value=None)),
    ):
        MockRT.instance.return_value = _mock_rt()
        resp = client.post("/api/genesis/attention/evt-1/reveal-text")
    assert resp.status_code == 404


# ── label ─────────────────────────────────────────────────────────────────

def test_label_ok_returns_prior(client):
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.db.crud.attention.update_acceptance_signal",
              new=AsyncMock(return_value=(True, "should"))),
    ):
        MockRT.instance.return_value = _mock_rt()
        resp = client.post("/api/genesis/attention/evt-1/label", json={"acceptance_signal": "shouldnt"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data == {"status": "ok", "id": "evt-1", "acceptance_signal": "shouldnt", "prior": "should"}


def test_label_400_on_invalid_signal(client):
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.db.crud.attention.update_acceptance_signal",
              new=AsyncMock(side_effect=ValueError("invalid acceptance_signal 'maybe'"))),
    ):
        MockRT.instance.return_value = _mock_rt()
        resp = client.post("/api/genesis/attention/evt-1/label", json={"acceptance_signal": "maybe"})
    assert resp.status_code == 400


def test_label_404_when_missing(client):
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.db.crud.attention.update_acceptance_signal",
              new=AsyncMock(return_value=(False, None))),
    ):
        MockRT.instance.return_value = _mock_rt()
        resp = client.post("/api/genesis/attention/evt-1/label", json={"acceptance_signal": "skip"})
    assert resp.status_code == 404


# ── auth gate (deliberate exception to the /api bypass, mirrors references) ──

def test_list_403_when_password_set_no_session(client):
    with patch("genesis.dashboard.auth.get_dashboard_password", return_value="secret"):
        resp = client.get("/api/genesis/attention/list")
    assert resp.status_code == 403


def test_reveal_403_when_password_set_no_session(client):
    with patch("genesis.dashboard.auth.get_dashboard_password", return_value="secret"):
        resp = client.post("/api/genesis/attention/evt-1/reveal-text")
    assert resp.status_code == 403
