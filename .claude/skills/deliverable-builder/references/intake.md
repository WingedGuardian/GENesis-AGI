# Intake — frame the deliverable, freeze the criteria

The Intake stage produces the **deliverable-spec** (`spec`), the small state object every
later stage reads and updates. Nothing gets drafted until Gate 1 passes.

## Autonomous mode (v2 — running as a Genesis task-executor step)

If you were dispatched as an executor **step** (your prompt names this skill and there is no
live user to talk to), you are in autonomous mode. The Gate-1 frame was already captured at
`/task` intake — your job is to read it and execute, not to interview. The whole foreground flow
below (Steps 1–4, Gate 1) is replaced by this:

1. **Read the COMPLETE skill first — by absolute path, NOT the `Skill` tool.** The injected copy
   is truncated (~2000 chars, `SKILL.md` only), and the `Skill` tool will NOT find this skill
   (an executor step runs outside the project, so the project skill isn't registered — verified).
   Instead Read `SKILL.md` and its `references/` files from the **absolute skill directory** given
   in your injected resources (the `### Skill: deliverable-builder (full skill dir: ...)` line)
   before doing anything else.
2. **Read the frame from the plan; do NOT interview.** The full task plan is embedded in this
   step's description (under `## Full task plan`); its `## Deliverable Frame` section is the
   frame. Map it into the spec: `format`,
   `visual_style`, `authenticity_target`, `audience`, `what_leads`, `acceptance`. The brief =
   the plan's `## Requirements` + `## Success Criteria`; freeze those into `acceptance_criteria`.
   **Never call `AskUserQuestion`** — there is no user. If a frame field is missing, use the
   conservative default (`visual_style=modern`, `authenticity_target=ai-assisted-ok`) and **record
   the assumption in `qa_summary.md`** so the user can correct it at Gate 3 — never block to ask.
3. **Run the pipeline** — Structure → Voice → Anti-slop → Render (font follows `visual_style`;
   default Lato). The Voice (`voice-master`) and Anti-slop (`humanizer`) stages are themselves
   skills — and the `Skill` tool may NOT load them here (same out-of-project limit). Read them by
   absolute path and apply their rules yourself: `voice-master` at the repo's
   `.claude/skills/voice-master/` (+ the user companion `~/.claude/skills/voice-master/`),
   `humanizer` at `~/.claude/skills/humanizer/`. **Hard final check before emitting — do it
   IN-SESSION, never assume a skill did it: the rendered artifact must contain ZERO spaced
   em-dashes (` — `; replace each with a comma, period, or colon) and none of the banned AI-tell
   words.** Spaced em-dashes are the #1 tell and a hard fail; verify the SOURCE before you render.
4. **Gate 2 is still YOURS — run it IN-SESSION.** The `Task` subagent tool is NOT available in an
   executor step (verified), so do not try to spawn a fresh-context reviewer. Instead re-open your
   rendered file yourself (Bash `pdftotext`/`pdfinfo`, or `python -m fitz`) and verify it against
   the frozen acceptance criteria using the `qa-protocol.md` bar — the executor's text-only
   verifier cannot open your PDF, so this in-session check is the real check on the rendered file.
   The executor's `VERIFYING` phase then runs a fresh adversarial pass over the `qa_summary.md` you
   emit (next step), which is the closest thing to the foreground fresh-context Gate 2.
5. **Emit two artifacts** (the executor's review loop reads text, not PDF bytes): the rendered
   deliverable file **and** a `qa_summary.md` beside it, capturing the Gate-2 verdict,
   per-criterion PASS/FAIL, and any assumptions made. Return a **compact** step result — the
   rendered file path + a one-line PASS/FAIL — because step results are truncated to ~2000 chars
   on persist; verbose detail belongs in `qa_summary.md`.
6. **Gate 3 and the Stop-hook do NOT apply here.** Sign-off is the executor's job (a
   `VERIFYING`-phase Telegram approval — see `approval-gates.md`), and the Stop-hook is a
   foreground-only backstop. Do not write the `rendered_unverified` marker and do not wait on a
   Stop-hook in autonomous mode.

Everything below is the **foreground** flow.

## Step 1 — Resolve the session id and open the spec

The spec lives in this session's existing per-session dir so the Stop-hook gate
(`deliverable_gate_guard.py`) can find it. Resolve the full session id from the
`[Session: XXXXXXXX]` prefix the clock hook injects into your context each turn:

```bash
PREFIX="dc22f977"   # <-- the 8 chars from the [Session: ...] line in your context
SID=$(ls -1 ~/.genesis/sessions/ 2>/dev/null | grep -E "^${PREFIX}" | head -1)
# fallback: newest transcript across all projects (= this session). The glob avoids
# hardcoding the home path / username, which Claude Code encodes into the project dir name.
[ -z "$SID" ] && SID=$(ls -t ~/.claude/projects/*/*.jsonl 2>/dev/null \
  | head -1 | xargs -r -n1 basename | sed 's/\.jsonl$//')
echo "$SID"   # full uuid; the spec marker is ~/.genesis/sessions/$SID/deliverable.json
```

If `SID` resolves empty, proceed anyway — the gate is fail-open; you just lose the
hard Stop-hook backstop for this run (the in-pipeline Gate 2 still runs).

## Step 2 — Interview (one decision at a time)

In foreground, this is a **real interview** — ask the user, one question at a time, and let
their answers drive the spec. Do NOT auto-decide and skip ahead. A deliverable going out under
someone's name needs their input on audience, format, and emphasis; guessing these is how you
ship the wrong thing politely. (Only an autonomous run reads these from a task spec instead of
asking — v2.)

Establish, and write into the spec:

- **brief_path** — absolute path to the *original* brief/requirements (the take-home
  instructions, the RFP, the email). Gate 2 re-reads this file directly, so it must be the
  real source, not a paraphrase. If the ask was verbal, write it to a file first.
- **audience** — who receives this, and what they already know.
- **purpose** — the job it does for them.
- **win_condition** — one sentence: what makes this a success in *their* eyes.
- **format** — a deliberate decision, **not a default**. Use `references/format-guide.md` to form
  a recommendation from audience+purpose, then **put it to the user with the alternatives.** The
  most common fork is **PDF (final, fixed) vs DOCX (they'll edit or comment on it)**; decks, XLSX,
  etc. live in the matrix. Confirm before drafting. **Never markdown for an external /
  under-the-user's-name deliverable.**
- **what_leads** — one sentence: the single strongest claim the reader sees first
  (the answer, not the setup). This is the altitude decision, made up front.
- **output_location** — where the finished artifact and working files go (see "Output location"
  below). Part of the Gate-1 confirm.
- **authenticity_target** — must this read as *fully human-made* (the user personally wrote it;
  any AI fingerprint undercuts it), or is *AI-assisted / polished* fine, or even expected? This
  is **not cosmetic** — it sets how hard the Voice and Anti-slop stages push and what Gate 2
  fails on. A take-home where the team knows AI was used wants maximal polish; a personal note
  under the user's name does not. **Ask — never assume "human-made."** (A recent job take-home was
  *correctly* polished because AI-assisted was fine there; the identical polish on a personal
  piece would be the wrong shape.)
- **visual_style** — cut-and-dry (plain, black-and-white, minimal) or designed (color, branded,
  visual polish), and how polished overall? Drives the render tool and styling (`render-guide.md`).

## Step 3 — Freeze the brief into acceptance criteria

Read `brief_path` and decompose it into a numbered, checkable list. Each criterion is
something Gate 2 can verify the finished artifact against, one by one — this is what makes
a skipped requirement impossible to hide.

- Keep them MECE (no overlap, jointly cover the brief).
- Mark each `must` (blocking) or `should` (graded, non-blocking).
- Include implicit-but-obvious requirements (e.g. "submitted as a single file the hiring
  team can open" → format criterion), not just the literally-stated ones.

## Step 4 — Write the spec

Write the marker to `~/.genesis/sessions/$SID/deliverable.json` with `status: "drafting"`:

```json
{
  "schema_version": "1",
  "session_id": "<full uuid>",
  "status": "drafting",
  "created_at": "<ISO-8601>",
  "brief_path": "/abs/path/to/original/brief",
  "audience": "", "purpose": "", "win_condition": "",
  "acceptance_criteria": [
    {"id": "AC1", "text": "...", "must": true},
    {"id": "AC2", "text": "...", "must": false}
  ],
  "format": "pdf",
  "what_leads": "the first claim the reader sees",
  "authenticity_target": "human-made | ai-assisted-ok",
  "visual_style": "cut-and-dry | designed (note colour/polish level)",
  "draft_path": "", "rendered_path": "",
  "audit_trail": {
    "intake":    {"ran": true,  "gate1_approved": false},
    "structure": {"ran": false},
    "voice":     {"ran": false},
    "antislop":  {"ran": false},
    "render":    {"ran": false},
    "verify":    {"iterations": 0, "result": null, "failures": []}
  }
}
```

`status` is the state machine the gate watches:
`drafting → rendered_unverified → verified → shipped` (or `cancelled`).
The gate blocks session-end **only** in `rendered_unverified`. Set `rendered_unverified`
right after Render; set `verified` only after a Gate-2 PASS; set `shipped` after Gate 3.
If the user abandons the deliverable, set `status: "cancelled"` so the gate releases.

## Output location

All deliverable files go under `~/.genesis/output/` (never the repo). Decide the shape at intake:

- **Single self-contained file the user just wants handed over** → the final artifact sits
  directly in `~/.genesis/output/<slug>.<ext>`; keep the working set (spec.json, draft) in a
  sibling scratch folder `~/.genesis/output/<slug>-work/` so it doesn't clutter the output dir.
- **A packet / multi-file deliverable** (report + appendix, deck + speaker notes, embedded
  diagrams, a data export alongside the doc) → its own subfolder `~/.genesis/output/<slug>/`
  holds the final artifact(s) and working files together.
- **Unsure → default to a subfolder.** If the user wants the final file to ultimately land
  somewhere specific (a Desktop path, an attachment folder), capture that too. Confirm at Gate 1.

## Gate 1 — Frame the deliverable (robust interview, every time)

This is the most important stage. A vague Gate 1 is how you ship a technically-correct
deliverable in the wrong *shape*. In foreground, run it as a **real interview using the
`AskUserQuestion` tool — every time, not optionally** — until you could describe the finished
artifact precisely, both visually and substantially. Recommend, then let the user decide; do
not settle for checking boxes off a list.

Get explicit clarity on all of these (recommend an option, but it's the user's call):

- **Format** — the *actual* artifact type. Whatever the deliverable genuinely is (PDF, DOCX,
  deck, spreadsheet, …), never a guess and never defaulted.
- **Authenticity target** — must it read as *fully human-made*, or is *AI-assisted / polished*
  fine or expected? Routinely missed, routinely wrong by default. Ask it outright; it changes
  how hard Voice and Anti-slop push and what Gate 2 fails on.
- **Visual style / polish** — cut-and-dry vs designed; colour vs black-and-white; how polished
  overall.
- **Substance** — audience, win condition, what leads, and the acceptance criteria.

Prefer one multi-question `AskUserQuestion` call so the user sets these *directly* rather than
reacting to your guesses. Write every answer into the spec, then set
`audit_trail.intake.gate1_approved = true` and proceed to Draft. If anything stays fuzzy, ask
again — real back-and-forth here is the point, not friction. (Autonomous runs read these from
the task spec instead — v2.)
