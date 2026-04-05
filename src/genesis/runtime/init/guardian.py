"""Bootstrap step: wire Guardian bidirectional monitoring.

Reads ~/.genesis/guardian_remote.yaml (written by install_guardian.sh) and
creates the GuardianRemote + GuardianWatchdog, injecting the watchdog into
the awareness loop for automatic recovery.

Graceful degradation: if the config file is missing or incomplete, monitoring
is silently disabled (Guardian is optional infrastructure).
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


async def init_guardian_monitoring(rt) -> None:
    """Set up Guardian bidirectional monitoring if config is available."""
    config_path = Path.home() / ".genesis" / "guardian_remote.yaml"
    if not config_path.exists():
        logger.info(
            "Guardian remote config not found (%s) — bidirectional monitoring disabled",
            config_path,
        )
        return

    try:
        config = yaml.safe_load(config_path.read_text()) or {}
    except Exception:
        logger.warning("Failed to read guardian_remote.yaml", exc_info=True)
        return

    host_ip = config.get("host_ip", "")
    host_user = config.get("host_user", "")
    ssh_key = config.get("ssh_key", "")

    if not host_ip or not host_user:
        logger.warning(
            "Guardian remote config incomplete (host_ip=%r, host_user=%r) "
            "— bidirectional monitoring disabled",
            host_ip, host_user,
        )
        return

    from genesis.guardian.remote import GuardianRemote
    from genesis.guardian.watchdog import GuardianWatchdog

    remote = GuardianRemote(
        host_ip=host_ip,
        host_user=host_user,
        key_path=ssh_key or "~/.ssh/genesis_guardian_ed25519",
    )
    rt._guardian_remote = remote
    watchdog = GuardianWatchdog(
        remote,
        event_bus=rt._event_bus,
    )

    if rt._awareness_loop:
        rt._awareness_loop.set_guardian_watchdog(watchdog)
        logger.info(
            "Guardian bidirectional monitoring enabled (host=%s@%s)",
            host_user, host_ip,
        )

        # Propagate Telegram credentials to shared mount for Guardian
        from genesis.guardian.credential_bridge import propagate_telegram_credentials
        rt._awareness_loop.set_credential_bridge(propagate_telegram_credentials)
        logger.info("Telegram credential bridge wired to awareness loop")

        exporter = getattr(rt, "_autonomous_cli_policy_exporter", None)
        if exporter is not None:
            rt._awareness_loop.set_autonomous_cli_policy_exporter(exporter.export)
            logger.info("Autonomous CLI policy exporter wired to awareness loop")

        # Write dynamic Guardian briefing to shared mount every tick
        from genesis.guardian.briefing import write_dynamic_guardian_briefing
        rt._awareness_loop.set_briefing_writer(write_dynamic_guardian_briefing)
        logger.info("Dynamic Guardian briefing wired to awareness loop")

        # Ingest Guardian diagnosis results from shared mount every tick
        from genesis.guardian.findings_ingest import ingest_guardian_findings
        rt._awareness_loop.set_findings_ingest(ingest_guardian_findings)
        logger.info("Guardian findings ingest wired to awareness loop")
    else:
        logger.warning(
            "Awareness loop not available — Guardian watchdog not wired",
        )
