# Contained peer execution

This is an opt-in server policy on the existing native managed runner. Ordinary
same-owner conversations keep their current profiles and provider observations.
It does not authorize a peer or enable cross-owner access by itself. The
cross-owner conversation consumer is tracked separately in #3570.

`DirectSessionRequest.contained_peer=True` requires a prepared managed agent
receipt, observe profile, explicit public system prompt, no skills or planning
instructions, and the existing native background authority checks. This field
is server-owned, absent from peer options. Construction bypasses the private
identity/profile/context/MCP builders. The tools-free conversation variant has
no MCP configuration, built-in tools, transcript persistence or owner callbacks.

The entire managed operation is private and provider-status isolated: readiness,
actual execution, cleanup and finalization. Per-receipt attempts and usage remain
attributable. Contained work cannot change shared home/standby health, recovery
banners, downgrade/cap callbacks or the Fallback monitor's last observation.
Default-false requests preserve their existing fingerprints and behavior.

The installed CLI policy uses empty setting sources, strict MCP configuration,
dontAsk permissions, disabled slash commands/Chrome, no session persistence and
instruction/automatic-memory/attachment suppression. Provider authentication and
proxy/CA connectivity are retained; owner API/session credentials and shell
startup overrides are excluded. GitHub credential variables remain present and
empty under the existing unconditional launch check. Safe PATH/executable
resolution excludes the peer working directory and aliases into it. This is a
closed tools-free/facade execution contract, not a general shell sandbox.

Every contained process uses an owned named systemd user scope under the
original receipt deadline, bounded by7200seconds and tied to the existing server
unit. Auth and version diagnostics use separate owned scopes with that same
deadline, fixed argv and no actual-task spawn callback. Native login expiry,
confirmed logged-out and setup-token age gates remain; contained probes neither
reuse nor update the shared login-probe cache.

Cancellation and deadline cleanup must confirm process-group and recursive scope
drain before freeing ownership. A successful drain retires its scope claim even
when the caller was cancelled repeatedly. Unconfirmed cleanup retains the claim
and native capacity until existing restart reconciliation; a fresh registry is
not proof a descendant exited. No task is replayed. Artifact failures terminate
with result_unavailable after receipt persistence; database finalization failures
retry while retaining capacity. Contained metadata has no transcript pointer.
Full results still require the canonical committed artifact and matching digest.

## Reserved exact facade variant

The retained facade policy requires nonempty exact `mcp__genesis_peer__...` tool
names and one private regular0600configuration owned by the current OS user.
It rejects symlinks, oversized files, extra servers, commands, env or lease
overrides. The pinned stdio shape is:

```json
{"mcpServers":{"genesis_peer":{"command":"<sys.executable>","args":["-P","-m","genesis.peers.facade","--lease-file","<absolute lease path>"]}}}
```

This base does not implement that facade module or grant leased tools. Its
standalone vendor fixture proves allowed-tool/CLI behavior, not a production
facade path. The tools-free consumer has a separate explicit mode; empty facade
rules never become a permissive fallback. Run full production-facade acceptance
when a real facade consumer is introduced.

Qualification uses hermetic homes, synthetic credentials/local providers and
uniquely owned harmless scopes. This does not certify live account credentials,
remote reachability or a service deployment. See the PR's exact-head evidence and
post-merge E2E obligation before activation.
