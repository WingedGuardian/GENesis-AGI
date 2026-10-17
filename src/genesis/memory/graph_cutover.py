"""The FalkorDB default-on cutover verdict, computed from durable rows only.

Owner rules (decisions cd6dd8c7, 8951a8c3, ba19c19a, c39d2993, 53d9a691): the
graph engine goes default-on after 14 CONSECUTIVE days in falkordb mode with no
fallback, on a live install, where every traversal was on record. Concretely:

* The clock starts at the first census row (``memory/graph_census.py``) in which
  no memory server could be running code older than the traversal telemetry,
  and restarts on a dirty census row, on a census gap longer than one missed
  hourly slot, and on any traversal row that is not a clean falkordb one.
* PASS needs 14 days on the clock, at least 100 traversals served by falkordb in
  the last 14 days, and falkordb traffic on at least 10 distinct UTC days.
* INCONCLUSIVE whenever the evidence itself may be missing: a row carrying
  ``prior_write_failures``, a lost-writes file line, or an unreadable row inside
  the window; no census, or a stale one; telemetry switched off.

Counting is an ALLOWLIST: ``primary`` is traffic, ``cancelled`` is neutral (the
caller gave up; its count and share are always reported), and every other
outcome, including one this module has never heard of, restarts the clock.

Pure: no I/O. ``scripts/graph_cutover_report.py`` reads the rows and prints this.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

WINDOW = timedelta(days=14)
MIN_FALKORDB_TRAVERSALS = 100
MIN_DAYS_WITH_TRAFFIC = 10
#: One missed :45 census slot is tolerated; the jitter margin keeps a single
#: miss from reading as two.
CENSUS_GAP = timedelta(hours=2, minutes=15)
KNOWN_CENSUS_SCHEMAS = frozenset({1})

_DISABLED = "telemetry disabled in genesis-server"
_CLEAN_OUTCOMES = frozenset({"primary"})
_NEUTRAL_OUTCOMES = frozenset({"cancelled"})


@dataclass
class Verdict:
    status: str  # "PASS" | "NOT_YET" | "INCONCLUSIVE"
    now: datetime
    clock_start: datetime | None = None
    reasons: list[str] = field(default_factory=list)
    resets: list[dict] = field(default_factory=list)
    falkordb_traversals: int = 0
    days_with_traffic: int = 0
    cancelled: int = 0
    traversals: int = 0
    census_rows: int = 0
    last_census: datetime | None = None

    def to_json(self) -> dict:
        def iso(t: datetime | None) -> str | None:
            return t.isoformat() if t else None

        return {
            "status": self.status,
            "now": iso(self.now),
            "clock_start": iso(self.clock_start),
            "days_on_clock": self.days_on_clock,
            "reasons": self.reasons,
            "resets": self.resets,
            "falkordb_traversals": self.falkordb_traversals,
            "days_with_traffic": self.days_with_traffic,
            "cancelled": self.cancelled,
            "traversals": self.traversals,
            "census_rows": self.census_rows,
            "last_census": iso(self.last_census),
        }

    @property
    def days_on_clock(self) -> float | None:
        if self.clock_start is None:
            return None
        return round((self.now - self.clock_start) / timedelta(days=1), 2)


def parse_ts(raw: str) -> datetime | None:
    """An ``eval_events.timestamp`` (``%Y-%m-%dT%H:%M:%S.%fZ``) or any ISO-8601
    string, as an aware UTC datetime; ``None`` if it does not parse."""
    try:
        t = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _load(metrics_json: str | None) -> dict | None:
    try:
        m = json.loads(metrics_json) if metrics_json else None
    except (TypeError, ValueError):
        return None
    return m if isinstance(m, dict) else None


def census_dirt(m: dict | None) -> str | None:
    """Why a census row is NOT clean, or ``None`` when it is."""
    if m is None:
        return "unreadable census row"
    if m.get("v") not in KNOWN_CENSUS_SCHEMAS:
        return f"unknown census schema {m.get('v')!r}"
    if "telemetry_disabled" in (m.get("reasons") or []):
        return _DISABLED
    if m.get("complete") is not True:
        return f"incomplete census ({', '.join(m.get('reasons') or []) or 'no reason'})"
    if m.get("unclassified"):
        return f"{m['unclassified']} MCP server(s) of unknown kind"
    if m.get("head_telemetry") is not True:
        return "the checkout's HEAD does not contain the telemetry module"
    if m.get("head_dirty") is not False:
        return "src/genesis/memory/ is dirty in the checkout"
    procs = m.get("procs")
    if not isinstance(procs, list):
        return "unreadable census row"
    for p in procs:
        if not isinstance(p, dict):
            return "unreadable census row"
        if p.get("foreign") is True:
            continue  # a bench server on a scratch DB: its traversals are not ours
        if p.get("telemetry_off") is not False:
            return f"memory server pid {p.get('pid')} has the telemetry kill switch on"
        if p.get("telemetry") is not True:
            why = p.get("unknown") or "started on, or has seen, code without telemetry"
            return f"memory server pid {p.get('pid')} may run old code ({why})"
    return None


def traversal_breaks_clock(m: dict) -> str | None:
    """Why this traversal row restarts the clock, or ``None`` when it does not.
    Raises ``ValueError`` for a row whose shape cannot be trusted."""
    outcomes, served, configured = m.get("outcomes"), m.get("served"), m.get("configured")
    traversals = m.get("traversals")
    if not all(isinstance(x, dict) for x in (outcomes, served, configured)):
        raise ValueError("missing counters")
    if not isinstance(traversals, int) or not all(
        isinstance(n, int) for c in (outcomes, served, configured) for n in c.values()
    ):
        raise ValueError("a count is not an integer")
    for name, counter in (("outcome", outcomes), ("served", served), ("configured", configured)):
        if sum(counter.values()) != traversals:
            raise ValueError(f"{name} counts do not add up to traversals")
    pwf = m.get("prior_write_failures", 0)
    if not isinstance(pwf, int):
        raise ValueError("prior_write_failures is not an integer")
    bad = set(outcomes) - _CLEAN_OUTCOMES - _NEUTRAL_OUTCOMES
    if bad:
        return "outcome " + ", ".join(sorted(bad))
    if set(configured) - {"falkordb"}:
        return "configured " + ", ".join(sorted(configured))
    if set(served) - {"falkordb", "none"}:
        return "served by " + ", ".join(sorted(set(served) - {"falkordb", "none"}))
    return None


def evaluate(
    traverse_rows: list[tuple[str, str | None]],
    census_rows: list[tuple[str, str | None]],
    lost_write_lines: list[str],
    now: datetime,
) -> Verdict:
    """The verdict at ``now`` from ``(timestamp, metrics_json)`` rows of both
    event types (any order) and the lost-writes file's lines."""
    v = Verdict(status="NOT_YET", now=now)
    window_start = now - WINDOW
    integrity: list[str] = []

    # ── the census clock ──────────────────────────────────────────────────
    # Rows after `now` do not exist yet as far as this verdict is concerned
    # (a replay with --now in the past). Equal timestamps put a dirty row last,
    # so a tie can only hold the clock back.
    parsed = []
    for ts, mj in census_rows:
        t, m = parse_ts(ts), _load(mj)
        if t is None:
            integrity.append("a census row has no readable timestamp")
        elif t <= now:
            parsed.append((t, census_dirt(m) is not None, m))
    census = sorted(parsed, key=lambda r: (r[0], r[1]))
    census_clock: datetime | None = None
    prev: datetime | None = None
    last_dirt: str | None = None
    disabled_in_window = False
    for t, _, m in census:
        v.census_rows += 1
        if prev is not None and t - prev > CENSUS_GAP:
            if census_clock is not None:
                v.resets.append(
                    {"at": prev.isoformat(), "why": f"census gap until {t.isoformat()}"}
                )
            census_clock = None
        dirt = census_dirt(m)
        if dirt is None:
            census_clock = census_clock or t
        else:
            if census_clock is not None:
                v.resets.append({"at": t.isoformat(), "why": f"census: {dirt}"})
            census_clock = None
            last_dirt = dirt
            if dirt == _DISABLED and t >= window_start:
                disabled_in_window = True
        prev = t
    v.last_census = prev
    if prev is None:
        integrity.append("no census rows: genesis-server has not run the census")
    elif now - prev > CENSUS_GAP:
        integrity.append(f"census stale: last row {prev.isoformat()}")
        census_clock = None
    elif census_clock is None and last_dirt:
        v.reasons.append(f"census not clean yet: {last_dirt}")
    if disabled_in_window:
        integrity.append("telemetry was switched off inside the window")

    # ── traversal rows ────────────────────────────────────────────────────
    last_reset: datetime | None = None
    days: set = set()
    for ts, mj in traverse_rows:
        t = parse_ts(ts)
        if t is not None and t > now:
            continue
        m = _load(mj)
        try:
            if t is None or m is None:
                raise ValueError("unreadable")
            why = traversal_breaks_clock(m)
        except ValueError as exc:
            if t is None or t >= window_start:
                integrity.append(f"unreadable traversal row ({exc})")
            continue
        if (m.get("prior_write_failures") or 0) > 0 and t >= window_start:
            integrity.append(
                f"{m['prior_write_failures']} telemetry row(s) lost before {t.isoformat()} "
                f"({m.get('proc')}, {m.get('caller')})"
            )
        if why is not None:
            if last_reset is None or t > last_reset:
                last_reset = t
            v.resets.append(
                {
                    "at": t.isoformat(),
                    "why": why,
                    "in_window": t >= window_start,
                    "proc": m.get("proc"),
                    "caller": m.get("caller"),
                    "events": m.get("events"),
                }
            )
            continue
        if t >= window_start:
            outcomes = Counter(m["outcomes"])
            v.traversals += m["traversals"]
            v.cancelled += outcomes.get("cancelled", 0)
            served = m["served"].get("falkordb", 0)
            v.falkordb_traversals += served
            if served:
                days.add(t.astimezone(UTC).date())
    v.days_with_traffic = len(days)

    # ── lost writes recorded outside the database ─────────────────────────
    for line in lost_write_lines:
        if not line.strip():
            continue
        try:
            t = parse_ts(json.loads(line)["ts"])
        except (ValueError, KeyError, TypeError):
            t = None
        if t is None:
            integrity.append("an unreadable line in the lost-writes file")
        elif window_start <= t <= now:
            integrity.append(f"a telemetry row failed to write at {t.isoformat()}")

    # ── the verdict ───────────────────────────────────────────────────────
    starts = [x for x in (census_clock, last_reset) if x is not None]
    v.clock_start = max(starts) if census_clock is not None else None
    v.resets.sort(key=lambda r: r["at"])

    if integrity:
        v.status = "INCONCLUSIVE"
        v.reasons = integrity + v.reasons
        return v
    if v.clock_start is None:
        v.reasons.append("the clock has not started")
        return v
    short: list[str] = []
    if now - v.clock_start < WINDOW:
        short.append(f"{v.days_on_clock} of 14 days on the clock")
    if v.falkordb_traversals < MIN_FALKORDB_TRAVERSALS:
        short.append(f"{v.falkordb_traversals} of {MIN_FALKORDB_TRAVERSALS} falkordb traversals")
    if v.days_with_traffic < MIN_DAYS_WITH_TRAFFIC:
        short.append(f"traffic on {v.days_with_traffic} of {MIN_DAYS_WITH_TRAFFIC} days")
    if short:
        v.reasons.extend(short)
        return v
    v.status = "PASS"
    return v
