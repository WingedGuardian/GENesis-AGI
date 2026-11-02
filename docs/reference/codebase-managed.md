# Managed Codebase configuration staging

`scripts/codebase_managed.py` provides configuration and native unit entry points: `configure` stages
a pinned provider and publishes immutable settings; `status` diagnoses settings
and native service state. The ordinary install/bootstrap loops render disabled
query service/client slice templates on every install and do not automatically
install, upgrade or version-probe a PATH Codebase binary. Registration always
uses the managed launcher, including when no PATH provider exists. The existing
index queue now uses managed admission and the contained native worker described
under "Queued indexing" below.
Staging and rendering alone do not make Codebase available.

Existing symlinks at either managed query unit destination, including `/dev/null`
masks and dangling links, are kept during install and bootstrap without writing
through them. Bootstrap lists the kept unit in its update summary and continues
to reload units and process timers. Only an explicitly named
`--take-template genesis-cbm-query.service` (or the client slice's exact name)
lets bootstrap back up and replace that link; `--take-templates` keeps masks.
Install always keeps existing links. Directories and FIFOs at these two
destinations remain errors. Use the configured disablement sentinel for the
runtime off-switch; masking may refuse when a local regular unit already exists.

## If you see the 'registered but unconfigured' alert

The health alert `codebase_mcp_registered_unconfigured`, and the matching
`Codebase: NOT SET UP` line that bootstrap, install and update print, mean that
`codebase-memory-mcp` is registered (the checkout's `.mcp.json` or the
user-scope `~/.claude.json`) but its launcher will refuse. Either
`~/.genesis/config/codebase-managed.json` is missing, it fails validation (the
`status` command below names the error), or `genesis-cbm-query.service` is not
persistently enabled. Every Claude Code session then shows the server as failed
to connect.

To set it up, obtain the pinned binary, then run from the primary checkout (see
[Configure](#configure) for what each argument must be):

```bash
.venv/bin/python -I scripts/codebase_managed.py configure \
  --main "$PWD" \
  --binary "$HOME/tmp/codebase-memory-mcp" \
  --state "$HOME/.genesis/cbm" \
  --sentinel "$HOME/.genesis/codebase-memory-mcp.disabled"
python3 -I scripts/codebase_managed.py enable
```

If a valid settings file already exists, run only `enable`; for an invalid one,
follow "Diagnose and recover" below. To opt out instead, create the kill-switch
file. If a settings file exists, that is its configured `sentinel` path;
otherwise it is `~/.genesis/codebase-memory-mcp.disabled`, or the path
`CODEBASE_MEMORY_MCP_DISABLE_FILE` names:

```bash
touch "$HOME/.genesis/codebase-memory-mcp.disabled"
```

The alert reads the infrastructure profile, which refreshes at boot and daily.
After either fix, run `infrastructure_profile(refresh=true)` and the alert
clears at the next hourly posture check. Both the alert and the installer line
come from `genesis.infra_profile.codebase_mcp`, so they always agree.

## Native lifecycle

After staging and ordinary template rendering, deliberately use:

```bash
python3 -I scripts/codebase_managed.py enable
python3 -I scripts/codebase_managed.py disable
python3 -I scripts/codebase_managed.py remove
```

`enable` uses the fixed installed settings path and validates the pin/cache/sentinel.
Before native mutation it requires exact current rendered service and slice sources (raw or carrying the shared renderer stamp)
in the supported installed namespaces, with no competing fragments or override
namespaces anywhere in the manager's lookup paths. This includes `.d`, `.wants`,
`.requires` and `.upholds` at the full-name, dash-prefix and unit-type scopes;
even empty directories or dangling namespace links are unsupported and retained.
Extra directives, comments,
whitespace changes, masks, generated/transient sources and final fragment symlinks
refuse; selected directory aliases and literal installation paths remain supported.
It explicitly refreshes accepted sources, verifies their loaded identities, commands
and limits, enables through the same manager's native installation API without
starting, rechecks and refreshes accepted sources again, then starts and verifies
native readiness. Backend argv must match
the installed positional bridge, including its `no-env-expand` flag; auxiliary
execution commands refuse. A custom installation must supply the same absolute
`VENV_PATH` used by `install.sh` when invoking `enable` or `available`; otherwise
the configured main checkout's `.venv` is expected. The rendered indexing runner
exports that same literal selection to its child processes.
Disable/remove do not require that variable.
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
No new template ownership or repair mechanism is introduced; the existing managed-unit stamp is accepted only over the exact current canonical render.

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

The pinned provider rewrites literal backslashes as path separators on Linux.
Repository and cache paths containing backslashes, including their resolved
physical identities, therefore refuse before staging or native execution. This
prevents native configuration writes from selecting a different cache directory.
A configure argument's symlink alias may contain a backslash if it resolves to a
supported physical main/state path. Binary, interpreter, runtime, sentinel and
settings paths retain their existing literal-path handling; this restriction
applies to provider-consumed repository/cache identities.

Runtime entry points validate those identities before cache checks, readiness
retries or provider execution. An incompatible path therefore refuses promptly
when enabling, launching, serving or checking readiness. Structural settings
reads remain available for diagnosis and retirement.

No service is enabled, no unit is rendered and no sentinel is removed.
Configuration uses schema 2: paths and build identity, with no mutable `enabled`
flag. Native systemd enablement owns operational state. The lifecycle lock coordinates cooperating same-user tools;
it is not protection against deliberate same-user filesystem interference.

## Native query unit entry points

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
and Unicode paths retain their literal meaning. Existing symlinks at either
managed template destination follow the preservation and explicit takeover
policy above; directories and FIFOs are refused before writing. Unrelated unit
and FalkorDB rendering retain their existing behavior. Install preserves existing
regular units; bootstrap follows the shared managed-unit ownership policy.

## Diagnose and recover

The read-only `available --repo /absolute/main --persistence true|false` command
checks configured main identity, cache and completed query-service startup, plus writer
prerequisites in the caller's filesystem namespace. It creates no probe files or
queue rows and does not index repositories. It validates the loaded startup
contract, accepted executable and active constrained backend without invoking the
native CLI. Current RPC responsiveness is checked again by final worker admission.
Persistence checks include the artifact destination
and the pinned provider's fixed snapshot scratch directory; false omits both.
The immutable `_config.db` stays a reader surface, while index databases and their
WAL/SHM and rollback-journal sidecars require read/write access, as does the
mutable queue database and its sidecars. Cache and queue directories require
read access for enumeration. The append-only runner log and non-enumerated
runtime/lock directories do not require read access. The accepted executable
must also be executable by the caller in this namespace. Existing writable
descendants can remain usable under a read-only parent.

Availability is advisory: a runner with filesystem restrictions must perform its
own checks. Custom state roots need an operator-owned `ReadWritePaths` allowlist;
persistent snapshot scratch can use an operator-owned disk `BindPaths` mapping.
Genesis does not install a policy drop-in or broaden the runner sandbox. Preserve
paths masked by a mapping and assess its capacity before using it. The separate
worker/queue integration performs final admission; this command grants no right
to index and does not activate providers.

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
Unsupported repository/cache identities and symlink-resolution failures also
populate `settings_error` without suppressing metadata or the systemd check.
Structural settings reads remain available for retained-state diagnosis; no
incompatible cache is repaired or deleted automatically.

The separate `queue_initialization` object observes
`$GENESIS_HOME/index-requests/.queue-db-init.lock` (defaulting to
`$HOME/.genesis`). Its `required_when` is `queue initialization or schema upgrade`:
a current-schema queue can remain usable when this append-only lock is not
writable. `state` reports `absent`, `observed_writable`, or `unavailable`, with
the selected `path`, observed lexical `exists`, and any `error`. Unknown
existence is `null` when lookup fails. An absent lock also checks its first
existing parent's write/search prerequisites without creating anything. This
advisory neither inspects the database schema nor acquires a lock, and does not
change availability or readiness. Observed writability does not guarantee a
future open or an uncontended lock. Settings and manager diagnostics remain
independent of this observation.

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
Direct container cleanup uses its process's `GENESIS_HOME`. Host cleanup defaults
to the container user's `HOME/.genesis`, independently of the host's environment.
For an external namespace, pass `--container-genesis-home /absolute/container/path`
to `scripts/uninstall.sh` on the host. It must match the namespace used by every
cooperating container runner, freeze and manual index entrypoint. The override
travels as literal arguments and is assigned after login initialization. Duplicate,
missing, empty, relative and CR/LF-containing values refuse before monitoring
changes. The option is invalid inside the container, with `--guardian-only`, or
with `--full`; it does not change direct cleanup or full-container deletion.
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
Retirement requires the backend to be loaded and disabled, or exactly absent;
masked, generated, transient and static backend definitions refuse cleanup.
Retention diagnostics distinguish recursive removal of real roots from unlinking
a final root symlink, and account for the exact retained runtime `locks` child.
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

## Queued indexing

Automatic consumption is paused while the queue protocol is incomplete. The
runner holds its ordinary lock, then observes readiness before reconciliation,
listing, claiming or invoking a provider. Missing, legacy and incomplete queues
defer with exit 0, logged as a paused tick, so the unit does not read as failed
while the pause is expected. Unsupported metadata, observation failures and a
crashed or unreadable readiness helper stop with exit 76, as does a helper that
reports ready (this protocol has no ready state yet); lock failures and a
missing entrypoint or helper file still defer with exit 75. This binary never reports automatic readiness, including if
a readiness value is manually changed. There is no setter or environment bypass.

Explicit enqueue and queue APIs retain their schema-2 behavior. The observer can
read known schema-3 metadata without upgrading or importing it; the writer still
refuses a schema newer than 2. `python3 scripts/lib/index_marker.py automatic-ready`
is read-only and returns the dedicated status 10 for an incomplete protocol or
status 2 for an observation error. Queue-operation exceptions raised from `main()`
also return 2. Status 10 is reserved so that an interpreter crash (status 1) is
never mistaken for the expected pause; the runner treats any other status as a
failure. The runner discards the observer's JSON, so read the current state
with `python3 scripts/lib/index_marker.py automatic-ready`. `import-status`
keeps its schema-2 receipt contract and refuses a schema-3 queue.
Later provider storage, checked
consumption and resource-failure safeguards must land before automatic work can
resume; that readiness does not authorize lifting a freeze or disable sentinel.

The retained idle-gated consumer probes managed availability before and after
claiming a Codebase-only request. Unavailability leaves the pending generation
uncharged; a post-claim refusal durably restores it. A combined request can
complete GitNexus and requeue its unfinished Codebase leg before consuming the
combined request. Partial-success outcomes (rc 4/5) use the ordinary request
writer: a new pending row starts at zero attempts; coalescing with an existing
pending row retains that row's higher count. The original inflight attempt
count is not carried into the split request. Operators must not manually reset
attempts to force retries. Retry-budget continuity across splits and commit
generations remains part of the resource-failure quarantine work.
The runner records availability errors in its log; the index entry point includes
the final refusal line in its skip diagnostic.

The adapter executes the accepted native inode inside the admitted 8 GiB,
zero-swap scope with a 6 GiB native budget. Its supervisor retains a nonmaximum
OOM adjustment; only the native child receives 1000. The validated physical main
path supplies both the native payload and working directory. Final admission
rechecks managed settings, cache, native readiness and enablement under the
lifecycle lock, which closes after child creation. Repository/runner locks and
the scope watchdog retain execution ownership. Missing canonical repository
locks refuse Codebase; GitNexus retains its independent handling.

This wiring merged in #3012. Read-only availability/access repairs remain tracked
in #3464. Neither fixture acceptance nor a successful probe grants permission to
clear the sentinel, release the freeze or retry unattended after resource failures.
Follow the supervised production acceptance and resource gates in
`../architecture/codebase-managed-runtime.md`.

### Legacy queue import evidence

The queue upgrades its import metadata atomically to schema 2. Each legacy
artifact is isolated by a savepoint: malformed content produces a retained
receipt containing its exact raw bytes and a reason, while unrelated valid
requests continue. These diagnostics do not replace a repository's genuine
failed request or conflicting-outcome state. Stale, unowned outcome artifacts
also appear in import receipts; owned conflicting outcomes retain their existing
business handling. Filesystem and SQLite failures remain errors, with claimed
files retained until the import transaction commits. A failure after commit is
recoverable from the receipt even if a later directory sync fails after unlink.

Inspect receipts without importing work or initializing the queue:

```bash
python3 scripts/lib/index_marker.py import-status --limit 20
```

The JSON reports actual totals, imported/quarantined counts, the display limit
and truncation. Source names use lossless hexadecimal filesystem bytes; raw
rejected payloads remain in the private queue database and are not printed.
An absent queue is explicit; schema 1 reports that receipt diagnostics are
unavailable. Unsupported schema/journal mode, read and lock failures exit 2.
The reader uses read-only/query-only SQLite against the supported rollback
journal format; it never ignores recovery journals with `immutable=1`.
Converted schema-1 metadata is labeled `legacy-metadata-only`: it proves prior
processing, not historical content validity. No rejected payload is invented.

Receipt identity includes source bytes, raw digest and exact decimal mtime.
Prior identities remain retained so a commit-before-unlink replay cannot import
work twice. Do not clear this history to retry a request. Older queue binaries
refuse schema 2; rolling back code must not restore an old queue snapshot over
new writes. This migration does not enable indexing, add resource quarantine,
or fix the runner's separate list/claim error handling tracked in #2043.

## Verification

The transitional SQLite contention fixture explicitly drives real claim,
fake-provider, durable outcome-spool and CLI reconciliation paths. After the
locker releases the ordinary runner lock, reconciliation must retire pending,
inflight, failure and spool state while showing only one provider call. Actual
automatic ticks separately prove refusal without changing queue bytes. Explicit
CLI replay is not automatic-dispatch acceptance. An empty pending list or a
successful lock-busy no-op does not prove completion. The fixture bounds its
locker readiness wait. These tests use an isolated home and fake provider; they
do not qualify production indexing or explain a separate first-tick timeout.

Permission-denial fixtures first verify the specific denied role with effective
IDs. If privilege defeats that premise, only that case skips with its role in
the reason; ordinary refusal assertions and writable positive controls still
run. The hot-journal positive case performs real SQLite recovery. Copy-source
configuration retains its mode-bit contract independently of effective execute
access, so its positive case also runs when privilege permits execution.

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
