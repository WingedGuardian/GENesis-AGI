# Prepared native background sessions

Machine connectors can use `DirectSessionRunner.adopt` to run an already
persisted `cc_sessions` receipt through the existing native invoker and profiles.
This is an execution primitive, not an enabled network connector. Ordinary
`spawn` callers retain their existing defaults and delivery/parking behavior.

## Provider selection before work

Roster-eligible managed invocations check provider readiness before sending the
task prompt. The home model is checked first; a classified provider refusal
advances through credentialed alternatives in the configured CC roster order.
Unusable backup entries are skipped; a misconfigured home or explicit pin fails
without trying another provider. Other failures stop selection. Explicit
provider pins, including native Claude, receive a single-provider readiness
check. Unpinned routing opt-outs retain direct execution. Foreground-only
`gmodel` catalog entries are not added to
the automated roster.

Ordinary checks use a fixed diagnostic prompt, safe mode with OAuth support,
`--tools ""`, and an empty strict MCP configuration. A complete exact response
and an observed empty tools/MCP inventory are required. The check carries no
task content or resume state. Each check receives an invocation timeout of up to
90 seconds within the receipt's existing absolute deadline and runner slot;
there is no separate task budget.
These extra inference calls consume provider quota and may incur provider fees.
Checks disable CLI session persistence, so deleting their scratch directory
does not leave separate probe transcripts in the CLI's project tree. Actual
task transcript persistence is unchanged. Informational limit events and usable
overage do not reject readiness; a final effective refusal does, even alongside
the exact readiness answer. Later allowed state can supersede an earlier refusal.

Once selected, one provider executes the real task through the existing native
profile and approval gates. A successful readiness check cannot guarantee that
the subsequent task will finish: a later limit or failure ends the task without
switching providers, parking, or replaying effects. Cleanup uncertainty retains
capacity and the probe directory. The idle recovery worker also suspends further
probes after uncertain cleanup until runtime recreation. Confirmed readiness
for the current home provider clears its shared unavailable status, even when
no fallback ever succeeded. A matching fallback banner is then retired.
Confirmed refusals from the still-current home update shared rate/quota health;
standby and pinned-other outcomes cannot overwrite it. Restart reconciliation
remains unchanged.

Receipt metadata records `provider_attempts`, `home_provider`,
`execution_provider` and the selected endpoint context without credential
values. The endpoint is pinned from the same roster snapshot as execution.
The neural monitor's **Fallback** entry records actual execution outcomes, not
probe success; a failed or cancelled selection does not replace the last actual
execution observation. Private prompts, results and provider error prose are
omitted. Home refusals remain in the receipt rather than the standby-peer
availability store. The first successful managed fallback in an outage emits
the existing owner switch alert once; later successes do not repeat it.
The existing idle recovery worker uses the same enforced readiness check while
retaining its 30-minute cadence and 300-second per-check bound.

Construct a server-owned `DirectSessionRequest(managed=True, notify=False,
source_tag="agent_api", caller_context="agent_api:<receipt>")`. Only `observe`
and `interact` profiles are supported in this mode; tool exceptions and origin
delivery are refused. `timeout_s` must be an integer from 1 through 7200 seconds.
The normal background autonomy circuit breaker still applies. Effective
background authority must permit observation (L1) or interaction (L3); missing
authority fails closed. The child has external provenance and is unattended.

Use `managed_session.prepared_metadata(request)` and the canonical
`cc_sessions.create(..., commit=False)` inside the connector's private atomic
admission transaction. The row must have `session_type="background_task"`,
`channel="agent"`, and `origin_class="external_untrusted"`. Persist identity,
idempotency and caller attribution in that same transaction. The connector
must prove that its private admission connection and the runtime use the same
database. Commit before calling `adopt`; never enqueue that receipt for replay.

`adopt` verifies the receipt fingerprint and registers it before awaited start
hooks. It refuses foreign, changed, terminal and already-adopted rows. A valid
receipt whose task cannot be constructed becomes failed with `not_started`.
Exact client retries are the connector's responsibility: return the saved
receipt without calling `adopt` again. Ordinary receipt deletion refuses
`source_tag="agent_api"`, retaining the retry anchor.

The supervisor owns the entire wait for start hooks, a runner slot and execution.
The deadline starts when receipt metadata is prepared. `cancel(id)` signals the
supervisor and is safe to repeat. Cancellation before its first step starts no
hooks or subprocess. Subprocess cancellation retains registration until the
leader has exited and its process group is confirmed absent. An OS refusal is
unknown, not confirmation. A stuck drain or failed receipt persistence holds
capacity; it must not be mistaken for a completed cancellation. Runtime shutdown
signals managed supervisors within its existing bounded shutdown grace.

Creation errors without a process handle are also unconfirmed: pipe setup can
fail after the OS child starts. They set `cleanup_unconfirmed=true` and retain
the active receipt, including through repeated cancellation. This mode does
not infer safe pre-exec failure from an exception class. An owner-authorized
service restart ends the supervisor and boot reconciliation records
`outcome_unknown`; it does not confirm cleanup of separately scoped children.
Actual child teardown requires scope/runtime or operator evidence. There is no
automatic replay or recovery endpoint.

Server-owned `contained_peer=True` additionally requires managed observe mode,
an explicit public system prompt and no skills/planning additions. It bypasses
the owner identity/context/MCP builders and uses the tools-free contained CLI
policy for readiness, auth/version helpers and actual work. The whole managed
lifetime, including finalization, cannot update shared provider health, banners
or Fallback observations. Default-false requests keep their existing receipt
fingerprints. Contained work has no transcript pointer; its committed full
artifact remains available. This execution primitive does not authorize foreign
peers. See [contained peer execution](peer-execution.md) for the exact contract.

Private invocations withhold raw execution/callback diagnostics and error span
text, including exceptions escaping to ancestor spans and the enumerated tracing
loggers. Child tool tracing is disabled for this mode because the ordinary trace
hook records argument and path previews. Concurrent ordinary sessions retain
their diagnostics. Owner notification and approval paths are not disabled.

Results preserve the existing `output_text` head (20,000 characters), tool
counts, transcript reference and error fields in metadata. Full text is
atomically published as UTF-8 with private file permissions at the existing
`~/.genesis/output/bg-session-<id>.md` convention independently of Telegram.
`native_state` distinguishes `pending`, `running`, `cancelling`, `succeeded`,
`failed` and `cancelled`. Consumers must check both terminal row status and
structured state; a legacy `completed` row alone is not a success receipt.
`result_committed` and `result_path` distinguish available full output from
publication failure. Missing CLI result or truncated background work cannot be
reported as managed success; `result_partial` and `stream_lines_dropped` report
partial output or incomplete telemetry. Provider errors are classified without
persisting their diagnostic text. Managed work never enters automatic parking.

At native runner initialization, remaining active agent receipts from the old
runtime become failed with `outcome_unknown`, before the new runner is exposed.
They are never replayed. A live registered receipt is excluded from the stale
background reaper. Retained receipts survive normal restart, not deliberate
database deletion or rollback. Existing output-file retention is independent:
a connector must verify the artifact exists before promising a full result.
