# Managed Codebase configuration staging

`scripts/codebase_managed.py` provides configuration and native unit entry points: `configure` stages
a pinned provider and publishes immutable settings; `status` diagnoses settings
and native service state. The ordinary install/bootstrap loops render disabled
query service/client slice templates on every install and do not automatically
install, upgrade or version-probe a PATH Codebase binary. The existing launcher,
registration and index queue remain unchanged until their integration concerns.
Staging and rendering alone do not make Codebase available.

## Configure

Run from the primary Genesis checkout, using the accepted v0.11.0 Linux x86_64
portable binary from the [upstream release](https://github.com/DeusData/codebase-memory-mcp/releases/tag/v0.11.0).
Other builds/platforms require new acceptance before changing the build pin.
The binary must be a regular executable file, rather than a symlink.

```bash
python3 -I scripts/codebase_managed.py configure \
  --main "$PWD" \
  --binary "$HOME/tmp/codebase-memory-mcp" \
  --state "$HOME/.genesis/cbm" \
  --sentinel "$HOME/.genesis/codebase-memory-mcp.disabled"
```

The binary argument names an already obtained executable. This command does
not download or run an installer. Its digest must match the existing stock
worker's accepted `BUILD`. It never executes a binary found on PATH.

Choose a new, short state path outside the repository and settings directory.
The provider's native runtime uses Unix sockets and refuses overlong endpoint
paths. The limit applies to the complete endpoint, including provider-added
directory/socket names, and to bytes rather than characters. A normal deep
pytest fixture can exceed it. Do not work around a refusal with another IPC
namespace or raw execution: choose a shorter state path.

Configure creates private `bin`, `cache` and `runtime` directories under that
state path. It verifies the source executable, copies and re-verifies it, sets
the provider's `auto_index`, `auto_watch` and `watcher_enabled` settings to false,
disables its UI and verifies the resulting cache before publishing settings.
The validation database handle is closed, every staged file is synchronized,
and staging directories are synchronized from children through the first
existing ancestor. Newly created settings-parent entries are made durable
before publication; the final settings-directory synchronization follows the
no-clobber link. These separate file and directory barriers follow
[fsync(2)](https://man7.org/linux/man-pages/man2/fsync.2.html).
These native `config set` calls initialize only the selected cache/runtime;
they do not index the repository or start a query daemon.

Settings are published at `~/.genesis/config/codebase-managed.json` last, without
overwriting any existing destination—including dangling links or nonregular
files. Configure refuses linked worktrees and a settings-path override that
differs from the installed service's default. Absolute paths retain whitespace.

No service is enabled, no unit is rendered and no sentinel is removed.
Configuration uses schema 2: paths and build identity, with no mutable `enabled`
flag. Native systemd enablement will own operational state when lifecycle
integration lands. The lifecycle lock coordinates cooperating same-user tools;
it is not protection against deliberate same-user filesystem interference.

## Native query unit entry points

`genesis-cbm-query.service` invokes `serve` and `ready` using the installed
settings path. These are unit entry points, not substitutes for the forthcoming
operator lifecycle and managed MCP launcher. They require immutable accepted
settings, the configured primary checkout, persistent native enablement and a
definitely absent sentinel. Runtime-only enablement and all other unit file
states refuse. Nothing automatically enables the service or removes the sentinel.

The query daemon runs in its own cgroup v2 service with exactly 2 GiB memory,
zero swap, TasksMax 128, CPUQuota 200%, OOMScoreAdjust 500 and control-group
cleanup. Restart is disabled. Every visible finite ancestor must admit the full
query budget; unreadable or malformed limits refuse. The aggregate client slice
has 2 GiB memory, zero swap and TasksMax 512; the frontend integration comes later.

Startup verifies cache flags and the executable inode, uses the pinned native
local configuration read to repair a stale endpoint generation, then execs the
stock permanent daemon. It holds no shared lifecycle lock while readiness is
pending, avoiding a deadlock with a future exclusive enable operation. Readiness
requires a native connect-only status RPC that names the permanent service PID,
the correct executable and actual kernel limits. Socket existence is insufficient.
Each status RPC uses the remaining 60-second readiness window: the pinned CLI
hashes its executable at startup, so a healthy status call can exceed three
seconds. No status call starts once that window has expired.

Both ordinary renderer loops substitute quoted Exec paths using separate systemd
and sed escaping. Whitespace, quote, backslash, dollar, percent, ampersand, pipe
and Unicode paths retain their literal meaning. A symlink, directory or FIFO at
either managed template destination is refused before writing; unrelated unit
and FalkorDB rendering retain their existing behavior. Install preserves an
existing regular unit; bootstrap updates it through its ordinary rendering loop.

## Diagnose and recover

```bash
python3 -I scripts/codebase_managed.py status
python3 -I scripts/codebase_managed.py --config /absolute/settings.json status
```

`CODEBASE_MEMORY_MCP_MANAGED_CONFIG` supplies the default diagnostic override;
it applies only to `status`. An explicit `--config` wins; an empty override uses
the standard path. `configure` always uses the installed default unless an
explicit argument names it, and refuses an explicit nondefault path.
Status does not execute the provider
or mutate settings. It returns JSON with separate settings and manager errors,
including when settings are missing, malformed, old, or unresolvable. A zero
status exit means the diagnostic completed, not that execution is ready.
Diagnostic settings include only known scalar metadata; unknown fields and
deeply nested invalid values cannot prevent reporting manager state.
An incompatible build populates `settings_error` while retaining its metadata
and independently reporting systemd status.

Existing settings and state are never refreshed in place. Schema 1 and other
builds remain diagnosable, but are not valid schema-2 execution configurations.
Before reconfiguration, stop any existing managed native processes, preserve
old settings and state at a backup location you select, then configure a fresh
state path. This staging command does not perform the stop or backup itself.

If staging fails, inspect the reported state path. It is retained for diagnosis;
settings remain unpublished unless their final link was already created before
a directory-sync failure. In that case the published settings are retained too
and the command reports failure. Do not blindly rerun: an existing state/settings
destination refuses. Preserve or remove only artifacts you have identified as
belonging to that failed attempt.

Failed staging is not an exemption from whole-install uninstall. Existing
documented deletion roots can still contain it. State outside those roots is
not automatically deleted merely because a settings file names it.

## Whole-install removal

Run the ordinary `scripts/uninstall.sh` entry point. Real container cleanup
enters the managed guard before changing monitoring or state. It acquires the
existing runner lock, physical repository index lock, then lifecycle lock without
waiting; a busy writer or freeze refuses removal. It also refuses surviving
index scopes, including workers whose queue parent has gone away.
Retirement precedes the direct script's backup/confirmation prompts. Cancelling
keeps the repository and state, but leaves the query service stopped and disabled,
as the entry point explicitly reports; deliberate native enablement is required
to resume it. Make any desired backup before entering removal.

The guard retires the fixed query service and client slice using native systemd
commands and verifies inactive/failed state, zero MainPID and empty cgroups,
including descendants. Missing, malformed or stale settings cannot bypass this
proof. Manager failures, failed stop or uncertain state abort cleanup.

The helper executes its fixed sibling uninstall script with the real lock
descriptors inherited and checked on entry. Those descriptors remain held through
removal of the repository, runtime state, database and Qdrant roots. Host cleanup
delegates once to this same container transaction after its normal backup and
confirmation; a missing/older guard or failed container command refuses cleanup
without falling back to separate deletion commands. Host Guardian removal and
full container deletion retain their existing distinct scope.

Only the fixed CBM service/slice fragments and their known persistent/runtime
enablement locations are removed. Symlinks are unlinked without deleting foreign
targets; unrelated slices survive. Valid settings can report external retained
binary/cache/runtime paths, but cannot expand the documented deletion roots.
Dry-run does not acquire locks, stop managed units or delete state. An internal
reentry marker alone is insufficient: it requires verified inherited descriptors.

## Verification

The configuration test suite uses synthetic paths and fixtures. Native staging
acceptance explicitly opts in to the independently pinned release binary:

```bash
GENESIS_TEST_CBM_PINNED_BINARY="$HOME/tmp/codebase-memory-mcp" \
  .venv/bin/pytest tests/test_scripts/test_codebase_managed_config.py
```

The native fixture uses its own short temporary home/repository/cache/runtime
below `~/tmp`. It verifies publication, disabled provider options, preserved
sentinel, repeat-configure refusal and retained failure for an overlong runtime
path. It does not run the full Genesis runtime or write the live code graph.
