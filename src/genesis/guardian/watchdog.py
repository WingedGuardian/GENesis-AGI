"""GuardianWatchdog — CONTAINER-SIDE. Monitors Guardian health from inside the container.

Called every awareness tick (5 min). Reads the Guardian heartbeat file,
triggers SSH recovery if stale, and escalates via Telegram if recovery fails.

When Guardian is stuck in confirmed_dead (state machine won't auto-reset and
timer restarts don't help), escalates to reset-state via SSH.

Also performs code drift detection: compares container's Guardian-relevant
commit hash with the host's deployed version, alerting if they diverge.

# GROUNDWORK(guardian-bidirectional): Container-side monitoring of host Guardian
"""

from __future__ import annotations

import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class GuardianWatchdog:
    """Container-side Guardian health monitor with automatic recovery.

    Reads the heartbeat file written by the Guardian into the container.
    When the heartbeat is stale (>STALE_THRESHOLD_S), attempts to restart
    the Guardian timer via SSH. A cooldown prevents restart storms.

    When Guardian is stuck in confirmed_dead across multiple ticks (timer
    restarts don't help), issues a reset-state command to force the state
    machine back to healthy.
    """

    RECOVERY_COOLDOWN_S = 900   # 15 min between restart attempts
    STALE_THRESHOLD_S = 300     # 5 min = DOWN (matches probe_guardian default)
    STUCK_THRESHOLD = 2         # Consecutive ticks seeing confirmed_dead before reset
    RESET_COOLDOWN_S = 1800     # 30 min between reset attempts (conservative)

    # Paths that constitute "Guardian-relevant code" for drift detection.
    _GUARDIAN_PATHS = [
        "src/genesis/guardian", "src/genesis/util", "src/genesis/env.py",
        "src/genesis/observability", "src/genesis/db",
        "config/guardian-claude.md", "pyproject.toml",
        "scripts/install_guardian.sh", "scripts/guardian-gateway.sh",
        "scripts/lib/host_swap.sh", "scripts/lib/cc_tmp_volume.sh",
    ]
    DRIFT_ALERT_THRESHOLD = 3   # Consecutive drifted ticks before alerting
    CC_TOKEN_WARN_AGE_DAYS = 335  # warn ~30d before a 1-year setup-token expires
    _CC_TOKEN_MAX_AGE_DAYS = 365  # setup-token lifetime; older = expired/unusable

    def __init__(
        self,
        remote,  # GuardianRemote — import avoided for loose coupling
        event_bus=None,
        outreach_pipeline=None,  # OutreachPipeline — for user-facing alerts
    ) -> None:
        self._remote = remote
        self._event_bus = event_bus
        self._outreach_pipeline = outreach_pipeline
        self._sentinel = None
        self._last_recovery_at: datetime | None = None
        self._last_reset_at: datetime | None = None
        self._consecutive_stuck: int = 0
        self._recovery_failed_escalated: bool = False  # one alert per DOWN episode
        self._drift_count: int = 0
        self._drift_escalated: bool = False  # one user alert per drift episode
        # Deployed-gateway staleness: the install-dir code can be current while
        # ~/.local/bin/guardian-gateway.sh lags (the bug that froze it ~2 months).
        self._gateway_drift_count: int = 0
        self._gateway_resync_attempted: bool = False
        self._gateway_escalated: bool = False
        # authorized_keys hardening reconciler: heal a guardian key line that
        # lost no-pty/from= (regression) or whose from= no longer matches the
        # container's source (a stable move heals; a flapping source escalates).
        self._authkey_drift_count: int = 0
        self._authkey_reharden_attempted: bool = False
        self._authkey_escalated: bool = False
        self._authkey_flap_escalated: bool = False
        self._authkey_last_src_hash: str | None = None
        # CC recovery-brain auth health: alert when the host login is dead with
        # no usable fallback token (dead-brain), and warn before the synced
        # setup-token expires. Alert-only — no safe non-interactive remediation
        # (claude login / setup-token both need a human; the token re-copy is the
        # diagnosis fallback's + credential-bridge's job). The expiry-warning
        # guard has its OWN token-refresh lifecycle, NOT the health reset.
        self._cc_auth_drift_count: int = 0
        self._cc_auth_escalated: bool = False
        self._cc_token_expiry_warned: bool = False
        # systemd-linger health: alert when linger is disabled (user timers die
        # silently on next logout). Two INDEPENDENT legs with separate episode
        # state — host (read from the gateway version payload) and container (a
        # local loginctl probe). Alert-only: enable-linger is privileged +
        # interactive, so there is no safe non-interactive remediation this ship.
        self._host_linger_drift_count: int = 0
        self._host_linger_escalated: bool = False
        self._container_linger_drift_count: int = 0
        self._container_linger_escalated: bool = False

    async def _alert_user(self, *, topic: str, context: str, source_id: str) -> None:
        """Deliver a user-facing (Telegram) alert about a Guardian degradation.

        Uses the outreach pipeline's ``submit_raw`` — the idiomatic urgent-infra
        path (BLOCKER category → Telegram, governance skipped, built-in dedup),
        the same one health alerts use. No-op when the pipeline isn't wired
        (graceful degradation). Wrapped so an outreach failure NEVER breaks a
        watchdog tick. Each call site passes a DISTINCT ``topic`` so the
        pipeline's (signal_type, topic, category) dedup never cross-suppresses
        one Guardian alert with another.
        """
        if not self._outreach_pipeline:
            return
        try:
            from genesis.outreach.types import OutreachCategory, OutreachRequest

            await self._outreach_pipeline.submit_raw(
                context,
                OutreachRequest(
                    category=OutreachCategory.BLOCKER,
                    topic=topic,
                    context=context,
                    salience_score=1.0,
                    signal_type="guardian_alert",
                    source_id=source_id,
                ),
            )
        except Exception:
            logger.warning(
                "Failed to send Guardian alert (%s)", source_id, exc_info=True,
            )

    def _in_cooldown(self) -> bool:
        if self._last_recovery_at is None:
            return False
        elapsed = (datetime.now(UTC) - self._last_recovery_at).total_seconds()
        return elapsed < self.RECOVERY_COOLDOWN_S

    def _in_reset_cooldown(self) -> bool:
        if self._last_reset_at is None:
            return False
        elapsed = (datetime.now(UTC) - self._last_reset_at).total_seconds()
        return elapsed < self.RESET_COOLDOWN_S

    def set_sentinel(self, sentinel) -> None:
        """Inject Sentinel dispatcher for escalation on reset-state failure."""
        self._sentinel = sentinel

    async def check_and_recover(self) -> None:
        """Check Guardian heartbeat and attempt recovery if stale.

        Called from the awareness loop tick. Safe to call frequently —
        returns immediately if Guardian is healthy or cooldown is active.

        Recovery escalation:
        1. First detection: restart-timer via SSH
        2. If stuck in confirmed_dead for STUCK_THRESHOLD ticks: reset-state
        """
        from genesis.observability.health import ProbeStatus, probe_guardian

        # Drift detection runs regardless of health status — its purpose is
        # to catch stale host code even when Guardian appears healthy.
        await self._check_code_drift()

        # Container-local systemd-linger health — runs on EVERY tick, deliberately
        # NOT inside _check_code_drift: a purely-local probe must not be disabled
        # by a feature-branch checkout, a missing update baseline, or a down host
        # (all of which early-return in the drift path). Own suppress so a probe
        # failure can't perturb recovery below.
        import contextlib
        with contextlib.suppress(Exception):
            await self._check_container_linger()

        result = await probe_guardian(guardian_remote=self._remote)

        if result.status != ProbeStatus.DOWN:
            self._consecutive_stuck = 0
            self._recovery_failed_escalated = False  # re-arm for the next episode
            return

        staleness = result.details.get("staleness_s", 0) if result.details else 0

        # Step 1: Try restart-timer if not in cooldown
        if not self._in_cooldown():
            logger.warning(
                "Guardian DOWN (stale %.0fs) — attempting restart via SSH", staleness,
            )
            success = await self._remote.restart()
            self._last_recovery_at = datetime.now(UTC)

            if success:
                logger.info("Guardian restart command sent — will verify on next tick")
                if self._event_bus:
                    from genesis.observability.types import Severity, Subsystem
                    await self._event_bus.emit(
                        Subsystem.GUARDIAN, Severity.WARNING,
                        "guardian.recovery.attempted",
                        f"Guardian heartbeat stale ({staleness:.0f}s) — "
                        "restart-timer sent via SSH",
                    )
            else:
                logger.error("Guardian restart failed via SSH — escalating to user")
                if self._event_bus:
                    from genesis.observability.types import Severity, Subsystem
                    await self._event_bus.emit(
                        Subsystem.GUARDIAN, Severity.ERROR,
                        "guardian.recovery.failed",
                        f"Guardian restart via SSH failed (stale {staleness:.0f}s)",
                    )
                if not self._recovery_failed_escalated:
                    self._recovery_failed_escalated = True
                    await self._alert_user(
                        topic="Guardian is DOWN",
                        context=(
                            f"🚨 Guardian is DOWN (heartbeat stale "
                            f"{staleness:.0f}s) and SSH restart failed. "
                            "Manual intervention needed."
                        ),
                        source_id="guardian:recovery_failed",
                    )

        # Step 2: Check if Guardian is stuck in confirmed_dead
        await self._check_stuck_state()

    async def _check_stuck_state(self) -> None:
        """Detect and recover from Guardian stuck in confirmed_dead.

        Timer restarts don't reset the state machine. If Guardian is stuck
        in confirmed_dead for STUCK_THRESHOLD consecutive ticks, issue a
        reset-state command to force it back to healthy.
        """
        try:
            status = await self._remote.status()
        except Exception:
            logger.warning("Could not query Guardian status for stuck detection", exc_info=True)
            return

        current_state = status.get("current_state", "unknown")

        if current_state in ("confirmed_dead", "recovering", "recovered"):
            self._consecutive_stuck += 1
            logger.info(
                "Guardian state is %s (consecutive stuck count: %d/%d)",
                current_state, self._consecutive_stuck, self.STUCK_THRESHOLD,
            )

            if self._consecutive_stuck >= self.STUCK_THRESHOLD and not self._in_reset_cooldown():
                logger.warning(
                    "Guardian stuck in %s for %d consecutive checks — resetting state",
                    current_state, self._consecutive_stuck,
                )
                result = await self._remote.reset_state()
                self._last_reset_at = datetime.now(UTC)

                if result.get("ok"):
                    stuck_count = self._consecutive_stuck
                    self._consecutive_stuck = 0
                    logger.info(
                        "Guardian state reset from %s to healthy — restarting timer",
                        result.get("previous_state", "unknown"),
                    )
                    await self._remote.restart()
                    if self._event_bus:
                        from genesis.observability.types import Severity, Subsystem
                        await self._event_bus.emit(
                            Subsystem.GUARDIAN, Severity.WARNING,
                            "guardian.state_reset",
                            f"Guardian stuck in {current_state} — "
                            f"reset to healthy after {stuck_count} checks",
                        )
                else:
                    logger.error(
                        "Guardian reset-state failed: %s", result.get("error", "unknown"),
                    )
                    # Dispatch Sentinel to diagnose why reset-state failed
                    if self._sentinel is not None:
                        from genesis.util.tasks import tracked_task
                        tracked_task(
                            self._sentinel.escalate_direct(
                                trigger_source="watchdog_reset_failed",
                                tier=1,
                                reason=f"Guardian reset-state failed: {result.get('error', 'unknown')}",
                                context={"current_state": current_state, "error": result.get("error")},
                            ),
                            name="sentinel-reset-failed",
                        )
        else:
            self._consecutive_stuck = 0

    async def _check_code_drift(self) -> None:
        """Detect Guardian code version drift between container and host.

        Compares the container's latest commit for Guardian-relevant paths
        against the host's deployed_commit (set by the redeploy verb).
        Alerts after DRIFT_ALERT_THRESHOLD consecutive drifted ticks.

        Best-effort: any failure silently skips. Drift detection must never
        interfere with the primary health monitoring flow.
        """
        import contextlib
        with contextlib.suppress(Exception):
            await self._check_code_drift_inner()

    async def _check_code_drift_inner(self) -> None:
        """Inner implementation of drift detection (may raise)."""
        import asyncio

        # Reference point = the commit update.sh LAST SUCCESSFULLY DEPLOYED, not
        # the container's live HEAD. The host Guardian is only redeployed by an
        # update.sh run, while container `main` advances on every PR merge — so
        # HEAD races ahead of the host between deploys and comparing against it
        # false-alarms on benign lag. Measuring against the last deploy makes
        # drift fire only when a redeploy was attempted but didn't take (a
        # genuinely stale/frozen host), which is what this check exists to catch.
        deploy_ref = await self._last_deployed_commit()
        if deploy_ref is None:
            # No successful update recorded → no deploy baseline. Skip rather
            # than fall back to HEAD (which re-introduces the false alarm).
            logger.debug(
                "Guardian drift check: no successful update_history baseline "
                "— skipping",
            )
            return

        # Get container's hash for Guardian-relevant paths AS OF the last deploy
        # (non-blocking).
        result = await asyncio.to_thread(
            subprocess.run,
            ["git", "-C", str(Path.home() / "genesis"),
             "log", "-1", "--format=%h", deploy_ref, "--"] + self._GUARDIAN_PATHS,
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            # deploy_ref unresolvable in the container's git (e.g. rewritten
            # history) — skip, never false-alarm.
            return
        container_hash = result.stdout.strip()

        # Skip drift detection when not on main (feature branch = expected divergence)
        branch_result = await asyncio.to_thread(
            subprocess.run,
            ["git", "-C", str(Path.home() / "genesis"),
             "symbolic-ref", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if branch_result.returncode == 0 and branch_result.stdout.strip() not in ("main",):
            return
        if not container_hash:
            # deploy_ref resolved but no Guardian-path commit is reachable from
            # it — nothing to compare. Log so this silent-skip is observable
            # (mirrors the no-baseline debug above) rather than going dark.
            logger.debug(
                "Guardian drift check: no Guardian-path commit reachable from "
                "deploy_ref=%s — skipping", deploy_ref,
            )
            return

        # Get host's deployed hash via SSH version command
        version_info = await self._remote.version()
        if not isinstance(version_info, dict):
            return

        # Deployed-gateway staleness check reuses this same version() payload.
        # Isolated in its own suppress so a failure here can't skip the
        # code-drift logic below.
        import contextlib
        with contextlib.suppress(Exception):
            await self._check_gateway_staleness(version_info)

        # Authorized_keys hardening reconciler reuses the same version() payload.
        # Own suppress so a failure here can't skip code-drift logic below.
        with contextlib.suppress(Exception):
            await self._check_authkey_hardening(version_info)

        # CC recovery-brain auth-health reconciler reuses the same payload. Own
        # suppress so a failure here can't skip code-drift logic below.
        with contextlib.suppress(Exception):
            await self._check_cc_auth(version_info)

        # Host systemd-linger health reconciler reuses the same version() payload.
        # Own suppress so a failure here can't skip the code-drift logic below.
        with contextlib.suppress(Exception):
            await self._check_host_linger(version_info)

        host_hash = version_info.get("deployed_commit", "unknown")

        if not isinstance(host_hash, str) or host_hash == "unknown":
            return  # Host hasn't been redeployed yet (pre-feature) — skip

        # The host records `deployed_commit` as the full deploy HEAD, while
        # `container_hash` is the LAST commit touching Guardian paths. A deploy
        # batch with non-Guardian commits landing after the last Guardian-touching
        # one leaves the two unequal even though HEAD *contains* the Guardian
        # commit — so test containment (ancestry), not equality.
        contains = await self._host_contains_commit(container_hash, host_hash)
        if contains is None:
            # Unresolvable (e.g. host ahead on a commit the container's git hasn't
            # fetched) — never false-alarm. The self-referential deploy of THIS
            # file is also covered: it drifts only until the host redeploys, which
            # DRIFT_ALERT_THRESHOLD absorbs.
            logger.debug(
                "Guardian code drift check unresolvable (container=%s host=%s) "
                "— skipping", container_hash, host_hash,
            )
            return
        if contains:
            if self._drift_count > 0:
                logger.info("Guardian code drift resolved (container=%s host=%s)",
                            container_hash, host_hash)
            self._drift_count = 0
            self._drift_escalated = False  # re-arm the user alert for next episode
            return

        # Drift detected — host's deployed HEAD does NOT contain the container's
        # latest Guardian-path commit (genuine staleness).
        self._drift_count += 1
        if self._drift_count >= self.DRIFT_ALERT_THRESHOLD and \
                self._drift_count % self.DRIFT_ALERT_THRESHOLD == 0:
            logger.error(
                "Guardian code drift detected for %d ticks: container=%s host=%s",
                self._drift_count, container_hash, host_hash,
            )
            if self._event_bus:
                from genesis.observability.types import Severity, Subsystem
                await self._event_bus.emit(
                    Subsystem.GUARDIAN, Severity.ERROR,
                    "guardian.code_drift",
                    f"Guardian code version mismatch — container={container_hash} "
                    f"host={host_hash} (drifted {self._drift_count} ticks)",
                )

        # User-facing alert: ONCE per drift episode (re-armed on resolution) —
        # NOT every 3 ticks like the event-bus observability emit above.
        if self._drift_count >= self.DRIFT_ALERT_THRESHOLD and \
                not self._drift_escalated:
            self._drift_escalated = True
            await self._alert_user(
                topic="Guardian code drift",
                context=(
                    f"⚠️ Guardian code drift: container={container_hash} "
                    f"host={host_hash}. Auto-redeploy may have failed. Check "
                    "update logs or run install_guardian.sh --non-interactive "
                    "on host."
                ),
                source_id="guardian:code_drift",
            )

    async def _host_contains_commit(
        self, container_hash: str, host_hash: str,
    ) -> bool | None:
        """Whether the host's deployed HEAD CONTAINS the container's latest
        Guardian-path commit (i.e. the host Guardian is current).

        Returns True when ``container_hash`` is an ancestor of (or equal to)
        ``host_hash`` — the host's deployed code includes the container's latest
        Guardian-relevant commit. Returns False on a genuine miss (real drift —
        the host lacks that commit). Returns None when the relationship can't be
        resolved (e.g. the host is ahead on a commit the container's git doesn't
        have yet) — the caller then skips, never false-alarms. Mirrors
        ``_expected_gateway_sha``'s None-on-unresolvable contract. Split out so
        tests can stub it / exercise the exit-code mapping directly.

        ``git merge-base --is-ancestor`` exits 0 (is ancestor), 1 (is not), or
        128 (unknown object). The mapping is EXPLICIT on purpose: a naive
        ``returncode == 0`` would fold 128 into False and re-introduce a false
        alarm whenever the host is legitimately ahead. Argument order is
        load-bearing: ``container_hash`` (the purported ancestor) FIRST.
        """
        import asyncio

        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["git", "-C", str(Path.home() / "genesis"),
                 "merge-base", "--is-ancestor", container_hash, host_hash],
                capture_output=True, timeout=5,
            )
        except Exception:
            return None  # subprocess/timeout failure → unresolvable → skip
        if result.returncode == 0:
            return True   # container's Guardian commit is in the host's HEAD
        if result.returncode == 1:
            return False  # genuinely not contained → real drift
        return None       # 128 (unknown object — host ahead) / other → skip

    async def _last_deployed_commit(self) -> str | None:
        """Commit hash that update.sh LAST SUCCESSFULLY deployed.

        This is the host Guardian's expected baseline: the host is only
        redeployed by an update.sh run (which records the run in
        ``update_history``), whereas the container's live HEAD advances on
        every merge. Drift must be measured against this, not HEAD, or normal
        between-deploy lag false-alarms.

        Returns None when no successful update is recorded (no baseline → the
        caller skips, never false-alarms). Best-effort: any DB failure → None.
        The SQL lives in ``genesis.db.crud.update_history`` (CRUD-layer reader);
        all imports are function-level so ``genesis.db`` stays off the host's
        import path (this watchdog is container-side only — see
        guardian/DEPLOYMENT.md), with connection management kept here.
        """
        import aiosqlite

        from genesis.db.connection import BUSY_TIMEOUT_MS
        from genesis.db.crud import update_history
        from genesis.env import genesis_db_path

        db_path = genesis_db_path()
        if not db_path.exists():
            return None
        try:
            async with aiosqlite.connect(str(db_path)) as db:
                await db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
                return await update_history.last_successful_deploy_commit(db)
        except Exception:
            return None

    async def _expected_gateway_sha(self, commit_ref: str) -> str | None:
        """sha256 of the gateway script at the given commit ref.

        Callers pass the host's ``deployed_commit`` (what the last redeploy
        shipped). Returns None if the container's git can't resolve that commit
        (e.g. the host is ahead of the container) — the caller then skips, never
        false-alarms. Split out so tests can stub the expected value.
        """
        import asyncio
        import hashlib

        show = await asyncio.to_thread(
            subprocess.run,
            ["git", "-C", str(Path.home() / "genesis"),
             "show", f"{commit_ref}:scripts/guardian-gateway.sh"],
            capture_output=True, timeout=5,
        )
        if show.returncode != 0 or not show.stdout:
            return None
        return hashlib.sha256(show.stdout).hexdigest()

    async def _check_gateway_staleness(self, version_info: dict) -> None:
        """Detect (and self-heal) a stale DEPLOYED gateway script.

        A guardian redeploy can fail to swap ``~/.local/bin/guardian-gateway.sh``
        (the atomic self-``cp``) while still advancing the install dir and
        recording the new ``deployed_commit`` — leaving the deployed gateway
        frozen (the bug that froze the host gateway ~2 months). We compare the
        host's reported ``gateway_sha`` against the sha of the gateway script at
        the host's ``deployed_commit`` — the authoritative record of what the
        last redeploy shipped. We deliberately do NOT key off ``code_version``
        (the install-dir git HEAD): the tar-based redeploy never runs ``git`` in
        the install dir, so HEAD systematically LAGS the deployed gateway, and
        keying off it inverts the check (false-alarm on every healthy redeploy,
        and the self-heal would then ``cp`` the STALE install-dir gateway back).
        This matches the sibling ``_check_code_drift_inner``, which also measures
        against ``deployed_commit``. On a confirmed mismatch we attempt ONE
        guarded ``sync-gateway`` self-heal per episode, then escalate if still
        unresolved. Best-effort (invoked under _check_code_drift's suppress).
        """
        host_gw_sha = version_info.get("gateway_sha", "unknown")
        host_deploy_commit = version_info.get("deployed_commit", "unknown")
        if not isinstance(host_gw_sha, str) or host_gw_sha in ("", "unknown"):
            return  # host gateway predates gateway_sha, or unreadable
        if not isinstance(host_deploy_commit, str) or \
                host_deploy_commit in ("", "unknown"):
            return  # host hasn't recorded a deploy yet — skip, never false-alarm

        expected = await self._expected_gateway_sha(host_deploy_commit)
        if expected is None:
            return  # can't determine expected sha — don't false-alarm

        if host_gw_sha == expected:
            if self._gateway_drift_count > 0:
                logger.info("Guardian deployed-gateway staleness resolved (sha=%s)",
                            host_gw_sha[:12])
            self._gateway_drift_count = 0
            self._gateway_resync_attempted = False
            self._gateway_escalated = False
            return

        # Deployed gateway does not match the install-dir's gateway → stale.
        self._gateway_drift_count += 1
        if self._gateway_drift_count < self.DRIFT_ALERT_THRESHOLD:
            return

        if not self._gateway_resync_attempted:
            # One guarded self-heal attempt per episode: redeploy from install dir.
            self._gateway_resync_attempted = True
            logger.warning(
                "Guardian deployed gateway stale (deployed=%s expected=%s @ %s) "
                "— auto-running sync-gateway",
                host_gw_sha[:12], expected[:12], host_deploy_commit,
            )
            try:
                res = await self._remote.sync_gateway()
            except Exception:
                logger.exception("sync-gateway self-heal raised")
                res = {"ok": False}
            if self._event_bus:
                from genesis.observability.types import Severity, Subsystem
                ok = res.get("ok")
                await self._event_bus.emit(
                    Subsystem.GUARDIAN, Severity.WARNING,
                    "guardian.gateway_resync",
                    f"Deployed gateway was stale (sha {host_gw_sha[:12]} vs "
                    f"{expected[:12]} @ {host_deploy_commit}); auto sync-gateway "
                    f"ok={ok}. Re-verifying next tick.",
                )
            return

        # Self-heal already attempted and it's STILL stale → escalate once per
        # episode: the event bus (observability) AND a user-facing Telegram alert.
        if not self._gateway_escalated:
            self._gateway_escalated = True
            logger.error(
                "Guardian deployed gateway STILL stale after auto sync-gateway "
                "(deployed=%s expected=%s @ %s)",
                host_gw_sha[:12], expected[:12], host_deploy_commit,
            )
            if self._event_bus:
                from genesis.observability.types import Severity, Subsystem
                await self._event_bus.emit(
                    Subsystem.GUARDIAN, Severity.ERROR,
                    "guardian.gateway_stale",
                    f"Deployed gateway still stale after auto-resync (sha "
                    f"{host_gw_sha[:12]} vs expected {expected[:12]} @ "
                    f"{host_deploy_commit}). Manual check needed.",
                )
            await self._alert_user(
                topic="Guardian gateway stale",
                context=(
                    f"Guardian deployed gateway is STILL stale after auto-resync "
                    f"(sha {host_gw_sha[:12]} vs expected {expected[:12]} @ "
                    f"{host_deploy_commit}). Manual intervention needed."
                ),
                source_id="guardian:gateway_stale",
            )

    def _reset_authkey_state(self) -> None:
        self._authkey_drift_count = 0
        self._authkey_reharden_attempted = False
        self._authkey_escalated = False
        self._authkey_flap_escalated = False
        self._authkey_last_src_hash = None

    async def _check_authkey_hardening(self, version_info: dict) -> None:
        """Detect (and self-heal) an under-hardened guardian authorized_keys line.

        The host's ``version`` reports whether its guardian key line still
        carries ``no-pty`` + ``from=`` and whether the stored ``from=`` matches
        the source of THIS connection (plus a hash of that source). Two trigger
        classes, deliberately treated differently because the reconciler runs
        live from merge:

        * **Regression** — ``no-pty`` stripped, or ``from=`` missing while a
          source is observable. These states do not oscillate, so heal on the
          first tick (one guarded ``reharden-key`` per episode), then escalate
          if still bad.
        * **Source mismatch** — ``from=`` present but no longer matching. A
          rewrite chases the source, so only heal a CONFIRMED STABLE move:
          ``DRIFT_ALERT_THRESHOLD`` consecutive ticks with the SAME observed
          source hash. A source that differs between ticks is a flap — never
          reharden it (that would churn the key file forever); escalate once.
          The guard is non-sticky: a source that later stabilizes reaches the
          streak and heals, so no episode can wedge until a process restart.

        Best-effort (invoked under _check_code_drift's suppress). Least
        disclosure: the host sends booleans + hashes, never the raw address.
        """
        no_pty = version_info.get("authkey_no_pty")
        has_from = version_info.get("authkey_has_from")
        from_matches = version_info.get("authkey_from_matches")
        src_hash = version_info.get("authkey_observed_src_hash")
        # Old gateway without the authkey_* fields → nothing to reconcile.
        if no_pty is None or has_from is None:
            return
        src_available = isinstance(src_hash, str) and src_hash != ""

        # A reharden can only add from= when a source is observable; treat a
        # missing from= as a healable regression ONLY when we have a source.
        regression = (no_pty is False) or (has_from is False and src_available)
        # A pure source-mismatch (opts otherwise fine): from= present, no longer
        # matching. Needs a stable-move streak, never healed on a flap.
        mismatch = (not regression) and has_from is True and from_matches is False

        if not regression and not mismatch:
            # Fully hardened + matching (or unfixable no-src state) → resolved.
            if self._authkey_drift_count > 0 or self._authkey_last_src_hash:
                logger.info("Guardian authkey hardening resolved")
            self._reset_authkey_state()
            return

        if regression:
            await self._heal_authkey_regression(no_pty, has_from)
            return

        # --- source mismatch: require a stable streak, guard against flaps ---
        if not src_available:
            return  # can't form a streak on an unobservable source
        if self._authkey_last_src_hash is None:
            self._authkey_last_src_hash = src_hash
            self._authkey_drift_count = 1
        elif src_hash == self._authkey_last_src_hash:
            self._authkey_drift_count += 1
        else:
            # Source moved again mid-episode → flapping. Never chase it.
            self._authkey_last_src_hash = src_hash
            self._authkey_drift_count = 1
            await self._escalate_authkey_flap()
            return

        if self._authkey_drift_count < self.DRIFT_ALERT_THRESHOLD:
            return
        await self._heal_authkey_mismatch()

    async def _heal_authkey_regression(self, no_pty, has_from) -> None:
        detail = "no-pty missing" if no_pty is False else "from= missing"
        if not self._authkey_reharden_attempted:
            self._authkey_reharden_attempted = True
            logger.warning("Guardian authkey under-hardened (%s) — reharden-key", detail)
            await self._run_reharden(reason=detail)
            return
        await self._escalate_authkey(
            f"Guardian authorized_keys STILL under-hardened ({detail}) after "
            f"auto reharden-key. Manual check needed.",
        )

    async def _heal_authkey_mismatch(self) -> None:
        if not self._authkey_reharden_attempted:
            self._authkey_reharden_attempted = True
            logger.warning(
                "Guardian authkey from= stably moved (drift streak hit threshold) "
                "— reharden-key",
            )
            await self._run_reharden(reason="from= no longer matches container source")
            return
        await self._escalate_authkey(
            "Guardian authorized_keys from= STILL mismatched after auto "
            "reharden-key. Manual check needed.",
        )

    async def _run_reharden(self, *, reason: str) -> None:
        """Run one guarded reharden-key and notify (a reharden means the
        container's host-facing source changed — the operator should know)."""
        try:
            res = await self._remote.reharden_key()
        except Exception:
            logger.exception("reharden-key self-heal raised")
            res = {"ok": False}
        if self._event_bus:
            from genesis.observability.types import Severity, Subsystem
            await self._event_bus.emit(
                Subsystem.GUARDIAN, Severity.WARNING,
                "guardian.authkey_reharden",
                f"Guardian authorized_keys re-hardened ({reason}); "
                f"ok={res.get('ok')} changed={res.get('changed')}. "
                f"Re-verifying next tick.",
            )
        await self._alert_user(
            topic="Guardian key re-hardened",
            context=(
                f"Guardian re-hardened its host SSH key ({reason}). "
                f"ok={res.get('ok')} changed={res.get('changed')} "
                f"confirmed={res.get('confirmed')}."
            ),
            source_id="guardian:authkey_rehardened",
        )

    async def _escalate_authkey(self, message: str) -> None:
        if self._authkey_escalated:
            return
        self._authkey_escalated = True
        logger.error(message)
        if self._event_bus:
            from genesis.observability.types import Severity, Subsystem
            await self._event_bus.emit(
                Subsystem.GUARDIAN, Severity.ERROR, "guardian.authkey_drift", message)
        await self._alert_user(
            topic="Guardian key hardening failed",
            context=message,
            source_id="guardian:authkey_drift",
        )

    async def _escalate_authkey_flap(self) -> None:
        if self._authkey_flap_escalated:
            return
        self._authkey_flap_escalated = True
        message = (
            "Guardian authorized_keys from= no longer matches and the "
            "container's source address is FLAPPING (differs between checks). "
            "Refusing to rewrite the key on a moving target — set a stable "
            "address or a subnet from= manually."
        )
        logger.error(message)
        if self._event_bus:
            from genesis.observability.types import Severity, Subsystem
            await self._event_bus.emit(
                Subsystem.GUARDIAN, Severity.ERROR, "guardian.authkey_flap", message)
        await self._alert_user(
            topic="Guardian key source flapping",
            context=message,
            source_id="guardian:authkey_flap",
        )

    def _reset_cc_auth_state(self) -> None:
        """Reset ONLY the dead-brain drift/escalation state (re-arm on health).

        The expiry-warning guard (``_cc_token_expiry_warned``) is deliberately
        NOT reset here — it has its own token-refresh lifecycle. Resetting it on
        health would re-warn every tick a healthy login is up (spam); never
        resetting it would one-shot forever and miss the NEXT token's expiry.
        """
        self._cc_auth_drift_count = 0
        self._cc_auth_escalated = False

    def _reset_host_linger_state(self) -> None:
        """Re-arm the host-linger episode on confirmed health."""
        self._host_linger_drift_count = 0
        self._host_linger_escalated = False

    def _reset_container_linger_state(self) -> None:
        """Re-arm the container-linger episode on confirmed health."""
        self._container_linger_drift_count = 0
        self._container_linger_escalated = False

    async def _check_host_linger(self, version_info: dict) -> None:
        """Alert when the HOST guardian user's systemd linger is disabled.

        Linger keeps the host user's timers/services (incl. the guardian's own
        units) alive after logout; disabled, they die silently on the next host
        logout. The ``version`` verb reports ``host_linger`` (True/False/None):

        * key absent (old gateway) -> skip, nothing to reconcile.
        * True -> healthy; reset the episode.
        * None -> probe error / ambiguous; never false-alarm AND never clear a
          real disabled streak (mirrors _check_cc_auth's None handling).
        * False (Linger=no) for DRIFT_ALERT_THRESHOLD ticks -> one alert/episode.

        Alert-only — ``loginctl enable-linger`` is privileged + interactive, so
        there is no safe non-interactive remediation. Best-effort (invoked under
        _check_code_drift's suppress).
        """
        if "host_linger" not in version_info:
            return  # old gateway without the field — nothing to reconcile
        linger = version_info.get("host_linger")  # True / False / None
        if linger is True:
            if self._host_linger_drift_count > 0:
                logger.info("Guardian host linger re-enabled")
            self._reset_host_linger_state()
            return
        if linger is not False:
            # None -> ambiguous/probe error: never false-alarm, never clear.
            return
        # linger is False -> genuinely disabled.
        self._host_linger_drift_count += 1
        if self._host_linger_drift_count < self.DRIFT_ALERT_THRESHOLD:
            return
        await self._escalate_host_linger()

    async def _check_container_linger(self) -> None:
        """Alert when the CONTAINER user's systemd linger is disabled.

        Dispatched from check_and_recover on EVERY tick (not the drift path), so
        this purely-local check survives a feature-branch checkout / down host.
        If linger is disabled here, genesis-server itself dies on the next
        logout. Probes the local logind over the SYSTEM bus (no login shell / no
        PATH dependence). Fail-safe: any probe error -> treat as unknown (never
        false-alarm, never clear a real streak). Alert-only (enable-linger is
        privileged + interactive).
        """
        import asyncio
        import getpass

        user = getpass.getuser()
        try:
            proc = await asyncio.create_subprocess_exec(
                "loginctl", "show-user", user, "--property=Linger",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except Exception:
            # loginctl missing / bus unreachable / timeout -> unknown; never
            # false-alarm, and leave the drift streak untouched.
            return
        value = out.decode(errors="replace").strip()
        if value == "Linger=yes":
            if self._container_linger_drift_count > 0:
                logger.info("Container linger re-enabled")
            self._reset_container_linger_state()
            return
        if value != "Linger=no":
            # Unexpected output / no user record -> ambiguous; never false-alarm.
            return
        # Linger=no -> genuinely disabled.
        self._container_linger_drift_count += 1
        if self._container_linger_drift_count < self.DRIFT_ALERT_THRESHOLD:
            return
        await self._escalate_container_linger(user)

    async def _escalate_host_linger(self) -> None:
        """One host-linger ALERT per episode (re-armed by _reset_host_linger_state)."""
        if self._host_linger_escalated:
            return
        self._host_linger_escalated = True
        msg = (
            "Guardian HOST systemd linger is DISABLED — the host user's timers "
            "and services (including the guardian's own units) will die silently "
            "on the next host logout. Re-enable it on the host: "
            "`sudo loginctl enable-linger <guardian-user>` (or re-run the installer)."
        )
        logger.error(msg)
        if self._event_bus:
            from genesis.observability.types import Severity, Subsystem
            await self._event_bus.emit(
                Subsystem.GUARDIAN, Severity.ERROR,
                "guardian.host_linger_disabled", msg,
            )
        await self._alert_user(
            topic="Guardian host linger disabled",
            context=msg,
            source_id="guardian:host_linger_disabled",
        )

    async def _escalate_container_linger(self, user: str) -> None:
        """One container-linger ALERT per episode (re-armed on health)."""
        if self._container_linger_escalated:
            return
        self._container_linger_escalated = True
        msg = (
            f"Container systemd linger is DISABLED for '{user}' — genesis-server "
            "and all Genesis user timers will die silently on the next logout. "
            f"Re-enable it: `sudo loginctl enable-linger {user}`."
        )
        logger.error(msg)
        if self._event_bus:
            from genesis.observability.types import Severity, Subsystem
            await self._event_bus.emit(
                Subsystem.GUARDIAN, Severity.ERROR,
                "guardian.container_linger_disabled", msg,
            )
        await self._alert_user(
            topic="Container linger disabled",
            context=msg,
            source_id="guardian:container_linger_disabled",
        )

    async def _check_cc_auth(self, version_info: dict) -> None:
        """Alert when the host CC recovery brain cannot authenticate.

        The host ``version`` verb reports ``cc_logged_in`` (tri-state True/
        False/None), ``cc_token_present`` (bool), and ``cc_token_age_days``
        (int, -1 unknown). Two independent concerns:

        * **Pre-expiry WARNING** — the synced setup-token nears its ~1-year end.
          Runs BEFORE the healthy early-return, so a token rotting while the
          primary login is fine is still surfaced (its whole point is insurance
          against a FUTURE login death).
        * **Dead-brain ALERT** — the host ``claude login`` is dead AND no usable
          fallback token exists. Needs ``DRIFT_ALERT_THRESHOLD`` ticks (mirrors
          the authkey/staleness reconcilers) so a blip doesn't page.

        Alert-only: unlike authkey (``reharden-key``) / gateway
        (``sync-gateway``) there is NO safe non-interactive remediation —
        ``claude login`` / ``setup-token`` both need a human, and the token
        re-copy is already the diagnosis fallback's + credential-bridge's job.
        Best-effort (invoked under _check_code_drift's suppress).
        """
        # Old gateway without the cc_* fields → nothing to reconcile.
        if "cc_logged_in" not in version_info:
            return
        logged_in = version_info.get("cc_logged_in")  # True / False / None
        token_present = version_info.get("cc_token_present") is True
        age = version_info.get("cc_token_age_days")
        age = age if isinstance(age, int) and not isinstance(age, bool) else -1

        # 1. Pre-expiry warning — independent of login health, BEFORE the gate.
        await self._check_cc_token_expiry(token_present, age)

        # 2. Dead-brain gate. A usable fallback = present AND not past its 1-year
        #    lifetime (age == -1 unknown → optimistically usable; the diagnosis
        #    fail-safe still backstops a truly-expired unknown-age token).
        token_usable = token_present and (
            age == -1 or age < self._CC_TOKEN_MAX_AGE_DAYS
        )
        if logged_in is True or token_usable:
            # Login works, or a usable fallback exists → brain can authenticate.
            if self._cc_auth_drift_count > 0:
                logger.info("Guardian CC recovery-brain auth recovered")
            self._reset_cc_auth_state()
            return
        if logged_in is not False:
            # logged_in is None (ambiguous / old CC / transient) AND no usable
            # token → never false-alarm on ambiguity; the diagnosis fail-safe
            # still catches a genuine outage at incident time. We deliberately
            # neither increment NOR reset the drift streak here: an ambiguous
            # tick must not CLEAR a real dead signal (so a host that reports
            # False/None/False, never True, still accumulates toward the alert),
            # but it also isn't itself evidence of a dead brain. So this
            # reconciler's threshold counts "ticks that never confirmed
            # auth-able," which is intentionally looser than the strictly-
            # consecutive semantics of the sibling reconcilers.
            return
        # logged_in is False AND no usable fallback token → genuinely dead brain.
        self._cc_auth_drift_count += 1
        if self._cc_auth_drift_count < self.DRIFT_ALERT_THRESHOLD:
            return
        await self._escalate_cc_auth(token_present=token_present, age=age)

    async def _check_cc_token_expiry(self, token_present: bool, age: int) -> None:
        """One WARNING per episode as the synced fallback token nears expiry.

        Re-arms on token refresh (age back below the warn threshold, or the
        token going absent) — NOT on login health — so it neither spams a
        healthy host nor one-shots forever and misses the next token's expiry.
        """
        if not token_present or age < 0:
            # No token, or unknown age → nothing to warn about; re-arm.
            self._cc_token_expiry_warned = False
            return
        if age < self.CC_TOKEN_WARN_AGE_DAYS:
            self._cc_token_expiry_warned = False  # fresh / refreshed → re-arm
            return
        if self._cc_token_expiry_warned:
            return
        self._cc_token_expiry_warned = True
        msg = (
            f"Guardian's synced CC fallback setup-token is nearing expiry "
            f"(age {age} d of a ~1-year lifetime). Re-run `claude setup-token` "
            f"and pipe it to `scripts/store_cc_token.sh` to refresh it before "
            f"the host login could ever need the fallback."
        )
        logger.warning(msg)
        if self._event_bus:
            from genesis.observability.types import Severity, Subsystem
            await self._event_bus.emit(
                Subsystem.GUARDIAN, Severity.WARNING,
                "guardian.cc_token_expiring", msg,
            )
        await self._alert_user(
            topic="Guardian CC token expiring",
            context=msg,
            source_id="guardian:cc_token_expiring",
        )

    async def _escalate_cc_auth(self, *, token_present: bool, age: int) -> None:
        """One dead-brain ALERT per episode (re-armed by _reset_cc_auth_state)."""
        if self._cc_auth_escalated:
            return
        self._cc_auth_escalated = True
        if token_present:
            remedy = (
                f"the synced setup-token appears expired/invalid (age {age} d) — "
                f"re-run `claude setup-token` → `scripts/store_cc_token.sh`, or "
                f"`claude login` on the host."
            )
        else:
            remedy = (
                "run `claude setup-token` and pipe it to "
                "`scripts/store_cc_token.sh` (preferred — no host access needed), "
                "or `claude login` on the host."
            )
        msg = (
            "Guardian's CC recovery brain cannot authenticate: the host `claude "
            f"login` is dead and no usable fallback token is available — {remedy} "
            "Host self-diagnosis is unavailable until then (other recovery paths "
            "still work)."
        )
        logger.error(msg)
        if self._event_bus:
            from genesis.observability.types import Severity, Subsystem
            await self._event_bus.emit(
                Subsystem.GUARDIAN, Severity.ERROR,
                "guardian.cc_auth_unhealthy", msg,
            )
        await self._alert_user(
            topic="Guardian CC auth dead",
            context=msg,
            source_id="guardian:cc_auth_unhealthy",
        )
