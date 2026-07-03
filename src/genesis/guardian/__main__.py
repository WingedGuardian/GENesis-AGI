"""Guardian entry point — HOST-SIDE. Invoked by systemd timer.

Usage:
    python -m genesis.guardian               # run a single check cycle
    python -m genesis.guardian --test        # test alert channels
    python -m genesis.guardian --check-only  # one-shot health check (no recovery)
    python -m genesis.guardian --test-approval  # E2E test the keyword-reply gate
    python -m genesis.guardian --disk-status  # print storage-pool JSON (read-only)
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

    if "--test-approval" in sys.argv:
        asyncio.run(_test_approval())
        return

    if "--disk-status" in sys.argv:
        asyncio.run(_disk_status())
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


async def _test_approval() -> None:
    """E2E self-test of the keyword-reply approval gate (no recovery).

    Sends a test gate prompt, then long-polls getUpdates for an APPROVE/DENY
    reply for ~120s. Lets a host operator verify the full
    send-prompt → reply → read-reply loop via the gateway without faking an
    outage. Prints what keyword it read, or a timeout notice.
    """
    import time

    from genesis.guardian.alert.telegram import CONFLICT_SENTINEL, TelegramAlertChannel
    from genesis.guardian.check import _build_dispatcher, _find_telegram_channel
    from genesis.guardian.config import load_config

    config = load_config()
    dispatcher = _build_dispatcher(config)
    channel: TelegramAlertChannel | None = _find_telegram_channel(dispatcher)

    if channel is None:
        print("  No Telegram channel configured — cannot test the approval gate")
        return

    gate_msg_id = await channel.send_text(
        "Guardian approval self-test — reply APPROVE or DENY"
    )
    if gate_msg_id is None:
        print("  Failed to send the test gate prompt")
        return
    print(f"  Sent test gate prompt (message_id={gate_msg_id}). "
          "Reply APPROVE or DENY within ~120s…")

    keywords = frozenset({"APPROVE", "DENY"})
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        kw = await channel.poll_for_keyword(gate_msg_id, keywords, timeout_s=25)
        if kw == CONFLICT_SENTINEL:
            print("  getUpdates 409 Conflict — main bot is polling the same "
                  "token (it is alive). Retrying…")
            await asyncio.sleep(5)
            continue
        if kw in ("APPROVE", "DENY"):
            print(f"  Read keyword: {kw}")
            return
    print("  Timeout — no APPROVE/DENY reply read within ~120s")


async def _disk_status() -> None:
    """Print storage-pool + snapshot status as JSON (read-only).

    Genesis's programmatic window into host capacity — consumed by the
    `disk-status` gateway verb. Reuses the same measurement the guardian alerts
    on, so the container sees exactly what the guardian sees.
    """
    import dataclasses
    import json

    from genesis.guardian.config import load_config
    from genesis.guardian.pool import measure_storage_pool, worst_tier
    from genesis.guardian.snapshots import SnapshotManager

    config = load_config()
    status = await measure_storage_pool(config)
    tier = worst_tier(status, config.storage_pool) if status.detected else "unknown"

    snap_mgr = SnapshotManager(config)
    snapshots = [
        {"name": name, "created_at": created.isoformat() if created else None}
        for name, created in await snap_mgr._list_snapshots_with_meta()
    ]

    print(json.dumps({
        "ok": True,
        "pool": dataclasses.asdict(status),
        "tier": tier,
        "snapshots": snapshots,
    }))


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
