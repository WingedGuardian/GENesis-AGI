"""Tests for the shared surplus job guard (genesis.surplus.jobs._guard).

Locks the uniform job protocol the decorator encodes: success recording,
exception swallowing with log-parity, the SKIP sentinel for
disabled-by-config paths, and the swallow-safety of the record helpers.
Job-health SQL persistence is owned by tests/test_runtime/test_job_health.py;
dispatch-level failure observation by test_dispatch.py — not re-covered here.
"""

import inspect
import logging
from unittest.mock import MagicMock, patch

from genesis.surplus.jobs._guard import (
    SKIP,
    job_guard,
    record_failure,
    record_success,
)


def _mock_runtime():
    """Patch genesis.runtime.GenesisRuntime; returns (patcher, mock_rt)."""
    patcher = patch("genesis.runtime.GenesisRuntime")
    mock_cls = patcher.start()
    mock_rt = MagicMock()
    mock_cls.instance.return_value = mock_rt
    return patcher, mock_rt


# ── decorator: the uniform protocol ─────────────────────────────────

async def test_success_records_once_and_propagates_return():
    sentinel = object()

    @job_guard("some_job", "Some job failed")
    async def body():
        return sentinel

    patcher, mock_rt = _mock_runtime()
    try:
        result = await body()
    finally:
        patcher.stop()

    assert result is sentinel
    mock_rt.record_job_success.assert_called_once_with("some_job")
    mock_rt.record_job_failure.assert_not_called()


async def test_exception_swallowed_logged_and_recorded(caplog):
    @job_guard("some_job", "Some job failed")
    async def body():
        raise RuntimeError("boom")

    patcher, mock_rt = _mock_runtime()
    try:
        with caplog.at_level(logging.ERROR):
            result = await body()  # must NOT raise
    finally:
        patcher.stop()

    assert result is None
    mock_rt.record_job_failure.assert_called_once_with("some_job", "boom")
    mock_rt.record_job_success.assert_not_called()
    # Log-parity: logger.exception(fail_log) under the body's module logger.
    err = [r for r in caplog.records if r.message == "Some job failed"]
    assert len(err) == 1
    assert err[0].name == body.__module__
    assert err[0].exc_info is not None


async def test_skip_sentinel_records_nothing():
    @job_guard("some_job", "Some job failed")
    async def body():
        return SKIP

    patcher, mock_rt = _mock_runtime()
    try:
        result = await body()
    finally:
        patcher.stop()

    assert result is None
    mock_rt.record_job_success.assert_not_called()
    mock_rt.record_job_failure.assert_not_called()


async def test_decorated_fn_is_still_a_coroutine_function():
    # test_jobs_extraction.py's contract: module job fns must satisfy
    # inspect.iscoroutinefunction after decoration.
    @job_guard("some_job", "Some job failed")
    async def body():
        return None

    assert inspect.iscoroutinefunction(body)
    assert body.__name__ == "body"


# ── record helpers: swallow-safety ──────────────────────────────────

def test_record_helpers_call_through():
    patcher, mock_rt = _mock_runtime()
    try:
        record_success("job_a")
        record_failure("job_b", "bad")
    finally:
        patcher.stop()

    mock_rt.record_job_success.assert_called_once_with("job_a")
    mock_rt.record_job_failure.assert_called_once_with("job_b", "bad")


def test_record_helpers_swallow_recorder_exceptions():
    patcher, mock_rt = _mock_runtime()
    try:
        mock_rt.record_job_success.side_effect = RuntimeError("db gone")
        mock_rt.record_job_failure.side_effect = RuntimeError("db gone")
        record_success("job_a")  # must not raise
        record_failure("job_b", "bad")  # must not raise
    finally:
        patcher.stop()


def test_record_helpers_swallow_instance_lookup_failure():
    with patch("genesis.runtime.GenesisRuntime") as mock_cls:
        mock_cls.instance.side_effect = RuntimeError("no runtime")
        record_success("job_a")  # must not raise
        record_failure("job_b", "bad")  # must not raise
