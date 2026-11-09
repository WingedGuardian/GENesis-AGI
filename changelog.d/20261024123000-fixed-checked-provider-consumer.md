- Checked queued indexing validates complete NUL records and actual queue-command
status before one-provider dispatch, preserves claimed identity and mode, and
replays retained completion without resending. Entrypoint and freeze repository
paths retain trailing newlines and agree on Linux root aliases before lock hashing.
A busy queue now defers the tick (logged, exit 0) instead of failing the unit, and
a queued repository that later moves no longer stops the whole list.
Automatic readiness remains disabled
until native evidence and persistent resource-failure quarantine are complete.
