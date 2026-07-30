"""NetworkSentinel — deterministic internet-connectivity detection.

The ~22.5h outage of 2026-07-28 exposed that Genesis had no first-class concept
of "the internet is down": every subsystem discovered the outage independently
(as provider failures, backup push errors, CC timeouts) and misattributed it.
This worker is the single ground-truth source. It directly probes the open
internet on a cadence and publishes a 3-state axis (NORMAL / DEGRADED / OFFLINE)
that drives watchdog forgiveness (skip zombie restarts a restart can't fix),
recovery gating (don't re-dispatch into a dead network), the dashboard "Internet"
light, and (PR-3/PR-4) degraded-mode parking + backup push retry.

Design (see plan ok-lets-plan-everything-sunny-diffie.md, PR-2):
- **Cause, not symptom.** CloudStatus is circuit-breaker-derived (a symptom of
  provider call outcomes); this is a direct DNS+TCP probe (the cause).
- **Restart-proof.** State is re-detected within ~1 probe of boot, so a watchdog
  restart (which wipes in-memory circuit-breaker state) can't lose the signal.
- **Asymmetric hysteresis** (owns its own — opts out of the state machine's
  symmetric flap protection, see ResilienceStateMachine.update_network): fast to
  declare OFFLINE (a couple all-fail rounds), slow to clear (a consecutive clean
  streak). A DNS-only failure (resolution dead, routing alive) caps at DEGRADED —
  forgiveness without parking.
- **Injectable prober + clock** for hermetic, network-free tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from genesis.resilience import network_config, network_state
from genesis.resilience.network_config import NetworkTuning
from genesis.resilience.state import NetworkStatus

logger = logging.getLogger(__name__)

# Round classifications, worst → best readability (not an ordering).
CLEAN = "clean"  # every anchor reachable
PARTIAL = "partial"  # some anchors reachable, mixed failures
DNS_ONLY = "dns_only"  # routing alive (IP anchors OK), name resolution dead
ALL_FAIL = "all_fail"  # nothing reachable
NO_ANCHORS = "no_anchors"  # sentinel enabled but zero probe anchors configured

# Per-anchor probe outcomes.
_OK = "ok"
_DNS_FAIL = "dns_fail"
_TCP_FAIL = "tcp_fail"

# Prober signature: given tuning, return the round classification string.
Prober = Callable[[NetworkTuning], Awaitable[str]]


def classify_round(dns_tcp_results: list[str], ip_results: list[str]) -> str:
    """Pure round classifier from per-anchor outcomes.

    - ``clean``    — every probed anchor OK.
    - ``all_fail`` — no anchor OK (routing itself is dead).
    - ``dns_only`` — every IP-literal anchor OK but every DNS+TCP anchor failed
      at the *resolution* step: the network routes, DNS does not. Capped at
      DEGRADED by the hysteresis (never OFFLINE) — a nameserver blip is not an
      outage worth parking cognition for.
    - ``partial``  — anything else (a genuine mix of reachable/unreachable).
    """
    all_results = dns_tcp_results + ip_results
    if not all_results:
        # Defensive only: _probe_round guards the zero-anchor case BEFORE calling
        # the prober (surfacing it as 'not configured'), so this branch is not
        # reached in normal flow. Return CLEAN as an inert default for any direct
        # caller that passes empty lists.
        return CLEAN
    ok = sum(1 for r in all_results if r == _OK)
    if ok == len(all_results):
        return CLEAN
    if ok == 0:
        return ALL_FAIL
    # Mixed. DNS-only iff routing demonstrably works (≥1 IP anchor, all IP OK)
    # while every DNS+TCP anchor died specifically at resolution.
    ip_all_ok = bool(ip_results) and all(r == _OK for r in ip_results)
    dns_all_resolution_fail = bool(dns_tcp_results) and all(r == _DNS_FAIL for r in dns_tcp_results)
    if ip_all_ok and dns_all_resolution_fail:
        return DNS_ONLY
    return PARTIAL


async def _probe_anchor_dns_tcp(host: str, port: int, timeout_s: int) -> str:
    """DNS-resolve then TCP-connect. Distinguishes a resolution failure from a
    routing failure so ``classify_round`` can isolate a DNS-only outage."""
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), timeout=timeout_s
        )
    except (TimeoutError, socket.gaierror, OSError):
        return _DNS_FAIL
    if not infos:
        return _DNS_FAIL
    ip = infos[0][4][0]
    return await _probe_anchor_ip(ip, port, timeout_s)


async def _probe_anchor_ip(ip: str, port: int, timeout_s: int) -> str:
    """TCP-connect to an IP literal (no DNS)."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=timeout_s)
    except (TimeoutError, OSError):
        return _TCP_FAIL
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return _OK


async def default_prober(tuning: NetworkTuning) -> str:
    """Real prober: probe all anchors concurrently, classify the round."""
    dns_tcp_tasks = [
        _probe_anchor_dns_tcp(h, tuning.probe_port, tuning.probe_timeout_s)
        for h in tuning.dns_tcp_anchors
    ]
    ip_tasks = [
        _probe_anchor_ip(ip, tuning.probe_port, tuning.probe_timeout_s) for ip in tuning.ip_anchors
    ]
    results = await asyncio.gather(*dns_tcp_tasks, *ip_tasks)
    n_dns = len(dns_tcp_tasks)
    return classify_round(list(results[:n_dns]), list(results[n_dns:]))


class NetworkSentinel:
    """Probes connectivity on a cadence; publishes a 3-state network axis."""

    def __init__(
        self,
        *,
        state_machine,
        prober: Prober | None = None,
        clock: Callable[[], datetime] | None = None,
        tuning_provider: Callable[[], NetworkTuning] | None = None,
        state_path: Path | None = None,
        on_stable_online: Callable[[], None] | None = None,
    ) -> None:
        self._state_machine = state_machine
        self._prober = prober or default_prober
        self._clock = clock or (lambda: datetime.now(UTC))
        # Re-read tuning each round so an operator can retune anchors/cadence
        # without a restart (repo_pulse live-read discipline).
        self._tuning_provider = tuning_provider or network_config.structural
        self._state_path = state_path
        self._on_stable_online = on_stable_online or self._default_recovery_hook

        now = self._clock()
        self._state: NetworkStatus = NetworkStatus.NORMAL
        self._state_since: datetime = now
        self._last_cause: str = CLEAN
        self._last_probe_at: datetime | None = None
        self._consecutive_all_fail = 0
        self._consecutive_clean = 0
        # Recovery hook is a ONE-SHOT that re-arms on each OFFLINE entry. Start
        # fired (True) so a fresh server that boots ONLINE never fires it — only
        # a genuine outage (OFFLINE) arms the next recovery.
        self._recovery_hook_fired = True
        self._open_window: dict | None = None
        self._closed_windows: list[dict] = []
        self._task: asyncio.Task | None = None

        # Restore persisted outage-window state so a restart doesn't erase history
        # or reset an in-progress outage's start (Codex P2).
        self._hydrate()

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the probe loop (idempotent)."""
        if self._task is None or self._task.done():
            from genesis.util.tasks import tracked_task

            self._task = tracked_task(self._loop(), name="network-sentinel")
            logger.info("Network sentinel started")

    async def stop(self) -> None:
        """Cancel the probe loop and unwind (idempotent)."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
            logger.info("Network sentinel stopped")

    # Safe cadence used when a round itself raises (so a transient failure to
    # read tuning can never leave the loop with an undefined sleep interval).
    _FALLBACK_CADENCE_S = 120

    async def _loop(self) -> None:
        while True:
            # _probe_round both runs the round AND returns the next cadence, so a
            # raising tuning_provider is caught HERE (not left to kill the loop on
            # a separate unguarded cadence read). On failure we fall back to a
            # safe steady cadence and try again next tick — the loop self-heals.
            cadence = self._FALLBACK_CADENCE_S
            try:
                cadence = await self._probe_round()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Network sentinel probe round failed", exc_info=True)
            # Cadence AFTER the round so the first probe fires promptly at start.
            try:
                await asyncio.sleep(cadence)
            except asyncio.CancelledError:
                raise

    def _cadence_for(self, tuning: NetworkTuning) -> int:
        # Fast cadence whenever we're not solidly green — catch recovery quickly.
        if self._state == NetworkStatus.NORMAL and self._last_cause == CLEAN:
            return tuning.steady_cadence_s
        return tuning.fast_cadence_s

    # ── one probe round ──────────────────────────────────────────────────────

    async def _probe_round(self) -> int:
        """Run one probe round; return the cadence (seconds) until the next one."""
        tuning = self._tuning_provider()

        # Zero-anchor guard: an enabled sentinel with BOTH anchor lists empty
        # cannot assert connectivity. Do NOT probe (classify_round([], []) would
        # return CLEAN and pin the axis NORMAL forever). Surface the
        # misconfiguration via a fresh `no_anchors` marker (dashboard "not
        # configured" light + WARNING log + the cause field in status.json).
        # The AXIS is deliberately left untouched (NORMAL). This is NOT a false
        # green: it is the correct fail-OPEN default — with zero connectivity
        # data we must not take degraded-mode actions. Routing to DEGRADED/OFFLINE
        # would make the WATCHDOG suppress zombie restarts (and, at OFFLINE, pause
        # DLQ replay) on a mere config typo — strictly worse than doing nothing.
        if not tuning.dns_tcp_anchors and not tuning.ip_anchors:
            logger.warning(
                "Network sentinel has zero probe anchors configured — cannot "
                "assert connectivity (check network.dns_tcp_anchors / "
                "network.ip_anchors); surfacing as 'not configured'"
            )
            self._last_probe_at = self._clock()
            self._last_cause = NO_ANCHORS
            # A no-anchors round is neither clean nor all-fail progress — reset
            # both streaks so a live config gap (anchors emptied then refilled)
            # cannot carry a stale streak across it and reach NORMAL/OFFLINE early.
            self._consecutive_clean = 0
            self._consecutive_all_fail = 0
            self._publish(tuning)
            return self._cadence_for(tuning)

        round_class = await self._prober(tuning)
        now = self._clock()
        self._last_probe_at = now
        self._last_cause = round_class

        new_state = self._next_state(round_class, tuning)
        if new_state != self._state:
            self._transition(new_state, now, tuning)

        # Publish EVERY round (not just on change): last_probe_at must stay fresh
        # or consumers (watchdog, dashboard) will read a stable-green network as
        # "stale" and fail-safe unnecessarily.
        self._publish(tuning)

        # Fire the one-shot recovery hook once ONLINE has held long enough.
        self._maybe_fire_recovery(now, tuning)
        return self._cadence_for(tuning)

    def _next_state(self, round_class: str, tuning: NetworkTuning) -> NetworkStatus:
        """Pure asymmetric hysteresis. Updates the streak counters as a side
        effect (they are per-round state, not cross-cutting)."""
        if round_class == CLEAN:
            self._consecutive_clean += 1
            self._consecutive_all_fail = 0
            if self._state == NetworkStatus.OFFLINE:
                # Fast recovery OFF offline → DEGRADED; the clean STREAK (counted
                # from here) still has to reach the threshold to hit NORMAL.
                return NetworkStatus.DEGRADED
            if self._state == NetworkStatus.DEGRADED:
                if self._consecutive_clean >= tuning.online_clean_rounds:
                    return NetworkStatus.NORMAL
                return NetworkStatus.DEGRADED
            return NetworkStatus.NORMAL  # already NORMAL, stay

        # Any non-clean round breaks the clean streak.
        self._consecutive_clean = 0
        if round_class == ALL_FAIL:
            # Counts EVERY consecutive all_fail, including the NORMAL→DEGRADED
            # entry round: OFFLINE fires after `offline_all_fail_rounds` consecutive
            # all_fail rounds total (a NORMAL round never jumps straight to OFFLINE
            # — the NORMAL branch below always routes through DEGRADED first).
            self._consecutive_all_fail += 1
        else:
            # partial / dns_only cannot drive OFFLINE — reset the all-fail streak.
            self._consecutive_all_fail = 0

        if self._state == NetworkStatus.NORMAL:
            return NetworkStatus.DEGRADED  # 1 non-clean round → DEGRADED (fast)
        if self._state == NetworkStatus.DEGRADED:
            if (
                round_class == ALL_FAIL
                and self._consecutive_all_fail >= tuning.offline_all_fail_rounds
            ):
                return NetworkStatus.OFFLINE
            return NetworkStatus.DEGRADED
        # current OFFLINE: any anchor OK (partial/dns_only — not all_fail) recovers
        # to DEGRADED; all_fail holds OFFLINE.
        if round_class == ALL_FAIL:
            return NetworkStatus.OFFLINE
        return NetworkStatus.DEGRADED

    def _transition(self, new_state: NetworkStatus, now: datetime, tuning: NetworkTuning) -> None:
        old = self._state
        self._state = new_state
        self._state_since = now
        # Publish to the composite state machine (drives is_any_degraded +
        # resilience_state display; NOT the legacy level in PR-2).
        self._state_machine.update_network(new_state)

        if new_state == NetworkStatus.OFFLINE:
            # Re-arm recovery FIRST: if opening the window ever raised, this
            # outage's recovery hook must still be armed (else PR-4's push-retry
            # would silently never fire on recovery). Window-open takes the SAME
            # tuning snapshot as this round (no re-read → mid-round config change
            # can't split a mergeable window).
            self._recovery_hook_fired = False
            self._open_or_extend_window(now, tuning)
        elif old == NetworkStatus.OFFLINE:
            # Left OFFLINE (→ DEGRADED) — close the outage window.
            self._close_window(now)

        logger.info("Network sentinel: %s → %s (%s)", old.name, new_state.name, self._last_cause)

    # ── outage windows ───────────────────────────────────────────────────────

    def _open_or_extend_window(self, now: datetime, tuning: NetworkTuning) -> None:
        if self._open_window is not None:
            return  # already open (shouldn't happen — OFFLINE→OFFLINE isn't a txn)
        # Merge with the most-recent closed window if the gap is short — an
        # intermittent outage is ONE window, not a storm of tiny ones.
        if self._closed_windows:
            last = self._closed_windows[-1]
            with contextlib.suppress(ValueError, KeyError, TypeError):
                end = datetime.fromisoformat(last["end"])
                if end.tzinfo is None and now.tzinfo is not None:
                    end = end.replace(tzinfo=now.tzinfo)
                if (now - end).total_seconds() <= tuning.merge_gap_s:
                    self._open_window = self._closed_windows.pop()
                    self._open_window.pop("end", None)
                    # Reflect the latest onset cause on the merged span (a
                    # dns_only blip that escalates to all_fail should read
                    # all_fail, not the first blip's cause).
                    self._open_window["cause"] = self._last_cause
                    return
        self._open_window = {"start": now.isoformat(), "cause": self._last_cause}

    def _close_window(self, now: datetime) -> None:
        if self._open_window is None:
            return
        self._open_window["end"] = now.isoformat()
        self._closed_windows.append(self._open_window)
        self._open_window = None
        # Bound in memory too (the file is capped independently on write).
        if len(self._closed_windows) > network_state.MAX_CLOSED_WINDOWS:
            self._closed_windows = self._closed_windows[-network_state.MAX_CLOSED_WINDOWS :]

    # ── recovery hook ────────────────────────────────────────────────────────

    def _maybe_fire_recovery(self, now: datetime, tuning: NetworkTuning) -> None:
        if self._recovery_hook_fired or self._state != NetworkStatus.NORMAL:
            return
        if (now - self._state_since).total_seconds() < tuning.stable_online_s:
            return
        self._recovery_hook_fired = True
        try:
            # Sync callback by contract (Callable[[], None]). PR-4 must inject a
            # SYNC hook (or one that schedules its own task) — an async def here
            # would create an un-awaited coroutine that silently no-ops.
            self._on_stable_online()
        except Exception:
            logger.warning("Network sentinel recovery hook raised", exc_info=True)

    def _default_recovery_hook(self) -> None:
        # PR-2 no-op (log only). PR-4 injects the backup-push-retry callback.
        # NOTE (architect F1c): this can fire inside a still-mergeable window
        # (merge_gap 600s > stable_online 300s), so PR-4's action MUST be
        # idempotent / window-independent.
        logger.info("Network sentinel: connectivity stable-online — recovery hook (no-op)")

    # ── publish / snapshot ───────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Immutable snapshot for the status writer's top-level ``network`` key.

        Returns a FRESH dict of primitives (no live reference to internal
        window state) so a status-write mid-mutation can never serialize a torn
        view. Carries ``last_probe_at`` so the watchdog can staleness-check the
        sentinel's own probe freshness rather than trusting status.json's file
        timestamp (which the 60s status loop refreshes independently)."""
        return {
            "state": self._state.name,
            "since": self._state_since.isoformat(),
            "cause": self._last_cause,
            "last_probe_at": (self._last_probe_at.isoformat() if self._last_probe_at else None),
            "window_open": self._open_window is not None,
        }

    def _publish(self, tuning: NetworkTuning) -> None:
        data = self.snapshot()
        data["closed_windows"] = [dict(w) for w in self._closed_windows]
        # Persist the OPEN window too (start + cause), so a restart mid-outage
        # can restore the original outage start rather than losing it.
        data["open_window"] = dict(self._open_window) if self._open_window else None
        network_state.write_state(data, self._state_path)

    def _hydrate(self) -> None:
        """Restore persisted outage-window state on construction.

        The store is a PERSISTENT outage-window store: without this, the first
        probe after ANY restart would publish empty collections over
        network_state.json, erasing closed-window history and — mid-outage —
        losing the original outage start (the motivating 22.5h outage spanned 25
        restarts). Fully fail-safe: a missing/corrupt/partial file leaves the
        fresh empty state. The OPEN window and the OFFLINE axis are restored
        together (coupled — a window is only ever open while OFFLINE) so a
        restored window can never be orphaned; DEGRADED/NORMAL simply re-detect
        on the first probe."""
        data = network_state.read_state(self._state_path)
        if not isinstance(data, dict):
            return
        cw = data.get("closed_windows")
        if isinstance(cw, list):
            valid = [
                dict(w)
                for w in cw
                if isinstance(w, dict)
                and isinstance(w.get("start"), str)
                and isinstance(w.get("end"), str)
            ]
            self._closed_windows = valid[-network_state.MAX_CLOSED_WINDOWS :]

        state_name = data.get("state")
        ow = data.get("open_window")
        # Only restore an open outage (and its OFFLINE axis) when the persisted
        # state agrees it was OFFLINE — keeps window/axis coherent.
        if (
            state_name == NetworkStatus.OFFLINE.name
            and isinstance(ow, dict)
            and isinstance(ow.get("start"), str)
        ):
            self._open_window = dict(ow)
            self._state = NetworkStatus.OFFLINE
            since = data.get("since")
            if isinstance(since, str):
                try:
                    parsed = datetime.fromisoformat(since)
                    self._state_since = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
                except ValueError:
                    pass
            cause = data.get("cause")
            if isinstance(cause, str):
                self._last_cause = cause
            self._recovery_hook_fired = False  # mid-outage → recovery must fire on return
            # Reflect the restored axis on the shared state machine immediately so
            # is_any_degraded / status.json are correct before the first probe.
            self._state_machine.update_network(NetworkStatus.OFFLINE)
