# Codex validator action boundary

## Interactive guard adapter (inactive)

`scripts/hooks/codex-validator-adapter` is a separate adapter for the planned
interactive validator profile. It does not replace the sealed request boundary
below, change ordinary Codex configuration, start a session, or admit new MCP
writers. Role-specific publishing, service and store policy and launcher trust
preflight are separate dependencies; this adapter alone is not an activated
validator.

Every Bash command must begin with this exact foreground opener and a literal
newline before its command body:

```bash
cd /absolute/existing/directory || exit
command
```

The opener permits no exit arguments, comments, redirections or continuations.
The former `cd /directory && command` format is refused: a later background
operator can run that entire AND-list in a subshell, leaving following commands
in the original directory. The newline completes the directory change before
the body runs; a failed directory change exits before the body. Later directory
changes still need the existing guards' handling and the planned role policy.
Single or double quoting of a literal directory is supported, without expansion
or a `--` option. Parent (`..`) components are refused because Bash's logical
parent resolution can differ from filesystem resolution through a symlink;
use the canonical directory instead. Ordinary symlink anchors remain supported.
The command is unchanged. Native 0.163.0 omitted an explicit
shell workdir from its hook input, so the adapter supplies the proven anchor as
the guards' cwd instead. Reads require this prefix too.
For the root directory, use `cd "/" || exit` followed by the newline and body:
the unquoted `cd /` spelling currently refuses (#3581).

The prefix makes the call a compound command. The unchanged parser deliberately
does not exempt data-heredoc bodies in compound openers, so inline prose can be
refused when it mentions a guarded operation. Keep message/data bodies in a file
created with `apply_patch` under a permitted scratch root, then use its absolute
filename in the anchored read or authorized filer call. The scanned filer still
checks public content. This workflow avoids the inline limitation; it does not
restore arbitrary heredoc parity or authorize raw publishing commands. Guards
still receive the complete unchanged command, including the anchor.

With asks enabled, the foreground opener also removes the shared push guard's
re-push auto-allowance: its `exit` neighbour is outside that guard's inert-step
allowlist, so it asks and the adapter refuses. The guard can still update its
local pushed-branch cache before asking; denial does not imply that no advisory
or cache state was written. Six original publishing-allowance tests retain
direct evidence with zero adapter credit; a separate independent-state proof
checks the real ask/refusal and cache effects. Existing ask suppression remains
authoritative. The later role policy owns unconditional validator push denial.

GraphQL file inputs have a separate accepted limitation: the push guard trusts
`@query` and `--input` bodies only in a single-step command. The mandatory anchor
therefore makes even a read-only GraphQL file request refuse, including with an
absolute filename. Ordinary `gh` read commands and REST GET remain the planned
read paths where available. An inline `-f query=...` is not a validator-role
workaround: the planned role policy prohibits those field flags. This restriction
does not affect message body files passed to the authorized scanned filer.

Patches use their native cwd for relative paths; they are never rebased into a
different scratch directory. Every source and move destination must pass the
existing patch policy under `~/tmp/validator/`, `~/.genesis/output/prv-*/` or
`~/.genesis/output/validator-runs/`. Symlinked scratch roots are refused as well
as the existing symlink, hardlink, special-file, traversal and trusted-config
checks. One refused target rejects the whole patch. Synthetic file events run
only the main-checkout file guard and secrets-access guard: there are no Bash
snapshots to leave unpaired.

The unchanged Bash guards run through `genesis-hook`, plus the unchanged inline
safety command from Claude settings. The evaluator docstring lists exclusions.
MCP calls under the two validator prefixes reach the MCP-aware secrets guard;
tool authorization remains the servers' responsibility. `CLAUDE_PROJECT_DIR`
comes from the adapter's location, and `GENESIS_CC_SESSION` is removed before
children start. Native session/call IDs remain available for advisories.

An ask actually emitted by a guard becomes a denial telling the model to raise
it with the owner. Existing install ask-suppression settings remain authoritative:
a suppressed ask keeps the existing guard's advisory/no-decision behavior.
The adapter does not force those asks on or alter other clients' policy. Known
advisories, successful decision reasons and successful stderr notes become
`additionalContext`. Observable child failures, malformed/unknown output,
missing launched programs and child deadlines deny. Existing guards' internal
fail-open paths remain unchanged. The unchanged inline safety command requires
the installer's mandatory `jq` dependency; a missing transitive dependency whose
error that command ignores is not observable as a failed child.
The shell wrapper requires an explicit private allow token and forwards only
the advisory JSON. It signals allow or explained denial on fd 3, with exit 0
or 2 respectively. `hook_command(runtime)` supplies the shared outer definition:
it requires those exact status/signal pairs and catches missing, broken or
silently exiting launchers. fd 4 preserves advisory stdout while the outer
definition collects the decision. The later profile must use this definition;
this PR does not install it.

The push guard has a 5-second child budget, others 8 seconds; the whole evaluator
has 10 seconds. At most 15 children run concurrently, including multi-file
patches. Cleanup kills and drains owned process groups before returning.
Descendants escaping an owned group, including one retaining a pipe, are outside
that cleanup guarantee and can cause the outer deadline to be reached. The
hook definition needs a 15-second timeout, with cold-path qualification before
activation. An outer hook that cannot start, is killed or times out can still
fail open in Codex. This is bounded accident prevention, not OS containment.

Twenty fresh-process plain-shell hook probes on the qualified source measured
1.61 seconds for the first call, p95 1.74 seconds and maximum 1.81 seconds.
These are normal-call measurements, not a worst-case guarantee; later policy
or profile changes require qualification again.
Multi-file patches share the same overall budget and may need smaller batches.
A single copied-source probe admitted 50 targets in 9.85 seconds; that is a
near-budget observation, not a guaranteed supported patch size.

## Sealed request foundation

`scripts/hooks/codex-validator-guard` is the native PreToolUse wrapper for a
separately configured external validator workspace. This foundation does not
install a hook or activate a validator session.

The launch definition supplies absolute runtime and workspace roots. Payload
cwd, role, session and transcript fields supply no authority. The dispatcher
uses the shared validator MCP profile and exact server prefixes. Shell admits
exactly one command: the sealed request invocation
(`<runtime>/.venv/bin/python -I <runtime>/scripts/codex_validator_request.py
--workspace-root <workspace> --request <workspace>/requests/<uuid>.json`),
matched as exact argv and exact canonical quoting, with the request file
private, owned and unshared (`codex_validator_shell.py`); every other shell
command is refused. Requests use strict JSON and closed per-operation schemas:
`protocol_probe`, `serving_status` and `serving_verify`. Serving requests perform
one invocation of the existing deployment status tripwire and project only its
validated fields. Unknown, changed, partial or oversized output does not establish
serving identity. The producer prints and hashes the same captured inputs; this
is not an atomic snapshot of systemd, Git and filesystem state.

The standalone Unix/main-thread reader scopes SIGTERM to task cancellation,
allowing owned process-group cleanup during creation or collection. It restores
the prior signal handler after loop shutdown. Cancellation at the request CLI
returns exit 2 with a static refusal and no JSON result or child diagnostics.
Output retention is bounded to 64 KiB per stream, with a 20-second collection
timeout after process creation and a bounded post-kill reap. SIGKILL, OS kill
refusals and descendants escaping the owned group are outside this guarantee.
These operations provide observations, not functional verification verdicts.
Patches admit
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

This is bounded accident prevention under full access. It does not isolate
arbitrary programs, later stdin, filesystem races, disabled hooks or failures of the outer hook
process. Activation requires the later workspace and readiness checks, including
vetted script bytes, a launch-owned interpreter with the required dependencies,
timeout margin above the measured cold import, and generated tool lists derived
from the shared profile.

The interpreter must also run with an environment the launch controls: no
inherited `PYTHONPATH`, `PYTHONHOME`, or user-site and `sitecustomize` code,
since any of them can run before the guard and print its result. Names that
must match the activated configuration exactly:

- MCP server keys `genesis_validator_health` and `genesis_validator_memory`
  (tool names `mcp__<server>__<tool>`, `MCP_SERVER_PREFIXES` in
  `codex_validator_guard.py`). The in-tree Codex configuration names its servers
  `genesis-health` and `genesis-memory`, which this guard denies.
- Profile servers `health` and `memory` and `ExternalProfile.VALIDATOR` in
  `src/genesis/mcp/external_profiles.py`.
- Codex tool names `Bash` and `apply_patch`, each carrying `tool_input.command`.

Focused checks: `pytest tests/test_hooks/test_codex_validator_guard.py tests/test_hooks/test_codex_validator_patch.py tests/test_hooks/test_codex_validator_shell.py`. Native
CLI/app checks in the same file require `GENESIS_CODEX_NATIVE_TESTS=1` and use
the isolated fake Responses provider and fake MCP from the native harness.
