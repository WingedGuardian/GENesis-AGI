# Managed Codebase configuration staging

`scripts/codebase_managed.py` provides **preparation only**: `configure` stages
a pinned provider and publishes immutable settings; `status` diagnoses settings
and native service state. This step does not change the existing launcher or
index queue. Native units, activation and managed execution are subsequent
integration changes; staging alone does not make Codebase available.

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
Resolved paths must satisfy the same validation as the published settings:
newline and carriage-return characters introduced by symlink ancestors are
refused before staging. The final settings filename remains unresolved.

No service is enabled, no unit is rendered and no sentinel is removed.
Configuration uses schema 2: paths and build identity, with no mutable `enabled`
flag. Native systemd enablement will own operational state when lifecycle
integration lands. The lifecycle lock coordinates cooperating same-user tools;
it is not protection against deliberate same-user filesystem interference.

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
