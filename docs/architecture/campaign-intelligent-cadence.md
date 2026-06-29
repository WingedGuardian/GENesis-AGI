# Campaign Intelligent Cadence — Design Note (follow-up)

Status: **designed, not built.** Captured during the campaigns-dashboard work
(2026-06-28). The foundation shipped (dashboard controls, jitter, the
pending-session reaper that decouples *result capture* from the cron tick). The
items below extend campaigns beyond purely time-driven firing.

## Motivation

Campaigns currently fire on a fixed cron cadence. Two limitations remain:

1. **Firing is purely periodic.** Jitter (now supported) randomizes the exact
   time but the trigger is still the clock. Some campaigns should react to
   *events* (a maintainer reply, a health alert) rather than wait for the next
   tick.
2. **One model/effort per campaign.** A campaign that mostly *checks* state
   (cheap: sonnet/medium) but occasionally needs to *act* (e.g. real coding on a
   PR: opus/high) must be provisioned for the expensive case on every tick.

## Proposed work

### A. Event-driven triggers
- `CampaignRunner` subscribes to the existing `GenesisEventBus`
  (`src/genesis/observability/events.py`).
- A campaign declares event subscriptions (subsystem + event_type) in its row /
  strategy doc; a matching event runs `campaign_tick(trigger_type="event")`
  after the same pre-checks as a scheduled tick.
- The schema already allows `campaign_runs.trigger_type='event'` — no migration.
- Guard against event storms: debounce per campaign; respect `max_daily_cost`.

### B. Scan / dispatch separation + dynamic escalation
- Split a tick into a cheap **scan** (sonnet/medium, frequent) that decides
  whether real work exists, and an expensive **dispatch** (opus/high) that only
  runs when the scan flags actionable/coding work.
- Implement via a `detect_escalation(campaign, state, recent_runs) -> (model,
  effort) | None` hook consulted before `DirectSessionRequest` is built in
  `campaign_tick` (currently model/effort are read statically at
  `runner.py` dispatch). Log the escalation reason on the run.

## Notes / constraints
- Keep "quality over cost": escalation is the campaign's judgment lever, never an
  automatic *down*grade.
- The reaper already makes capture event-like (near-real-time); event-driven
  *dispatch* is the remaining half.
