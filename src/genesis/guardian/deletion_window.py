"""Maintenance window for the Guardian's ROUTINE snapshot deletions — HOST-SIDE.

Creating a snapshot is cheap; deleting one is not. Deleting a large btrfs
subvolume can stall filesystem commits for minutes under quota (qgroup)
accounting, and every writer on the pool waits on those commits. The daily
healthy-snapshot rotation used to delete the superseded snapshot in the same
second it created the new one, on a cadence that drifts through the day.

So routine deletions wait for a configurable window (``snapshots.
deletion_window``, default 05:00-07:00 in the user's timezone):

* the superseded healthy snapshot left by a rotation (the NEWEST healthy
  snapshot, the rollback lifeline, is never a routine deletion);
* prune's retention and age deletions, and take()'s retention eviction before
  a healthy snapshot.

Pressure never waits: pool relief at its reserve, delete-first rotation, and a
pre-recovery snapshot's eviction act at once, and so does a healthy create the
pool refuses. Early (runway) relief waits only while the pool stays above its
reserve until the window opens (pool_relief.early_waits_for_window).

``"any"`` disables the window and restores immediate routine scheduling.
Failed batches still stop; pressure cooldowns still apply. The env ``GUARDIAN_DELETION_WINDOW``
overrides the config value (same syntax), e.g. ``any`` as an operator lever.

Stdlib only: this runs in the host venv (stdlib + pyyaml).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, tzinfo

logger = logging.getLogger(__name__)

DEFAULT_WINDOW = "05:00-07:00"
ANY_TIME = "any"
ENV_OVERRIDE = "GUARDIAN_DELETION_WINDOW"
_SPEC_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def parse_window(spec: object) -> tuple[time, time] | None:
    """``(start, end)`` for an ``HH:MM-HH:MM`` spec, None for ``"any"``.

    Raises ValueError for anything else, including an empty window
    (start == end: "never" or "always" — ambiguous, so neither is guessed).
    An end earlier than the start crosses midnight.
    """
    if isinstance(spec, str) and spec.strip().lower() == ANY_TIME:
        return None
    m = _SPEC_RE.match(spec) if isinstance(spec, str) else None
    if not m:
        raise ValueError(f"deletion_window={spec!r} is not 'HH:MM-HH:MM' or 'any'")
    h1, m1, h2, m2 = (int(g) for g in m.groups())
    if not (0 <= h1 <= 23 and 0 <= h2 <= 23 and 0 <= m1 <= 59 and 0 <= m2 <= 59):
        raise ValueError(f"deletion_window={spec!r} has an out-of-range time")
    start, end = time(h1, m1), time(h2, m2)
    if start == end:
        raise ValueError(f"deletion_window={spec!r} starts and ends at the same time")
    return start, end


@dataclass(frozen=True)
class DeletionWindow:
    """A daily local-time window; ``start is None`` means any time."""

    start: time | None
    end: time | None
    tz: tzinfo
    tz_name: str
    # Where the zone came from: "config" (deletion_window_timezone), "user"
    # (genesis.env.user_timezone()), or "fallback" — no source on this host,
    # so UTC. A host Guardian usually has no genesis.yaml; disk-status shows
    # this so a fallback is visible rather than silently 05:00 UTC.
    tz_source: str = "config"

    @property
    def any_time(self) -> bool:
        return self.start is None

    def describe(self) -> str:
        if self.any_time:
            return "any time"
        return f"{self.start:%H:%M}-{self.end:%H:%M} {self.tz_name}"

    def contains(self, now: datetime) -> bool:
        """Whether ``now`` (aware) falls inside the window, by LOCAL wall clock.

        Start inclusive, end exclusive. Wall-clock comparison makes DST
        natural: a window inside a skipped hour does not occur that day (the
        next one catches up); one inside a repeated hour lasts both passes.
        """
        if self.start is None or self.end is None:
            return True
        t = now.astimezone(self.tz).time()
        if self.start < self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end

    def occurrence_start(self, now: datetime) -> datetime | None:
        """Start of the window occurrence containing ``now``, or None outside.

        A once-per-window pass runs when its last stamp is older than this, so
        a missed window is caught up in the next one, and a repeated DST hour
        does not run it twice.
        """
        if self.start is None or not self.contains(now):
            return None
        local = now.astimezone(self.tz)
        day = local.date()
        if self.start > self.end and local.time() < self.end:
            day -= timedelta(days=1)  # crosses midnight: began yesterday
        return datetime.combine(day, self.start, tzinfo=self.tz)

    def hours_until_open(self, now: datetime) -> float:
        """Hours until the window next opens; 0 inside it (or any time)."""
        if self.start is None or self.contains(now):
            return 0.0
        return (self.next_open(now) - now.astimezone(UTC)).total_seconds() / 3600.0

    def next_open(self, now: datetime) -> datetime:
        """Next real opening, skipping this daily occurrence when inside it.

        Search actual UTC instants at the config's minute precision. Local
        datetime construction alone invents instants during DST gaps. Four
        days cover a skipped hour or an entire skipped local date; folds of
        the current occurrence are skipped together. Modern IANA offsets are
        minute-aligned; historical sub-minute offsets are not a scheduling API.
        """
        if self.any_time:
            return now.astimezone(UTC)
        current = self.occurrence_start(now)
        candidate = now.astimezone(UTC).replace(second=0, microsecond=0)
        if candidate < now.astimezone(UTC):
            candidate += timedelta(minutes=1)
        for _ in range(4 * 24 * 60):
            occurrence = self.occurrence_start(candidate)
            if occurrence is not None and occurrence != current:
                return candidate
            candidate += timedelta(minutes=1)
        raise ValueError("no real deletion-window opening within four days")


def _zone(name: str) -> tzinfo | None:
    try:
        # Lazy, as in genesis.env: importing zoneinfo reads sysconfig at import
        # time, which the host-import contract probe cannot resolve.
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        return ZoneInfo(name)
    except Exception:  # ZoneInfoNotFoundError, ValueError, a missing tz database
        return None


def _user_tz_configured() -> bool:
    """Whether this host names a user timezone at all (genesis.yaml
    ``timezone`` or ``USER_TIMEZONE``) — else user_timezone()'s UTC is a
    fallback, not a choice."""
    try:
        from genesis.env import _local_config

        named = (_local_config().get("timezone"), os.environ.get("USER_TIMEZONE"))
        return any(isinstance(v, str) and v.strip() and _zone(v.strip()) for v in named)
    except Exception:
        return False


def _resolve_tz(configured: object) -> tuple[tzinfo, str, str]:
    """The window's zone ``(tz, name, source)``: the configured IANA name,
    else the user timezone, else UTC."""
    if isinstance(configured, str) and configured.strip():
        zone = _zone(configured.strip())
        if zone is not None:
            return zone, configured.strip(), "config"
        logger.warning(
            "snapshots.deletion_window_timezone=%r is not a valid zone — using the user timezone",
            configured,
        )
    try:
        from genesis.env import user_timezone

        name = user_timezone()
    except Exception:
        logger.warning("could not resolve the user timezone — using UTC", exc_info=True)
        return UTC, "UTC", "fallback"
    zone = _zone(name)
    if zone is None:
        logger.warning("timezone %r unavailable on this host — using UTC", name)
        return UTC, "UTC", "fallback"
    return zone, name, "user" if _user_tz_configured() else "fallback"


def resolve_window(config) -> DeletionWindow:
    """The effective window. Never raises; an invalid spec -> the default.

    Invalid degrades toward LESS deletion authority (the default window), not
    toward "any": a typo must not put deletions back into working hours.
    """
    snaps = config.snapshots
    spec = os.environ.get(ENV_OVERRIDE) or getattr(snaps, "deletion_window", DEFAULT_WINDOW)
    try:
        bounds = parse_window(spec)
    except ValueError as exc:
        logger.warning("%s — using the default %s", exc, DEFAULT_WINDOW)
        bounds = parse_window(DEFAULT_WINDOW)
    if bounds is None:
        return DeletionWindow(None, None, UTC, "UTC")
    tz, tz_name, source = _resolve_tz(getattr(snaps, "deletion_window_timezone", ""))
    return DeletionWindow(bounds[0], bounds[1], tz, tz_name, source)
