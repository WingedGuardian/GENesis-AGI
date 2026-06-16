# Operator — You're on a Genesis Host VM

You (a human) are SSHed into the **host VM** of a Genesis install. You are **not** inside Genesis,
and you are **not** the Guardian. Genesis (the autonomous agent) runs inside an Incus container; a
host-side **Guardian** systemd timer watches that container and can auto-recover it.

Shared install facts (host/container identity, IPs, dashboard URL) are in `~/.claude/CLAUDE.md`,
auto-loaded alongside this file.

## Stop / pause the Guardian (e.g. before host maintenance)

The Guardian checks for a maintenance flag on every tick and stands down if it is present:

- **Pause**:  `touch /var/lib/guardian-snapshots/.guardian-maintenance`
- **Resume**: `rm /var/lib/guardian-snapshots/.guardian-maintenance`
- **Hard stop**: `systemctl --user stop genesis-guardian.timer genesis-guardian-watchman.timer`
  (stop the watchman too — it re-arms the guardian timer)
- **Status / logs**: `systemctl --user status genesis-guardian.timer` ·
  `journalctl --user -u genesis-guardian -n 50`

## Work inside Genesis (the container)

- `genesis`  — alias added to your `~/.bashrc`, or
- `incus exec __CONTAINER_NAME__ --user __UBUNTU_UID__ --env HOME=/home/ubuntu --cwd /home/ubuntu/genesis -t -- bash -l`

## Don't

- Don't hand-edit `~/.local/share/genesis-guardian/` — it's a deploy target (overwritten on update).
- Genesis's own config/state/database live **in the container** under `~/.genesis/` and `~/genesis/`,
  not on the host.
