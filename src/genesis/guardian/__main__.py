"""Guardian entry point — HOST-SIDE. Invoked by systemd timer.

Usage:
    python -m genesis.guardian               # run a single check cycle
    python -m genesis.guardian --test        # test alert channels
    python -m genesis.guardian --check-only  # one-shot health check (no recovery)
    python -m genesis.guardian --test-approval  # E2E test the keyword-reply gate
    python -m genesis.guardian --disk-status  # print storage-pool JSON (read-only)
    python -m genesis.guardian --ram-status   # print RAM tier JSON (read-only)
    python -m genesis.guardian --host-profile  # host body-schema JSON (read-only)
    python -m genesis.guardian --bundle-status # offline repo-bundle archive JSON (read-only)
    python -m genesis.guardian --provision-status              # host capacity (read-only)
    python -m genesis.guardian --provision-grow-disk <disk> <GiB>  # EXECUTE (pre-approved)
    python -m genesis.guardian --provision-grow-memory <MiB>       # EXECUTE (pre-approved)
    python -m genesis.guardian --provision-vzdump                   # EXECUTE (pre-approved) START a backup, returns UPID
    python -m genesis.guardian --provision-vzdump-status [UPID]     # verify probe (read-only + ledger flip); no arg = latest in-flight
    python -m genesis.guardian --storage-expand               # absorb a grown disk
    python -m genesis.guardian --grow-root <GB>               # EXECUTE (pre-approved) grow container root
    python -m genesis.guardian --set-container-limits <mem_mib|-> <cpu|->  # EXECUTE (pre-approved) raise cgroup caps

The provisioning grow/expand verbs are EXECUTE-ONLY: they run the shared
execute-core (fresh due-diligence re-check + rate cap + one attempt + ledger),
with NO Telegram approval gate. Approval is the CALLER's responsibility — the
container obtains it via its own bot before invoking the gateway verb. The
guardian's own getUpdates approval path (Genesis-DOWN) lives in check.py's
autonomous pool-crit hook, never here.
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

    if "--ram-status" in sys.argv:
        asyncio.run(_ram_status())
        return

    if "--host-profile" in sys.argv:
        sys.exit(asyncio.run(_host_profile()))

    if "--bundle-status" in sys.argv:
        sys.exit(_bundle_status())

    if "--provision-status" in sys.argv:
        sys.exit(asyncio.run(_provision_status()))

    if "--provision-grow-disk" in sys.argv:
        sys.exit(asyncio.run(_provision_grow_disk(sys.argv)))

    if "--provision-grow-memory" in sys.argv:
        sys.exit(asyncio.run(_provision_grow_memory(sys.argv)))

    if "--provision-vzdump-status" in sys.argv:
        sys.exit(asyncio.run(_provision_vzdump_status(sys.argv)))

    if "--provision-vzdump" in sys.argv:
        sys.exit(asyncio.run(_provision_vzdump()))

    if "--storage-expand" in sys.argv:
        sys.exit(asyncio.run(_storage_expand()))

    if "--grow-root" in sys.argv:
        sys.exit(asyncio.run(_grow_root(sys.argv)))

    if "--set-container-limits" in sys.argv:
        sys.exit(asyncio.run(_set_container_limits(sys.argv)))

    if "--configure-provisioning" in sys.argv:
        sys.exit(_configure_provisioning(sys.argv))

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


async def _ram_status() -> None:
    """Print the guardian's RAM view as JSON (read-only).

    Genesis's programmatic window into what the guardian's out-of-band RAM
    alert sees — both axes (container cgroup + host-VM) and the worst-of tier.
    Consumed by the `ram-status` gateway verb.
    """
    import json

    from genesis.guardian.config import load_config
    from genesis.guardian.memory_watch import memory_status_snapshot

    print(json.dumps(await memory_status_snapshot(load_config())))


async def _host_profile() -> int:
    """Print the host body-schema JSON (read-only).

    Consumed by the `host-profile` gateway verb → the container's
    ``infra_profile.collectors.host``, which owns the facts/metrics split.
    Emits JSON even on total failure so the client never has to guess.
    """
    import json

    from genesis.guardian.config import load_config
    from genesis.guardian.host_profile import gather_host_profile

    try:
        result = await gather_host_profile(load_config())
    except Exception as exc:  # noqa: BLE001 — the verb contract is JSON-always
        result = {"ok": False, "action": "host-profile", "error": repr(exc)}
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


def _bundle_status() -> int:
    """Print the offline-bundle archive status as JSON (read-only).

    Consumed by the `bundle-status` gateway verb → the container's programmatic
    window onto the host-only offline re-clone lifeline (archived bundles + the
    newest stamp). No mutation, no secrets, no sudo. Emits JSON even on failure so
    the client never has to guess.
    """
    import json

    from genesis.guardian.bundle_watch import bundle_archive_status
    from genesis.guardian.config import load_config

    try:
        result = bundle_archive_status(load_config())
    except Exception as exc:  # noqa: BLE001 — the verb contract is JSON-always
        result = {"ok": False, "action": "bundle-status", "error": repr(exc)}
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


def _emit(obj: dict) -> int:
    """Print a JSON result line and return a shell exit code (0 iff ok)."""
    import json
    print(json.dumps(obj))
    return 0 if obj.get("ok") else 1


def _args_after(argv: list[str], flag: str, n: int) -> list[str] | None:
    """Return the n positional args following ``flag``, or None if absent."""
    try:
        i = argv.index(flag)
    except ValueError:
        return None
    tail = argv[i + 1 : i + 1 + n]
    return tail if len(tail) == n else None


def _configure_provisioning(argv: list[str]) -> int:
    """Land/refresh the host provisioning config as a state-dir override.

    Takes ``key=value`` args (only ProvisioningConfig fields). Writes
    ``<state_dir>/provisioning.local.yaml`` — outside the git checkout, so it
    survives guardian redeploys — then re-loads to confirm it parses and echoes
    the merged result. No secrets here (the Proxmox tokens cross the bridge).
    """
    from genesis.guardian.config import load_config, write_provisioning_override

    i = argv.index("--configure-provisioning")
    kvs = argv[i + 1:]
    if not kvs:
        return _emit({"ok": False, "action": "configure-provisioning",
                      "error": "usage: --configure-provisioning key=value [key=value ...]"})
    params: dict[str, str] = {}
    for tok in kvs:
        if "=" not in tok:
            return _emit({"ok": False, "action": "configure-provisioning",
                          "error": f"bad arg {tok!r} (expected key=value)"})
        k, _, v = tok.partition("=")
        params[k.strip()] = v.strip()

    try:
        config = load_config()
        dest = write_provisioning_override(config.state_dir, params)
    except (ValueError, OSError) as exc:
        return _emit({"ok": False, "action": "configure-provisioning", "error": str(exc)})

    p = load_config().provisioning  # re-read to reflect the merged result
    return _emit({"ok": True, "action": "configure-provisioning", "path": str(dest),
                  "provisioning": {"enabled": p.enabled, "api_host": p.api_host,
                                   "api_port": p.api_port, "node": p.node, "vmid": p.vmid,
                                   "target_disk": p.target_disk, "storage": p.storage,
                                   "verify_tls": p.verify_tls,
                                   "require_recent_backup": p.require_recent_backup}})


async def _provision_status() -> int:
    """Read-only host capacity via the audit token. Genesis's provisioning
    window: VM cores/RAM, per-disk sizes, storage + node-RAM headroom."""
    import dataclasses

    from genesis.guardian.check import _build_provisioning_adapter
    from genesis.guardian.config import load_config
    from genesis.guardian.provisioning.flow import _vzdump_in_flight_upid
    from genesis.guardian.provisioning.ledger import ProvisioningLedger

    config = load_config()
    adapter = _build_provisioning_adapter(config)
    if adapter is None:
        return _emit({"ok": False, "action": "provision-status",
                      "error": "provisioning disabled or unconfigured"})
    cap = await adapter.get_capacity()
    pc = config.provisioning
    # Backup context: lets the container decide the JIT backup→grow chain
    # BEFORE proposing, so ONE approval can truthfully cover the whole chain.
    backup = {
        "age_days": await adapter.newest_backup_age_days(),
        "require_recent_backup": pc.require_recent_backup,
        "backup_max_age_days": pc.backup_max_age_days,
        "in_flight_upid": _vzdump_in_flight_upid(
            config, ProvisioningLedger(config.state_dir),
        ),
    }
    return _emit({"ok": cap.detected, "action": "provision-status",
                  "capacity": dataclasses.asdict(cap), "backup": backup})


async def _provision_grow_disk(argv: list[str]) -> int:
    """EXECUTE (pre-approved) a VM disk grow + absorb into the thin pool."""
    import re

    from genesis.guardian.check import _build_dispatcher, _build_provisioning_adapter
    from genesis.guardian.config import load_config
    from genesis.guardian.provisioning.flow import (
        ProvisionRequest,
        execute_provisioning_action,
    )
    from genesis.guardian.provisioning.ledger import ProvisioningLedger

    args = _args_after(argv, "--provision-grow-disk", 2)
    if not args:
        return _emit({"ok": False, "action": "provision-grow-disk",
                      "error": "usage: --provision-grow-disk <disk> <GiB>"})
    disk, gib_s = args
    if not re.fullmatch(r"(scsi|virtio|sata)[0-9]{1,2}", disk):
        return _emit({"ok": False, "action": "provision-grow-disk",
                      "error": f"invalid disk {disk!r}"})
    if not re.fullmatch(r"[1-9][0-9]{0,2}", gib_s):
        return _emit({"ok": False, "action": "provision-grow-disk",
                      "error": f"invalid GiB {gib_s!r} (1-999)"})

    config = load_config()
    adapter = _build_provisioning_adapter(config)
    if adapter is None:
        return _emit({"ok": False, "action": "provision-grow-disk",
                      "error": "provisioning disabled or unconfigured"})
    request = ProvisionRequest(kind="disk", disk=disk, add_gib=int(gib_s),
                               absorb_after=True, origin="container (approved)")
    result = await execute_provisioning_action(
        config, request, adapter, _build_dispatcher(config),
        ProvisioningLedger(config.state_dir),
    )
    return _emit(result)


async def _provision_grow_memory(argv: list[str]) -> int:
    """EXECUTE (pre-approved) a VM memory grow (requires a later VM reboot)."""
    import re

    from genesis.guardian.check import _build_dispatcher, _build_provisioning_adapter
    from genesis.guardian.config import load_config
    from genesis.guardian.provisioning.flow import (
        ProvisionRequest,
        execute_provisioning_action,
    )
    from genesis.guardian.provisioning.ledger import ProvisioningLedger

    args = _args_after(argv, "--provision-grow-memory", 1)
    if not args or not re.fullmatch(r"[1-9][0-9]{2,5}", args[0]):
        return _emit({"ok": False, "action": "provision-grow-memory",
                      "error": "usage: --provision-grow-memory <MiB> (100-999999)"})

    config = load_config()
    adapter = _build_provisioning_adapter(config)
    if adapter is None:
        return _emit({"ok": False, "action": "provision-grow-memory",
                      "error": "provisioning disabled or unconfigured"})
    request = ProvisionRequest(kind="memory", new_mib=int(args[0]),
                               origin="container (approved)")
    result = await execute_provisioning_action(
        config, request, adapter, _build_dispatcher(config),
        ProvisioningLedger(config.state_dir),
    )
    return _emit(result)


async def _provision_vzdump() -> int:
    """EXECUTE (pre-approved) phase 1: START a vzdump, return its UPID.

    Two-phase: this only launches (a full-VM dump runs for tens of minutes+).
    The start is ledgered immediately — rate-cap entry, in-flight latch, and
    restart-resume handle in one row. Verify via --provision-vzdump-status.
    """
    from genesis.guardian.check import _build_dispatcher, _build_provisioning_adapter
    from genesis.guardian.config import load_config
    from genesis.guardian.provisioning.flow import execute_vzdump_start
    from genesis.guardian.provisioning.ledger import ProvisioningLedger

    config = load_config()
    adapter = _build_provisioning_adapter(config)
    if adapter is None:
        return _emit({"ok": False, "action": "provision-vzdump",
                      "error": "provisioning disabled or unconfigured"})
    result = await execute_vzdump_start(
        config, adapter, _build_dispatcher(config),
        ProvisioningLedger(config.state_dir),
    )
    result.setdefault("action", "provision-vzdump")
    return _emit(result)


async def _provision_vzdump_status(argv: list[str]) -> int:
    """One verify probe for a started vzdump (phase 2; read-only + ledger flip).

    Optional UPID arg; without it, resumes the latest unverified ledger entry
    (restart-safe). ``state`` in the JSON is the caller's contract: running/
    unknown = transient (retry), verified/failed = terminal. Exit 0 for any
    usable probe answer; non-zero only for terminal failure / nothing in flight.
    """
    from genesis.guardian.check import _build_dispatcher, _build_provisioning_adapter
    from genesis.guardian.config import load_config
    from genesis.guardian.provisioning.flow import verify_vzdump_step
    from genesis.guardian.provisioning.ledger import ProvisioningLedger

    args = _args_after(argv, "--provision-vzdump-status", 1)
    upid = args[0] if args else ""

    config = load_config()
    adapter = _build_provisioning_adapter(config)
    if adapter is None:
        return _emit({"ok": False, "action": "provision-vzdump-status",
                      "error": "provisioning disabled or unconfigured"})
    result = await verify_vzdump_step(
        config, adapter, _build_dispatcher(config),
        ProvisioningLedger(config.state_dir), upid=upid,
    )
    result.setdefault("action", "provision-vzdump-status")
    return _emit(result)


async def _storage_expand() -> int:
    """Absorb an already-grown virtual disk into the LVM-thin pool (host-side).

    Strictly additive LVM ops (pvresize → autoextend profile → verify). Used
    standalone to retry the absorb after a disk grow already landed."""
    from genesis.guardian.config import load_config
    from genesis.guardian.provisioning.expand import expand_storage

    result = await expand_storage(load_config())
    result.setdefault("action", "storage-expand")
    return _emit(result)


async def _grow_root(argv: list[str]) -> int:
    """EXECUTE (pre-approved) a container root-volume grow to <GB> total."""
    import re

    from genesis.guardian.config import load_config
    from genesis.guardian.grow_capacity import grow_root

    args = _args_after(argv, "--grow-root", 1)
    if not args or not re.fullmatch(r"[1-9][0-9]{0,3}", args[0]):
        return _emit({"ok": False, "action": "grow-root",
                      "error": "usage: --grow-root <GB total, 1-9999>"})
    return _emit(await grow_root(load_config(), int(args[0])))


async def _set_container_limits(argv: list[str]) -> int:
    """EXECUTE (pre-approved) a container cgroup limit raise (grow-only).

    Args: <mem_mib> <cpu>; use '-' for an axis to leave it unchanged."""
    import re

    from genesis.guardian.config import load_config
    from genesis.guardian.grow_capacity import set_container_limits

    args = _args_after(argv, "--set-container-limits", 2)
    if not args:
        return _emit({"ok": False, "action": "set-container-limits",
                      "error": "usage: --set-container-limits <mem_mib|-> <cpu|->"})
    mem_s, cpu_s = args
    if mem_s != "-" and not re.fullmatch(r"[1-9][0-9]{2,6}", mem_s):
        return _emit({"ok": False, "action": "set-container-limits",
                      "error": f"invalid mem_mib {mem_s!r} (100-9999999 or -)"})
    if cpu_s != "-" and not re.fullmatch(r"[1-9][0-9]{0,2}", cpu_s):
        return _emit({"ok": False, "action": "set-container-limits",
                      "error": f"invalid cpu {cpu_s!r} (1-999 or -)"})
    if mem_s == "-" and cpu_s == "-":
        return _emit({"ok": False, "action": "set-container-limits",
                      "error": "nothing to do (both axes '-')"})
    mem_mib = None if mem_s == "-" else int(mem_s)
    cpu = None if cpu_s == "-" else int(cpu_s)
    return _emit(await set_container_limits(load_config(), mem_mib, cpu))


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
