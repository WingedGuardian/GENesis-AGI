"""Shared aggregate-deadline arithmetic."""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from review_deadline import Deadline, bounded_timeout  # noqa: E402


def test_absolute_deadline_reuses_one_clock_budget():
    now = [100.0]
    deadline = Deadline.after(5.0, monotonic=lambda: now[0])
    assert deadline.timeout(8.0) == 5.0
    now[0] = 104.0
    assert deadline.remaining() == 1.0
    assert deadline.timeout(8.0) == 1.0


def test_expired_local_probe_gets_only_the_fail_fast_floor():
    assert bounded_timeout(10.0, 8.0, monotonic=lambda: 11.0) == 0.001


def test_network_minimum_is_a_separate_caller_policy():
    deadline = Deadline(10.5, monotonic=lambda: 10.0)
    assert deadline.exhausted(minimum_useful=0.75)
    assert not deadline.exhausted(minimum_useful=0.25)
