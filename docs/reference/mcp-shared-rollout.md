# Shared MCP rollout switch

Genesis MCP servers run as one stdio process per Claude Code session. The shared
service design replaces that with one long-lived service per server and a thin
per-session adapter. This page covers the Claude Code side of the rollout: the
switch that picks a command per server, the deploy integration, and the
qualification checklist. The switch is **inert**: the shipped stage is `off`, and
the code ceiling (`MAX_STAGE` in `scripts/lib/mcp_rollout.py`) is `off` too, so no
configuration can move any server off stdio until a reviewed code change raises it.

## Where the switch sits

Every Genesis registration Claude Code reads — the interactive `.mcp.json` and the
config rendered for each dispatched session, both from `config/mcp.json.template` —
runs `.claude/mcp/run-mcp-server --server <name>`. The switch lives in that launcher,
so it covers foreground, dispatched and conversation-channel sessions and every
checkout alike, and it adds or removes no registration.

- **No opt-in, no change.** Unless `GENESIS_MCP_SHARED_STAGE` is set or
  `${GENESIS_HOME:-~/.genesis}/config/mcp_shared.json` exists, the launcher execs the
  stdio server exactly as before and never starts the helper.
- **Opted in.** The launcher hands its argv to `scripts/lib/mcp_rollout.py launch`,
  which execs either that stdio command or the configured adapter. The final process
  is the same `python genesis_mcp_server.py …` on the stdio path, so process census
  and spawn identity see what they always saw.
- **Never routed:** any argv other than the registered `--server <name>` form, so
  Codex (`--external-client`, which `scripts/codex_external_mcp.py` passes; the Codex
  switch is separate) always runs stdio; and any spawn while
  `GENESIS_MCP_SHARED_DISABLED` is set to anything but `0`.
- **On any error** (an unreadable overlay, an unknown stage name) the helper launches
  the stdio server and says why on stderr, which Claude Code keeps in its MCP log.

### Stages

The effective stage is the lowest of: the requested stage (the environment variable,
else the config), the code ceiling, and the server's unit being ready.

| Stage | Servers on the adapter |
|---|---|
| `off` | none |
| `memory-floor` | memory |
| `health-reads` | memory, health |
| `writes` | memory, health, outreach, recon |

The adapter receives the stage (`{stage}` in its argv) and owns per-tool routing
inside it. A server outside the stage, or whose unit is not ready, runs stdio.
`discord-bot` and the payments vault path are never on the adapter.

Ready means `systemctl --user show <unit>` reports `LoadState=loaded`,
`UnitFileState=enabled`, `ActiveState=active` and `SubState=running`. A shared
service unit should therefore be `Type=notify`, signalling ready after it binds.

### Configuration

`config/mcp_shared.json` holds the defaults: the stage, the adapter argv (empty, so
nothing routes), and each server's unit name and loopback URL
(`genesis-mcp-<server>.service`, the ports `scripts/genesis_mcp_server.py` defaults
to). The overlay at `${GENESIS_HOME:-~/.genesis}/config/mcp_shared.json` merges per
key. The shared service and the adapter use these names, or the install sets its own
in the overlay. Adapter argv placeholders: `{python}` `{root}` `{server}` `{url}`
`{stage}`. These names and the adapter contract are provisional: the shared
service and the adapter own them and set their own where they differ.

The stdlib reader is `genesis.mcp.shared_config.load_config`; the rollout helper
re-exports it and its configuration error. The merge stays shallow: an overlay's
server key replaces that key, including a complete nested `service` object.
Direct script execution works with `python -I -S` and imports no MCP/provider or
runtime modules. The deploy helper captures both reader and CLI source when it
is sourced, so a merge cannot change either part of that run's reader.

Package floors are FastMCP 2.14.6 and MCP 1.30, retaining their current major API
lines. This prepares the inert shared core; a dependency range does not qualify
every later release or activate shared routing. No shared-service launcher or
service unit is added by the reader extraction.

A session decides once, when Claude Code spawns its MCP servers. Changing the stage
affects new sessions; running ones keep the command they started with.

### Duplicate registrations

Where a server is registered at more than one scope (project `.mcp.json`, a local or
user entry, a wrapper around the launcher), Claude Code picks one by scope
precedence. Every Genesis registration runs this launcher, directly or wrapped, so
the switch decides for whichever copy wins. Removing the duplicates is a separate
change.

## Deploy integration

`scripts/lib/shared_mcp_units.sh` restarts each configured unit that is installed and
enabled, then requires the restart to succeed, the unit to be active and its
invocation id to have changed. Units that are not installed or not enabled are left
alone, so on an install with no shared service nothing is restarted. An enabled unit
someone stopped stays stopped; a failed one is restarted. A unit whose state systemd
could not report counts as a failure, not as absent.

- `scripts/deploy_code_only.sh` (deploy and restart modes) runs it under the deploy
  lock once genesis-server passes its health check. Not on a deploy with nothing to
  deploy, and not in pull mode. A unit that does not come back raises a critical
  alert and exits 1; the tree is not reverted.
- `scripts/update.sh` runs it after its success disarm. A failure warns and raises a
  critical alert, and never rolls back the healthy server.

A restart interrupts in-flight tool calls on that service for every attached session;
it ends no session.

## Report

```bash
python3 scripts/lib/mcp_rollout.py status --root "$PWD"
```

Read-only: the requested stage, the ceiling, the kill switch, and per server the unit
state and what a new session would launch, with the reason.

## Qualification checklist (when the shared service and adapter exist)

Each step runs on one session first (`GENESIS_MCP_SHARED_STAGE=<stage>` in that
session's environment), then for the install (the overlay).

1. `status` names the expected units as ready and the expected servers as `adapter`.
2. A foreground session at `memory-floor`: a recall returns results; the memory MCP
   process count for that session is the adapter, not a full server.
3. A dispatched session (a Telegram turn) at the same stage: the same recall works,
   and its writes are attributed to that session, not to the service.
4. Restart a shared unit through the deploy path during an idle session: the next
   tool call succeeds without restarting the session.
5. Restart it during an in-flight write: the call reports an outcome-unknown error
   and is not resent.
6. Stop a unit: new sessions launch stdio for that server and the launcher's MCP log
   names the unit.
7. `GENESIS_MCP_SHARED_DISABLED=1`: every server is stdio.
8. Only then move to the next stage, in order.
