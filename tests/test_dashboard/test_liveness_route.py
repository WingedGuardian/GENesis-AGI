"""GET /api/genesis/liveness — the off-loop, sync liveness probe.

The probe exists to answer *is the loop starved, or is the process dead?* during
an incident, when every ``@_async_route`` health endpoint hangs on the starved
loop. Its load-bearing property is that it is a SYNC route: Flask serves it on
its own worker thread, so it answers even when the runtime event loop is not
running. The regression guard below (a configured-but-stopped loop still returns
200) is the RED test — an ``@_async_route`` version 503s in that exact case.
"""

from __future__ import annotations

import asyncio

from flask import Flask

import genesis.dashboard.routes.health  # noqa: F401 - registers routes on the blueprint
from genesis.dashboard._blueprint import blueprint
from genesis.util import loop_health


def _app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    return app


def _sample(**over) -> loop_health.LoopHealthSample:
    base = dict(
        drift_ms=1234.5,
        peak_ms=1500.0,
        lagging=True,
        threshold_ms=250.0,
        executor={"pending": 7, "workers": 11, "max_workers": 11},
        sampled_monotonic=100.0,
    )
    base.update(over)
    return loop_health.LoopHealthSample(**base)


def test_loop_null_when_no_sample(monkeypatch):
    """No sample ever published → loop is null (UNKNOWN), never a healthy default."""
    monkeypatch.setattr(loop_health, "_latest", None)
    with _app().test_client() as c:
        resp = c.get("/api/genesis/liveness")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["alive"] is True
    assert body["loop"] is None


def test_reports_sample_with_age_at_read_time(monkeypatch):
    monkeypatch.setattr(loop_health, "_latest", _sample())
    # age_s is computed at READ time from monotonic — pin it deterministically.
    monkeypatch.setattr(loop_health.time, "monotonic", lambda: 103.0)  # 100.0 → 3.0s
    with _app().test_client() as c:
        body = c.get("/api/genesis/liveness").get_json()
    loop = body["loop"]
    assert loop["lag_ms"] == 1234.5
    assert loop["lagging"] is True
    assert loop["sample_age_s"] == 3.0
    assert loop["executor"]["pending"] == 7


def test_answers_200_with_configured_but_stopped_loop(monkeypatch):
    """RED guard: a configured-but-NOT-running loop still gets 200 — proving the
    route is SYNC and never bounces onto the loop. An @_async_route would 503
    here (see test_async_route_configured_but_stopped_loop_returns_503)."""
    monkeypatch.setattr(loop_health, "_latest", None)
    app = _app()
    loop = asyncio.new_event_loop()  # configured, NOT running (starvation proxy)
    app.config["GENESIS_EVENT_LOOP"] = loop
    try:
        with app.test_client() as c:
            resp = c.get("/api/genesis/liveness")
        assert resp.status_code == 200
        assert resp.get_json()["alive"] is True
    finally:
        loop.close()


def test_awareness_null_when_runtime_unavailable(monkeypatch):
    """A failure reading the awareness loop degrades to awareness:null, still 200."""
    monkeypatch.setattr(loop_health, "_latest", None)

    def _boom():
        raise RuntimeError("no runtime")

    monkeypatch.setattr("genesis.runtime.GenesisRuntime.peek", _boom)
    with _app().test_client() as c:
        resp = c.get("/api/genesis/liveness")
    assert resp.status_code == 200
    assert resp.get_json()["awareness"] is None
