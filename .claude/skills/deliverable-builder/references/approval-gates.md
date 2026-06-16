# Gates & enforcement

Three gates wrap the pipeline. In v1 (foreground) Gates 1 and 3 are interactive; Gate 2 is the
automated policeman (`qa-protocol.md`). A Stop-hook makes the whole thing non-skippable.

## Gate 1 — Frame & Format (after Intake)
Interactive. Defined in `intake.md`: confirm audience, format, what-leads, win-condition, and
the acceptance criteria before any drafting. Sets `audit_trail.intake.gate1_approved = true`.

## Gate 3 — User sign-off (after a Gate-2 PASS)
The user approves an *already-verified* artifact going out under their name. Present, in one
message:

> Done and verified (Gate 2: GREEN). **<format>** at `<rendered_path>`.
> Leads with: "<what_leads>". Acceptance criteria AC1…ACn all met.
> Approve to ship, or send edits?

- **Approve** → set `status: "shipped"`. The deliverable is the user's to send; this skill does
  not auto-send anything in v1.
- **Edits** → treat as new/changed criteria, set `status: "drafting"`, loop back to the right
  stage, re-verify (Gate 2), re-present.

Never reach Gate 3 without a recorded Gate-2 PASS.

## The Stop-hook gate (`scripts/hooks/deliverable_gate_guard.py`)

The marker `~/.genesis/sessions/<session_id>/deliverable.json` carries a `status`. The Stop hook
(registered on the `Stop` event) reads the hook's `session_id` from stdin, opens *that session's*
marker, and:

- blocks session-end (`exit 2`) **iff** `status == "rendered_unverified"` — i.e. something was
  rendered but Gate 2 hasn't passed it. The block message says exactly how to resolve: run Gate 2,
  or cancel.
- **allows** in every other state (`drafting`, `verified`, `shipped`, `cancelled`), when there is
  no marker, when the marker is for a different session, or on ANY error. **Fail-open is
  absolute** — a bug in this hook must never prevent a session from ending.

Why it only blocks `rendered_unverified`: in a correct run, Render → Gate 2 is immediate, so that
state only persists if the session rendered something and then tried to quit *without verifying*
— exactly the failure we're preventing. Intake / Gate-1 / Gate-3 yields sit in other states, so
normal interaction never trips it.

### Cancel path (no deadlock)
If the user abandons a deliverable, set `status: "cancelled"` in the marker (or delete it). The
gate releases immediately. Always give the user this exit when surfacing a blocked stop.

## Autonomous mode — v2 stub (not built)
Same stages, different surfaces: Gate 1 reads the frame from the task spec
(`task_states.description` + plan); Gate 3 becomes a persisted draft + Telegram approval via
`outreach_send_and_wait(category="approval")`; the Stop-hook gains a max-block→escalate so an
unattended session can't wedge. Models to mirror: `executor/engine.py` (bounded review loop +
`_persist_blocker`), `executor/review.py` (tool-capable verify chain). Do not build in v1.
