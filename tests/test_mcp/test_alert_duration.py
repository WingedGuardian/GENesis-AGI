"""Alerts carry how LONG they have been firing, not just that they fire.

The 2026-08-28/29 provider outage was invisible for 3 days not because nothing
detected it but because every surface renders instantaneous state. `alert_events`
has carried `created_at`/`resolved_at` with 90-day retention the whole time and
nothing read the TIMESTAMPS — before this change its only production reader was
`_compute_alerts`' fail-open re-emit, which takes only `severity`/`message`.
(Enumerated: outside `db/crud/alert_events.py`, the table is touched by
`awareness/loop.py` as the writer, `runtime/init/learning.py` as the 90-day
pruner, and `mcp/health/errors.py` as that reader. Nothing else.)

Enriching the alert MESSAGE rather than adding a widget means every surface that
renders `message` gains the duration at once — the dashboard banner, the morning
report (`outreach/morning_report.py:947`) and the Telegram path
(`outreach/health_outreach.py:82`).

What that does NOT do, stated because it is easy to over-read: those two
non-dashboard surfaces FILTER before they render. The morning report drops
`call_site:`-prefixed WARNINGs by construction, and the Telegram path takes only
CRITICAL ids on an escalation whitelist. The alert this incident actually raises
is a `call_site:` WARNING, so for that alert the duration lands on the dashboard
only. Reaching the user is a separate mechanism, not a consequence of this one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from genesis.mcp.health.errors import _apply_ongoing_duration, _ongoing_for

_NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)


def _ago(**kw) -> str:
    return (_NOW - timedelta(**kw)).isoformat()


class TestOngoingFor:
    def test_multi_day_outage_reads_in_days_and_hours(self):
        assert _ongoing_for(_ago(days=3, hours=4), _NOW) == "3d 4h"

    def test_sub_day_reads_in_hours(self):
        assert _ongoing_for(_ago(hours=5), _NOW) == "5h"

    def test_whole_days_omit_a_zero_hour(self):
        assert _ongoing_for(_ago(days=2), _NOW) == "2d"

    def test_young_alert_is_not_decorated(self):
        """Under the floor, a duration is noise — most alerts flap briefly."""
        assert _ongoing_for(_ago(minutes=20), _NOW) is None

    def test_unparseable_timestamp_is_not_decorated(self):
        """Never raise into the alert path; a bad row just gets no suffix."""
        assert _ongoing_for("not-a-timestamp", _NOW) is None
        assert _ongoing_for("", _NOW) is None
        assert _ongoing_for(None, _NOW) is None

    def test_future_timestamp_is_not_decorated(self):
        """Clock skew must not produce a negative or absurd duration."""
        assert _ongoing_for((_NOW + timedelta(hours=2)).isoformat(), _NOW) is None


class TestApplyOngoingDuration:
    def test_open_row_adds_duration_to_the_message(self):
        alerts = [{"id": "provider:x", "severity": "CRITICAL", "message": "Provider x is down"}]
        rows = {"provider:x": {"created_at": _ago(days=3, hours=4)}}
        _apply_ongoing_duration(alerts, rows, now=_NOW)
        assert alerts[0]["message"] == "Provider x is down (ongoing for 3d 4h)"

    def test_alert_without_an_open_row_is_untouched(self):
        """First fire: the row is written AFTER this runs, so there is no age yet."""
        alerts = [{"id": "provider:x", "severity": "CRITICAL", "message": "Provider x is down"}]
        _apply_ongoing_duration(alerts, {}, now=_NOW)
        assert alerts[0]["message"] == "Provider x is down"

    def test_young_row_is_untouched(self):
        alerts = [{"id": "a", "severity": "WARNING", "message": "Blip"}]
        rows = {"a": {"created_at": _ago(minutes=5)}}
        _apply_ongoing_duration(alerts, rows, now=_NOW)
        assert alerts[0]["message"] == "Blip"

    def test_is_idempotent(self):
        """_compute_alerts runs on every tick; a message must not accrete suffixes."""
        alerts = [{"id": "a", "severity": "CRITICAL", "message": "Down"}]
        rows = {"a": {"created_at": _ago(days=1)}}
        _apply_ongoing_duration(alerts, rows, now=_NOW)
        _apply_ongoing_duration(alerts, rows, now=_NOW)
        assert alerts[0]["message"].count("ongoing for") == 1

    def test_an_alert_without_a_message_is_left_alone(self):
        """The widening from "only rewrite decorated alerts" to "always rewrite"
        must not invent a message. `outreach/morning_report.py` falls back to
        "Unknown" when the key is ABSENT but renders an empty string as blank,
        so writing `message=""` here would silently blank a real alert row."""
        alerts = [{"id": "a", "severity": "CRITICAL"}]
        rows = {"a": {"created_at": _ago(days=2)}}
        _apply_ongoing_duration(alerts, rows, now=_NOW)
        assert "message" not in alerts[0]

        explicit_none = [{"id": "a", "severity": "CRITICAL", "message": None}]
        _apply_ongoing_duration(explicit_none, rows, now=_NOW)
        assert explicit_none[0]["message"] is None, (
            "an explicit None was stringified into the literal 'None'"
        )

    def test_a_broken_row_never_raises_into_the_alert_path(self):
        alerts = [{"id": "a", "severity": "CRITICAL", "message": "Down"}]
        _apply_ongoing_duration(alerts, {"a": {}}, now=_NOW)
        _apply_ongoing_duration(alerts, {"a": None}, now=_NOW)
        assert alerts[0]["message"] == "Down"


class TestSuffixIsRefreshedNotFrozen:
    """A persisted message must not freeze its duration.

    `_compute_alerts` has a fail-open path that re-emits durable alerts straight
    from `alert_events` open rows, copying the STORED message.

    Scope note, corrected after review — the stored message cannot ACTUALLY
    carry a suffix today, and the earlier version of this docstring claimed it
    could. Traced: `_apply_ongoing_duration` runs at the END of
    `_compute_alerts`, and `reconcile_open_set` writes rows with
    `INSERT OR IGNORE` (`db/crud/alert_events.py:47`), so a row's message is
    captured once at first fire and never rewritten. At first fire there is no
    open row yet, so no suffix is applied. The stored string is therefore always
    clean.

    The strip-then-recompute rule is kept anyway, and this test with it, for two
    reasons that do not depend on that false premise: it makes the decoration
    idempotent within a tick regardless of how many times it runs, and it keeps
    the invariant true by CONSTRUCTION rather than by an ordering coincidence in
    a different module. A skip-if-decorated guard would be correct only for as
    long as nobody makes the writer update `message` — and a stale duration is
    worse than none, because it reads as current.
    """

    def test_a_stale_suffix_is_replaced_with_the_current_duration(self):
        alerts = [{
            "id": "a", "severity": "CRITICAL",
            # As re-emitted from a durable row written hours ago.
            "message": "Provider down (ongoing for 1h)",
        }]
        rows = {"a": {"created_at": _ago(days=3, hours=4)}}
        _apply_ongoing_duration(alerts, rows, now=_NOW)
        assert alerts[0]["message"] == "Provider down (ongoing for 3d 4h)"
        assert alerts[0]["message"].count("ongoing for") == 1

    def test_repeated_application_is_still_idempotent(self):
        alerts = [{"id": "a", "severity": "CRITICAL", "message": "Down"}]
        rows = {"a": {"created_at": _ago(days=1)}}
        for _ in range(4):
            _apply_ongoing_duration(alerts, rows, now=_NOW)
        assert alerts[0]["message"] == "Down (ongoing for 1d)"

    def test_a_stale_suffix_is_removed_when_the_alert_is_now_too_young(self):
        """Re-emitted row whose age no longer qualifies: drop the suffix, don't keep a lie."""
        alerts = [{"id": "a", "severity": "WARNING", "message": "Blip (ongoing for 9d)"}]
        rows = {"a": {"created_at": _ago(minutes=5)}}
        _apply_ongoing_duration(alerts, rows, now=_NOW)
        assert alerts[0]["message"] == "Blip"

    def test_parentheses_elsewhere_in_the_message_survive(self):
        """The strip must be anchored, not a greedy match on any parenthetical."""
        alerts = [{
            "id": "a", "severity": "CRITICAL",
            "message": "Call site x is DOWN (all providers exhausted)",
        }]
        rows = {"a": {"created_at": _ago(hours=6)}}
        _apply_ongoing_duration(alerts, rows, now=_NOW)
        assert alerts[0]["message"] == (
            "Call site x is DOWN (all providers exhausted) (ongoing for 6h)"
        )


class TestWiredIntoComputeAlerts:
    """The decoration is reached by the REAL `_compute_alerts` path.

    Everything above tests the two helpers directly, which proves the logic and
    not that anything calls it. That gap is real rather than theoretical: the
    duration block is guarded by `if _service and _service._db:`, and the
    existing health-MCP mock sets `_db = None`, so every pre-existing test in
    this area skips the block entirely.

    This is the PR's actual thesis end to end — breaker stays tripped, call site
    stays degraded, the `alert_events` row stays open, and the age accumulates —
    so it is the assertion a future refactor would otherwise break silently.
    """

    async def _fire(self, empty_db, *, created_at: str, site: str = "42_probe_site"):
        from unittest.mock import AsyncMock

        from genesis.mcp import health as health_mcp
        from genesis.mcp.health import errors as errors_mod

        alert_id = f"call_site:{site}"
        await empty_db.execute(
            "INSERT INTO alert_events (id, alert_id, source, severity, message, created_at) "
            "VALUES (?, ?, 'call_site', 'WARNING', ?, ?)",
            ("ae-1", alert_id, f"Call site {site} is degraded (using fallback provider)",
             created_at),
        )
        await empty_db.commit()

        svc = AsyncMock()
        svc.snapshot = AsyncMock(return_value={
            "call_sites": {site: {"status": "degraded", "active_provider": "fallback-1"}},
            "cc_sessions": {}, "infrastructure": {}, "queues": {}, "cost": {},
        })
        svc._dead_letter = None
        svc._breakers = None
        svc._routing_config = None
        svc._db = empty_db

        # Inject through the REAL entry point (`init_health_mcp`) rather than
        # poking a module global — `errors.py` resolves the service off the
        # package at call time, so a monkeypatched attribute on `errors` is not
        # the object the code reads.
        prev = health_mcp._service
        health_mcp.init_health_mcp(svc)
        try:
            alerts, _ids = await errors_mod._compute_alerts()
        finally:
            health_mcp._service = prev
        return next((a for a in alerts if a.get("id") == alert_id), None)

    async def test_an_open_row_gives_the_call_site_alert_a_duration(self, empty_db):
        old = (datetime.now(UTC) - timedelta(days=3, hours=4)).isoformat()
        alert = await self._fire(empty_db, created_at=old)
        assert alert is not None, "the degraded call site produced no alert at all"
        assert "(ongoing for 3d" in alert["message"], (
            "the duration never reached the alert through _compute_alerts — the "
            f"decoration is not wired. Got: {alert['message']!r}"
        )

    async def test_a_young_row_is_not_decorated_through_the_real_path(self, empty_db):
        """Guards against the wiring test passing for the wrong reason: if the
        suffix appeared regardless of age, the test above would still be green."""
        young = (datetime.now(UTC) - timedelta(minutes=4)).isoformat()
        alert = await self._fire(empty_db, created_at=young)
        assert alert is not None
        assert "ongoing for" not in alert["message"]
