"""Tests for system-job scheduler control helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from genesis.dashboard.routes.scheduler import _trigger_system_job


def _rt_with(scheduler):
    """A fake runtime exposing one system scheduler component."""
    return SimpleNamespace(
        _surplus_scheduler=SimpleNamespace(_scheduler=scheduler),
        _learning_scheduler=None,
        _outreach_scheduler=None,
        _reflection_scheduler=None,
    )


def test_trigger_system_job_found_runs_now():
    """A known system job (e.g. dream_cycle) is rescheduled to run immediately."""
    sched = MagicMock()
    sched.running = True
    sched.get_job.return_value = object()  # job exists in this scheduler

    rt = _rt_with(sched)
    assert _trigger_system_job(rt, "dream_cycle") is True
    sched.modify_job.assert_called_once()
    # Triggered by setting next_run_time to "now"
    assert "next_run_time" in sched.modify_job.call_args.kwargs


def test_trigger_system_job_not_found():
    """An unknown job triggers nothing and reports not-found."""
    sched = MagicMock()
    sched.running = True
    sched.get_job.return_value = None  # job not present

    rt = _rt_with(sched)
    assert _trigger_system_job(rt, "no_such_job") is False
    sched.modify_job.assert_not_called()


def test_trigger_system_job_skips_stopped_scheduler():
    """A not-running scheduler is skipped without error."""
    sched = MagicMock()
    sched.running = False

    rt = _rt_with(sched)
    assert _trigger_system_job(rt, "dream_cycle") is False
    sched.get_job.assert_not_called()
