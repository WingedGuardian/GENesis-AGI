# Managed Codebase configuration staging

`scripts/codebase_managed.py` provides configuration and native unit entry points: `configure` stages
a pinned provider and publishes immutable settings; `status` diagnoses settings
and native service state. The ordinary install/bootstrap loops render disabled
query service/client slice templates on every install and do not automatically
install, upgrade or version-probe a PATH Codebase binary. The existing launcher,
registration and index queue remain unchanged until their integration concerns.
Staging and rendering alone do not make Codebase available.

## Native lifecycle

After staging and ordinary template rendering, deliberately use:

```bash
python3 -I scripts/codebase_managed.py enable
python3 -I scripts/codebase_managed.py disable
python3 -I scripts/codebase_managed.py remove
```

`enable` uses the fixed installed settings path, validates the pin/cache/sentinel
and loaded aggregate slice limits before any enablement change, then enables and
starts the query service and verifies native readiness. It never removes the
sentinel. Failed startup attempts independent native disable and stops of backend
and client slice; startup and rollback errors remain visible. A command timeout
does not prevent the remaining retirement attempts.
Persistent and runtime enablement links are disabled independently before final
native state proof, including installations with both kinds of link.

`disable` retires the fixed native units without reading settings, including
missing, malformed, old-schema or stale-build settings. It verifies service PID,
native enablement and recursive empty cgroups; slices/scopes do not expose the
service-only MainPID property. Manager uncertainty or failed stop refuses success.
`remove` first performs the same retirement, then unlinks only the fixed service,
client slice and known persistent/runtime enablement artifacts and reloads the
manager. Foreign symlink targets, unrelated slices, settings and provider state
are preserved. No template ownership/header/repair mechanism is introduced.

All three hold the exclusive nonblocking lifecycle lock through completion or
rollback. Busy frontend/worker admission refuses; a worker already admitted is
left to its existing watchdog. Daemon startup/readiness does not take a shared
lock that could deadlock with this exclusive enable operation. The commands do
not change immutable settings bytes. None starts the indexing queue or registers
the forthcoming managed MCP frontend.

## Configure

Run from the primary Genesis checkout, using the accepted v0.11.0 Linux x86_64
portable binary from the [upstream release](https://github.com/DeusData/codebase-memory-mcp/releases/tag/v0.11.0).
Other builds/platforms require new acceptance before changing the build pin.
The binary must be a regular executable file, rather than a symlink.

```bash
.venv/bin/python -I scripts/codebase_managed.py configure \
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
Resolved paths must satisfy the same validation as the published settings:
newline and carriage-return characters introduced by symlink ancestors are
refused before staging. The final settings filename remains unresolved.

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
query budget. Startup checks current charged memory using the existing clean-file
cache discount and 2 GiB cache reserve, and checks host available memory. It
conservatively includes the small staging Python process without subtracting its
leaf charge. This is a capacity observation, not an allocation reservation.
Unreadable or malformed limits/current usage refuse; invalid cache statistics use
the full charge. Recurring readiness verifies containment without repeating
startup admission when siblings allocate memory. The aggregate client slice
has 2 GiB memory, zero swap and TasksMax 512; the frontend integration comes later.

Startup verifies cache flags and the executable inode, uses the pinned native
local configuration read to repair a stale endpoint generation, then execs the
stock permanent daemon. It holds no shared lifecycle lock while readiness is
pending, avoiding a deadlock with a future exclusive enable operation. Readiness
requires a native connect-only status RPC that names the permanent service PID,
the correct executable and actual kernel memory, swap, CPU and task ceilings.
Missing or unlimited CPU/task controls refuse, even if unit properties name caps.
Socket existence is insufficient. Readiness uses an overall 120-second clock
including its binary hash and startup polling. The first accepted native PID
starts a 60-second RPC window clipped to that original deadline; a late native
appearance can receive less than 60 seconds. Status and manager calls use the
remaining window. The pinned CLI
hashes its executable at startup, so a healthy status call can exceed three
seconds. No status call starts once that window has expired. The unit's
TimeoutStartSec remains the outer enforcement; ordinary file reads cannot be
interrupted by the Python clock, so direct callers need their own timeout.

Both ordinary renderer loops substitute quoted Exec paths using separate systemd
and sed escaping, including the installed venv interpreter. Install uses its
selected VENV_PATH; bootstrap uses the checkout's .venv. Operator commands below
assume that standard path; use the selected interpreter on a custom install.
The fixed `/bin/sh -c 'exec "$@"' --` bridge passes that absolute interpreter path
as a literal positional argument, because systemd's executable-name grammar
rejects some characters that its argument grammar accepts. The fixed script
never interpolates path data into shell code or performs PATH lookup.
Whitespace, quote, backslash, dollar, percent, ampersand, pipe
and Unicode paths retain their literal meaning. A symlink, directory or FIFO at
either managed template destination is refused before writing; unrelated unit
and FalkorDB rendering retain their existing behavior. Install preserves an
existing regular unit; bootstrap updates it through its ordinary rendering loop.

## Diagnose and recover

```bash
.venv/bin/python -I scripts/codebase_managed.py status
.venv/bin/python -I scripts/codebase_managed.py --config /absolute/settings.json status
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

If an attempt fails after creating its staging directory, inspect the reported
state path. It is retained for diagnosis;
settings remain unpublished unless their final link was already created before
a directory-sync failure. In that case the published settings are retained too
and the command reports failure. Do not blindly rerun: an existing state/settings
destination refuses. Preserve or remove only artifacts you have identified as
belonging to that failed attempt.
Source-executable refusal before staging creation does not report a retained
staging directory.

Failed staging is not an exemption from whole-install uninstall. Existing
documented deletion roots can still contain it. State outside those roots is
not automatically deleted merely because a settings file names it.

## Whole-install removal

Run the ordinary `scripts/uninstall.sh` entry point. Real container cleanup
enters the managed guard before changing monitoring or state. It acquires the
existing runner lock, physical repository index lock, then lifecycle lock without
waiting; a busy writer or freeze refuses removal. It also refuses surviving
index scopes, including workers whose queue parent has gone away.
It also observes same-account fallback GitNexus entrypoints/analyze processes
for this checkout, using kernel argv/cwd and stable process start identity.
Observed writers or uncertain identity refuse removal; discovery sends no signals.
The snapshot cannot prohibit a new unlocked process launched afterward.
Retirement precedes the direct script's backup/confirmation prompts. Cancelling
keeps the repository and state, but leaves the query service stopped and disabled,
as the entry point explicitly reports; deliberate native enablement is required
to resume it. Make any desired backup before entering removal.

The guard retires the fixed query service and client slice using native systemd
commands and verifies inactive/failed state, zero MainPID and empty cgroups,
including descendants. Missing, malformed or stale settings cannot bypass this
proof. Manager failures, failed stop or uncertain state abort cleanup.
Final enablement is checked even when `systemctl disable` succeeds: surviving
global/runtime enablement still refuses removal.

The helper executes its fixed sibling uninstall script with the real lock
descriptors inherited and checked on entry. Those descriptors remain held through
removal of the repository, runtime contents, database and Qdrant roots. Existing
`~/.genesis/locks` and its original inodes remain as coordination state; settings,
queue and provider contents elsewhere within the fixed deletion roots are removed. This prevents a
stalled entrypoint from recreating a different lock inode during teardown.
Plain default locks and nonoverlapping external `GENESIS_HOME` are supported.
Custom lock layouts intersecting the four deletion roots, or default namespace
symlinks that cleanup would unlink, refuse before retirement. No lock relocation
is performed. The lifecycle lock's systemd-directory namespace must also remain
outside all four roots in both lexical and resolved paths, including through
symlink ancestors; aliasing it into the retained runner locks is unsupported.
Safe external systemd-directory aliases remain supported. No broad native-state
retention mode is introduced. Assess conflicting layouts
with all writers stopped; do not move live locks to bypass refusal. Host cleanup
delegates once to this same container transaction after its normal backup and
confirmation; a missing/older guard or failed container command refuses cleanup
without falling back to separate deletion commands. Default host cleanup finishes
before Guardian artifacts are removed. On refusal, its installation remains;
the earlier monitoring pause is not automatically undone. Full container deletion
keeps its existing inner-cleanup skip and separate confirmation behavior.

Only the fixed CBM service/slice fragments and their known persistent/runtime
enablement locations are removed. Symlinks are unlinked without deleting foreign
targets; unrelated slices survive. Valid settings can report external retained
binary/cache/runtime paths, but cannot expand the documented deletion roots.
Unclassifiable diagnostic paths are reported and do not alter deletion authority.
Both Qdrant binary locations and dangling known unit links are handled; foreign
symlink targets remain untouched. Failed runtime inventory aborts before deleting
install roots and retains the inventory for inspection.
Dry-run does not acquire locks, stop managed units or delete state. An internal
reentry marker alone is insufficient: it requires verified inherited descriptors.
The teardown helper retains its system Python entry point and uses only standard
library operations available on the supported Python 3.10 platform. It does not
perform the native binary hash/launch acceptance used by the installed query
runtime. Current-install tests are not a separate Python 3.10 runtime qualification.

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
