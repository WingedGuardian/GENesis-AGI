"""Tests for scripts/gfs_select.py — Grandfather-Father-Son retention selection.

This is the crown-jewel safety math for off-site backup pruning: it decides which dated
snapshots to DELETE. The invariants under test:
  * the NEWEST stamp is ALWAYS kept (restore.sh depends on the latest COMPLETE);
  * keep the newest snapshot per day (daily N), per ISO-week (weekly M), per month (monthly K);
  * everything outside every bucket is deleted;
  * an unparseable stamp is never deleted (fail-safe keep).
Pure function, no I/O — deterministic, wall-clock-independent (stamps are explicit).
"""

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "gfs_select.py"
_spec = importlib.util.spec_from_file_location("gfs_select", _SCRIPT)
_gfs = importlib.util.module_from_spec(_spec)
sys.modules["gfs_select"] = _gfs
_spec.loader.exec_module(_gfs)


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _daily_series(days: int, *, end: datetime, per_day: int = 1) -> list[str]:
    """`days` consecutive calendar days ending at `end`, `per_day` stamps each."""
    out = []
    for d in range(days):
        base = end - timedelta(days=d)
        for h in range(per_day):
            out.append(_stamp(base - timedelta(hours=h)))
    return out


_END = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


def _select(stamps, daily=7, weekly=4, monthly=6):
    return set(_gfs.select_deletions(stamps, daily=daily, weekly=weekly, monthly=monthly))


def test_empty_deletes_nothing():
    assert _gfs.select_deletions([], daily=7, weekly=4, monthly=6) == []


def test_single_stamp_kept():
    s = _stamp(_END)
    assert _select([s]) == set()


def test_newest_is_always_kept():
    stamps = _daily_series(300, end=_END)
    newest = _stamp(_END)
    deletions = _select(stamps, daily=1, weekly=0, monthly=0)
    assert newest not in deletions


def test_same_day_keeps_only_newest_for_daily_bucket():
    # 3 stamps on one day; daily=1, no weekly/monthly -> keep the newest, delete the 2 older.
    stamps = _daily_series(1, end=_END, per_day=3)
    deletions = _select(stamps, daily=1, weekly=0, monthly=0)
    assert _stamp(_END) not in deletions          # newest kept
    assert len(deletions) == 2                     # the two older same-day stamps


def test_daily_keeps_n_most_recent_days():
    # one stamp/day for 5 days; daily=3 -> keep newest 3 days, delete oldest 2.
    stamps = _daily_series(5, end=_END)
    deletions = _select(stamps, daily=3, weekly=0, monthly=0)
    assert len(deletions) == 2
    # the two OLDEST days are deleted
    oldest_two = {_stamp(_END - timedelta(days=3)), _stamp(_END - timedelta(days=4))}
    assert deletions == oldest_two


def test_gfs_full_policy_keeps_daily_weekly_monthly_and_prunes_rest():
    # 200 daily snapshots; classic 7/4/6. Keep-set = newest-per-day(7) ∪ per-week(4) ∪ per-month(6).
    stamps = _daily_series(200, end=_END)
    deletions = _select(stamps, daily=7, weekly=4, monthly=6)
    kept = set(stamps) - deletions
    assert _stamp(_END) in kept                    # newest always kept
    assert deletions, "old snapshots outside all buckets must be pruned"
    # keep-set is bounded by the bucket counts (with overlaps -> at most 7+4+6)
    assert len(kept) <= 7 + 4 + 6
    # a very old snapshot (>6 months) that is not a month-boundary keeper is deleted
    assert _stamp(_END - timedelta(days=199)) in deletions


def test_unparseable_stamp_is_never_deleted():
    good_old = _stamp(_END - timedelta(days=100))
    stamps = [_stamp(_END), "not-a-stamp", good_old]
    deletions = _select(stamps, daily=1, weekly=0, monthly=0)
    assert "not-a-stamp" not in deletions           # fail-safe: unparseable -> keep


def test_weekly_buckets_by_iso_week_not_calendar_year():
    """Two stamps in the SAME ISO week but DIFFERENT calendar years must share ONE weekly
    bucket. 2025-12-30 and 2026-01-01 are both ISO week 1 of 2026. With weekly=2, correct
    ISO bucketing sees a single bucket -> keeps only the newest -> prunes 2025-12-30; a buggy
    (calendar_year, week) key would make two buckets and wrongly RETAIN 2025-12-30."""
    older = _stamp(datetime(2025, 12, 30, 12, 0, 0, tzinfo=UTC))
    newer = _stamp(datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC))
    assert (datetime(2025, 12, 30).isocalendar()[:2]
            == datetime(2026, 1, 1).isocalendar()[:2])  # sanity: same ISO (year, week)
    deletions = _select([older, newer], daily=0, weekly=2, monthly=0)
    assert older in deletions       # same ISO week -> only the newest survives
    assert newer not in deletions   # newest always kept
