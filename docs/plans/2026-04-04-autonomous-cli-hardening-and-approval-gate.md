# Autonomous CLI Hardening And Manual Approval Gate

> **Status:** Partially implemented in this pass.
> **Date:** 2026-04-04
> **Scope in this pass:** Autonomous/background CC callsites only, plus dashboard and routing-monitor support.
> **Explicitly out of scope:** Guardian host-side diagnosis and recovery.

---

## Summary

Autonomous/background Genesis execution should be API-first by default.
`claude -p` remains available only as an explicit fallback path, never as a
silent default for background automation.

When an autonomous/background flow would fall back to `claude -p`, Genesis must
pause and require explicit human approval before dispatch. Telegram is the
first delivery channel, but the approval contract must be channel-agnostic so
other channels and the dashboard can resolve the same request later.

This pass excludes Guardian because its host-side diagnosis path carries higher
operational risk and should be handled separately.

Current implementation status in this repo (as of 2026-04-04, later in this pass):

- reflection, inbox, and executor routing changes are in place
- autonomous CLI fallback approval gating is in place
- dashboard approval resolution backend is in place
- dashboard approval queue UI and neural-monitor metadata are in place
- global approval-gate controls are now config-backed and dashboard-visible
- effective policy export to the shared mount is wired (for Guardian to read later)
- Guardian behavior changes remain deferred

Operator direction after initial implementation:

- add a dashboard-visible global toggle for autonomous `claude -p` approval policy
- make Genesis config the source of truth for that toggle
- export the effective policy to the shared mount so Guardian can honor it
- keep Guardian on `claude -p` for now rather than introducing an API diagnosis route
- require Guardian to ask once before diagnosis starts and again before recovery action

Update: the first three items above are now implemented in this repo. The Guardian
double-approval flow remains outstanding.

---

## Why This Change Now

Anthropic's April 3-4, 2026 shift around third-party harness patterns makes
subscription-backed autonomous CLI orchestration commercially and policy-fragile.

The current runtime still uses direct `claude -p` dispatch for several
autonomous/background flows. That means:

- autonomous work can silently default to CLI orchestration
- API routing is underused even where call sites already exist
- operators do not get an explicit gate before autonomous CLI use
- fallback behavior is not consistently observable

The runtime should treat autonomous CLI use as higher risk than API use.

---

## Due Diligence Findings

Audited the actual runtime before implementation. Key findings:

### Current autonomous/background CLI callsites

- [src/genesis/cc/reflection_bridge/_bridge.py](${HOME}/genesis/src/genesis/cc/reflection_bridge/_bridge.py)
  - `CCReflectionBridge.reflect()` still calls `_invoker.run(...)`
  - Light, Deep, and Strategic reflections are all direct CLI paths today
- [src/genesis/ego/session.py](${HOME}/genesis/src/genesis/ego/session.py)
  - `EgoSession.run_cycle()` invokes `_invoker.run(...)`
- [src/genesis/autonomy/executor/engine.py](${HOME}/genesis/src/genesis/autonomy/executor/engine.py)
  - `_dispatch_step()` invokes `_invoker.run(...)`
- [src/genesis/inbox/monitor.py](${HOME}/genesis/src/genesis/inbox/monitor.py)
  - inbox evaluation dispatches directly to `_invoker.run(...)`

### Foreground conversation is a separate path

- [src/genesis/cc/conversation.py](${HOME}/genesis/src/genesis/cc/conversation.py)
  - foreground chat still goes through `CCInvoker`
  - contingency behavior exists there already
  - this work should not change foreground conversation behavior

### API routing already exists and should be reused

- [config/model_routing.yaml](${HOME}/genesis/config/model_routing.yaml)
  already contains API callsites for:
  - `4_light_reflection`
  - `5_deep_reflection`
  - `6_strategic_reflection`
  - several contingency and research-style routes
- [src/genesis/routing/router.py](${HOME}/genesis/src/genesis/routing/router.py)
  already provides fallback-chain selection, observability, and `record_last_run`

### Existing contingency dispatcher is not the right abstraction

- [src/genesis/cc/contingency.py](${HOME}/genesis/src/genesis/cc/contingency.py)
  is for "CC unavailable" contingency, not for default autonomous routing
- it currently defers light/deep/strategic reflection instead of using the API
- it should remain a distinct concern from the new autonomous routing policy

### Approval storage exists, but not full dispatch gating

- [src/genesis/autonomy/approval.py](${HOME}/genesis/src/genesis/autonomy/approval.py)
  provides approval request creation and resolution
- [src/genesis/db/crud/approval_requests.py](${HOME}/genesis/src/genesis/db/crud/approval_requests.py)
  persists requests in `approval_requests`
- [src/genesis/dashboard/routes/state.py](${HOME}/genesis/src/genesis/dashboard/routes/state.py)
  already lists pending approvals

What is missing:

- no generic approval delivery flow for runtime dispatch gating
- no generic dashboard resolve endpoint for approvals
- no reusable "wait for approval before dispatch" service

### Telegram reply plumbing already exists and is reusable

- [src/genesis/outreach/reply_waiter.py](${HOME}/genesis/src/genesis/outreach/reply_waiter.py)
- [src/genesis/ego/proposals.py](${HOME}/genesis/src/genesis/ego/proposals.py)
- [src/genesis/channels/base.py](${HOME}/genesis/src/genesis/channels/base.py)
- [src/genesis/channels/telegram/adapter_v2.py](${HOME}/genesis/src/genesis/channels/telegram/adapter_v2.py)
- [src/genesis/channels/telegram/handlers_v2.py](${HOME}/genesis/src/genesis/channels/telegram/handlers_v2.py)

These provide enough infrastructure for a Telegram-first implementation without
hard-coding approval semantics to Telegram long-term.

### One correctness issue must be fixed first

- [src/genesis/db/crud/approval_requests.py](${HOME}/genesis/src/genesis/db/crud/approval_requests.py)
  resolves by request ID without constraining the current status to `pending`

That is too loose for a dispatch gate. Duplicate approval or rejection events
could race or overwrite each other. This should be tightened before the new
approval-gated fallback goes live.

---

## Scope

### In scope

- autonomous/background API-first routing for:
  - reflection bridge
  - ego session
  - inbox monitor
  - autonomy executor step dispatch
- explicit `claude -p` fallback policy for those callsites
- global toggle requiring approval before autonomous CLI fallback dispatch
- Telegram approval request delivery
- dashboard approval visibility and resolution
- routing and approval observability
- tests and operator docs/config

### Out of scope

- [src/genesis/guardian/diagnosis.py](${HOME}/genesis/src/genesis/guardian/diagnosis.py)
- any Guardian recovery logic
- foreground conversation routing changes

---

## Behavior Target

- Autonomous/background flows route through API providers by default.
- `claude -p` is never the silent default for autonomous/background execution.
- `claude -p` remains available only as explicit fallback policy.
- If autonomous/background work would fall back to `claude -p`, Genesis must
  obtain explicit operator approval before dispatch.
- If the API chain fails and CLI fallback is disabled, rejected, or still
  pending approval, the subsystem must fail cleanly or defer cleanly with logs.

---

## Neural Monitor Follow-Up

The routing editor is now the source of truth for autonomous API callsites, but
the monitor still needs a clearer distinction between configured state and live
runtime state.

### UX improvements to add

- add an `Automation Modes` panel that shows each subsystem as:
  - `api_first`
  - `hybrid_cli_gated`
  - `not_wired`
  - `host_side_manual`
  - `alert_only`
- split routing visibility into:
  - active routed callsites
  - configured but dormant callsites
- add a small fallback / approval ledger:
  - pending approvals
  - recent approved/rejected requests
  - recent blocked autonomous runs
- link approval entries back to the related API callsite row

### Ego in the neural monitor

Ego is currently built but not runtime-wired.

That means the monitor should not present Ego as an active subsystem yet.

Recommended treatment:

- show Ego as `configured but dormant`
- expose `7_ego_cycle_api` as editable in routing config
- show Ego status as `not_wired` in any subsystem/automation panel
- keep Ego cadence/budget visible as configuration, not as active runtime state

Before enabling Ego, fix the callsite identity split:

- runtime code still records last-run under `7_ego_cycle`
- API route is configured as `7_ego_cycle_api`

That should be collapsed to a single operator-visible callsite ID before Ego is
considered live.

### Guardian in the neural monitor

Guardian should not be added to normal routing health yet.

Reason:

- Guardian diagnosis is host-side
- it is not just "which model answered"
- it can investigate and recover
- its safety boundary is different from normal in-container API routing

Recommended treatment:

- give Guardian its own safety/status panel
- show:
  - current mode
  - last diagnosis time
  - last diagnosis source
  - last recovery outcome
  - whether host-side CLI use is blocked, manual, or alert-only

Do not add Guardian host recovery as a standard routing callsite in this pass.

---

## Global Toggle Follow-Up

The autonomous CLI approval policy should move from env-only control to a
dashboard-visible Genesis setting.

Source of truth:

- Genesis config/settings are authoritative
- runtime reads effective values from config
- env vars remain fallback defaults only
- Genesis exports effective policy to the shared mount for Guardian

Status: implemented.

Implementation notes:

- new config file: `config/autonomous_cli_policy.yaml`
- new settings domain: `autonomous_cli_policy`
- new exporter: `src/genesis/autonomy/cli_policy.py`
- runtime export wiring: `src/genesis/runtime/init/autonomy.py`,
  `src/genesis/runtime/init/guardian.py`, `src/genesis/awareness/loop.py`
- dashboard visibility: `src/genesis/dashboard/routes/state.py` and
  `src/genesis/dashboard/templates/genesis_dashboard.html`

Policy fields to expose:

- autonomous CLI fallback enabled
- manual approval required before autonomous CLI dispatch
- re-ask interval
- preferred approval delivery channel

Neural monitor / dashboard should show:

- current effective policy
- whether CLI is disabled, gated, or ungated
- whether Guardian has successfully consumed the exported policy

---

## Guardian Follow-Up Direction

Guardian will remain on `claude -p` for now.

No separate Guardian API diagnosis route is planned in this pass.

Instead, Guardian should gain two approval checkpoints:

### 1. Diagnosis approval

Before Guardian launches `claude -p` to investigate, it must first ask for
explicit approval.

This is the new gate added to align Guardian with the broader autonomous CLI
hardening model.

### 2. Recovery approval

If the diagnosis concludes that action is required, Guardian must then ask for
explicit approval again before taking recovery action.

This preserves the existing safety model for destructive or operationally
sensitive actions.

In plain terms:

- first approval: "May Guardian investigate?"
- second approval: "May Guardian act?"

The global autonomous CLI policy should affect Guardian as well.

Recommended implementation:

- Genesis writes an exported policy file to the shared mount
- Guardian reads that exported policy before deciding whether diagnosis may
  begin automatically or must wait for approval
- if the exported policy is missing, Guardian should take the conservative path
  and require approval before diagnosis

Neural monitor treatment:

- keep Guardian in a dedicated safety/status panel
- show whether Guardian is:
  - blocked on diagnosis approval
  - diagnosing
  - blocked on recovery approval
  - recovering
  - escalated / alert-only
- do not add Guardian diagnosis as a normal routing-health callsite

---

## Design

### 1. Add a dedicated autonomous dispatch policy layer

Do not try to force API routing through `AgentProvider`.

The safe design is a new runtime-owned dispatcher above the current CLI
invoker and the existing API router.

Responsibilities:

- identify autonomous/background callsite and policy
- attempt API routing first
- decide whether CLI fallback is allowed
- request manual approval before CLI fallback dispatch
- return a structured result to the caller
- emit logs and events for path selection and fallback rationale

This avoids broad churn to:

- [src/genesis/cc/protocol.py](${HOME}/genesis/src/genesis/cc/protocol.py)
- [src/genesis/cc/types.py](${HOME}/genesis/src/genesis/cc/types.py)

### 2. Keep CLI fallback as policy, not implementation default

Global policy defaults:

- autonomous CLI fallback enabled: `true`
- autonomous CLI manual approval enabled: `true`
- re-ask interval: once every 24 hours

Per-callsite policy should include:

- API `call_site_id`
- whether CLI fallback is allowed
- whether CLI fallback requires approval
- user-facing action label
- defer/fail behavior when fallback is blocked or pending

### 3. Build a channel-agnostic approval gate

Approval semantics should be generic even though Telegram is the first channel.

Core approval gate behavior:

- create approval request before CLI fallback dispatch
- persist structured context in `approval_requests.context`
- deliver request to the preferred channel
- wait for approval or rejection
- re-ask once per day while pending
- cancel re-ask scheduling once resolved
- ensure only one dispatch proceeds for a given approval

Resolution surfaces in this pass:

- Telegram reply flow
- dashboard approve/reject action

### 4. Reuse the dashboard and channel bridge, but extend them

Needed changes:

- extend dashboard API beyond read-only pending list
- provide generic approval resolve actions
- render enough structured approval context for operator judgment
- keep Telegram message format generic so another adapter can implement the
  same contract later

---

## Runtime Coverage In This Pass

### Reflection bridge

- [src/genesis/cc/reflection_bridge/_bridge.py](${HOME}/genesis/src/genesis/cc/reflection_bridge/_bridge.py)
- use API first for:
  - `4_light_reflection`
  - `5_deep_reflection`
  - `6_strategic_reflection`
- only consider CLI after API exhaustion or explicit policy override
- if CLI is considered, request approval before `_invoker.run(...)`
- preserve current deferred-work semantics where they already exist for budget
  throttling or explicit dispatch gating
- stop hardcoding provider `"cc"` in last-run recording when API path was used

### Ego cycle

- [src/genesis/ego/session.py](${HOME}/genesis/src/genesis/ego/session.py)
- route through API first using a dedicated ego callsite
- CLI fallback remains available but approval-gated
- if API and CLI both fail or CLI is blocked, fail cleanly and log why

### Inbox monitor

- [src/genesis/inbox/monitor.py](${HOME}/genesis/src/genesis/inbox/monitor.py)
- route inbox evaluation through API first
- CLI fallback requires approval
- if approval remains pending, batch should defer rather than silently proceed

### Autonomy executor

- [src/genesis/autonomy/executor/engine.py](${HOME}/genesis/src/genesis/autonomy/executor/engine.py)
- background task step dispatch becomes API-first
- CLI fallback requires approval per dispatch attempt
- task state should reflect blocked/pending approval rather than appearing
  silently stuck

---

## Config And Policy Additions

Add a focused autonomous-dispatch config surface.

Possible shape:

```yaml
autonomous_dispatch:
  cli_fallback_enabled: true
  cli_manual_approval_enabled: true
  cli_reask_interval_hours: 24
  callsites:
    reflection_light:
      api_call_site_id: 4_light_reflection
      cli_fallback_allowed: true
      approval_required_for_cli: true
    reflection_deep:
      api_call_site_id: 5_deep_reflection
      cli_fallback_allowed: true
      approval_required_for_cli: true
    reflection_strategic:
      api_call_site_id: 6_strategic_reflection
      cli_fallback_allowed: true
      approval_required_for_cli: true
    ego_cycle:
      api_call_site_id: 7_ego_cycle_api
      cli_fallback_allowed: true
      approval_required_for_cli: true
    inbox_evaluation:
      api_call_site_id: contingency_inbox
      cli_fallback_allowed: true
      approval_required_for_cli: true
    executor_step:
      api_call_site_id: autonomous_executor_step
      cli_fallback_allowed: true
      approval_required_for_cli: true
```

Notes:

- the exact callsite IDs may differ slightly at implementation time
- reflection callsites already exist in `model_routing.yaml`
- ego and executor will likely need dedicated API callsites added

---

## Approval Request Schema Use

Reuse `approval_requests` and store structured JSON in `context`.

Suggested context payload:

```json
{
  "kind": "autonomous_cli_fallback",
  "subsystem": "reflection",
  "policy_id": "reflection_deep",
  "api_call_site_id": "5_deep_reflection",
  "reason_cli_considered": "API providers exhausted",
  "api_failure_summary": "primary provider timeout; fallback quota exhausted",
  "action_label": "deep reflection",
  "channel": "telegram",
  "delivery_id": "123456",
  "last_sent_at": "2026-04-04T12:34:56+00:00",
  "next_reask_at": "2026-04-05T12:34:56+00:00"
}
```

---

## Observability Requirements

Logs and/or events should make routing choices explicit:

- autonomous policy loaded
- API attempt started
- API path succeeded
- API path failed and why
- CLI fallback considered
- approval requested
- approval approved or rejected
- CLI fallback dispatched
- callsite exhausted with no approved fallback

Startup and runtime logging must make it obvious whether work ran through API,
was deferred pending approval, or failed because no approved path remained.

---

## Implementation Order

### Step 1. Harden approval invariants

Files:

- [src/genesis/db/crud/approval_requests.py](${HOME}/genesis/src/genesis/db/crud/approval_requests.py)
- [src/genesis/autonomy/approval.py](${HOME}/genesis/src/genesis/autonomy/approval.py)
- tests under `tests/test_autonomy/`

Work:

- make resolve pending-only
- make duplicate or late resolve attempts no-op
- add tests for race-safe/idempotent behavior

### Step 2. Add autonomous dispatch policy layer

Likely new file(s):

- `src/genesis/autonomy/autonomous_dispatch.py`
- `src/genesis/autonomy/cli_fallback_approval.py`

Work:

- define policy/config types
- implement API-first routing plus optional CLI fallback
- return structured dispatch result

### Step 3. Add approval delivery and resolution plumbing

Files:

- dashboard route module(s)
- channel/Telegram integration
- outreach/reply waiter integration if reused directly

Work:

- send approval requests to Telegram
- parse reply approval or rejection
- add dashboard resolve endpoints
- synchronize resolution across surfaces

### Step 4. Wire runtime bootstrap

Files:

- [src/genesis/runtime/init/autonomy.py](${HOME}/genesis/src/genesis/runtime/init/autonomy.py)
- [src/genesis/runtime/init/cc_relay.py](${HOME}/genesis/src/genesis/runtime/init/cc_relay.py)

Work:

- construct the new dispatcher and approval gate service
- inject dependencies: router, invoker, approval manager, channels

### Step 5. Migrate callsites

Files:

- [src/genesis/cc/reflection_bridge/_bridge.py](${HOME}/genesis/src/genesis/cc/reflection_bridge/_bridge.py)
- [src/genesis/ego/session.py](${HOME}/genesis/src/genesis/ego/session.py)
- [src/genesis/inbox/monitor.py](${HOME}/genesis/src/genesis/inbox/monitor.py)
- [src/genesis/autonomy/executor/engine.py](${HOME}/genesis/src/genesis/autonomy/executor/engine.py)

Work:

- replace direct autonomous `_invoker.run(...)` call paths with dispatcher calls
- preserve subsystem-specific defer/fail semantics
- update last-run/provider recording

### Step 6. Docs and verification

Files:

- this plan
- operator-facing config docs
- any README or deployment notes that mention autonomous CC behavior

---

## Testing Plan

### Routing policy tests

- API path is used by default when successful
- CLI fallback is not used when API succeeds
- CLI fallback is considered only after API failure
- CLI fallback is skipped when globally disabled

### Approval gate tests

- approval request is created before CLI dispatch
- duplicate approval does not produce duplicate dispatch
- rejection blocks dispatch
- pending approval leaves work deferred/blocked
- re-ask happens at most once per 24 hours
- resolved requests stop future re-asks

### Telegram and dashboard tests

- Telegram delivery occurs with expected approval context
- Telegram reply resolves approval and unblocks dispatch
- dashboard approve/reject resolves the same request
- resolution from one surface is visible from the other

### Subsystem tests

- reflection paths use API first, then gated CLI fallback
- ego cycle uses API first, then gated CLI fallback
- inbox evaluation defers or fails cleanly while approval is pending
- executor step exposes blocked/pending state rather than silently hanging

### Non-regression tests

- foreground conversation behavior remains unchanged
- existing contingency behavior still works when CC is actually unavailable

---

## Acceptance Criteria

- no autonomous/background execution reaches `claude -p` by default
- `claude -p` remains available only as explicit fallback policy
- no autonomous/background CLI fallback dispatch occurs without explicit approval
- routing decisions and approval gating are covered by tests
- startup/runtime logs show selected engine path and rationale
- docs/config explain defaults and operator controls
- Guardian remains untouched in this pass

---

## Risks And Mitigations

### Risk: accidental foreground regression

Mitigation:

- leave [src/genesis/cc/conversation.py](${HOME}/genesis/src/genesis/cc/conversation.py) unchanged
- keep new dispatcher scoped to autonomous/background callsites only

### Risk: approval races or duplicate dispatch

Mitigation:

- harden `approval_requests.resolve()` to pending-only
- add idempotency tests before wiring fallback dispatch

### Risk: policy duplication across callsites

Mitigation:

- centralize policy in one runtime-owned dispatcher
- keep callsites declarative

### Risk: executor blocked state is opaque

Mitigation:

- add explicit blocked/pending approval result states and logs

---

## Explicit Deferral

Guardian diagnosis is intentionally excluded.

Reason:

- it is host-side
- it invokes `claude -p` directly with operational recovery consequences
- it deserves a separate pass with tighter controls and verification

---

## Outstanding Work

- Guardian diagnosis hardening remains for a separate pass
- any remaining dashboard polish beyond approve/reject basics remains follow-up work
- executor tooling steps remain CLI-backed by design in this pass and only gain approval gating, not API execution

---

## Next Steps When We Resume

- implement Guardian pre-diagnosis approval gate (before any `claude -p` invocation)
- keep existing Guardian recovery approval gate (post-diagnosis, pre-action)
- make Guardian read the shared exported policy file to decide whether the pre-diagnosis gate is required
- add a Guardian status surface in the dashboard/neural monitor (mode, last diagnosis, blocked state)
- add tests for:
  - Guardian blocks diagnosis when approval required
  - Guardian proceeds when approval granted
  - policy export missing → Guardian defaults to requiring approval
