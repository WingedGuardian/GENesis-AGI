"""Tests for UserGoalStalenessCollector follow-up scoping (PR3).

The signal must measure genuine USER-goal staleness — i.e. only follow-ups in
the user_world domain — not internal-dev backlog parked under the
`user_input_needed` strategy. This is the change that (correctly) de-skews the
signal from ~0.97 and, as a downstream effect, softens Light-reflection cadence.
"""

from datetime import UTC, datetime, timedelta

from genesis.db.crud import follow_ups
from genesis.learning.signals.user_goal_staleness import UserGoalStalenessCollector


async def _make(db, *, domain, age_days, now):
    fid = await follow_ups.create(
        db, source="t", content=f"{domain} item",
        strategy="user_input_needed", domain=domain,
    )
    await db.execute(
        "UPDATE follow_ups SET created_at = ? WHERE id = ?",
        ((now - timedelta(days=age_days)).isoformat(), fid),
    )
    await db.commit()
    return fid


async def test_check_follow_ups_scoped_to_user_world(db):
    now = datetime(2026, 6, 22, tzinfo=UTC)
    collector = UserGoalStalenessCollector(db)

    # An ancient INTERNAL user_input_needed item is ignored entirely — pre-scoping
    # this single 60-day row pinned the signal at 1.0.
    await _make(db, domain="internal", age_days=60, now=now)
    assert await collector._check_follow_ups(now) == 0.0

    # A recent user_world item makes the signal reflect ITS (low) age — proving
    # the 60-day internal row did NOT leak in (else it would read 1.0).
    uw_id = await _make(db, domain="user_world", age_days=1, now=now)
    assert await collector._check_follow_ups(now) < 1.0

    # And an OLD user_world item DOES drive it to 1.0 (user_world is counted).
    await db.execute(
        "UPDATE follow_ups SET created_at = ? WHERE id = ?",
        ((now - timedelta(days=60)).isoformat(), uw_id),
    )
    await db.commit()
    assert await collector._check_follow_ups(now) == 1.0
