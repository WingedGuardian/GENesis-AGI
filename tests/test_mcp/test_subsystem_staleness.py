"""Tests for pulse-staleness (total-cessation) surfacing.

Two surfaces, one shared signal (``compute_heartbeat_staleness``):
  - ``_impl_health_alerts`` emits a ``subsystem_stale:<name>`` alert when a
    heartbeat-emitting subsystem's last durable ``heartbeat`` event is older
    than its per-subsystem overdue threshold (its scheduler/loop stopped — a
    silent death the failure-gap job alarms cannot see). The alert set is
    ``ego`` (CRITICAL) + ``inbox`` + ``dashboard`` (WARNING). **surplus** is
    dropped (PR-B's shipped tile already flips red on a wedged/dead surplus via
    its job_health signal) and **outreach** is dropped (its pulse is
    config-gated — never fires on a Telegram-less install). reflection/awareness
    stay excluded (reflection's pulse tracks the awareness loop; awareness has
    ``awareness:tick_overdue``).
  - ``compute_heartbeat_staleness`` — the pure per-subsystem verdict the alert
    AND the ego dashboard tile both read, so the two can never disagree. It is
    pause-aware for the pause-gated subsystems ({surplus, inbox}, whose pulse
    stops behind their loop's ``if paused: return``) and fails loud on a corrupt
    or future timestamp.

Exercised against a REAL in-memory SQLite ``events`` table so the crud query +
threshold math are tested, not mocked away.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest


def _iso_ago(seconds: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


async def _seed_heartbeat(db, subsystem: str, age_seconds: float) -> None:
    from genesis.db.crud import events as events_crud

    await events_crud.insert(
        db,
        subsystem=subsystem,
        severity="debug",
        event_type="heartbeat",
        message=f"{subsystem} pulse",
        timestamp=_iso_ago(age_seconds),
    )


@pytest.fixture(autouse=True)
def _no_real_bootstrap_manifest(tmp_path):
    """Hermetic default: no persisted bootstrap manifest.

    ``compute_heartbeat_staleness`` reads ``~/.genesis/bootstrap_manifest.json``
    (via ``_read_persisted_manifest`` → ``_MANIFEST_FILE``) to compute the
    never_started verdict. This box HAS that file from its running server; CI does
    not. Point ``_MANIFEST_FILE`` at an ABSENT path so the REAL reader returns None
    (fresh-install empty state, benign) unless a test explicitly injects a manifest
    — install-agnostic + no cross-test leakage. Patching the file path (not the
    reader) keeps the real read+parse chain live for the integration test."""
    from genesis.mcp.health import manifest as _m

    absent = tmp_path / "no_such_bootstrap_manifest.json"  # intentionally does not exist
    with patch.object(_m, "_MANIFEST_FILE", absent):
        yield


def _manifest(mapping: dict[str, str], *, persisted_age_s: float = 0.0) -> dict:
    """A persisted-manifest dict as ``_read_persisted_manifest`` would return it."""
    return {
        "bootstrapped": False,
        "manifest": mapping,
        "source": "persisted_manifest",
        "persisted_at": _iso_ago(persisted_age_s),
    }


# ── compute_heartbeat_staleness (the shared signal) ──────────────────────────


@pytest.mark.asyncio
async def test_staleness_overdue(db):
    """A pulse older than the subsystem's overdue threshold → status overdue."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    # ego threshold is 14400s (4h); 5h old → overdue.
    await _seed_heartbeat(db, "ego", age_seconds=5 * 3600)
    hb = await compute_heartbeat_staleness("ego", db=db)
    assert hb["status"] == "overdue"
    assert hb["age_seconds"] > 14400


@pytest.mark.asyncio
async def test_staleness_alive(db):
    """A fresh pulse well within threshold → status alive."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    await _seed_heartbeat(db, "surplus", age_seconds=10)
    hb = await compute_heartbeat_staleness("surplus", db=db)
    assert hb["status"] == "alive"


@pytest.mark.asyncio
async def test_staleness_no_heartbeat_empty_state(db):
    """No pulse ever (fresh install) → no_heartbeat, never 'stalled'."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    hb = await compute_heartbeat_staleness("ego", db=db)
    assert hb["status"] == "no_heartbeat"
    assert hb["last_seen"] is None


@pytest.mark.asyncio
async def test_staleness_unparseable_timestamp_is_unknown(db):
    """A corrupt timestamp → unknown (never silently 'alive')."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    await db.execute(
        "INSERT INTO events (id, timestamp, subsystem, severity, event_type, "
        "message, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("x1", "not-a-date", "ego", "debug", "heartbeat", "bad", "not-a-date"),
    )
    await db.commit()
    hb = await compute_heartbeat_staleness("ego", db=db)
    assert hb["status"] == "unknown"


@pytest.mark.asyncio
async def test_staleness_future_timestamp_is_unknown(db):
    """A pulse materially in the FUTURE (clock skew / corrupt row) cannot confirm
    liveness → unknown, never a false 'alive' from a negative age (P2-3)."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    await _seed_heartbeat(db, "ego", age_seconds=-3600)  # 1h in the future
    hb = await compute_heartbeat_staleness("ego", db=db)
    assert hb["status"] == "unknown"


@pytest.mark.asyncio
async def test_staleness_uses_freshest_of_db_and_ring(db):
    """A fresh in-memory ring pulse beats a stale DB row (the ring updates
    synchronously; the DB persists fire-and-forget) → alive, not overdue (P2-2)."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    # DB has an OVERDUE ego pulse (5h), ring has a FRESH one (10s).
    await _seed_heartbeat(db, "ego", age_seconds=5 * 3600)
    fresh_event = SimpleNamespace(
        subsystem=SimpleNamespace(value="ego"),
        event_type="heartbeat",
        timestamp=_iso_ago(10),
    )
    fake_bus = SimpleNamespace(_ring=[fresh_event])
    with patch("genesis.mcp.health_mcp._event_bus", fake_bus):
        hb = await compute_heartbeat_staleness("ego", db=db)
    assert hb["status"] == "alive"  # freshest wins, not the stale DB row


@pytest.mark.asyncio
async def test_corrupt_future_row_does_not_hide_valid_pulse(db):
    """A materially-future heartbeat sorts FIRST under the events table's TEXT
    ORDER BY, but the window scan still finds the newest valid pulse beneath it →
    alive, never a stuck/never-resolving verdict off the bad row (#9)."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    await _seed_heartbeat(db, "ego", age_seconds=-100 * 365 * 86400)  # ~century in future
    await _seed_heartbeat(db, "ego", age_seconds=30)  # fresh valid pulse beneath it
    hb = await compute_heartbeat_staleness("ego", db=db)
    assert hb["status"] == "alive"


@pytest.mark.asyncio
async def test_naive_timestamp_normalized_not_crash(db):
    """A naive (tz-less) heartbeat timestamp is normalized to UTC by the shared
    parser, not a TypeError that breaks the whole helper (#8)."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    naive = datetime.now(UTC).replace(tzinfo=None).isoformat()  # no offset
    await db.execute(
        "INSERT INTO events (id, timestamp, subsystem, severity, event_type, "
        "message, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("n1", naive, "ego", "debug", "heartbeat", "naive", naive),
    )
    await db.commit()
    hb = await compute_heartbeat_staleness("ego", db=db)
    assert hb["status"] == "alive"  # normalized + recent → alive, not unknown/crash


def test_read_global_paused_reads_persisted_file(tmp_path):
    """_read_global_paused reads the persisted pause file (the cross-process source
    of truth), NOT the in-memory runtime singleton — so the pause-guard works in the
    standalone health MCP where no GenesisRuntime is bootstrapped (#7)."""
    from genesis.mcp.health import manifest as _m

    pf = tmp_path / "paused.json"
    with patch.object(_m, "_PAUSE_FILE", pf):
        assert _m._read_global_paused() is False  # absent file → not paused
        pf.write_text('{"paused": true}')
        assert _m._read_global_paused() is True
        pf.write_text('{"paused": false}')
        assert _m._read_global_paused() is False


@pytest.mark.asyncio
async def test_staleness_unknown_subsystem_is_no_heartbeat(db):
    """A name not in the threshold table degrades to no_heartbeat, never raises."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    hb = await compute_heartbeat_staleness("not_a_subsystem", db=db)
    assert hb["status"] == "no_heartbeat"


@pytest.mark.asyncio
async def test_staleness_read_error_default_degrades_and_logs():
    """Default (alert/display callers): a query error is ERROR-logged and the
    verdict degrades to no_heartbeat — the read failure is recorded, and the
    verdict is never a spurious 'alive'."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    bad_db = MagicMock()
    bad_db.execute_fetchall = AsyncMock(side_effect=aiosqlite.OperationalError("db exploded"))
    with (
        patch("genesis.mcp.health_mcp._service", None),
        patch("genesis.mcp.health_mcp._event_bus", None),
    ):
        hb = await compute_heartbeat_staleness("ego", db=bad_db)
    assert hb["status"] == "no_heartbeat"


@pytest.mark.asyncio
async def test_staleness_read_error_raises_when_requested():
    """Tile caller (raise_on_error=True): a query error RE-RAISES so the caller
    fails loud (surfaces unknown) instead of a healthy tile on a broken read."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    bad_db = MagicMock()
    bad_db.execute_fetchall = AsyncMock(side_effect=aiosqlite.OperationalError("db exploded"))
    with (
        patch("genesis.mcp.health_mcp._service", None),
        patch("genesis.mcp.health_mcp._event_bus", None),
        pytest.raises(aiosqlite.OperationalError),
    ):
        await compute_heartbeat_staleness("ego", db=bad_db, raise_on_error=True)


# ── pause-guard (pause-gated subsystems only) ────────────────────────────────


@pytest.mark.asyncio
async def test_staleness_paused_downgrades_surplus(db):
    """surplus's pulse stops behind ``if paused: return`` — so an overdue surplus
    while globally paused is NOT dead → downgraded to 'paused', not 'overdue'."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    await _seed_heartbeat(db, "surplus", age_seconds=2 * 3600)  # > 600 threshold
    hb = await compute_heartbeat_staleness("surplus", db=db, paused=True)
    assert hb["status"] == "paused"


@pytest.mark.asyncio
async def test_staleness_paused_downgrades_inbox(db):
    """inbox is pause-gated too → overdue-while-paused downgrades to 'paused'."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    await _seed_heartbeat(db, "inbox", age_seconds=3 * 3600)  # > 7200 threshold
    hb = await compute_heartbeat_staleness("inbox", db=db, paused=True)
    assert hb["status"] == "paused"


@pytest.mark.asyncio
async def test_staleness_paused_does_not_downgrade_ego(db):
    """ego's heartbeat pulses THROUGH pause (dedicated job) — so an overdue ego
    even while paused is a genuine scheduler death and stays 'overdue'."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    await _seed_heartbeat(db, "ego", age_seconds=5 * 3600)
    hb = await compute_heartbeat_staleness("ego", db=db, paused=True)
    assert hb["status"] == "overdue"


@pytest.mark.asyncio
async def test_staleness_not_paused_still_overdue(db):
    """A pause-gated subsystem overdue while NOT paused → overdue (real stall)."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    await _seed_heartbeat(db, "inbox", age_seconds=3 * 3600)
    hb = await compute_heartbeat_staleness("inbox", db=db, paused=False)
    assert hb["status"] == "overdue"


# ── #240 newest-by-parsed-instant (mixed UTC offsets) ────────────────────────


def test_newest_valid_ts_picks_by_instant_not_text():
    """Textual ORDER BY is not chronological across UTC offsets — pick the newest
    PARSED instant, not the textually-first row (#240)."""
    from genesis.mcp.health.manifest import _newest_valid_ts

    now = datetime(2026, 8, 25, 20, 0, tzinfo=UTC)
    a = "2026-08-25T15:00:00+00:00"  # 15:00 UTC — sorts textually AFTER b ("15" > "11")
    b = "2026-08-25T11:30:00-04:00"  # 15:30 UTC — chronologically LATER, sorts first-text
    # In textual-DESC order the query returns `a` first; the newer instant is `b`.
    iso, saw = _newest_valid_ts([a, b], now=now)
    assert iso == b
    assert saw is True


# ── #372 post-resume grace (pause-gated) ─────────────────────────────────────


async def _seed_resume(db, age_seconds: float) -> None:
    from genesis.db.crud import events as events_crud

    await events_crud.insert(
        db,
        subsystem="runtime",
        severity="info",
        event_type="resume",
        message="Resumed",
        timestamp=_iso_ago(age_seconds),
    )


@pytest.mark.asyncio
async def test_post_resume_grace_suppresses_pause_gated_overdue(db):
    """Just after resuming from a >threshold pause, a pause-gated subsystem's stale
    pulse hasn't refreshed yet → 'resuming' (non-alerting), not 'overdue' (#372)."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    await _seed_heartbeat(db, "inbox", age_seconds=3 * 3600)  # > 7200 threshold
    await _seed_resume(db, age_seconds=60)  # resumed 1 min ago
    hb = await compute_heartbeat_staleness("inbox", db=db, paused=False)
    assert hb["status"] == "resuming"


@pytest.mark.asyncio
async def test_overdue_fires_after_resume_grace_expires(db):
    """Once the post-resume grace (interval + buffer) has elapsed with STILL no fresh
    pulse, it is a genuine stall → overdue (#372 does not mask a real death)."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    await _seed_heartbeat(db, "inbox", age_seconds=3 * 3600)
    await _seed_resume(db, age_seconds=3600)  # resumed 1h ago (> inbox 1800+120 grace)
    hb = await compute_heartbeat_staleness("inbox", db=db, paused=False)
    assert hb["status"] == "overdue"


@pytest.mark.asyncio
async def test_no_resume_event_no_grace(db):
    """With no resume on record, a pause-gated overdue is a normal stall → overdue."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    await _seed_heartbeat(db, "inbox", age_seconds=3 * 3600)
    hb = await compute_heartbeat_staleness("inbox", db=db, paused=False)
    assert hb["status"] == "overdue"


# ── #973 disabled-subsystem suppression ──────────────────────────────────────


def test_subsystem_enabled_reads_ego_and_inbox_config():
    """_subsystem_enabled defaults True (fail toward surfacing) but honors the ego AND
    inbox disable switches — a disabled/unconfigured subsystem must not alarm (#973 +
    never-started gating)."""
    from types import SimpleNamespace as _NS

    from genesis.mcp.health import manifest as _m

    # A subsystem with no disable switch still defaults True.
    assert _m._subsystem_enabled("dashboard") is True
    # ego honors its config switch.
    with patch("genesis.ego.config.load_ego_config", return_value=_NS(enabled=False)):
        assert _m._subsystem_enabled("ego") is False
    with patch("genesis.ego.config.load_ego_config", return_value=_NS(enabled=True)):
        assert _m._subsystem_enabled("ego") is True
    # inbox now honors config presence + .enabled (was unconditionally True before).
    with patch("genesis.mcp.health.manifest._inbox_enabled", return_value=False):
        assert _m._subsystem_enabled("inbox") is False
    with patch("genesis.mcp.health.manifest._inbox_enabled", return_value=True):
        assert _m._subsystem_enabled("inbox") is True


def test_inbox_enabled_reads_config(tmp_path):
    """_inbox_enabled: no yaml → False (unconfigured); present + enabled honored."""
    from types import SimpleNamespace as _NS

    from genesis.mcp.health import manifest as _m

    # No config/inbox_monitor.yaml under the (empty) repo root → not configured.
    with patch("genesis.env.repo_root", return_value=tmp_path):
        assert _m._inbox_enabled() is False
    # Present + enabled=True → True; enabled=False → False.
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "inbox_monitor.yaml").write_text("inbox:\n  enabled: true\n")
    with (
        patch("genesis.env.repo_root", return_value=tmp_path),
        patch("genesis.inbox.config.load_inbox_config", return_value=_NS(enabled=True)),
    ):
        assert _m._inbox_enabled() is True
    with (
        patch("genesis.env.repo_root", return_value=tmp_path),
        patch("genesis.inbox.config.load_inbox_config", return_value=_NS(enabled=False)),
    ):
        assert _m._inbox_enabled() is False


@pytest.mark.asyncio
async def test_disabled_ego_suppresses_cessation_alert(db):
    """A DISABLED ego with a retained stale pulse must NOT raise a (permanent)
    subsystem_stale:ego CRITICAL — the stale pulse is intentional, not a death (#973)."""
    await _seed_heartbeat(db, "ego", age_seconds=5 * 3600)
    with patch(
        "genesis.mcp.health.manifest._subsystem_enabled",
        side_effect=lambda n: n != "ego",
    ):
        alerts = await _run_alerts_with_db(db)
    assert not [a for a in _stale(alerts) if a["id"] == "subsystem_stale:ego"]


# ── loosened thresholds (near-zero false-reds) ───────────────────────────────


@pytest.mark.asyncio
async def test_inbox_threshold_loosened_no_false_fire_at_90min(db):
    """inbox check interval is 30min; a 90min gap (one slow/skipped check) is
    within the loosened 4× (7200s) threshold → alive, not a false overdue."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    await _seed_heartbeat(db, "inbox", age_seconds=90 * 60)  # 90min — one skip
    assert (await compute_heartbeat_staleness("inbox", db=db))["status"] == "alive"


@pytest.mark.asyncio
async def test_inbox_overdue_past_threshold(db):
    """A 2.5h inbox gap (> the 7200s threshold) is genuinely stale → overdue."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    await _seed_heartbeat(db, "inbox", age_seconds=150 * 60)  # 2.5h
    assert (await compute_heartbeat_staleness("inbox", db=db))["status"] == "overdue"


@pytest.mark.asyncio
async def test_dashboard_threshold_loosened(db):
    """dashboard pulses every 60s; overdue at 600s (10×), not 240s (4×)."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    await _seed_heartbeat(db, "dashboard", age_seconds=5 * 60)  # 5min → alive
    assert (await compute_heartbeat_staleness("dashboard", db=db))["status"] == "alive"


# ── subsystem_stale alert (via _impl_health_alerts / _compute_alerts) ─────────


def _snapshot():
    return {
        "call_sites": {},
        "infrastructure": {},
        "cc_sessions": {},
        "queues": {},
        "awareness": {},
        "services": {},
    }


async def _run_alerts_with_db(db, *, paused: bool = False):
    """Run the real _compute_alerts path with `db` as the health service DB.

    ``paused`` stubs the persisted-pause read the helper uses for pause-gated
    subsystems (deterministic — never touches the real ~/.genesis/paused.json).
    """
    svc = MagicMock()
    svc._db = db
    svc.snapshot = AsyncMock(return_value=_snapshot())
    with (
        patch("genesis.mcp.health_mcp._service", svc),
        patch("genesis.mcp.health_mcp._activity_tracker", None),
        patch("genesis.mcp.health_mcp._job_retry_registry", None),
        patch("genesis.mcp.health_mcp._alert_history", {}),
        patch("genesis.mcp.health_mcp._event_bus", None),
        patch("genesis.mcp.health.manifest._read_global_paused", return_value=paused),
        # Hermetic default: treat inbox as configured+enabled so existing
        # subsystem_stale:inbox tests don't depend on the repo's inbox_monitor.yaml.
        # A test wanting a disabled inbox overrides this or patches _subsystem_enabled.
        patch("genesis.mcp.health.manifest._inbox_enabled", return_value=True),
    ):
        from genesis.mcp.health.errors import _impl_health_alerts

        return await _impl_health_alerts(active_only=True)


def _stale(alerts):
    return [a for a in alerts if a.get("id", "").startswith("subsystem_stale:")]


@pytest.mark.asyncio
async def test_ego_cessation_alerts_critical(db):
    """A dead ego scheduler (heartbeat 5h old) → subsystem_stale:ego CRITICAL."""
    await _seed_heartbeat(db, "ego", age_seconds=5 * 3600)
    alerts = await _run_alerts_with_db(db)
    stale = _stale(alerts)
    assert any(a["id"] == "subsystem_stale:ego" for a in stale)
    ego = next(a for a in stale if a["id"] == "subsystem_stale:ego")
    assert ego["severity"] == "CRITICAL"
    assert "ego" in ego["message"]


@pytest.mark.asyncio
async def test_unknown_heartbeat_surfaces_warning_not_silent(db):
    """An UNREADABLE ego heartbeat (corrupt/clock-skewed ts) → a WARNING
    subsystem_heartbeat_unknown:ego ('liveness cannot be confirmed'), never a silent
    skip and never a mislabeled subsystem_stale:ego (which would assert death)."""
    await db.execute(
        "INSERT INTO events (id, timestamp, subsystem, severity, event_type, "
        "message, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("u1", "not-a-date", "ego", "debug", "heartbeat", "bad", "not-a-date"),
    )
    await db.commit()
    alerts = await _run_alerts_with_db(db)
    ids = {a["id"] for a in alerts}
    assert "subsystem_heartbeat_unknown:ego" in ids
    unk = next(a for a in alerts if a["id"] == "subsystem_heartbeat_unknown:ego")
    assert unk["severity"] == "WARNING"
    assert "subsystem_stale:ego" not in ids  # not mislabeled as a death


@pytest.mark.asyncio
async def test_inbox_cessation_alerts_warning(db):
    """A stalled inbox (3h old vs 2h threshold) → subsystem_stale:inbox WARNING."""
    await _seed_heartbeat(db, "inbox", age_seconds=3 * 3600)
    alerts = await _run_alerts_with_db(db)
    inbox = next((a for a in _stale(alerts) if a["id"] == "subsystem_stale:inbox"), None)
    assert inbox is not None
    assert inbox["severity"] == "WARNING"


@pytest.mark.asyncio
async def test_dashboard_cessation_alerts_warning(db):
    """A stalled dashboard (11min vs 10min threshold) → subsystem_stale:dashboard."""
    await _seed_heartbeat(db, "dashboard", age_seconds=11 * 60)
    alerts = await _run_alerts_with_db(db)
    dash = next((a for a in _stale(alerts) if a["id"] == "subsystem_stale:dashboard"), None)
    assert dash is not None
    assert dash["severity"] == "WARNING"


@pytest.mark.asyncio
async def test_surplus_dropped_from_alert_set(db):
    """surplus is DROPPED from the alert set (PR-B's tile already covers it) —
    even a badly-overdue surplus emits NO subsystem_stale:surplus."""
    await _seed_heartbeat(db, "surplus", age_seconds=3 * 3600)  # ≫ 600 threshold
    alerts = await _run_alerts_with_db(db)
    assert not [a for a in _stale(alerts) if a["id"] == "subsystem_stale:surplus"]


@pytest.mark.asyncio
async def test_outreach_dropped_from_alert_set(db):
    """outreach is DROPPED (config-gated pulse → false-alarm trap) — even a
    3-day-old outreach heartbeat emits NO subsystem_stale:outreach."""
    await _seed_heartbeat(db, "outreach", age_seconds=3 * 86400)  # ≫ 172800 threshold
    alerts = await _run_alerts_with_db(db)
    assert not [a for a in _stale(alerts) if a["id"] == "subsystem_stale:outreach"]


@pytest.mark.asyncio
async def test_alive_subsystem_does_not_alert(db):
    """A freshly-pulsing alert-set subsystem emits no subsystem_stale alert."""
    await _seed_heartbeat(db, "inbox", age_seconds=10)
    alerts = await _run_alerts_with_db(db)
    assert not [a for a in _stale(alerts) if a["id"] == "subsystem_stale:inbox"]


@pytest.mark.asyncio
async def test_reflection_is_excluded(db):
    """reflection is not in the alert set even when overdue."""
    await _seed_heartbeat(db, "reflection", age_seconds=5 * 3600)  # > 14400 threshold
    alerts = await _run_alerts_with_db(db)
    assert not [a for a in _stale(alerts) if a["id"] == "subsystem_stale:reflection"]


@pytest.mark.asyncio
async def test_awareness_not_duplicated(db):
    """awareness overdue is covered by awareness:tick_overdue, NOT a
    subsystem_stale:awareness duplicate."""
    await _seed_heartbeat(db, "awareness", age_seconds=5 * 3600)
    alerts = await _run_alerts_with_db(db)
    assert not [a for a in _stale(alerts) if a["id"] == "subsystem_stale:awareness"]


@pytest.mark.asyncio
async def test_empty_state_no_alerts(db):
    """Fresh install (no heartbeat events at all) → no subsystem_stale alerts."""
    alerts = await _run_alerts_with_db(db)
    assert _stale(alerts) == []


@pytest.mark.asyncio
async def test_paused_inbox_is_suppressed(db):
    """A globally-paused Genesis must NOT alert on inbox (its pulse legitimately
    stops on pause) — the helper downgrades it, so no subsystem_stale:inbox."""
    await _seed_heartbeat(db, "inbox", age_seconds=3 * 3600)
    alerts = await _run_alerts_with_db(db, paused=True)
    assert not [a for a in _stale(alerts) if a["id"] == "subsystem_stale:inbox"]


@pytest.mark.asyncio
async def test_paused_ego_still_alerts(db):
    """ego pulses through pause → a dead ego while paused STILL alerts (a paused
    box must not mask a genuinely dead ego scheduler)."""
    await _seed_heartbeat(db, "ego", age_seconds=5 * 3600)
    alerts = await _run_alerts_with_db(db, paused=True)
    assert any(a["id"] == "subsystem_stale:ego" for a in _stale(alerts))


@pytest.mark.asyncio
async def test_read_error_preserves_open_alert(db):
    """P2-4 fail-open fix: if the staleness read for a subsystem RAISES, a
    genuinely-open subsystem_stale alert for it must be PRESERVED in current_ids
    (so reconcile can't auto-resolve a real death on a transient read blip)."""
    from genesis.db.crud import alert_events as ae_crud
    from genesis.runtime._core import GenesisRuntime

    # An open subsystem_stale:inbox already on the durable open-set.
    await ae_crud.reconcile_open_set(
        db,
        active=[
            {
                "alert_id": "subsystem_stale:inbox",
                "source": "health",
                "severity": "WARNING",
                "message": "inbox overdue",
            }
        ],
        now=datetime.now(UTC).isoformat(),
    )

    async def _raise_for_inbox(name, **kwargs):
        if name == "inbox":
            raise aiosqlite.OperationalError("boom")
        return {"status": "alive", "last_seen": _iso_ago(10), "age_seconds": 10.0}

    svc = MagicMock()
    svc._db = db
    svc.snapshot = AsyncMock(return_value=_snapshot())
    stub_rt = SimpleNamespace(paused=False)
    with (
        patch("genesis.mcp.health_mcp._service", svc),
        patch("genesis.mcp.health_mcp._activity_tracker", None),
        patch("genesis.mcp.health_mcp._job_retry_registry", None),
        patch("genesis.mcp.health_mcp._alert_history", {}),
        patch("genesis.mcp.health_mcp._event_bus", None),
        patch.object(GenesisRuntime, "peek", return_value=stub_rt),
        patch(
            "genesis.mcp.health.manifest.compute_heartbeat_staleness",
            new=AsyncMock(side_effect=_raise_for_inbox),
        ),
    ):
        from genesis.mcp.health.errors import _compute_alerts

        alerts, current_ids = await _compute_alerts()

    alert_ids = {a["id"] for a in alerts}
    # The open inbox alert is RE-EMITTED into the alerts list — that is the
    # container the reconcile writer builds its active-set from (loop.py:158-166),
    # so the read blip cannot auto-resolve a genuinely-open death alert. It is also
    # in current_ids (lockstep with every other emit, for alert-history dedup).
    assert "subsystem_stale:inbox" in alert_ids
    assert "subsystem_stale:inbox" in current_ids
    # ego/dashboard read fine and are alive → NOT re-emitted / not open.
    assert "subsystem_stale:ego" not in alert_ids
    assert "subsystem_stale:dashboard" not in alert_ids


async def _compute_alerts_with_hb_side_effect(db, side_effect):
    """Run the real _compute_alerts with compute_heartbeat_staleness stubbed to
    `side_effect` (e.g. raise for one subsystem). Returns (alerts, current_ids)."""
    svc = MagicMock()
    svc._db = db
    svc.snapshot = AsyncMock(return_value=_snapshot())
    with (
        patch("genesis.mcp.health_mcp._service", svc),
        patch("genesis.mcp.health_mcp._activity_tracker", None),
        patch("genesis.mcp.health_mcp._job_retry_registry", None),
        patch("genesis.mcp.health_mcp._alert_history", {}),
        patch("genesis.mcp.health_mcp._event_bus", None),
        patch("genesis.mcp.health.manifest._read_global_paused", return_value=False),
        patch(
            "genesis.mcp.health.manifest.compute_heartbeat_staleness",
            new=AsyncMock(side_effect=side_effect),
        ),
    ):
        from genesis.mcp.health.errors import _compute_alerts

        return await _compute_alerts()


@pytest.mark.asyncio
async def test_read_error_preserves_open_unknown_alert(db):
    """SF-1: the fail-open guard preserves BOTH alert families the block emits — an
    open subsystem_heartbeat_unknown alert must not flap (auto-resolve) on a
    read-failure tick, only the subsystem_stale family."""
    from genesis.db.crud import alert_events as ae_crud

    await ae_crud.reconcile_open_set(
        db,
        active=[
            {
                "alert_id": "subsystem_heartbeat_unknown:inbox",
                "source": "health",
                "severity": "WARNING",
                "message": "inbox heartbeat unreadable",
            }
        ],
        now=datetime.now(UTC).isoformat(),
    )

    async def _raise_for_inbox(name, **kwargs):
        if name == "inbox":
            raise aiosqlite.OperationalError("boom")
        return {"status": "alive", "last_seen": _iso_ago(10), "age_seconds": 10.0}

    alerts, current_ids = await _compute_alerts_with_hb_side_effect(db, _raise_for_inbox)
    alert_ids = {a["id"] for a in alerts}
    assert "subsystem_heartbeat_unknown:inbox" in alert_ids  # preserved, not flapped
    assert "subsystem_heartbeat_unknown:inbox" in current_ids


@pytest.mark.asyncio
async def test_read_error_preserves_open_never_started_alert(db):
    """SF-2: the fail-open guard must preserve the subsystem_never_started family too —
    an open never_started alert (CRITICAL ego) must not flap (auto-resolve then reopen)
    on a transient staleness-read-failure tick."""
    from genesis.db.crud import alert_events as ae_crud

    await ae_crud.reconcile_open_set(
        db,
        active=[
            {
                "alert_id": "subsystem_never_started:ego",
                "source": "health",
                "severity": "CRITICAL",
                "message": "ego never started",
            }
        ],
        now=datetime.now(UTC).isoformat(),
    )

    async def _raise_for_ego(name, **kwargs):
        if name == "ego":
            raise aiosqlite.OperationalError("boom")
        return {"status": "alive", "last_seen": _iso_ago(10), "age_seconds": 10.0}

    alerts, current_ids = await _compute_alerts_with_hb_side_effect(db, _raise_for_ego)
    alert_ids = {a["id"] for a in alerts}
    assert "subsystem_never_started:ego" in alert_ids  # preserved, not flapped
    assert "subsystem_never_started:ego" in current_ids


@pytest.mark.asyncio
async def test_freshest_ignores_corrupt_future_db_row(db):
    """N-2: a corrupt FUTURE db heartbeat must not mask a valid fresh ring pulse —
    the subsystem reads alive, not a false unknown."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    await _seed_heartbeat(db, "ego", age_seconds=-3600)  # DB row 1h in the FUTURE
    fresh_event = SimpleNamespace(
        subsystem=SimpleNamespace(value="ego"),
        event_type="heartbeat",
        timestamp=_iso_ago(10),  # valid fresh ring pulse
    )
    fake_bus = SimpleNamespace(_ring=[fresh_event])
    with patch("genesis.mcp.health_mcp._event_bus", fake_bus):
        hb = await compute_heartbeat_staleness("ego", db=db)
    assert hb["status"] == "alive"


@pytest.mark.asyncio
async def test_helper_failure_isolated_from_other_alerts(db):
    """If the pulse-staleness block raises wholesale, the OTHER alerts still
    compute (its own try/except isolates it) — never a silent crash/green."""
    await _seed_heartbeat(db, "ego", age_seconds=5 * 3600)
    with patch(
        "genesis.mcp.health.manifest.compute_heartbeat_staleness",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ):
        alerts = await _run_alerts_with_db(db)
    # Block failed for every subsystem, none open on the durable set → none emitted.
    assert _stale(alerts) == []


# ── heartbeat GC preserves the last pulse (durable staleness signal) ─────────


@pytest.mark.asyncio
async def test_gc_keeps_latest_heartbeat_keeps_dead_subsystem_detectable(db):
    """The 7-day heartbeat GC must KEEP the most-recent pulse per subsystem, so a
    scheduler dead LONGER than the retention window stays detectable — its last
    pulse is not pruned into a false ``no_heartbeat``/green."""
    from genesis.db.crud import events as events_crud
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    # ego died 10 days ago — its ONLY pulse is older than the 7-day GC cutoff.
    await _seed_heartbeat(db, "ego", age_seconds=10 * 86400)
    cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    await events_crud.prune(
        db, older_than=cutoff, event_type="heartbeat", keep_latest_per_subsystem=True
    )

    hb = await compute_heartbeat_staleness("ego", db=db)
    assert hb["status"] == "overdue"  # last pulse survived → still detectably dead


@pytest.mark.asyncio
async def test_gc_without_keep_latest_would_lose_the_signal(db):
    """Control: the OLD prune (keep_latest_per_subsystem=False) deletes the sole
    old pulse → the dead subsystem reverts to no_heartbeat. This is the hole the
    keep-latest fix closes; asserting it makes the fix's value explicit."""
    from genesis.db.crud import events as events_crud
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    await _seed_heartbeat(db, "ego", age_seconds=10 * 86400)
    cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    await events_crud.prune(db, older_than=cutoff, event_type="heartbeat")

    hb = await compute_heartbeat_staleness("ego", db=db)
    assert hb["status"] == "no_heartbeat"  # sole pulse pruned → signal lost (the bug)


@pytest.mark.asyncio
async def test_gc_keeps_only_newest_and_still_prunes_older(db):
    """keep-latest retains exactly the single newest pulse per subsystem; older
    pulses past the cutoff are still pruned (volume stays bounded)."""
    from genesis.db.crud import events as events_crud

    await _seed_heartbeat(db, "ego", age_seconds=12 * 86400)  # older → prunable
    await _seed_heartbeat(db, "ego", age_seconds=9 * 86400)  # newest → retained
    await _seed_heartbeat(db, "surplus", age_seconds=8 * 86400)  # sole → retained
    cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    await events_crud.prune(
        db, older_than=cutoff, event_type="heartbeat", keep_latest_per_subsystem=True
    )

    ego_rows = await events_crud.query(db, subsystem="ego", event_type="heartbeat", limit=10)
    surplus_rows = await events_crud.query(
        db, subsystem="surplus", event_type="heartbeat", limit=10
    )
    assert len(ego_rows) == 1  # only the newest ego pulse kept
    assert len(surplus_rows) == 1  # the sole surplus pulse kept


# ── never_started detection (failed-start / started-but-silent) #10 ───────────

_GRACE_EGO_S = 300 + 60  # HEARTBEAT_EXPECTED["ego"][0] + 60


async def _compute(name, db):
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    # paused=False keeps the never_started verdict deterministic without touching the
    # real ~/.genesis/paused.json (a pause test calls compute directly with paused=True).
    return await compute_heartbeat_staleness(name, db=db, paused=False)


def _patch_manifest(mapping, *, persisted_age_s=0.0):
    return patch(
        "genesis.mcp.health.manifest._read_persisted_manifest",
        return_value=_manifest(mapping, persisted_age_s=persisted_age_s),
    )


@pytest.mark.asyncio
async def test_never_started_ego_ok_but_silent_past_grace(db):
    """manifest ego=ok, no pulse, past interval+60 grace → never_started/started-silent."""
    with _patch_manifest({"ego": "ok"}, persisted_age_s=_GRACE_EGO_S + 60):
        hb = await _compute("ego", db)
    assert hb["status"] == "never_started"
    assert hb.get("reason") == "started-silent"


@pytest.mark.asyncio
async def test_never_started_ego_within_grace_is_benign(db):
    """Just after boot (within grace), an unpulsed ego is benign, not never_started."""
    with _patch_manifest({"ego": "ok"}, persisted_age_s=10):
        hb = await _compute("ego", db)
    assert hb["status"] == "no_heartbeat"


@pytest.mark.asyncio
async def test_never_started_inbox_degraded_enabled(db):
    """A configured+enabled inbox recorded 'degraded' (init failed/short-circuited) →
    never_started/init-failed immediately (no grace)."""
    with (
        _patch_manifest({"inbox": "degraded"}),
        patch("genesis.mcp.health.manifest._inbox_enabled", return_value=True),
    ):
        hb = await _compute("inbox", db)
    assert hb["status"] == "never_started"
    assert hb.get("reason") == "init-failed"


@pytest.mark.asyncio
async def test_never_started_inbox_failed_prefix_enabled(db):
    """manifest stores 'failed: <msg>' with a prefix — matched via startswith."""
    with (
        _patch_manifest({"inbox": "failed: boom"}),
        patch("genesis.mcp.health.manifest._inbox_enabled", return_value=True),
    ):
        hb = await _compute("inbox", db)
    assert hb["status"] == "never_started"
    assert hb.get("reason") == "init-failed"


@pytest.mark.asyncio
async def test_degraded_disabled_inbox_is_benign(db):
    """A DISABLED/unconfigured inbox is ALSO 'degraded' — must stay benign, not alarm."""
    with (
        _patch_manifest({"inbox": "degraded"}),
        patch("genesis.mcp.health.manifest._inbox_enabled", return_value=False),
    ):
        hb = await _compute("inbox", db)
    assert hb["status"] == "no_heartbeat"


@pytest.mark.asyncio
async def test_never_started_absent_from_manifest_is_benign(db):
    """A subsystem not present in the manifest → fresh-install empty state, benign."""
    with _patch_manifest({}):  # no ego key
        hb = await _compute("ego", db)
    assert hb["status"] == "no_heartbeat"


@pytest.mark.asyncio
async def test_never_started_manifest_none_is_benign(db):
    """No persisted manifest at all (missing/unreadable) → benign (protect fresh installs)."""
    with patch("genesis.mcp.health.manifest._read_persisted_manifest", return_value=None):
        hb = await _compute("ego", db)
    assert hb["status"] == "no_heartbeat"


@pytest.mark.asyncio
async def test_never_started_persisted_at_none_is_benign_no_crash(db):
    """manifest ok but persisted_at missing (older build/partial write) → benign, no crash."""
    bad = {
        "bootstrapped": False,
        "manifest": {"ego": "ok"},
        "source": "persisted_manifest",
        "persisted_at": None,
    }
    with patch("genesis.mcp.health.manifest._read_persisted_manifest", return_value=bad):
        hb = await _compute("ego", db)
    assert hb["status"] == "no_heartbeat"


@pytest.mark.asyncio
async def test_never_started_persisted_at_unparseable_is_benign(db):
    bad = {"manifest": {"ego": "ok"}, "persisted_at": "not-a-date"}
    with patch("genesis.mcp.health.manifest._read_persisted_manifest", return_value=bad):
        hb = await _compute("ego", db)
    assert hb["status"] == "no_heartbeat"


@pytest.mark.asyncio
async def test_inbox_ok_silent_uses_overdue_grace_no_flap(db):
    """inbox emits no boot pulse; its first real pulse is ~1800s in. A no-pulse read at
    2000s (past interval+60=1860 but within overdue_s=7200) must stay benign — no flap."""
    with (
        _patch_manifest({"inbox": "ok"}, persisted_age_s=2000),
        patch("genesis.mcp.health.manifest._inbox_enabled", return_value=True),
    ):
        hb = await _compute("inbox", db)
    assert hb["status"] == "no_heartbeat"


@pytest.mark.asyncio
async def test_inbox_ok_silent_past_overdue_grace_never_started(db):
    with (
        _patch_manifest({"inbox": "ok"}, persisted_age_s=8000),  # > overdue_s 7200
        patch("genesis.mcp.health.manifest._inbox_enabled", return_value=True),
    ):
        hb = await _compute("inbox", db)
    assert hb["status"] == "never_started"
    assert hb.get("reason") == "started-silent"


@pytest.mark.asyncio
async def test_paused_inbox_ok_silent_is_benign(db):
    """inbox is pause-gated (its pulse stops behind ``if paused: return``) and emits no
    boot pulse — a globally-PAUSED Genesis legitimately has zero inbox pulses, so an
    ok-but-silent inbox past its grace must NOT false-fire never_started while paused."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    with (
        _patch_manifest({"inbox": "ok"}, persisted_age_s=8000),  # > overdue_s
        patch("genesis.mcp.health.manifest._inbox_enabled", return_value=True),
    ):
        hb = await compute_heartbeat_staleness("inbox", db=db, paused=True)
    assert hb["status"] == "no_heartbeat"


@pytest.mark.asyncio
async def test_not_paused_inbox_ok_silent_still_never_started(db):
    """The pause suppression is pause-specific — NOT paused, same shape → never_started."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    with (
        _patch_manifest({"inbox": "ok"}, persisted_age_s=8000),
        patch("genesis.mcp.health.manifest._inbox_enabled", return_value=True),
    ):
        hb = await compute_heartbeat_staleness("inbox", db=db, paused=False)
    assert hb["status"] == "never_started"


@pytest.mark.asyncio
async def test_paused_inbox_degraded_still_never_started(db):
    """Pause excuses only a stopped PULSE, never a real init fault. A paused inbox whose
    manifest shows 'degraded' (init failed/short-circuited) must STILL surface
    never_started/init-failed — pause is applied after boot and cannot cause it."""
    from genesis.mcp.health.manifest import compute_heartbeat_staleness

    with (
        _patch_manifest({"inbox": "degraded"}),
        patch("genesis.mcp.health.manifest._inbox_enabled", return_value=True),
    ):
        hb = await compute_heartbeat_staleness("inbox", db=db, paused=True)
    assert hb["status"] == "never_started"
    assert hb.get("reason") == "init-failed"


@pytest.mark.asyncio
async def test_pulse_present_overrides_never_started(db):
    """never_started only fires with NO usable pulse — a recent pulse → alive even if
    the manifest says degraded."""
    await _seed_heartbeat(db, "inbox", age_seconds=10)
    with (
        _patch_manifest({"inbox": "degraded"}),
        patch("genesis.mcp.health.manifest._inbox_enabled", return_value=True),
    ):
        hb = await _compute("inbox", db)
    assert hb["status"] == "alive"


@pytest.mark.asyncio
async def test_never_started_integration_real_manifest_file(db, tmp_path):
    """E2E through the REAL _read_persisted_manifest file read (NOT mocked): a real
    bootstrap_manifest.json on disk with ego=ok + an old persisted_at and no pulse →
    never_started/started-silent. Proves the file → JSON parse → verdict chain end to
    end (the unit tests inject the manifest dict; this exercises the actual reader,
    confirming the on-disk key shape {manifest, persisted_at} matches the verdict)."""
    import json as _json

    from genesis.mcp.health import manifest as _m

    manifest_file = tmp_path / "bootstrap_manifest.json"
    manifest_file.write_text(
        _json.dumps({"manifest": {"ego": "ok"}, "persisted_at": _iso_ago(_GRACE_EGO_S + 120)})
    )
    with patch.object(_m, "_MANIFEST_FILE", manifest_file):  # real reader, real file
        hb = await _m.compute_heartbeat_staleness("ego", db=db, paused=False)
    assert hb["status"] == "never_started"
    assert hb.get("reason") == "started-silent"


@pytest.mark.asyncio
async def test_never_started_integration_real_manifest_degraded_inbox(db, tmp_path):
    """E2E real-file read: inbox recorded 'degraded' (the real failed/short-circuited
    init shape) + enabled → never_started/init-failed, straight off disk."""
    import json as _json

    from genesis.mcp.health import manifest as _m

    manifest_file = tmp_path / "bootstrap_manifest.json"
    manifest_file.write_text(
        _json.dumps({"manifest": {"inbox": "degraded"}, "persisted_at": _iso_ago(100)})
    )
    with (
        patch.object(_m, "_MANIFEST_FILE", manifest_file),
        patch("genesis.mcp.health.manifest._inbox_enabled", return_value=True),
    ):
        hb = await _m.compute_heartbeat_staleness("inbox", db=db, paused=False)
    assert hb["status"] == "never_started"
    assert hb.get("reason") == "init-failed"


@pytest.mark.asyncio
async def test_alert_never_started_ego_critical(db):
    with _patch_manifest({"ego": "ok"}, persisted_age_s=_GRACE_EGO_S + 60):
        alerts = await _run_alerts_with_db(db)
    a = next((a for a in alerts if a["id"] == "subsystem_never_started:ego"), None)
    assert a is not None
    assert a["severity"] == "CRITICAL"


@pytest.mark.asyncio
async def test_alert_never_started_inbox_warning(db):
    # _run_alerts_with_db defaults _inbox_enabled True.
    with _patch_manifest({"inbox": "degraded"}):
        alerts = await _run_alerts_with_db(db)
    a = next((a for a in alerts if a["id"] == "subsystem_never_started:inbox"), None)
    assert a is not None
    assert a["severity"] == "WARNING"


@pytest.mark.asyncio
async def test_never_started_alert_absent_when_healthy(db):
    """No manifest (autouse None) + fresh pulses → zero never_started alerts."""
    await _seed_heartbeat(db, "ego", age_seconds=10)
    await _seed_heartbeat(db, "inbox", age_seconds=10)
    alerts = await _run_alerts_with_db(db)
    assert not [a for a in alerts if str(a["id"]).startswith("subsystem_never_started:")]


@pytest.mark.asyncio
async def test_never_started_resolves_on_pulse(db):
    """Once the subsystem pulses, compute flips to alive → no never_started alert (the
    reconcile writer then stamps resolved_at). Auto-resolve is by absence from the set."""
    await _seed_heartbeat(db, "ego", age_seconds=10)  # ego has now pulsed
    with _patch_manifest({"ego": "ok"}, persisted_age_s=_GRACE_EGO_S + 60):
        alerts = await _run_alerts_with_db(db)
    assert not [a for a in alerts if a["id"] == "subsystem_never_started:ego"]


def test_heartbeat_status_set_is_closed():
    """Lock the full status set so a new status can't ship while a consumer that
    switches on status silently mis-renders it."""
    from genesis.mcp.health.manifest import _HEARTBEAT_STATUSES

    assert {
        "alive",
        "overdue",
        "paused",
        "resuming",
        "no_heartbeat",
        "unknown",
        "never_started",
    } == _HEARTBEAT_STATUSES
