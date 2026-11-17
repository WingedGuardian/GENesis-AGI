- **The Sentinel proposes stopping a single runaway process.** A new health
  alarm, `infra:process_memory_high` (WARNING, tier 2), fires when one of the
  user's processes outside a systemd service holds `process_stop.propose_at_mb`
  (default 6144 MB) of RAM plus swap, and wakes the Sentinel to propose the new
  `python -m genesis.sentinel.process_stop`. The tool is dry-run by default.
  With `--execute --only PID:STARTTIME,...`, the exact line its dry-run prints,
  run only inside an action the owner approves, it stops only those processes
  that are still over the line (SIGTERM, then SIGKILL, via a pidfd), so an
  approval never covers a process that crossed the line afterwards. Services, pid 1 and the tool's own ancestors are never targets.
  The lever is `process_stop.mode` in `config/sentinel.yaml`.
