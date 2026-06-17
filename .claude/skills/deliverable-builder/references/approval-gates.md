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

## Autonomous mode (v2 — running under the task executor)

Same stages, different surfaces. The skill runs as the **terminal step** of an executor task
(the decomposer assigns it when the plan has a `## Deliverable Frame`). The three gates map onto
the executor's own machinery — the skill does NOT re-implement them:

- **Gate 1** moved to `/task` intake: the frame is read from the plan's `## Deliverable Frame`
  (see `intake.md` → "Autonomous mode"). No interview at execution time.
- **Gate 2** is still the skill's own check (`qa-protocol.md`), run **in-session** — the `Task`
  subagent tool is NOT available to an executor step (verified), so it's an in-session re-read of
  the rendered file, not a fresh subagent. The executor's `VERIFYING` phase then adds a *fresh*
  adversarial pass over the `qa_summary.md` text artifact you emit (it cannot open the PDF). The
  Stop-hook does not apply.
- **Gate 3 = a `VERIFYING`-phase approval, handled by the executor, not the skill.** After the
  step produces the verified deliverable, the engine (`executor/gate.py`) creates an
  `approval_request` carrying the `task_id`, sends the deliverable (path + summary in v2.0; the
  real file attachment is follow-up #2) to the user over Telegram, and transitions the task to
  `BLOCKED`. The dispatcher resumes the task when an approved-and-unconsumed `approval_request`
  for it appears (`dispatcher.py:271-305`); on resume the engine marks it consumed, skips
  re-blocking, and proceeds `VERIFYING → SYNTHESIZING → DELIVERING → COMPLETED`. "Request changes"
  (rejected/with-feedback) loops back via the existing fixup path.

**Why a blocker, not an in-step wait:** human sign-off can take hours — longer than any step
timeout — so the task must release its resources while waiting. The `BLOCKED` phase + dispatcher
resume is exactly that pattern; an in-step `outreach_send_and_wait` would hold the session open
and time out.

**Escalation is executor-native.** If Gate 2 / `VERIFYING` fails twice
(`MAX_REVIEW_ITERATIONS=2`, `engine.py:41`), the executor escalates to the user via the normal
blocker path. There is no Stop-hook block-counter in autonomous mode — that was a foreground
construct and is not used here.
