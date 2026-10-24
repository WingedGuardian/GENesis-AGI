# Contained Headlong stock controls

This opt-in CI experiment characterizes pinned stock mechanics and three fake
text-provider routes. It is a separate slice from the canonical proposal baseline
in PR #3590. It does not implement a native executor, ego-policy adaptation or
runtime comparison, and uses no real CLI, model, credentials or production data.

The `Headlong offline controls` workflow runs when its files change in a PR, or
on manual dispatch once available on the default branch. Source is pinned to
`laude-institute/headlong` commit
`eab04ad4097fb2030f27bee6026e24f57289f12d`. The build context contains an archive
of that source and the two fixture scripts; it excludes checkout credentials
and the Genesis tree. The base image is digest-pinned and SDK version is pinned.
Package versions and source hashes identify the actual run; package repository
contents are not frozen across builds.

Run only on a disposable GitHub-hosted runner. The container has no host mounts,
Docker socket or supplied secrets. It uses a read-only root, private writable
tmpfs, loopback-only network, nonroot UID, no capabilities, no-new-privileges and
CPU/memory/process/time bounds. The runner records actual image and container
configuration. In-container preflight refuses an unexpected UID, effective
capabilities, privilege setting, root mount, network, socket or credential-like
environment before invoking upstream code. Failure is an error, never a skip or
an invitation to execute upstream shell code on a production host.

Five reviewed upstream test scripts exercise adapter propagation, monolith
backoff, model-tier selection, dispatcher cleanup and recap caching. Three
filtered dispatcher tests exercise named-start dispatch and the two sides of
claim-versus-launch crash recovery. Every invocation must complete successfully,
report actual passing assertions and report no skips. These are eight upstream
test invocations, not eight total assertions or the full upstream test suite.

The route matrix crosses fixed-response adapter, loopback fake HTTP provider and
CLI text adapter with effect, no-op and provider failure: nine cells. Each uses
the real stock `shellm`, `llm`, `traj` and `context` with fresh scratch state.
The effect control requires exact independently read artifact bytes, successful
completion and an exact final trajectory record linked to the run. No-op requires
successful completion, its own exact final, verified request capture and no
artifact. Failure requires nonzero completion, verified capture and no effect or
final. Missing evidence cannot certify any of those controls. Background summary
calls are captured separately from main generation.

The CLI route uses the actual pinned Claude Agent SDK command builder with a
fixed fake executable. It explicitly disables built-in tools, MCP and settings,
and starts with no resume state. The fake verifies the command and records the
complete message array transported as one serialized prompt, plus the separate
system prompt. Those copies must match the adapter input. This establishes
command construction and fixture text transport only. It does not exercise an
SDK handshake, real Claude Code flag behavior, authentication, model generation,
context limits or multi-turn conversation semantics. Requested token/thinking/
effort settings are recorded; inference-setting parity is unqualified.

Only the outer CI container executes generated Bash. The CLI adapter itself
returns text. A native execution bridge is later work. Stock mechanics here
have no Genesis proposal authority, and the result cannot establish owner-policy,
feedback-learning, recovery or initiative parity across G/H/B/C.

The workload prints one versioned JSON report. CI checks the container exit code,
the exact nine-cell population, all selected upstream invocations and cleanup.
It uploads the report and actual image/container metadata for seven days, then
removes only that run's container. A killed or timed-out run is incomplete and
cannot pass. No integrated success is claimed until the contained job is observed.
