"""Tests for the W0 graduation door: envelope validator + /v1/voice/graduate.

The route tests use the same minimal-Flask-app + daemon-thread-loop fixture
shape as ``TestVoiceAPI`` (test_voice.py); the DB layer is mocked at the crud
seam (``graduation_events.insert_event``) and the runtime at
``GenesisRuntime.instance`` — CRUD behavior itself is covered in
``tests/test_db/test_crud_graduation_events.py``.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.channels.voice.graduation import (
    EVENT_TYPES,
    PROVENANCE_CLASSES,
    SCHEMA_VERSION,
    validate_envelope,
)


def _envelope(**overrides) -> dict:
    data = {
        "event_id": "3f2a9c1e0b4d4e8f9a1b2c3d4e5f6a7b",
        "schema_version": SCHEMA_VERSION,
        "type": "memory_candidate",
        "source": "pe-livingroom",
        "occurred_at": "2026-07-17T21:00:00Z",
        "payload": {"claim": "someone mentioned a trip"},
        "provenance": {"class": "ambient_overheard"},
    }
    data.update(overrides)
    return data


# ── Envelope validator (pure) ────────────────────────────────────────


class TestValidateEnvelope:
    def test_valid_envelope_passes(self):
        assert validate_envelope(_envelope()) == []

    def test_all_event_types_and_classes_accepted(self):
        for etype in EVENT_TYPES:
            for pclass in PROVENANCE_CLASSES:
                env = _envelope(type=etype, provenance={"class": pclass})
                assert validate_envelope(env) == []

    @pytest.mark.parametrize(
        ("overrides", "fragment"),
        [
            ({"event_id": ""}, "event_id"),
            ({"event_id": 42}, "event_id"),
            ({"event_id": "x" * 200}, "128"),
            ({"schema_version": 2}, "schema_version"),
            ({"schema_version": None}, "schema_version"),
            ({"type": "unknown"}, "type"),
            ({"source": ""}, "source"),
            ({"occurred_at": "not-a-date"}, "occurred_at"),
            ({"occurred_at": None}, "occurred_at"),
            ({"payload": "text"}, "payload"),
            ({"provenance": None}, "provenance"),
            ({"provenance": {"class": "first_party"}}, "provenance.class"),
        ],
    )
    def test_each_violation_yields_its_error(self, overrides, fragment):
        errors = validate_envelope(_envelope(**overrides))
        assert errors, f"expected errors for {overrides}"
        assert any(fragment in e for e in errors)

    def test_empty_envelope_reports_all_fields(self):
        errors = validate_envelope({})
        assert len(errors) >= 6


# ── /v1/voice/graduate route ─────────────────────────────────────────


class TestVoiceGraduate:
    @pytest.fixture
    def app(self):
        from flask import Flask

        from genesis.dashboard.routes.voice_api import voice_api_bp

        app = Flask(__name__)
        app.register_blueprint(voice_api_bp)
        loop = asyncio.new_event_loop()
        app.config["GENESIS_EVENT_LOOP"] = loop
        yield app
        loop.close()

    @contextmanager
    def _running_loop(self, app):
        loop = app.config["GENESIS_EVENT_LOOP"]

        def run_loop():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        t = threading.Thread(target=run_loop, daemon=True)
        t.start()
        try:
            yield
        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(timeout=2)

    @contextmanager
    def _ready_runtime(self, insert_result=True):
        rt = MagicMock()
        rt.is_bootstrapped = True
        rt.db = object()
        insert = AsyncMock(return_value=insert_result)
        with (
            patch("genesis.runtime.GenesisRuntime") as runtime_cls,
            patch("genesis.db.crud.graduation_events.insert_event", insert),
        ):
            runtime_cls.instance.return_value = rt
            yield rt, insert

    _AUTH = {"Authorization": "Bearer secret"}
    _ENV = {"GENESIS_MCP_HTTP_TOKEN": "secret"}

    def test_accepted_and_insert_kwargs(self, app):
        with (
            patch.dict("os.environ", self._ENV, clear=False),
            self._ready_runtime() as (rt, insert),
            self._running_loop(app),
            app.test_client() as client,
        ):
            resp = client.post("/v1/voice/graduate", json=_envelope(), headers=self._AUTH)
            assert resp.status_code == 200
            assert resp.get_json() == {"status": "accepted"}

            insert.assert_awaited_once()
            kwargs = insert.await_args.kwargs
            assert insert.await_args.args == (rt.db,)
            env = _envelope()
            for field in (
                "event_id",
                "schema_version",
                "type",
                "source",
                "occurred_at",
            ):
                assert kwargs[field] == env[field]
            assert kwargs["payload"] == env["payload"]
            assert kwargs["provenance"] == env["provenance"]
            assert kwargs["received_at"]  # core-stamped, ISO string

    def test_replay_answers_duplicate(self, app):
        with (
            patch.dict("os.environ", self._ENV, clear=False),
            self._ready_runtime(insert_result=False),
            self._running_loop(app),
            app.test_client() as client,
        ):
            resp = client.post("/v1/voice/graduate", json=_envelope(), headers=self._AUTH)
            assert resp.status_code == 200
            assert resp.get_json() == {"status": "duplicate"}

    def test_invalid_envelope_rejected_before_any_dispatch(self, app):
        # No running loop on purpose — validation must answer without it.
        with (
            patch.dict("os.environ", self._ENV, clear=False),
            self._ready_runtime() as (_rt, insert),
            app.test_client() as client,
        ):
            resp = client.post(
                "/v1/voice/graduate",
                json=_envelope(schema_version=99, type="nope"),
                headers=self._AUTH,
            )
            assert resp.status_code == 400
            body = resp.get_json()
            assert body["status"] == "rejected"
            assert len(body["errors"]) == 2
            insert.assert_not_awaited()

    @pytest.mark.parametrize("body", ['[1, 2, 3]', '"hello"', "42"])
    def test_non_object_json_body_rejected(self, app, body):
        """A top-level JSON array/string/number is a 400 rejection, never a 500
        (the edge outbox would retry a 500 forever on a permanently-bad body)."""
        with (
            patch.dict("os.environ", self._ENV, clear=False),
            app.test_client() as client,
        ):
            resp = client.post(
                "/v1/voice/graduate",
                data=body,
                content_type="application/json",
                headers=self._AUTH,
            )
            assert resp.status_code == 400
            assert resp.get_json()["status"] == "rejected"

    def test_oversized_envelope_rejected(self, app):
        with (
            patch.dict("os.environ", self._ENV, clear=False),
            app.test_client() as client,
        ):
            big = _envelope(payload={"claim": "x" * (70 * 1024)})
            resp = client.post("/v1/voice/graduate", json=big, headers=self._AUTH)
            assert resp.status_code == 400
            assert "64KB" in resp.get_json()["errors"][0]

    def test_auth_required_bad_bearer(self, app):
        with (
            patch.dict("os.environ", self._ENV, clear=False),
            app.test_client() as client,
        ):
            resp = client.post(
                "/v1/voice/graduate",
                json=_envelope(),
                headers={"Authorization": "Bearer wrong"},
            )
            assert resp.status_code == 401

    def test_no_open_door_when_token_unset(self, app):
        """Unlike the pre-flip read routes, the write surface NEVER rides
        network trust — unset token means 503, not open access."""
        with (
            patch.dict("os.environ", {"GENESIS_MCP_HTTP_TOKEN": ""}, clear=False),
            app.test_client() as client,
        ):
            resp = client.post("/v1/voice/graduate", json=_envelope())
            assert resp.status_code == 503

    def test_runtime_not_ready_is_503(self, app):
        rt = MagicMock()
        rt.is_bootstrapped = False
        rt.db = None
        with (
            patch.dict("os.environ", self._ENV, clear=False),
            patch("genesis.runtime.GenesisRuntime") as runtime_cls,
            self._running_loop(app),
            app.test_client() as client,
        ):
            runtime_cls.instance.return_value = rt
            resp = client.post("/v1/voice/graduate", json=_envelope(), headers=self._AUTH)
            assert resp.status_code == 503
            assert "not ready" in resp.get_json()["error"]

    def test_loop_not_running_is_503(self, app):
        with (
            patch.dict("os.environ", self._ENV, clear=False),
            app.test_client() as client,
        ):
            resp = client.post("/v1/voice/graduate", json=_envelope(), headers=self._AUTH)
            assert resp.status_code == 503
            assert "loop" in resp.get_json()["error"].lower()
