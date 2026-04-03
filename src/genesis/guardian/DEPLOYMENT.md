# Guardian Deployment Sides

This package serves two deployment targets from one codebase.
Each file's module docstring starts with HOST-SIDE, CONTAINER-SIDE, or BOTH SIDES.

## HOST-SIDE (runs on host VM via systemd timer)

Entry point: `python -m genesis.guardian`
Install dir: `~/.local/share/genesis-guardian/`
CLAUDE.md: Generated from `config/guardian-claude.md` (NOT the repo root CLAUDE.md)

- `__main__.py` — systemd timer entry point
- `check.py` — core check cycle (signal collection → state machine → diagnosis → recovery)
- `diagnosis.py` — CC diagnosis engine (invokes `claude -p` on host)
- `diagnosis_writer.py` — writes diagnosis results to shared mount
- `collector.py` — gathers diagnostic metrics (memory, disk, processes, journal)
- `recovery.py` — executes recovery actions (restart, revert, rollback)
- `health_signals.py` — 5 probes + 6 suspicious checks
- `state_machine.py` — confirmation protocol (prevents false positives)
- `snapshots.py` — Incus snapshot management
- `approval.py` — HTTP approval server for recovery confirmation
- `dialogue.py` — Guardian↔Genesis dialogue protocol
- `alert/` — alert channels (Telegram, journal)

## CONTAINER-SIDE (runs inside Genesis container via awareness loop)

Wired from `runtime/init/guardian.py`. Imports use function-level scoping
for container modules (genesis.db, genesis.observability) so the host
never needs them.

- `watchdog.py` — monitors Guardian heartbeat, triggers SSH recovery if stale
- `remote.py` — SSH interface to host (6 whitelisted commands via gateway)
- `findings_ingest.py` — reads Guardian diagnosis results, creates observations + events

## BOTH SIDES (different functions for each deployment)

- `briefing.py` — Container writes briefings (write_guardian_briefing), host reads them (read_guardian_briefing)
- `credential_bridge.py` — Container propagates Telegram creds, host loads them
- `config.py` — YAML config loader (host reads guardian.yaml, container reads guardian_remote.yaml)

## Shared Mount

The Incus shared mount bridges the two sides:
- Host: `~/.local/state/genesis-guardian/shared/`
- Container: `~/.genesis/shared/`

Subdirectories:
- `briefing/` — Genesis→Guardian (service baselines, metric norms, recent activity)
- `findings/` — Guardian→Genesis (diagnosis results for post-recovery learning)
- `guardian/` — Genesis→Guardian (Telegram credentials)
