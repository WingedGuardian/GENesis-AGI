# Shared Serena for the main checkout

Serena 1.7.0 can serve multiple MCP clients through native Streamable HTTP.
This integration is opt-in and shares only the canonical main checkout.
The loopback services trust local users and processes. Profile separation is
configuration separation, not authentication or a cross-user security boundary. It
maintains separate Claude and Codex profiles; linked worktrees use native stdio
against their nearest project boundary. Editing a shared checkout changes the
same files for all its clients, as it does without sharing.

## Enable

Install Serena 1.7.0 and Terse, then run from the main checkout:

```bash
python3 scripts/serena_shared.py configure --main "$PWD" --enable
```

This snapshots native global settings and each effective context into
`~/.genesis/serena-shared/`, installs and enables two systemd user services,
and records opt-in settings in `~/.genesis/config/serena-shared.json`.
Native custom modes, prompt templates and global memories remain linked to
their original provider home so customizations and memory edits are preserved.
An existing resource containing data is never silently replaced.
`GENESIS_HOME` relocates the settings/provider-home root. Both profiles use
native single-project mode; the Codex context retains its native name for
OpenAI tool schemas. Dashboard and GUI surfaces are disabled. Ports 9165 and
9166 must be free. Checkout paths ending in whitespace or a backslash are
rejected before configuration changes. Startup also checks the actual native exposed tools and
refuses any configuration that reintroduces project or mode switching.
Enable/disable transitions take one per-user process lock, including preflight
and final publication. Shared launchers hold a shared lock from settings read
through the native Terse process lifetime and check the running provider’s
checkout before attaching. A stale marker in another configuration root refuses.
Configure refuses while shared clients or another configuration are active;
close shared MCP connections before enabling, disabling or changing snapshots.
Native stdio/worktree clients release this lock before starting their provider.
Revalidate inherited lock ownership when upgrading the native Terse transport. Version
1.7.0 is checked before changing configuration and again at service startup;
Automatic upgrades skip immediately if that lock is busy; otherwise they take it and require both managed services to be
stopped and disabled (or persistently masked), regardless of `GENESIS_HOME`.
Unreadable user-manager state skips the optional upgrade with a warning; it
does not stop installation of Genesis. Native upgrades remain available when
no shared services are installed.
An occupied port refuses configuration. Stop the two owned
services before deliberately refreshing snapshots with another configure run.

Configure clients to execute `.claude/mcp/run-serena --context claude-code`
or `.claude/mcp/run-serena --context codex`. Bootstrap and install register the
Claude launcher. Codex registration remains an install-local choice. The
launcher uses Terse's existing URL transport for main; it does not implement
MCP or Serena tools. Existing stdio sessions stay alive until their clients
reconnect using the updated configuration.

## Verify and operate

```bash
systemctl --user status genesis-serena-claude-code genesis-serena-codex
journalctl --user -u genesis-serena-claude-code -u genesis-serena-codex
```

Each service has a 4 GiB hard limit, zero swap, two CPU quota, cgroup-owned
children, and at most three starts within five minutes after failures. Manual starts, including configure retries, also count toward that limit;
after repeated failures, fix the cause and wait for the five-minute interval
before retrying. Configure does not implicitly reset the failure counter. These
limits are based on the measured main-checkout experiment, not a guarantee for
arbitrary repository growth. Measure again before provider upgrades; service
startup refuses versions other than 1.7.0. Shared queries serialize through
Serena's native task executor.
Terse's native HTTP forwarding waits for each response before forwarding the
next message from that client. Cancellation therefore does not promptly
interrupt an outstanding provider call; this integration preserves that stock
behavior. Another client's connection remains usable.

A missing/failed shared service produces an MCP startup error without starting
another stdio server. Restore that service, then reconnect the affected MCP
client. Terminals and conversations are not killed. A service restart invalidates
its previous HTTP sessions; reconnect clients afterward. Worktree processes
retain their existing native stdio lifecycle and resource behavior.

## Rollback

```bash
python3 scripts/serena_shared.py configure --main "$PWD"
```

Close shared MCP connections first. This disables sharing in settings and then disables/stops the two owned
services. It does not require a working provider or proxy. Reconnect clients;
the same launcher now uses native stdio. Preserve provider snapshots for
inspection; they live under the existing backed-up `.genesis` directory.
Codebase MCP enablement and GitNexus indexing are independent of this change.
