---
name: deliverable-builder
description: >
  Produce a professional, send-ready deliverable on the user's behalf — in the correct file
  format (never raw markdown), in the user's voice, structured to lead with the strongest
  material, and free of document-level AI tells. Runs a gated pipeline with a fresh-context
  verification policeman and a Stop-hook that won't let the session finish without a verified
  PASS. Use when producing a job take-home, client report, executive one-pager, slide deck,
  proposal, or anything going out under the user's name.
keywords: [deliverable, take-home, submission, report, one-pager, deck, proposal, pdf, docx,
  pptx, export, professional, send-ready, polish, client]
---

# Deliverable Builder

This skill is the **conductor**. It produces a finished, human-quality deliverable by running a
fixed pipeline of stages — and it makes that process *non-skippable*, because a skipped or lazy
stage shows up at Gate 2 as a requirement the artifact fails. Most stages reuse an existing
skill or tool; this skill owns the *sequence, the gates, and the quality bars*.

Read each reference file when you reach its stage — don't load them all at once.

## When to use
Any time the user asks you to *produce something they'll send* — a take-home, client report,
one-pager, deck, proposal. Not for internal scratch notes, code (use code-voice), or content
you're publishing as Genesis (use content-publish / genesis-voice).

## The pipeline

| # | Stage | Read / invoke | Gate |
|---|---|---|---|
| 1 | **Intake** — frame, freeze acceptance criteria, pick format, open the spec | `references/intake.md` | **Gate 1** (interactive) |
| 2 | **Draft** — produce the actual substance against the frozen criteria | (your analysis) | |
| 3 | **Structure & altitude** — lead with the answer, action titles, methodology→appendix | `references/structure-altitude.md` | |
| 4 | **Voice** — make it sound like the user | invoke the **voice-master** skill | |
| 5 | **Anti-slop** — strip document/sentence AI tells | invoke the **humanizer** skill (voice-calibration off — voice-master already voiced it) | |
| 6 | **Render** — produce the real file (PDF/DOCX/PPTX); set `status: rendered_unverified` | `references/render-guide.md` (pandoc / `/make-pdf` / `/drawio-skill`) | |
| 7 | **Verify** — fresh-context adversarial PASS/FAIL vs. the frozen criteria | `references/qa-protocol.md` | **Gate 2** (policeman) |
| 8 | **Sign-off** — user approves the verified artifact | `references/approval-gates.md` | **Gate 3** (interactive) |

Gate 2 FAIL loops back to the tagged stage (bounded: 2 cycles, then escalate). Only a PASS
advances to Gate 3.

## Hard rules
- **No raw markdown as the final external artifact.** Render to the format the audience expects
  (`render-guide.md`). Markdown is for authoring, not delivery.
- **Voice and anti-slop are mandatory**, not optional polish. Every external deliverable goes
  through voice-master *and* humanizer before Render.
- **Lead with the strongest material.** Decide `what_leads` at Intake; the verifier checks it.
- **The spec is the state.** Carry state in `~/.genesis/sessions/<session_id>/deliverable.json`,
  not in your head — see `intake.md` for the schema and session-id resolution.
- **Never claim done without a Gate-2 PASS.** The Stop-hook enforces this; don't fight it —
  pass the gate or `cancel` the deliverable (`approval-gates.md`).

## State machine
`drafting → rendered_unverified → verified → shipped` (or `cancelled`). The Stop-hook gate
(`scripts/hooks/deliverable_gate_guard.py`) blocks session-end only in `rendered_unverified`.
Set `rendered_unverified` right after Render, `verified` only after a Gate-2 PASS, `shipped`
after Gate 3. If the user abandons it, set `cancelled`.

## Modes
- **Foreground** (default): interactive Gates 1 & 3, the Gate-2 policeman, and the Stop-hook
  backstop. Triggered when the user asks you directly.
- **Autonomous** (v2): runs as a Genesis **task-executor step** (the decomposer assigns this
  skill when the task plan has a `## Deliverable Frame`). Gate 1 is read from that frame — no
  interview; Gate 2 is still this skill's own check (run in-session — the `Task` tool isn't
  available to an executor step); Gate 3 is a Telegram approval
  handled by the executor (a `VERIFYING`-phase blocker); escalation is executor-native; the
  Stop-hook does NOT apply. See `references/intake.md` → "Autonomous mode" and
  `references/approval-gates.md`.

## What this does NOT do (yet)
XLSX (needs `xlsxwriter`; offer CSV), polished decks beyond pandoc-basic, real Telegram file
*attachment* for autonomous Gate 3 (sends path + summary for now — fast-follow), and the
`enterprise-ai-skills` / `hallmark` extras. One deliverable at a time.

## Example
**User:** "Turn my take-home analysis into the submission packet it should have been."
**You:** Intake (audience = FDE hiring team; format = PDF; what_leads = the pipeline decision +
benchmark results; freeze the brief's asks into AC1…ACn) → Gate 1 → Draft → Structure (answer
first, methodology to appendix, action-title headings) → voice-master → humanizer → render PDF
via pandoc/make-pdf → Gate 2 (fresh reviewer re-reads the original brief, byte-checks the PDF is real,
per-criterion verdict) → Gate 3 sign-off → ship.
