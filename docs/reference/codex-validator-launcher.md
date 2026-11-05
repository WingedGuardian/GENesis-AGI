# Interactive Codex validator launcher

`scripts/codex-validator` prepares and starts an owner-supervised validator with
a dedicated `CODEX_HOME`. It does not install a service, schedule a session,
deploy Genesis, or copy credentials from another Codex home. Run it from the
deployed main checkout after the launcher and its dependencies have merged,
deployed and passed native qualification on its recorded reference binary.

The native qualification reference is Codex CLI 0.166.0. Other recognizable
Codex versions warn and continue to the same startup checks; version inequality
alone does not refuse admission. Missing, failed or unrecognizable clients
still refuse. Native results apply to their recorded version and binary hash.
Unknown versions may change tool dispatch despite passing startup inspection.

## Prepare, log in, check and run

Choose the model explicitly when creating a new private profile:

```sh
scripts/codex-validator prepare --model <owner-selected-model>
scripts/codex-validator login
scripts/codex-validator check --qualified-commit <full-qualified-commit>
scripts/codex-validator run --qualified-commit <full-qualified-commit>
```

`--home /absolute/dedicated/path` selects a different canonical private home;
the default is `~/.codex-validator`. Preparation refuses an existing home rather
than overwrite partial or changed policy. Login is a separate, explicit native
Codex login under that home. No authentication is performed by preparation or
check. Run requires an interactive terminal and starts Codex in the foreground.
There is no passthrough for arbitrary client configuration or server overrides.
Inspection and run pass the fixed `--disable apps` option so account-connected
Apps cannot add a third server to the two-server validator profile. Admission
also checks that Apps are disabled in the effective configuration. This is a
launcher override; existing prepared profiles and device logins remain valid.
Login omits `--strict-config`, which the native authentication command rejects;
inspection and interactive execution retain it. Login still validates the
private profile and, in branch-test mode, the qualified sources before execution.
On a remote or headless machine, use `scripts/codex-validator login --device-auth`.
Complete the displayed device-code flow in your local browser. This option is
valid only for login; the default browser login remains available without it.

## Owner-run branch qualification

`--owner-branch-test` is an explicit exception for the one supervised branch
check before publication. It requires a genuine linked worktree, the exact
`--qualified-commit`, and already merged/deployed dependencies in its owning
main checkout. No deployment or source synchronization is performed. Without
the flag, linked-worktree launches still refuse; the flag itself refuses in main.

The branch entry executes, but policy, child guards, MCP servers and Codex cwd
all target main. Both hook trees and the closed import inventory are physically
checked in both trees against the qualified commit. Only the two new launcher
files are omitted from the main comparison; they are still checked in the
branch. Both tracked trees must be clean. Inherited Git overrides and local
hook-development selection do not reach the child environment.

Pass the flag and qualified commit to every branch operation, including
`prepare` and `login`. Those operations also require matching dependencies and
absence of a detected deployment before creating profile state or launching a
child. Source checks repeat around inspection and before foreground execution.
Use the direct Codex binary selected for qualification on PATH. Version drift
warns; this does not transfer earlier qualification evidence to the new binary.
The branch wrapper needs its own `.venv` entry pointing to the existing main
venv, never an editable installation targeting the branch.

The owner runs the actual branch entry once and types one prompt. A response
to that prompt confirms a model turn; a queued prompt alone does not. Automated
terminal trials are not part of qualification. This smoke check does not prove
whole-role calibration or authorize a verdict close.

Preparation generates one user-level PreToolUse adapter and exactly the
`genesis_validator_health` and `genesis_validator_memory` MCP servers, each using
the server-side `validator` profile, currently nine health and nine memory tools.
It reads native hook discovery, records the exact definition hash, then verifies
trust again in a new inspection process.
The project layer stays untrusted. Plugins, remote plugins and agent delegation
are disabled in the generated policy. The client uses full disk access and
approval policy `never`; this profile supplies bounded guards, not OS isolation.

## Admission and qualification

Check and run require the exact full Git commit named by the qualification
receipt. Physical bytes, file types and Git owner-execute modes are compared to
that commit for both hook trees and the closed launcher/import source inventory
in `EXTRA_SOURCES`. Index hints cannot hide a physical byte change. Extra hook
inputs are refused except Python bytecode-cache contents. This is a client and
guard source check, not a fingerprint of the entire interpreter or installation.

Tracked main files must be clean; existing untracked runtime directories are
permitted. Linked worktrees cannot launch the production validator. Active
deployment names, including the full updater's temporary executable, prevent
admission. Native discovery must confirm the exact effective model, permissions,
trusted adapter and two server definitions, with the expected tools available.
Profile and source checks repeat before interactive execution. Inspection child
groups must be confirmed retired; cleanup uncertainty refuses startup.

Startup inspects registration, trust, policy and discovery. It does not exercise
actual tool denial. The isolated native suite checks real shell, patch and MCP
allow/deny behavior at release qualification, including no-op controls. That is
bounded evidence for its recorded binary and source, not certification of later
releases. A startup contract mismatch still refuses, regardless of version.

MCP inspection starts the configured servers and may update existing permitted
Genesis metadata. It is not a side-effect-free health probe. It does not submit a
model turn or discharge a verification obligation.

An owner interruption must remain an interruption after owned cleanup completes.
Catchable termination and hangup during inspection cancel the inspection task
and drain its owned group before refusal; prior signal handlers are restored.
Uncatchable SIGKILL cannot run that cleanup.
No startup check locks deployment for the entire session; a deployment starting
after admission and mutable files changing after their final check remain
explicit limits. Follow the verification skill's deployment-bracketing rules.

## Session boundary

The opening prompt loads the canonical validating-merges skill and its Codex
instructions, uses its risk-tier sampling, records progress under
`~/.genesis/output/validator-runs/`, and stops on a denial. Validators report
findings; they do not repair, push, deploy or restart services. Verdict closes
remain subject to the server's calibration gate.

This conversation is an external client, not a Genesis lifecycle session. Never
register its transcript or identifiers, create a charter, or write its session
ledger. Shell guards cannot contain arbitrary programs under full access; see
[the action boundary](codex-validator-boundary.md) for the accepted limitations.
