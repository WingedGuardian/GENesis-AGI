"""Tests for the dedicated OMI ingest Flask service (the only exposed surface).

Driven through Flask's ``test_client`` with every dependency injected: a tmp state
store, a controllable clock, and a capturing ``write_rows`` (no real disk / no
engine). Asserts the auth matrix, the OMI-shaped error policy (NEVER 5xx/429
post-auth; the response NEVER carries a ``message`` key — that would push-notify the
user's phone), delivery + segment dedup, buffers-only, and the heartbeat.
"""
import json
import logging

import pytest

from genesis.attention.omi_ingest import OmiConfig, create_app
from genesis.attention.omi_state import OmiState

TOKEN = "primary-secret-token"
PREV = "previous-secret-token"
UID = "acct-uid"


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _seg(sid, text="hello there world", start=0.0, end=2.0, **kw):
    return {"id": sid, "text": text, "speaker": "SPEAKER_0", "start": start, "end": end, **kw}


@pytest.fixture()
def ctx(tmp_path):
    state = OmiState(tmp_path / "omi_state.db")
    clock = Clock()
    written = []

    def write_rows(rows, recv_ts):
        written.extend((recv_ts, r) for r in rows)
        return len(rows)

    cfg = OmiConfig(
        enabled=True, port=8799, uid_allowlist=frozenset({UID}), anchor_tolerance_s=60.0
    )
    app = create_app(
        config=cfg,
        tokens=(TOKEN, PREV),
        state=state,
        write_rows=write_rows,
        now_fn=clock,
        heartbeat_path=tmp_path / "hb.json",
    )
    app.testing = True
    yield type("Ctx", (), {
        "client": app.test_client(), "state": state, "clock": clock,
        "written": written, "hb": tmp_path / "hb.json",
    })()
    state.close()


def _post(ctx, payload, *, token=TOKEN, uid=UID, idem=None, **kw):
    headers = {"Idempotency-Key": idem} if idem else {}
    return ctx.client.post(
        f"/omi/{token}/ingest?uid={uid}", json=payload, headers=headers, **kw
    )


def _no_message(resp):
    body = resp.get_json(silent=True)
    assert body is None or "message" not in body, f"response carries a message key: {body}"


# ── auth matrix ────────────────────────────────────────────────────────────
def test_correct_token_accepts(ctx):
    r = _post(ctx, {"segments": [_seg("s1")], "session_id": UID})
    assert r.status_code == 200
    assert r.get_json()["accepted"] == 1
    _no_message(r)


def test_previous_token_accepts(ctx):
    r = _post(ctx, {"segments": [_seg("s1")]}, token=PREV)
    assert r.status_code == 200
    _no_message(r)


def test_wrong_token_401(ctx):
    r = _post(ctx, {"segments": [_seg("s1")]}, token="nope")
    assert r.status_code == 401
    _no_message(r)


def test_uid_not_allowlisted_403(ctx):
    r = _post(ctx, {"segments": [_seg("s1")]}, uid="stranger")
    assert r.status_code == 403
    _no_message(r)


# ── body handling / error policy ───────────────────────────────────────────
def test_non_json_body_400(ctx):
    r = ctx.client.post(
        f"/omi/{TOKEN}/ingest?uid={UID}", data="not json at all", content_type="text/plain"
    )
    assert r.status_code == 400
    _no_message(r)


def test_oversize_body_413(ctx):
    big = b'{"segments": [' + b'0,' * 200_000 + b']}'  # > 256 KB
    r = ctx.client.post(
        f"/omi/{TOKEN}/ingest?uid={UID}", data=big, content_type="application/json"
    )
    assert r.status_code == 413
    _no_message(r)


def test_parseable_but_empty_is_accepted_zero(ctx):
    for payload in ({}, {"segments": []}, {"foo": "bar"}, {"segments": [{"text": "  "}]}):
        r = _post(ctx, payload)
        assert r.status_code == 200
        assert r.get_json()["accepted"] == 0


def test_bare_array_payload_accepted(ctx):
    r = _post(ctx, [_seg("s1"), _seg("s2", start=3.0, end=4.0)])
    assert r.status_code == 200
    assert r.get_json()["accepted"] == 2


# ── dedup ──────────────────────────────────────────────────────────────────
def test_idempotency_key_retry_untouched(ctx):
    p = {"segments": [_seg("s1")], "session_id": UID}
    r1 = _post(ctx, p, idem="delivery-1")
    assert r1.get_json()["accepted"] == 1
    anchor_after_first = ctx.state.get_anchor(UID)
    r2 = _post(ctx, p, idem="delivery-1")  # same key -> pure retry
    assert r2.get_json()["accepted"] == 0
    assert ctx.state.get_anchor(UID) == anchor_after_first  # state untouched
    assert len(ctx.written) == 1  # no second write


def test_segment_uuid_dedup(ctx):
    _post(ctx, {"segments": [_seg("s1")]}, idem="d1")
    r = _post(ctx, {"segments": [_seg("s1"), _seg("s2", start=3.0, end=4.0)]}, idem="d2")
    assert r.get_json()["accepted"] == 1  # s1 already seen, only s2 inserted


# ── internal error → 200 (never spend OMI's 5xx failure budget) ────────────
def test_internal_error_returns_200(tmp_path):
    state = OmiState(tmp_path / "s.db")
    cfg = OmiConfig(enabled=True, port=8799, uid_allowlist=frozenset({UID}), anchor_tolerance_s=60.0)

    def boom(rows, recv_ts):
        raise RuntimeError("disk exploded")

    app = create_app(config=cfg, tokens=(TOKEN, ""), state=state, write_rows=boom,
                     now_fn=Clock(), heartbeat_path=tmp_path / "hb.json")
    app.testing = False  # let the route's own catch-all handle it, not the test reraise
    r = app.test_client().post(f"/omi/{TOKEN}/ingest?uid={UID}", json={"segments": [_seg("s1")]})
    assert r.status_code == 200
    assert r.get_json()["accepted"] == 0
    state.close()


# ── buffers-only: the engine is never invoked from the ingest path ─────────
def test_buffers_only_engine_never_called(ctx, monkeypatch):
    import genesis.attention.engine as engine

    called = []
    monkeypatch.setattr(engine, "evaluate", lambda *a, **k: called.append(1), raising=False)
    _post(ctx, {"segments": [_seg("s1")]})
    assert called == []


# ── anchoring through the service ──────────────────────────────────────────
def test_reanchor_across_conversations(ctx):
    # Conversation A at recv=1_000_000, ends ~2s in -> ts ~= recv.
    ctx.clock.t = 1_000_000.0
    _post(ctx, {"segments": [_seg("a1", start=0.0, end=2.0)]}, idem="a")
    # Conversation B hours later; OMI's start/end reset to ~0 -> must re-anchor.
    ctx.clock.t = 1_050_000.0
    _post(ctx, {"segments": [_seg("b1", start=0.0, end=1.0)]}, idem="b")
    # The second row's ts must reflect the NEW wall clock, not conversation A's.
    _, row_b = ctx.written[-1]
    from datetime import datetime
    ts_epoch = datetime.fromisoformat(row_b.ts).timestamp()
    assert abs(ts_epoch - 1_050_000.0) < 5  # anchored to recv, not ~1_000_000


# ── heartbeat + log hygiene ────────────────────────────────────────────────
def test_heartbeat_written(ctx):
    _post(ctx, {"segments": [_seg("s1")]})
    hb = json.loads(ctx.hb.read_text())
    assert hb["accepted_total"] == 1
    assert hb["last_recv_ts"] == ctx.clock.t
    assert set(hb) >= {"started_at", "last_recv_ts", "accepted_total", "auth_failures", "internal_errors"}


def test_auth_failure_counted(ctx):
    _post(ctx, {"segments": [_seg("s1")]}, token="wrong")
    hb = json.loads(ctx.hb.read_text())
    assert hb["auth_failures"] == 1


def test_werkzeug_logger_silenced(ctx):
    # INFO access log would journal the secret path-token on every request.
    assert logging.getLogger("werkzeug").level == logging.WARNING
