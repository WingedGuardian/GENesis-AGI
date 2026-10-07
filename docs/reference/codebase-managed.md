# Managed Codebase configuration staging

`scripts/codebase_managed.py` provides configuration and native unit entry points: `configure` stages
a pinned provider and publishes immutable settings; `status` diagnoses settings
and native service state. The ordinary install/bootstrap loops render disabled
query service/client slice templates on every install and do not automatically
install, upgrade or version-probe a PATH Codebase binary. Registration always
uses the managed launcher, including when no PATH provider exists. The index
queue remains on its previous route until its separate integration lands.
Staging and rendering alone do not make Codebase available.

## Native lifecycle

After staging and ordinary template rendering, deliberately use:

```bash
python3 -I scripts/codebase_managed.py enable
python3 -I scripts/codebase_managed.py disable
python3 -I scripts/codebase_managed.py remove
```

`enable` uses the fixed installed settings path and validates the pin/cache/sentinel.
Before native mutation it requires exact current rendered service and slice sources
in the supported installed namespaces, with no competing fragments or override
namespaces anywhere in the manager's lookup paths. Extra directives, comments,
whitespace changes, masks, generated/transient sources and final fragment symlinks
refuse; selected directory aliases and literal installation paths remain supported.
It explicitly refreshes accepted sources, verifies their loaded identities, commands
and limits, enables through the same manager's native installation API without
starting, rechecks and refreshes accepted sources again, then starts and verifies
native readiness. Backend argv must match
the installed positional bridge, including its `no-env-expand` flag; auxiliary
execution commands refuse. A custom installation must supply the same absolute
`VENV_PATH` used by `install.sh` when invoking `enable`; otherwise the configured
main checkout's `.venv` is expected. Disable/remove do not require that variable.
Missing typed manager properties, unsupported native namespace layouts or unavailable
`busctl` refuse activation. Installation calls cannot fall back to a client-side
reader. Current canonical templates are the authority, not a partial directive
allowlist or `NeedDaemonReload` flag.
Enable also requires an initially inactive or failed backend with no main PID or
populated cgroup. An already running backend refuses before reload or retirement;
this prevents rollback from executing newly reloaded incompatible stop commands.
It never removes the
sentinel. Failed startup uses the same canonical retirement contract; startup and
incomplete rollback errors remain visible. A command timeout does not prevent the
remaining independently authorized retirement attempts.
Persistent and runtime enablement links are disabled independently before final
native state proof, including installations with both kinds of link.

`disable` retires supported canonical native units without reading settings, including
missing, malformed, old-schema or stale-build settings. It verifies service PID,
native enablement and recursive empty cgroups; slices/scopes do not expose the
service-only MainPID property. Inert parameters from both typed startup commands
allow retirement without the current virtualenv selection, interpreter existence,
pin or sentinel. Unknown/customized/stale unit contracts refuse with files,
settings and processes preserved for operator-directed native intervention. Because
refresh is global, an unsupported source for either fixed unit blocks refresh and
automated stopping of the pair; both units' state proofs and errors are reported.
Manager uncertainty or failed stop refuses success. Accepted sources and quiescence
are rechecked before refreshing cached installation state for final disablement.
Native disable removes fixed-name and target-associated enablement links in its
persistent/runtime namespaces, including aliases to the managed target; it never
deletes their target files or recursively disables an independent auxiliary unit.
`remove` first performs the same retirement, then unlinks only the fixed service,
client slice and known persistent/runtime enablement artifacts and reloads the
manager. All eight fixed candidates are validated before the first unlink;
directories, FIFOs, lookup errors and observed replacements refuse. Selected unit
and membership directory aliases are supported and remain intact. Final artifact
links are unlinked without following their targets. Unrelated names, settings and
provider state are preserved. A later unlink failure reports partial removal and
still reloads after successful deletions; no artifacts are recreated as rollback.
Partial removal refresh requires this invocation's prior successful retirement,
both units quiescent and remaining sources canonical or intentionally removed.
No template ownership/header/repair mechanism is introduced.

All three hold the exclusive nonblocking lifecycle lock through completion or
rollback. Busy frontend/worker admission refuses; a worker already admitted is
left to its existing watchdog. Daemon startup/readiness does not take a shared
lock that could deadlock with this exclusive enable operation. The commands do
not change immutable settings bytes. None starts the indexing queue.
These checks coordinate cooperating tools; validation followed by native start or
unlink is not atomic against a noncooperating operator replacing files or parents.
The user manager's environment, global defaults and unrelated native dependency
owners remain trusted; canonical matching is not a sandbox for the whole manager.

## Managed MCP frontend

`.claude/mcp/run-codebase-memory` invokes the managed `launch` command. Missing
or invalid settings, disabled native backend, armed sentinel, wrong pin/cache
or unavailable manager refuse clearly. There is no raw/PATH binary, address-space
fallback or legacy binary/memory/sentinel environment override. The immutable
configuration selects these paths. Extra provider arguments are refused; the
native tool profile is fixed to `analysis`.

Install/bootstrap register this launcher on every install and heal stale raw
registrations through the existing registration helper. Registration does not
enable or start a provider. Worktree launchers use the configured physical main
helper in the actual child, with `/` as its neutral native working directory.
The launcher selects the literal absolute `VENV_PATH`, or the primary checkout
`.venv`, and refuses an unavailable interpreter without a system-Python fallback.
An absent or empty HOME is normalized to the passwd home before path selection;
a nonempty HOME must be absolute and is retained.

Each frontend is a unique transient user service with 256 MiB memory, zero swap,
TasksMax32 and OOMScoreAdjust500, inside the 2 GiB/zero-swap/TasksMax512 aggregate
client slice. Requisite/After/StopPropagatedFrom bind it to the already running
backend without activating that backend. Native collection, wait and pipe preserve
MCP stdio. A fixed shell bridge transports paths in environment values and
escapes systemd expansion with `$$`; it uses options available in systemd 249.
This preserves literal paths without the newer `--expand-environment` option.

Inside the actual capped child, shared nonblocking lifecycle admission precedes
a fresh settings read and kernel leaf/aggregate/ancestor checks. Cache, pinned
binary and native permanent-daemon RPC PID are verified again. The shared lock
is released by CLOEXEC at the accepted binary exec; admitted readers do not hold
up disable, which stops their dependency and aggregate slice. Inherited CBM_*
values are scrubbed and explicit cache/runtime/root paths supplied. No graph
indexing or runtime unit repair is performed by a frontend.

The stock pinned MCP bootstrap can start a session daemon if its endpoint
disappears after admission. Native StopPropagatedFrom also propagates unexpected
backend death, and KillMode=control-group retires the reader and any descendants.
A short spawn before the manager processes that transition remains possible;
it stays inside the reader's 256MiB/zero-swap cgroup and aggregate. This is an
accepted bounded timing residue, not a strict connect-only/no-fork guarantee.
No alternate executable or uncapped execution path is provided by the launcher.


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
flag. Native systemd enablement owns operational state. The lifecycle lock coordinates cooperating same-user tools;
it is not protection against deliberate same-user filesystem interference.

## Queued physical indexing

The existing idle-gated runner checks managed availability before and after a
CBM-only claim. The index entrypoint checks again before its CBM leg. Missing or
invalid configuration, native disablement, an armed sentinel or an unavailable
backend defers CBM without starting a service or charging an indexing failure.
A combined request can finish GitNexus and durably retain only CBM (`rc=5`).

CBM always uses the pinned stock internal worker in the existing admitted
8 GiB, zero-swap scope, with a 6 GiB native memory budget. The Python
coordinator preserves its inherited OOM adjustment and refuses an inherited
maximum (`1000`). Only the disposable native child is promoted to `1000`, through
the existing fixed Bash exec launcher, with its accepted executable FD retained.
This removes deliberate maximum preference from the coordinator; it does not
promise OOM immunity or override cgroup group-kill policy.
Smaller or larger job-cap overrides refuse. Destination capacity, sibling/cache
reserves and the existing pressure watchdog remain enforced. No PATH binary,
ordinary daemon-delegating CLI or worker-binary environment override is used.

Inside that actual scope, the worker acquires the shared lifecycle lock before
fresh configuration and authorization checks, requires the configured physical
main checkout, and executes the accepted inode with explicit managed environment.
The lock releases immediately after child creation, before waiting. Disable
refuses competing admission but does not cancel an already admitted worker;
the existing scope watchdog owns cancellation and descendant cleanup.

Authorization/capacity/lock refusals use the existing refusal marker and preserve
queue attempts. Spawn, provider, response and crash failures remain charged
errors (`111`); provider exit codes cannot impersonate queue deferrals. The
generation-aware queue, escalation clock and durable outcomes are unchanged.
The read-only `available --repo /absolute/main` command exposes these preflight
checks; it is not execution authorization for another process. Worker invocation
requires an explicit `--managed-config` argument from the existing entrypoint.
Managed indexing also requires the absolute canonical repository lock namespace.
Failure to create it remains a refusal even when a temporary fallback lock opens.
The legacy GitNexus fallback remains available to its own leg; it never authorizes
the managed Codebase leg on a different inode.

## Native query service

`genesis-cbm-query.service` invokes `serve` and `ready` using the installed
settings path. These are unit entry points alongside the operator lifecycle and
managed MCP launcher. They require immutable accepted
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
has 2 GiB memory, zero swap and TasksMax 512.


Startup verifies cache flags and the executable inode, uses the pinned native
local configuration read to repair a stale endpoint generation, then execs the
stock permanent daemon. It holds no shared lifecycle lock while readiness is
pending, avoiding a deadlock with the exclusive enable operation. Readiness
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
proof. Unsupported unit contracts abort before deleting the installation; inspect
the preserved definitions and use operator-directed native recovery. A native
implicit slice can remain loaded without a fragment: it counts as absent only
with empty source/override/enablement metadata, no transient identity, and proven
inactive state and empty descendant groups. Manager failures, failed stop or
uncertain state abort cleanup.
Final enablement is checked even when the native disable call succeeds: surviving
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
