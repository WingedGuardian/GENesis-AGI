"""Round-5 fixes on PR #1706 that live outside the reaper.

Three separate defects, each with its own denominator-bearing rationale:

1. The `julianday()` class was fixed in the SNAPSHOT reader and left in this
   CRUD module — two more cutoffs in a file the PR already edits. MEASURED on
   the live table while verifying: the lexical form returned 45 rows where the
   date-aware form returned 41, i.e. four rows aged 24-26h counted as "last 24
   hours".
2. `cc_budget` dropped NULL-`source_tag` rows from the hourly count, because
   `NULL != 'voice'` is NULL rather than true. Latent (0 of 5,021 live rows are
   NULL) but inconsistent with the CRUD layer, which already writes
   `COALESCE(source_tag, '') != 'voice'` and says why.
3. The dead-pid job's cadence is CONFIG-DRIVEN and `knob_int` enforces only
   `> 0`, so an overlay could silently produce an `IntervalTrigger` above the
   repo's 1-hour rule — where a restart reset means the job may never fire.
"""

from __future__ import annotations

import pytest
from apscheduler.triggers.interval import IntervalTrigger

from genesis.db.crud import cc_sessions


async def _seed(db, sid, started_at, *, status="completed", topic="t"):
    await cc_sessions.create(
        db,
        id=sid,
        session_type="foreground",
        model="sonnet",
        effort="medium",
        status=status,
        user_id="u",
        channel="telegram",
        started_at=started_at,
        last_activity_at=started_at,
        source_tag="foreground",
    )
    if topic:
        await db.execute(
            "UPDATE cc_sessions SET topic = ? WHERE id = ?", (topic, sid)
        )


class TestDateAwareCutoffs:
    """Both operands through `julianday()`: stored values are ISO-8601 with a
    'T' while `datetime('now', …)` renders with a SPACE, and 'T' (0x54) sorts
    after ' ' (0x20) — so a lexical compare admits every row sharing the
    cutoff's calendar date, whatever its time."""

    @pytest.mark.asyncio
    async def test_status_counts_excludes_a_row_older_than_the_window(self, db):
        # 30h old: same calendar date as a 24h cutoff can be, but out of window.
        # ANCHORED to midnight of the cutoff's own calendar date, not a
        # relative "-30 hours". A relative seed separates the lexical and
        # date-aware forms only when the current hour is late enough that the
        # seed lands on an earlier date — MEASURED, it agrees with the BUGGY
        # code for 6 of 24 hours, so this cell's verify-RED would pass on the
        # clock rather than on the fix. Midnight of the cutoff date is the
        # worst case for a lexical compare at every hour.
        cur = await db.execute("SELECT date('now', '-24 hours') || 'T00:00:00+00:00'")
        old = (await cur.fetchone())[0]
        cur = await db.execute("SELECT datetime('now', '-1 hours')")
        recent = (await cur.fetchone())[0].replace(" ", "T") + "+00:00"

        await _seed(db, "old-row", old)
        await _seed(db, "new-row", recent)
        await db.commit()

        counts = await cc_sessions.get_status_counts(db, hours=24)
        assert sum(counts.values()) == 1, f"only the in-window row may count, got {counts}"

    @pytest.mark.asyncio
    async def test_recent_topics_excludes_a_row_older_than_the_window(self, db):
        # ANCHORED to midnight of the cutoff's own calendar date, not a
        # relative "-30 hours". A relative seed separates the lexical and
        # date-aware forms only when the current hour is late enough that the
        # seed lands on an earlier date — MEASURED, it agrees with the BUGGY
        # code for 6 of 24 hours, so this cell's verify-RED would pass on the
        # clock rather than on the fix. Midnight of the cutoff date is the
        # worst case for a lexical compare at every hour.
        cur = await db.execute("SELECT date('now', '-24 hours') || 'T00:00:00+00:00'")
        old = (await cur.fetchone())[0]
        cur = await db.execute("SELECT datetime('now', '-1 hours')")
        recent = (await cur.fetchone())[0].replace(" ", "T") + "+00:00"

        await _seed(db, "old-row", old, topic="stale-topic")
        await _seed(db, "new-row", recent, topic="fresh-topic")
        await db.commit()

        topics = await cc_sessions.get_recent_topics(db, hours=24)
        assert topics == ["fresh-topic"], topics


class TestBudgetSourceTag:
    @pytest.mark.asyncio
    async def test_schema_forbids_a_null_source_tag(self, db):
        """The COALESCE in `_count_recent_sessions` is CONSISTENCY with the
        four sibling predicates in the CRUD layer, not a live-bug fix — and
        this pins WHY, so nobody later "simplifies" it back believing the
        NULL case is reachable. If this assertion ever fails, the constraint
        was relaxed and the COALESCE became load-bearing."""
        cur = await db.execute("SELECT sql FROM sqlite_master WHERE name='cc_sessions'")
        ddl = (await cur.fetchone())[0]
        assert "source_tag" in ddl
        tag_line = next(ln for ln in ddl.splitlines() if "source_tag" in ln)
        assert "NOT NULL" in tag_line, tag_line

    @pytest.mark.asyncio
    async def test_voice_row_still_excluded(self, db):
        """Negative control — the COALESCE must not have broken the exclusion
        it wraps."""
        from genesis.resilience.cc_budget import CCBudgetTracker

        cur = await db.execute("SELECT datetime('now', '-10 minutes')")
        recent = (await cur.fetchone())[0].replace(" ", "T") + "+00:00"
        await _seed(db, "voice-row", recent, status="active")
        await db.execute("UPDATE cc_sessions SET source_tag = 'voice' WHERE id = ?", ("voice-row",))
        await db.commit()

        tracker = CCBudgetTracker(db)
        assert await tracker._count_recent_sessions() == 0


class TestDeadPidCadence:
    """The dead-pid job's poll interval.

    An earlier revision of this fix chose a CronTrigger above 60 minutes and
    built `hour="*/{N}"` from the config value. `hour` has range 0-23, so
    N > 23 raised ValueError — and `learning.init()` wraps everything in one
    broad `except Exception`, so that raise would have taken the remaining
    add_job calls AND `scheduler.start()` with it. A config value that was
    harmless before the "fix" would have killed the whole learning scheduler.

    These cells EXECUTE the shipped selection rather than mirroring it: the
    mirror is exactly how that blocker survived a 10-mutation verify-RED
    sweep, because the shipped line was never run at any value.
    """

    @staticmethod
    def _shipped_interval(minutes: int) -> int:
        """Run the REAL module function and return the resulting interval."""

        from genesis.runtime.init.learning import _build_dead_pid_trigger

        trig = _build_dead_pid_trigger(minutes)
        assert isinstance(trig, IntervalTrigger), type(trig)
        return int(trig.interval.total_seconds() // 60)

    @pytest.mark.parametrize("configured", [1, 30, 60])
    def test_at_or_under_the_hour_is_used_verbatim(self, configured):
        assert self._shipped_interval(configured) == configured

    @pytest.mark.parametrize("configured", [61, 90, 120, 1410, 1440, 100000])
    def test_above_the_hour_is_capped_not_converted(self, configured):
        """Every one of these is a value `settings_update` accepts today, and
        1410+ is the range that made the previous revision raise."""
        assert self._shipped_interval(configured) == 60

    def test_the_selection_never_raises_for_any_accepted_config(self):
        """The property that matters: no positive int may make this throw,
        because the raise is swallowed by init()'s broad handler."""
        for m in (1, 23, 24, 59, 60, 61, 1409, 1410, 1441, 999999):
            self._shipped_interval(m)
