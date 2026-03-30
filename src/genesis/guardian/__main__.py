"""Guardian entry point — invoked by systemd timer.

Usage:
    python -m genesis.guardian              # run a single check cycle
    python -m genesis.guardian --test       # test alert channels
    python -m genesis.guardian --check-only # one-shot health check (no recovery)
"""

from __future__ import annotations

import asyncio
import sys


def main() -> None:
    """Entry point for the Guardian."""
    from genesis.guardian.check import _setup_logging, run_check

    _setup_logging()

    if "--test" in sys.argv:
        asyncio.run(_test_alerts())
        return

    if "--check-only" in sys.argv:
        asyncio.run(_check_only())
        return

    asyncio.run(run_check())


async def _test_alerts() -> None:
    """Test alert channel connectivity."""
    from genesis.guardian.alert.base import Alert, AlertSeverity
    from genesis.guardian.check import _build_dispatcher
    from genesis.guardian.config import load_config

    config = load_config()
    dispatcher = _build_dispatcher(config)

    results = await dispatcher.test_all()
    for name, ok in results.items():
        status = "OK" if ok else "FAILED"
        print(f"  {name}: {status}")

    if not results:
        print("  No alert channels configured")
        return

    # Send test alert
    await dispatcher.send(Alert(
        severity=AlertSeverity.INFO,
        title="Guardian test alert",
        body="This is a test alert from the Guardian. If you see this, alerts are working.",
    ))
    print("  Test alert sent")


async def _check_only() -> None:
    """One-shot health check — collect signals, print status, exit."""
    from genesis.guardian.config import load_config
    from genesis.guardian.health_signals import collect_all_signals

    config = load_config()
    snapshot = await collect_all_signals(config)

    print(f"All alive: {snapshot.all_alive}")
    print(f"Any alive: {snapshot.any_alive}")
    for name, signal in snapshot.signals.items():
        status = "OK" if signal.alive else "FAILED"
        print(f"  {name}: {status} ({signal.detail})")

    if snapshot.pause_state.paused:
        print(f"  Genesis is PAUSED: {snapshot.pause_state.reason}")

    for warn in snapshot.suspicious_warnings:
        print(f"  WARNING: {warn.name} — {warn.detail}")


if __name__ == "__main__":
    main()
