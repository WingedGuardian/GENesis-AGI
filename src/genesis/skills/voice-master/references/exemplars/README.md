# Voice Exemplars — Overlay Directory

This directory holds **hand-curated samples of the user's actual writing**,
organized by medium. It is intentionally empty in the public template: personal
writing samples are user data and do not belong in the repo.

## Where Exemplars Actually Live

Exemplars are loaded from the user overlay at:

```
${GENESIS_VOICE_OVERLAY:-$HOME/.claude/skills/voice-master/}exemplars/
├── index.md           # registry of all exemplars with metadata
├── social.md          # social media voice exemplars
├── professional.md    # professional communication exemplars
└── longform.md        # blog / essay / long-form writing exemplars
```

If this overlay exists when voice-master runs, it uses the overlay files
instead of looking for exemplars in the in-repo directory. If no overlay
exists, voice-master falls back to the TEMPLATE behavior (generic voice
with a warning) — see `../voice-dimensions-TEMPLATE.md`.

## Why This Is Empty in the Repo

Three reasons:

1. **User data doesn't belong in source control.** A writing sample is
   personal data, not code. It shouldn't be in a repo that can be cloned,
   forked, or backed up to shared storage.
2. **The release pipeline can't leak what isn't there.** By keeping exemplars
   out of the repo entirely, there's no risk that a future refactor forgets
   to strip them on the way to the public distribution.
3. **The skill is shareable.** Anyone can clone Genesis and calibrate their
   own voice overlay without inheriting (or needing to remove) the original
   user's voice.

## Building Your Own Overlay

### Option A: Calibration session (recommended)

See `../../SKILL.md` → Standalone Modes → Calibrate. Start with 5-10 rounds.
The skill generates content, you edit it to sound right, and the diffs are
distilled into exemplars that are written to the overlay directory.

### Option B: Manual curation

If you already have writing samples you want to use as exemplars:

1. Create the overlay directory: `mkdir -p ~/.claude/skills/voice-master/exemplars/`
2. Pick a medium file: `social.md`, `professional.md`, or `longform.md`
3. Use the exemplar format below
4. Update `index.md` to list every exemplar with its metadata — the skill
   uses the index to pick the right exemplars for each generation request

## Exemplar Format

Each exemplar is a short passage (50-200 words) from the user's real writing,
plus metadata the skill uses for selection:

```markdown
### Exemplar [N]: [brief label]

- **Source:** transcript session [date] / inbox / manual / calibration
- **Tone:** direct / reflective / persuasive / analytical / casual
- **Formality:** 1-5 (1=casual conversation, 5=formal writing)
- **Topic domain:** [the user's actual domain, e.g., infrastructure, design]
- **Why distinctive:** [1 sentence on what makes this recognizably the user]

> [The actual passage, 50-200 words, copied verbatim from a real writing sample]
```

## Index Format

`index.md` is the registry. Voice-master reads it first to pick candidates
without having to load every exemplar file. Format:

```markdown
## Exemplar Registry

| # | Label | File | Tone | Formality | Domain | Why distinctive |
|---|-------|------|------|-----------|--------|-----------------|
| 1 | late-night strategy rant | longform.md | direct | 2 | strategy | fragment-heavy, swears mid-sentence |
```

Keep the index sorted by file, then by `#`. Regenerate it whenever you
add, remove, or move exemplars between files.

## Keeping the Index in Sync

The index is hand-maintained and is the single source of truth for
"what exemplars exist." If you add exemplars to a file without updating
the index, voice-master will ignore them. If you remove an exemplar
without updating the index, voice-master will try to read a nonexistent
passage and fall back to cross-medium exemplars or voice-dimensions.

During calibration sessions the skill updates the index automatically.
For manual curation, edit the index yourself after editing the exemplar
files.
