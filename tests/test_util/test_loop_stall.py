"""Tests for the off-loop loop-stall stack sampler (Part C, diagnostic-only).

poll_once() is driven deterministically via injected seams — a fake frames
source, a fake loop-health reader, and a fake age function — so no real threads,
sleeps, or event loop are needed.
"""

from __future__ import annotations

import threading

from genesis.util.loop_stall import LoopStallSampler, run_loop_stall_sampler


class _Sample:
    """Minimal stand-in for loop_health.LoopHealthSample."""

    def __init__(self):
        self.executor = {"pending": 0, "workers": 11, "max_workers": 11}
        self.drift_ms = 2600.0


def _make(*, age_holder, sample, frames, tid=7, stall_ms=1000.0):
    return LoopStallSampler(
        loop_thread_id=tid,
        stall_ms=stall_ms,
        frames_source=lambda: frames,
        health_read=lambda: sample,
        age_fn=lambda s, **kw: age_holder["v"],
    )


def test_wedged_dumps_once_then_resets_for_next_episode():
    s = _Sample()
    age = {"v": 2.0}  # 2000ms >= 1000ms stall -> wedged
    sampler = _make(age_holder=age, sample=s, frames={7: None})

    assert sampler.poll_once() is True  # first observation of the stall -> dump
    assert sampler.poll_once() is False  # still wedged, already dumped this episode
    age["v"] = 0.1  # loop recovered
    assert sampler.poll_once() is False  # healthy -> no dump, episode resets
    age["v"] = 2.0  # a new stall
    assert sampler.poll_once() is True  # new episode -> dump again


def test_none_sample_never_dumps():
    sampler = LoopStallSampler(
        loop_thread_id=7,
        stall_ms=1000.0,
        frames_source=lambda: {},
        health_read=lambda: None,  # publisher never ran / disabled
        age_fn=lambda s, **kw: 5.0,
    )
    assert sampler.poll_once() is False


def test_below_threshold_never_dumps():
    s = _Sample()
    age = {"v": 0.2}  # 200ms < 1000ms
    sampler = _make(age_holder=age, sample=s, frames={7: None})
    assert sampler.poll_once() is False


def test_missing_loop_frame_is_safe():
    s = _Sample()
    age = {"v": 2.0}
    # The loop thread id is absent from the frames snapshot (race: loop moved on).
    sampler = _make(age_holder=age, sample=s, frames={})
    assert sampler.poll_once() is True  # dumps a placeholder, never raises


def test_invalid_stall_ms_falls_back_to_default():
    # NaN/inf/zero/negative all parse as valid floats but break the sampler
    # (inf never dumps; NaN/non-positive report a healthy heartbeat as wedged and
    # never reset). __init__ clamps them to the default. (Codex P2-4.)
    from genesis.util.loop_stall import _DEFAULT_STALL_MS

    for bad in (float("inf"), float("nan"), 0.0, -5.0):
        s = LoopStallSampler(loop_thread_id=7, stall_ms=bad)
        assert s._stall_ms == _DEFAULT_STALL_MS
    # A valid positive value is kept.
    assert LoopStallSampler(loop_thread_id=7, stall_ms=500.0)._stall_ms == 500.0


def test_runner_exits_when_stop_set():
    stop = threading.Event()
    stop.set()  # already stopped -> the runner returns without polling
    run_loop_stall_sampler(loop_thread_id=7, stop_event=stop, poll_interval_s=0.01)
