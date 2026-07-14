"""Tests for the container alert-queue drainer — genesis.runtime.init.alert_drain.

Covers the critical cross-module seam WARNING-2 flagged: the ``_send`` closure's
mapping of outreach status → terminal(unlink)/transient(keep), the
identity-derived topic, lazy pipeline resolution, and the unconditional ``wire``.
"""

from __future__ import annotations

import pytest

from genesis.guardian.alert import queue as q
from genesis.outreach.types import OutreachResult, OutreachStatus
from genesis.runtime.init import alert_drain


class _FakePipeline:
    """Stand-in for OutreachPipeline.submit_raw returning a scripted status."""

    def __init__(self, status: OutreachStatus) -> None:
        self._status = status
        self.calls: list = []

    async def submit_raw(self, text, request, **kw) -> OutreachResult:
        self.calls.append((text, request))
        return OutreachResult(
            outreach_id="x",
            status=self._status,
            channel="telegram",
            message_content=text,
            governance_result=None,
        )


class _RT:
    def __init__(self, pipeline=None, loop=None) -> None:
        self._outreach_pipeline = pipeline
        self._awareness_loop = loop


def _enqueue(root, **kw):
    kw.setdefault("severity", "emergency")
    kw.setdefault("source", "backup")
    kw.setdefault("title", "Backup failed")
    kw.setdefault("body", "disk full")
    q.enqueue_alert(root, **kw)


@pytest.mark.asyncio
async def test_delivered_unlinks_and_uses_identity_topic(tmp_path, monkeypatch):
    root = tmp_path / "queue"
    monkeypatch.setattr(alert_drain, "_QUEUE_ROOT", root)
    _enqueue(root, dedupe_key="backup:k1")
    pipe = _FakePipeline(OutreachStatus.DELIVERED)

    await alert_drain._make_drainer(_RT(pipeline=pipe))()

    assert len(pipe.calls) == 1
    _, req = pipe.calls[0]
    assert req.topic == "alert:backup:k1"  # identity, not bare source
    assert req.category.value == "blocker"
    assert req.signal_type == "queued_alert"
    assert q.list_queued(root) == []  # terminal → unlinked


@pytest.mark.asyncio
async def test_rejected_is_terminal_unlinks(tmp_path, monkeypatch):
    # REJECTED (outreach dedup) must NOT wedge the entry forever.
    root = tmp_path / "queue"
    monkeypatch.setattr(alert_drain, "_QUEUE_ROOT", root)
    _enqueue(root, dedupe_key="backup:k1")
    await alert_drain._make_drainer(_RT(pipeline=_FakePipeline(OutreachStatus.REJECTED)))()
    assert q.list_queued(root) == []


@pytest.mark.asyncio
async def test_failed_keeps_for_retry(tmp_path, monkeypatch):
    root = tmp_path / "queue"
    monkeypatch.setattr(alert_drain, "_QUEUE_ROOT", root)
    _enqueue(root)
    await alert_drain._make_drainer(_RT(pipeline=_FakePipeline(OutreachStatus.FAILED)))()
    assert len(q.list_queued(root)) == 1  # transient → kept


@pytest.mark.asyncio
async def test_unwired_pipeline_keeps_entry(tmp_path, monkeypatch):
    root = tmp_path / "queue"
    monkeypatch.setattr(alert_drain, "_QUEUE_ROOT", root)
    _enqueue(root)
    await alert_drain._make_drainer(_RT(pipeline=None))()  # outreach not up yet
    assert len(q.list_queued(root)) == 1  # kept, never lost


@pytest.mark.asyncio
async def test_empty_queue_is_noop(tmp_path, monkeypatch):
    root = tmp_path / "queue"
    monkeypatch.setattr(alert_drain, "_QUEUE_ROOT", root)
    # No entries — drainer runs clean, no pipeline call.
    pipe = _FakePipeline(OutreachStatus.DELIVERED)
    await alert_drain._make_drainer(_RT(pipeline=pipe))()
    assert pipe.calls == []


def test_wire_noops_without_loop():
    alert_drain.wire(_RT(loop=None))  # must not raise


def test_wire_sets_drainer_when_loop_present():
    installed = {}

    class _Loop:
        def set_alert_queue_drainer(self, fn):
            installed["fn"] = fn

    alert_drain.wire(_RT(loop=_Loop()))
    assert callable(installed.get("fn"))
