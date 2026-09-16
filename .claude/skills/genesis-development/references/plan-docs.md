# Plan Documents — the structured header

A plan doc is the working document for a build that outlives one session. It
lives outside the repo (`~/.claude/plans/<name>.md`), survives compaction, and
is usually the only place a multi-day design is written down in full.

That makes it durable. It does not make it **readable back**, and three failure
modes follow from the difference. The header exists to close these three —
nothing else. Two are measured on this install; the third is a mechanical
property, marked as such.

1. **Nothing reads a plan file's COMMITMENTS back.** Three things do enumerate
   the plan directory — `memory/open_loops._plan_lines()` (which renders the
   SessionStart in-flight block), `mcp/memory/locate.py`'s `plans` scope, and
   `scripts/plan_bookmark_hook.py` — so "a plan doc is read by nobody" is
   false, and an earlier draft of this file said it. What is true is narrower
   and worse: all three key on **filename and mtime**, and the third opens the
   file only for its title. None reads what the plan OWES. So a commitment
   living only in a plan file is enumerated by nothing, which is how finished,
   tested code sat unpushed for a day and a half on this install
   (CLAUDE.md, zero-drop rule). The header's tracker ids are the cheap half of
   the fix: they make the plan→tracker direction navigable. The tracker→plan
   direction still is not.

2. **A plan accretes, and the format offers no signal for which part is live.**
   A long-running plan file on this install reached **4,488 lines, of which
   2,501 — 56% — sat below the divider once one was drawn**
   (`~/.claude/plans/tidy-orbiting-haven.md`, measured 2026-09-15). Before that
   the session that noticed had to hand-write a warning banner and invent an
   "archaeology" marker, because nothing in the format said where live content
   ended.

3. **A plan goes stale against the repo silently.** Line numbers, PR heads and
   "origin/main is at X" are true when written and quietly stop being true.
   This one is structural rather than measured: the plan cannot know it has
   gone stale, and a reader only finds out by checking against something the
   plan never recorded.

## The header

YAML frontmatter, the first thing in the file:

```yaml
---
plan: tidy-orbiting-haven        # stable slug; matches the filename
status: active                   # active | stalled | superseded | done
updated: 2026-09-15              # last revision of the LIVE section
pinned:
  main: 71e91e148ab3             # origin/main as of the `updated` date above
  prs: [1932, 2033]              # PRs this plan is about        (optional)
decisions: [828d7458b5dd4785]    # ego_decision ids it executes  (optional)
ledger: [a9db0a3dac0a4dbfa8ca6d9221c70cac]   # ledger rows it advances (optional)
issues: [2052, 2060]             # issues it opens or closes     (optional)
binds: "the drain triad ships before any new review automation."
prevents: "a second findings store — adjudication writes to the existing one."
---
```

Required: `plan`, `status`, `updated`, `pinned.main`, `binds`, `prevents`. The
three id lists are optional.

**Quote `binds` and `prevents`.** They are prose, and unquoted YAML punishes
prose in two different ways — MEASURED with this repo's PyYAML:

| written | loaded |
|---|---|
| `binds: ship this: the triad first` | `ScannerError` — the whole header is unparseable |
| `binds: PR #2046 ships before the worker` | `{'binds': 'PR'}` — **silently truncated** |

The `#` case is the dangerous one, it parses clean, and this repo writes PR
numbers as `#NNNN` constantly. Quote both fields. Quote a list element too if
its value could ever be all digits.

**Use full ids, not prefixes.** A ledger row id is 32 hex
(`session_ledger.id`), an `ego_decision` id is 16 (`ego_directives.id`). The
PR-body conventions in CLAUDE.md key on the full ledger id, so a template that
teaches a prefix teaches something that will not match.

**An absent id list means "none" — so only omit it once you have looked.** If
you have not, write `issues: unchecked` rather than omitting it. Omitting it
asserts a verified negative, and asserting a negative nobody verified is
exactly the failure CLAUDE.md's evidence principle names.

## The one body rule

```
## ═══ SUPERSEDED BELOW ═══
```

Everything above that divider is live plan content. Everything below is
archaeology, kept for provenance. A plan with no divider is entirely live.

**This closes failure mode 2 for a reader who OPENS the file. It does not close
it for a grepper** — a hit at line N carries no signal about which side of the
divider it fell on, and the grepper is the reader failure mode 2 names. So the
grepper has a half to do:

```bash
awk '/═══ SUPERSEDED BELOW ═══/{print NR; exit}' <plan>
```

A hit below that line number is archaeology. The divider is what makes that
check possible at all; it is not the check.

It is deliberately one line. Deleting superseded sections instead would lose
the reasoning trail, and per-section status markers are a convention every
future edit must remember, which is the shape that drifts.

**One known collision, named rather than left to be discovered.**
`config/gstack-patches/codex-SKILL.md` instructs its review flow to append a
`## GSTACK REVIEW REPORT` section and "always place it as the very last section
in the plan file" — i.e. below the divider. The divider governs *plan content*;
a tool-appended status block at end-of-file is not plan content and is current
by construction. That is a real rough edge, not a clean exception, and it will
read as archaeology to anyone scrolling.

## What each field is for

**`status`** — a `superseded`, `done` or `stalled` plan is not deleted; it is
labelled, so a grep hit inside it is immediately recognisable. `stalled` exists
because several plans on this install are neither active nor finished, and
without a word for that state they stay labelled `active` forever.

**`updated` + `pinned.main`** — together they answer "what was this written
against?" mechanically: `git log --oneline <pinned.main>..origin/main` is then
a one-command staleness read. Both are defined against the same event — a
revision of the LIVE section — so an edit confined to the archaeology zone
moves neither. A moved main means re-verify, never that the plan is wrong.

**`decisions` / `ledger` / `issues`** — the plan's handles into the systems
that are read back. A ledger row says what was agreed in one sentence; the plan
says how. Naming the row in the header makes that pair navigable from the plan
end.

**`binds` / `prevents`** — one line each, and they are the two sentences that
make a plan reviewable rather than merely long. `binds` is what adopting this
plan commits the project to. `prevents` is what it forecloses — a second store,
a different approach, an option we are spending. A plan whose `prevents` is
honestly "nothing" says so; a plan that cannot state either has not decided
anything yet, which is itself worth knowing before the build starts.

## Provenance, and what was NOT carried across

The shape is adapted from BMAD's spine-document schema (MIT). Two things are
borrowed and both are borrowed loosely, so "adapted from" is doing real work in
that sentence:

- **`binds`/`prevents`** — upstream these are part of a per-decision triple
  (`Binds` / `Prevents` / **`Rule`**) inside numbered `AD-n` body blocks. Here
  they are one document-level pair and **`Rule` has no analogue**. The
  vocabulary is carried across; the shape is not.
- **Decision ids** — upstream they are `AD-n` markers structuring the body.
  Here they are a flat frontmatter list of ids belonging to other systems.

**The pinned-version stack table is declined on purpose.** BMAD pins library
versions because a spine doc's staleness axis is the dependency set. A Genesis
plan's staleness axis is the repo state it reasoned against — the line numbers
it cites, the PR heads it assumes. Pinning versions here would pin something
that rarely moves and leave the thing that moves daily unpinned. Recorded so a
later reader does not "restore" a field that was considered and declined.

## What this does not do

**`lint_spine.py` cannot lint this header, and adopting the header does not
change that.** Verified against the upstream source: it walks `AD-n` **body**
blocks requiring `binds`/`prevents`/`rule`, and locates a `## Stack` **body**
markdown table by column header — its own docstring says *"Pinning lives in the
body table now, not frontmatter."* This header emits neither structure. So
lint_spine is a source to adapt, not a tool to point at a Genesis plan. An
earlier draft of this file claimed the opposite.

The checker for this header is therefore a separate, later change, and until it
exists the header is written by hand. Stated plainly rather than implied: **a
plan doc today can be missing its header entirely and nothing will say so.** Do
not read the presence of this convention as enforcement of it.
