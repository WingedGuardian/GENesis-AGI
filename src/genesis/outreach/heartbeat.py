"""Outreach heartbeat — a dedicated, channel-independent liveness pulse.

Outreach's heartbeat used to be *emergent*: it only fired as a side-effect of an
outreach job succeeding, and the outreach scheduler only ``.start()``s once a
messaging channel registers. So a channel-less (dashboard-only) install never
emitted an outreach pulse, and adding outreach to the cessation-alert set naively
would fire a permanent unresolvable alert on exactly those installs — the
false-alarm trap that kept outreach OUT of the alert set.

This daemon breaks that coupling: it runs independently of channel registration
and pulses ONLY while the outreach scheduler is actually running
(``is_running``). Paired with the ``_subsystem_enabled('outreach')`` enable-gate,
that gives honest coverage:
  - channel-less install → not enabled → benign (no alert), and this daemon emits
    no pulse anyway (the scheduler never started).
  - channel configured, scheduler running → steady pulse → ``alive``.
  - channel configured, scheduler stopped/never-restarted → pulse ceases → the
    shared ``compute_heartbeat_staleness`` goes ``overdue`` →
    ``subsystem_stale:outreach``.

Wedge behaviour (CHANGED, and this docstring used to say the opposite): the
scheduler is an ``AsyncIOScheduler`` running on the runtime's main loop, and the
pulse is now emitted ON that loop. So a WEDGED-but-alive loop no longer keeps this
thread pulsing — the tick times out, the pulse ceases, and the stall surfaces as
BOTH ``subsystem_stale:outreach`` and a job-health failure. Wedge detection is
therefore no longer "job_health's domain, out of scope here"; it falls out of the
mechanism. Recorded because the previous wording described the old contract.

The threading, loop capture and health recording live in
``observability.heartbeat_daemon``; only the identity and the gate are here.
"""

from __future__ import annotations

from genesis.observability.heartbeat_daemon import HeartbeatDaemon


class OutreachHeartbeat(HeartbeatDaemon):
    """Emits the outreach liveness pulse while its scheduler is running."""

    subsystem_name = "OUTREACH"
    job_name = "outreach_heartbeat"
    message = "Outreach scheduler alive"
    thread_name = "outreach-heartbeat"

    def _should_emit(self, rt: object) -> bool:
        """Emit a pulse only when outreach is genuinely running.

        Gated on the scheduler's ``is_running`` (accurate across the start/stop
        lifecycle: False before start and after ``stop()`` nulls it) so a
        never-started / cleanly-stopped scheduler goes stale rather than reading a
        false ``alive`` — while a channel-less install simply never pulses.
        """
        scheduler = getattr(rt, "_outreach_scheduler", None)
        return bool(
            super()._should_emit(rt)
            and scheduler is not None
            and getattr(scheduler, "is_running", False)
        )
