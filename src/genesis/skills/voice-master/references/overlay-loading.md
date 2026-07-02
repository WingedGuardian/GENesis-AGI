# Overlay Loading & Calibration (voice-master)

The full procedure for resolving the user voice overlay, loading voice data,
and running calibration. SKILL.md keeps a condensed version; this is the
authoritative detail. **Generic machinery — never put user voice data here.**

## Why an overlay

Voice-master separates **skill machinery** (this file, SKILL.md, the anti-slop
and style references — shipped in the repo) from **user data** (exemplars,
voice-dimensions). The machinery is public template; the user data lives
OUTSIDE the repo so personal writing samples never enter version control.

**Overlay default location:** `~/.claude/skills/voice-master/` (no trailing
slash when concatenating). Override via the `GENESIS_VOICE_OVERLAY` env var.

```
<overlay_root>/
├── voice-dimensions.md      # user's actual voice description
└── exemplars/
    ├── index.md
    ├── social.md
    ├── professional.md
    └── longform.md
```

## Overlay Resolution Procedure (follow EXACTLY)

Do NOT mentally expand shell parameter substitution. The Read tool does not
perform shell expansion. Resolve the path with Bash first, then pass the
resolved absolute path to the Read tool.

**Step 1 — Resolve the overlay root.** Run via Bash:

```bash
echo "${GENESIS_VOICE_OVERLAY:-$HOME/.claude/skills/voice-master}"
```

Capture the output as `<overlay_root>` for the session. Strip any trailing slash.
If Bash is unavailable, restricted, or times out, assume the default
`~/.claude/skills/voice-master` and proceed without hanging — Step 2's
no-overlay warning still fires if the overlay is genuinely absent.

**Step 2 — Load voice-dimensions.** Read `<overlay_root>/voice-dimensions.md`
(absolute path, no shell syntax).

- **If Read succeeds:** this is the authoritative voice profile.
- **If Read fails:** fall back to `references/voice-dimensions-TEMPLATE.md` in
  this skill dir. The template is generic — output will not match the user.
  Begin your reply with this MANDATORY warning on its own line:

  ```
  WARNING: No voice overlay detected. Generating with generic template voice. Run the voice-master calibration workflow to build a profile.
  ```

  Do not skip it, do not bury it mid-paragraph. The reader must see it before
  any generated content.

**Step 3 — Load the exemplar index.** Read `<overlay_root>/exemplars/index.md`.

- **If Read succeeds:** pick 3-5 best matches for the request, then read the
  matching `<overlay_root>/exemplars/{social,professional,longform}.md` files.
- **If Read fails:** read `references/exemplars/README.md` (onboarding) and
  proceed on voice-dimensions only. If voice-dimensions also fell back, the
  Step 2 warning covers it; otherwise add:

  ```
  WARNING: No exemplars in overlay — generation is based on voice-dimensions only.
  ```

**Step 4 — Never cache overlay state.** Repeat Steps 1-3 on every generation
request. The overlay can change between calls (the user may have run
calibration and added exemplars).

**Precedence:** exemplars always beat voice-dimensions. Exemplars show what the
user actually sounds like; voice-dimensions is supplementary guidance for edge
cases the exemplars don't cover. If exemplars are empty/no match, use
cross-medium exemplars (note it) or voice-dimensions; never generate with zero
voice constraint — at minimum apply the anti-slop filter.

## Overlay Hygiene Rules

- Do NOT write personal user data into the in-repo files. `voice-dimensions-
  TEMPLATE.md` and `exemplars/README.md` are generic scaffolding. Real samples
  and profiles go only in `<overlay_root>/`.
- Do not emit the fallback warning if the overlay loaded successfully.
- If asked to write to the overlay (e.g., during calibration), write to the
  resolved `<overlay_root>/` path, never to the in-repo template files.

## Calibrate — edit-and-learn loop (full procedure)

Run to refine the user's voice profile:

1. Ask the user for a topic they care about (or pick from a known domain).
2. Generate a short piece (2-3 paragraphs) using current exemplars.
3. Write the draft to a temp file (e.g., `~/tmp/voice-calibrate-draft.md`).
4. Give the user the path; ask them to edit it to sound right and signal when
   done (or paste the edited version back).
5. Diff original vs edited.
6. Analyze the diff: what changed? Added personality? Removed hedging? Changed
   vocabulary? Restructured sentences? Made it more direct?
7. Propose a voice insight: "You prefer X over Y in this context."
8. User confirms or corrects.
9. Encode the insight — write it to the **overlay**
   (`<overlay_root>/exemplars/` or `<overlay_root>/voice-dimensions.md`).
   **Never** to the in-repo template files.
10. Repeat. Initial session ~10 rounds (20-30 min); tune-ups 2-3 rounds.

## Curate — propose exemplars

- Present candidates from recent transcripts in batches of 5-10.
- User rates: "yes this is me" / "no" / "me but for a different medium".
- Tag accepted exemplars with medium, tone, formality, domain.
- Write to the appropriate exemplar file + index **in the overlay directory**.
