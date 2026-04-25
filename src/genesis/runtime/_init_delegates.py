"""Bootstrap step delegates for ``GenesisRuntime``.

Each subsystem has a corresponding ``runtime/init/<name>.py`` module
that owns its actual initialization logic. The methods on this mixin
are thin two-line passthroughs that exist so ``bootstrap()`` can
register them as named init steps via ``_run_init_step`` /
``_run_init_step_async`` (which want methods of the runtime, not free
functions).

Keeping the delegates separate from ``_core.py`` lets the bootstrap
orchestrator stay focused on sequencing while the per-subsystem init
logic stays in ``runtime/init/``. The mixin owns no state of its own
and has no ``__init__``.

``_probe_guardian_status`` is included here despite being a 24-line
non-delegate because it IS called as an init step from ``bootstrap()``;
keeping it adjacent to the other init steps is the pragmatic choice.
``_run_pipeline_cycle`` is also here because pipeline.run_pipeline_cycle
is the only non-init function the bootstrap layer wraps as a method on
the runtime.
"""

from __future__ import annotations

import logging
from pathlib import Path

from genesis.runtime.init import (
    autonomy,
    awareness,
    cc_relay,
    db,
    direct_session,
    health_data,
    inbox,
    learning,
    mail,
    memory,
    modules,
    observability,
    outreach,
    perception,
    pipeline,
    providers,
    reflection,
    router,
    secrets,
    surplus,
    tasks,
)

logger = logging.getLogger("genesis.runtime")


class _InitDelegatesMixin:
    """Mixin: per-subsystem bootstrap step delegates."""

    def _load_secrets(self) -> None:
        secrets.load(self)

    async def _init_db(self) -> None:
        await db.init(self)

    async def _init_tool_registry(self) -> None:
        await db.init_tool_registry(self)

    def _init_observability(self) -> None:
        observability.init(self)

    def _init_providers(self) -> None:
        providers.init(self)

    async def _init_modules(self) -> None:
        await modules.init(self)

    async def _init_awareness(self) -> None:
        await awareness.init(self)

    def _init_router(self) -> None:
        router.init(self)

    def _init_perception(self) -> None:
        perception.init(self)

    async def _init_cc_relay(self) -> None:
        await cc_relay.init(self)

    async def _init_direct_session(self) -> None:
        await direct_session.init(self)

    async def _init_memory(self) -> None:
        await memory.init(self)

    async def _init_pipeline(self) -> None:
        await pipeline.init(self)

    async def _run_pipeline_cycle(self, profile_name: str) -> None:
        await pipeline.run_pipeline_cycle(self, profile_name)

    async def _init_surplus(self) -> None:
        await surplus.init(self)

    async def _init_learning(self) -> None:
        await learning.init(self)

    async def _init_reflection(self) -> None:
        await reflection.init(self)

    async def _init_inbox(self) -> None:
        await inbox.init(self)

    async def _init_mail(self) -> None:
        await mail.init(self)

    def _init_health_data(self) -> None:
        health_data.init(self)

    async def _init_outreach(self) -> None:
        await outreach.init(self)

    async def _init_autonomy(self) -> None:
        await autonomy.init(self)

    async def _init_tasks(self) -> None:
        await tasks.init(self)

    async def _init_guardian_monitoring(self) -> None:
        from genesis.runtime.init.guardian import init_guardian_monitoring
        await init_guardian_monitoring(self)

    async def _init_sentinel(self) -> None:
        from genesis.runtime.init.sentinel import init_sentinel
        await init_sentinel(self)

    def _probe_guardian_status(self) -> None:
        """Check if the Guardian is alive by reading its heartbeat file.

        The Guardian runs on the host VM, not inside the container.
        It writes ~/.genesis/guardian_heartbeat.json every check cycle.
        This probe always succeeds — Guardian is optional infrastructure.
        Actual staleness monitoring is handled by probe_guardian() in the
        health data infrastructure snapshot.
        """
        import json
        from datetime import UTC, datetime
        heartbeat_path = Path.home() / ".genesis" / "guardian_heartbeat.json"
        try:
            data = json.loads(heartbeat_path.read_text())
            ts_str = data.get("timestamp", "")
            if ts_str:
                staleness = (datetime.now(UTC) - datetime.fromisoformat(ts_str)).total_seconds()
                logger.info("Guardian heartbeat: %.0fs ago", staleness)
            else:
                logger.info("Guardian heartbeat file exists but missing timestamp")
        except FileNotFoundError:
            logger.info("Guardian heartbeat not found (Guardian may not be installed)")
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            logger.info("Guardian heartbeat unreadable: %s", exc)
