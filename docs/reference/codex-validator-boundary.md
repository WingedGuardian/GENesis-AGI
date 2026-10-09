# Codex validator action boundary

`scripts/hooks/codex-validator-guard` is the native PreToolUse wrapper for a
separately configured external validator workspace. The manual terminal launcher
prepares a dedicated profile; ordinary project configuration does not activate it.

The launch definition supplies absolute runtime and workspace roots. Payload
cwd, role, session and transcript fields supply no authority. The dispatcher
uses the shared validator MCP profile and exact server prefixes. Shell actions
admit only the canonical seven-argument invocation of
`scripts/codex_validator_request.py` with a bounded private UUID request; all
other commands are refused. The closed operations are `protocol_probe`,
`serving_status`, `serving_verify`, `pilot_packet`, `pilot_probe`, and
`pilot_preview`. Serving observations project the existing
deploy status tripwire; unknown, changed, partial or oversized output does not
establish serving identity. The bracket retains the existing deploy tripwire
limitations. Patches admit
every potential source and move destination inside the trusted workspace;
existing symlinks, special files, hardlinked leaves, parent traversal, and
instruction/configuration paths (`AGENTS.md`, `.codex`, `.agents`, `.git`) are
refused. The conservative native 0.161.0 subset supports LF/CRLF and ordinary
Unicode filenames, while refusing heredoc envelopes, URI/backslash filenames,
environment selectors and multiple envelopes. Header-looking context can
cause an extra path check or conservative refusal. Native chunk validation
still follows admission; the guard never executes a patch itself.
Malformed inputs and unsuccessful evaluator results produce explicit denials;
native allows have empty stdout.

The pilot packet comes from a launch-owned, private `.codex/pilot.json`, not
caller-supplied database paths or commands. Its fixed recipes bind source hashes
and exact pytest case counts. Probes serialize on the private operation lock,
hold the existing deployment lock shared during the finite batch, and verify
serving identity before and after it. The resource governor uses the real
installation policy; test children use a private home while retaining the
installation's pytest lock. A failed attempted batch retires its prior receipt.
Completed receipts are atomically published in the private workspace and state
the limits of the disposable fixtures. They do not prove live provider or queue
behavior. `pilot_preview` requires that receipt digest, the exact enrolled
repository/PR/merge identity, the strict existing evidence document, a nullable
bounded note, and a boolean `park`. It rechecks configuration, case census,
source bytes and serving brackets before invoking the existing CLI with literal
`--dry-run`; it verifies the bracket again before returning bounded CLI text.
It adds the fixed fixture coverage gap and never independently chooses a verdict.
The request limit is 64 KiB, smaller than the direct CLI's 256 KiB evidence limit.

The verification CLI's `--dry-run` uses a quarantined-path admission check and a
SQLite `mode=ro` connection, including committed WAL data. Ordinary recording
retains its existing writable connection. Read-only access can still use SQLite
locks or sidecars; it is not a promise of zero filesystem activity.

This is bounded accident prevention under full access. It does not isolate
arbitrary programs, later stdin, filesystem races, disabled hooks or failures of the outer hook
process. Native trust fingerprints the hook definition, not the referenced
script bytes; enrolled source hashes independently pin the guard and its policy
helpers, launcher, preview and canonical doctrine. The shared MCP profile still
exists for other callers, but the initial terminal MVP starts **zero Genesis MCP
servers**. It offers no health/memory, public issue, recording or lifecycle tools.

## Manual terminal MVP

After merge/deploy and separate approval to activate a supervised pilot, the
operator creates a canonical, disjoint workspace owned by their UID with mode
0700. Its `requests`, `.codex`, and `.codex/receipts` directories must also be
0700; `.codex/operation.lock` and `.codex/pilot.json` must be private regular
0600 files. The launch-owned JSON config has exactly `version: 1`,
`runtime_commit` (40 lowercase hex), an absolute `ledger`, and one to three
`rows`. Each row has `pr`, `repo`, `merge_commit`, a known `recipe`, `intent`,
and the exact `source_paths(recipe)` SHA256 map. Enrollment is an operator
task; a model cannot choose the store, source set or executable. Recipes are
`peer_availability`, `exhaustion` and `queue_snapshot`.

```bash
python3 scripts/codex_validator_terminal.py prepare --workspace-root /absolute/private/workspace
# Operator authenticates through normal Codex login in the new isolated home.
HOME=/absolute/private/workspace/.codex/client \
  CODEX_HOME=/absolute/private/workspace/.codex/client codex login
python3 scripts/codex_validator_terminal.py run --workspace-root /absolute/private/workspace
```

Preparation refuses an existing client home rather than overwriting login or
trust state. It copies the canonical validator skill into `.agents/skills/`,
writes a private Codex profile with explicit trust for this workspace, obtains native hook identity/trust and verifies
it in a fresh app-server process. No model inference is requested by preflight;
upstream authentication/metadata network behavior is not an isolation guarantee.
The launcher never copies credentials from the ordinary Codex home or Genesis.
An interrupted preparation may leave private files; use a new operator-prepared
workspace rather than treating those files as proof of success.

Every run checks actual effective policy and hook trust, exact profile bytes,
copied doctrine and enrolled source hashes. Codex 0.162.0 is the qualified
version; version drift requires requalification. Plugins, native memory, web,
JS REPL, apps, browser/computer control and delegation are disabled. Both
`agents.enabled=false` and `features.multi_agent_v2.enabled=false` are needed
alongside the legacy multi-agent flag. Programmatic orchestration can still
invoke admitted underlying tools; its shell calls undergo the same boundary.
An exclusive private run lock prevents concurrent sessions without holding the
deployment lock during model or human waits. The finite terminal `codex exec`
run uses a private HOME/CODEX_HOME, a scrubbed environment, umask 0077,
persisted guard trust, fixed instructions and a two-hour timeout. This is
terminal command qualification, not interactive TUI/app qualification.

On Linux, each fixed probe governor arms a parent-death SIGTERM in its own
process before calling the existing resource CLI. A post-arm parent check
refuses a request that died before arming. Native hard termination of a request
therefore reaches the governor's existing job-stop path without relying on
request Python cleanup. Unsupported platforms or failed binding refuse the
probe. This does not guarantee cleanup after the governor itself is hard-killed,
privilege changes, escaped descendants, or deliberately ignored termination.

The supervisor checks every measurement and judgment, and handles any eventual
real recording through the established workflow. A preview is never permission
to record, publish, merge or deploy. Ordinary external-client MCP capability and
real provider authentication remain separate from this fixture-qualified MVP.

Focused checks: `pytest tests/test_hooks/test_codex_validator_guard.py tests/test_hooks/test_codex_validator_patch.py`. Native
CLI/app checks in the same file require `GENESIS_CODEX_NATIVE_TESTS=1` and use
the isolated fake Responses provider and fake MCP from the native harness.
