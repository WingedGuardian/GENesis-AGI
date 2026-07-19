"""Tests for POST /v1/voice/conversation (W0.5 extraction parity landing).

Same minimal-Flask-app + daemon-thread-loop fixture shape as
``TestVoiceGraduate``; the writer is mocked at the ``get_shared_writer``
seam — writer behavior itself is covered in
``tests/test_channels/test_voice_transcript_writer.py``.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _body(n_turns: int = 2, **overrides) -> dict:
    data = {
        "session_id": "edge-cache-0001",
        "satellite_id": "pe-livingroom",
        "turns": [
            {"role": "user" if i % 2 == 0 else "assistant", "text": f"turn {i}"}
            for i in range(n_turns)
        ],
    }
    data.update(overrides)
    return data


class TestVoiceConversation:
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
    def _ready_runtime(self, appended=2):
        rt = MagicMock()
        rt.is_bootstrapped = True
        rt.db = object()
        writer = MagicMock()
        writer.sync_cumulative = AsyncMock(return_value=appended)
        get_writer = AsyncMock(return_value=writer)
        with (
            patch("genesis.runtime.GenesisRuntime") as runtime_cls,
            patch(
                "genesis.channels.voice.transcript_writer.get_shared_writer",
                get_writer,
            ),
        ):
            runtime_cls.instance.return_value = rt
            yield rt, writer

    _AUTH = {"Authorization": "Bearer secret"}
    _ENV = {"GENESIS_MCP_HTTP_TOKEN": "secret"}

    def test_ok_and_writer_args(self, app):
        with (
            patch.dict("os.environ", self._ENV, clear=False),
            self._ready_runtime() as (_rt, writer),
            self._running_loop(app),
            app.test_client() as client,
        ):
            body = _body(4)
            resp = client.post("/v1/voice/conversation", json=body, headers=self._AUTH)
            assert resp.status_code == 200
            assert resp.get_json() == {"status": "ok", "appended": 2}
            writer.sync_cumulative.assert_awaited_once_with(
                body["session_id"],
                body["turns"],
            )

    def test_replay_returns_zero_appended(self, app):
        with (
            patch.dict("os.environ", self._ENV, clear=False),
            self._ready_runtime(appended=0),
            self._running_loop(app),
            app.test_client() as client,
        ):
            resp = client.post("/v1/voice/conversation", json=_body(), headers=self._AUTH)
            assert resp.status_code == 200
            assert resp.get_json() == {"status": "ok", "appended": 0}

    def test_invalid_body_rejected_before_any_dispatch(self, app):
        # No running loop on purpose — validation must answer without it.
        with (
            patch.dict("os.environ", self._ENV, clear=False),
            self._ready_runtime() as (_rt, writer),
            app.test_client() as client,
        ):
            resp = client.post(
                "/v1/voice/conversation",
                json=_body(turns=[{"role": "narrator", "text": "hi"}]),
                headers=self._AUTH,
            )
            assert resp.status_code == 400
            assert resp.get_json()["status"] == "rejected"
            writer.sync_cumulative.assert_not_awaited()

    @pytest.mark.parametrize("body", ["[1, 2, 3]", '"hello"', "42"])
    def test_non_object_json_body_rejected(self, app, body):
        with (
            patch.dict("os.environ", self._ENV, clear=False),
            app.test_client() as client,
        ):
            resp = client.post(
                "/v1/voice/conversation",
                data=body,
                content_type="application/json",
                headers=self._AUTH,
            )
            assert resp.status_code == 400
            assert resp.get_json()["status"] == "rejected"

    def test_oversized_body_rejected(self, app):
        with (
            patch.dict("os.environ", self._ENV, clear=False),
            app.test_client() as client,
        ):
            big = _body(turns=[{"role": "user", "text": "x" * (270 * 1024)}])
            resp = client.post("/v1/voice/conversation", json=big, headers=self._AUTH)
            assert resp.status_code == 400
            assert "256KB" in resp.get_json()["errors"][0]

    def test_auth_required_bad_bearer(self, app):
        with (
            patch.dict("os.environ", self._ENV, clear=False),
            app.test_client() as client,
        ):
            resp = client.post(
                "/v1/voice/conversation",
                json=_body(),
                headers={"Authorization": "Bearer wrong"},
            )
            assert resp.status_code == 401

    def test_fail_closed_when_token_unset(self, app):
        with (
            patch.dict("os.environ", {"GENESIS_MCP_HTTP_TOKEN": ""}, clear=False),
            app.test_client() as client,
        ):
            resp = client.post("/v1/voice/conversation", json=_body())
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
            resp = client.post("/v1/voice/conversation", json=_body(), headers=self._AUTH)
            assert resp.status_code == 503
            assert "not ready" in resp.get_json()["error"]

    def test_loop_not_running_is_503(self, app):
        with (
            patch.dict("os.environ", self._ENV, clear=False),
            app.test_client() as client,
        ):
            resp = client.post("/v1/voice/conversation", json=_body(), headers=self._AUTH)
            assert resp.status_code == 503
