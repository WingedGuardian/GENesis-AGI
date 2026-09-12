"""PR-3: the ORIGINAL dispatch context (e.g. "ego_proposal:<id>") must survive a
rate-limit park→resume so proposal-outcome recording and dispatch follow-through
still fire on the resumed session. A resume rewrites caller_context to
"rate_limit_resume:<park_id>" (for park-lineage), so the origin rides across on a
dedicated origin_caller_context field, threaded park payload → queue → request →
metadata, and both consumers resolve back to it via effective_caller_context.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from genesis.cc.rate_limit_park import effective_caller_context

# --- the resolver (the single source of truth both consumers share) ---


def test_effective_ctx_resume_prefix_prefers_origin():
    assert (
        effective_caller_context("rate_limit_resume:park-1", "ego_proposal:X") == "ego_proposal:X"
    )


def test_effective_ctx_plain_ego_context_unchanged():
    assert effective_caller_context("ego_proposal:X", None) == "ego_proposal:X"


def test_effective_ctx_plain_context_ignores_stray_origin():
    # A non-resume live context is authoritative; a stray origin never overrides it.
    assert effective_caller_context("follow_up:F", "ego_proposal:X") == "follow_up:F"


def test_effective_ctx_resume_without_origin_falls_back_to_prefix():
    # A non-ego parked job: no origin preserved → the prefix stands (and is
    # correctly NOT treated as a proposal downstream).
    assert effective_caller_context("rate_limit_resume:park-1", None) == "rate_limit_resume:park-1"


def test_effective_ctx_none_is_none():
    assert effective_caller_context(None, None) is None


# --- request_from_payload round-trips the field (the single queue→request map) ---


def test_request_from_payload_maps_origin_caller_context():
    from genesis.runtime.init.direct_session import request_from_payload

    req = request_from_payload(
        {
            "prompt": "p",
            "caller_context": "rate_limit_resume:park-9",
            "origin_caller_context": "ego_proposal:PID",
        }
    )
    assert req.caller_context == "rate_limit_resume:park-9"
    assert req.origin_caller_context == "ego_proposal:PID"


def test_request_from_payload_legacy_payload_defaults_none():
    from genesis.runtime.init.direct_session import request_from_payload

    req = request_from_payload({"prompt": "p"})
    assert req.origin_caller_context is None


# --- enqueue serializes the field into payload_json ---


class _CaptureDB:
    """Minimal aiosqlite stand-in capturing the INSERT params."""

    def __init__(self):
        self.params = None

    async def execute(self, _sql, params):
        self.params = params

    async def commit(self):
        pass


@pytest.mark.asyncio
async def test_enqueue_serializes_origin_caller_context():
    from genesis.db.crud import direct_session_queue as dsq

    db = _CaptureDB()
    await dsq.enqueue(
        db,
        prompt="p",
        caller_context="rate_limit_resume:park-3",
        origin_caller_context="ego_proposal:PID",
    )
    payload = json.loads(db.params[1])
    assert payload["origin_caller_context"] == "ego_proposal:PID"


# --- _redispatch carries the preserved origin from the park payload ---


@pytest.mark.asyncio
async def test_redispatch_passes_origin_caller_context(monkeypatch):
    from genesis.cc import rate_limit_resume

    captured = {}

    async def _fake_enqueue(_db, **kwargs):
        captured.update(kwargs)
        return "dsq-1"

    monkeypatch.setattr(rate_limit_resume.direct_session_queue, "enqueue", _fake_enqueue)
    park = {
        "id": "park-77",
        "origin_session_id": "orig-1",
        "payload_json": json.dumps(
            {"prompt": "resume me", "origin_caller_context": "ego_proposal:PID"}
        ),
    }
    await rate_limit_resume._redispatch(object(), park)

    assert captured["caller_context"] == "rate_limit_resume:park-77"
    assert captured["origin_caller_context"] == "ego_proposal:PID"


# --- park serializes the ORIGINAL context into the fresh-park payload ---


@pytest.mark.asyncio
async def test_park_serializes_origin_caller_context(monkeypatch):
    from genesis.cc import rate_limit_park

    captured = {}

    async def _fake_upsert(_db, **kwargs):
        captured.update(kwargs)
        return "park-new"

    monkeypatch.setattr(rate_limit_park.parks, "upsert_open_park", _fake_upsert)

    req = SimpleNamespace(
        prompt="do X",
        profile="research",
        caller_context="ego_proposal:PID",
        origin_session_id="orig-1",
        source_tag="ego_dispatch",
    )
    park_id = await rate_limit_park.park_direct_session(
        None, request=req, exc=Exception("rate limited"), mode="live"
    )
    assert park_id == "park-new"
    assert captured["payload"]["origin_caller_context"] == "ego_proposal:PID"
