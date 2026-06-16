# Structure & altitude — lead with the strongest, cut to the appendix

This stage decides *what the reader meets first* and *in what order* — before any voice work.
It applies the public Minto Pyramid / BLUF method (implemented here in our own words; no text
is copied from any source). It is also a **Gate-2 quality bar** — the verifier checks the
rendered artifact against the rules below — so keep it self-contained.

Scope boundary: this file governs **document-level structure**. Sentence-level tells (banned
words, em-dash habits, hedging openers) belong to the Voice stage (voice-master) and the
Anti-slop stage (humanizer). Don't duplicate those here.

## The one rule: lead with the answer

The order you *thought* your way to the conclusion is the opposite of the order the reader
needs to *receive* it.

| You (author) | Reader |
|---|---|
| gathered data → found patterns → drew conclusion | wants the conclusion → then the structure → then evidence only if they doubt it |

So invert it. The first thing the reader meets is the **answer** (`spec.what_leads`), not the
setup. **Test:** can the reader stop after the first paragraph (or the first slide) and still
know what you think and what you want? If they must read to the end to find the point, it's a
story, not a deliverable — restructure.

## The pyramid

```
            [LEAD]  one sentence: the answer / recommendation / finding
               │
   ┌───────────┼───────────┐
[ARG 1]     [ARG 2]     [ARG 3]      2–4 supports, MECE, that JOINTLY prove the lead
   │            │           │
 evidence    evidence    evidence    specific data/sources, under the argument it backs
```

- **2–4 supporting arguments.** Three is the sweet spot. >4 means you haven't synthesized;
  1 means it's a one-liner, not a document.
- **MECE supports** — no overlap, and together they fully defend the lead. If all the args are
  true, the lead must be true. Cut anything that doesn't ladder up.
- **Evidence sits under the argument it supports**, not in a separate "data" section — the
  reader skims to the claim they doubt, then drops down for proof.
- **Background goes last, or gets cut.** Most "background" sections exist because the author was
  nervous about jumping to the answer. **Methodology → appendix**, never the opening.
- **One deliverable, one answer.** Two recommendations = two deliverables.

## Action titles (headings carry the message)

Every section heading / slide title states the **finding**, not the topic.

- Good: *"The pipeline halves resolution time but needs a fallback for the 8% it can't classify"*
- Bad: *"Pipeline Analysis"*

**Ghost-deck test:** read only the headings/titles, in order. They should tell the whole story
on their own. If they read as a table of contents ("Background", "Approach", "Results",
"Conclusion"), they're labels, not messages — rewrite them as claims.

## Document-level AI-tell checklist (structural)

These are the *structural* fingerprints of machine authorship — the things that mark a document
as AI-made even when every sentence is clean.

**Calibrate to `spec.authenticity_target` first.** For an *AI-assisted-OK* deliverable, polish
and effort artifacts are acceptable or even desired — apply this list with a light hand. For a
*human-made* deliverable, enforce it hard. These are tells only *against* a human-made target.
Flag and fix accordingly:

- [ ] **Meta-narration** — "In this section, we will explore…", "Let's dive into…", "Having
      established X, we now turn to Y." Cut it; just say the thing.
- [ ] **Exhaustive-coverage-as-virtue** — covering every angle equally instead of weighting by
      what matters. A human picks. Lead with what's load-bearing; demote or cut the rest.
- [ ] **Equal-length sections** — uniform blocks signal a template, not thinking. Length should
      track importance: the recommendation gets the room, the caveats get a line.
- [ ] **Relentless triadic parallelism** — everything in threes, every list grammatically
      identical. Vary it; break the pattern where the content doesn't earn it.
- [ ] **Table / bullet overload** — bullets where prose carries an argument better; tables for
      things that aren't tabular. Use them where they genuinely clarify, not by reflex.
- [ ] **Absent point of view** — surveys options, recommends nothing, hedges the close. A
      professional deliverable commits: it says what to do and why, and owns the tradeoff.
- [ ] **Formulaic open/close** — "This document presents…" / "In conclusion, …". Open on the
      answer; close on the ask or the next step, not a summary of what was just said.
- [ ] **Effort artifacts a human wouldn't hand-make** (a tell *only* when `authenticity_target`
      = human-made) — elaborate formatted tables, exhaustive specs, rich per-row descriptors,
      dense formatting. They take real work a person rarely does by hand, so their *presence*
      reads as AI even when every sentence is clean. The phData packet's biggest tell wasn't the
      prose; it was the polished tables and descriptors. For an AI-assisted-OK target they're
      fine, often desirable; for a human-made target, cut them or replace with what the user
      would actually produce by hand.

## After shaping — update the spec

Set `audit_trail.structure` → `{"ran": true, "method": "top-down/pyramid",
"lead": "<the lead sentence as written>"}`. Then proceed to Voice (`voice-master`), then
Anti-slop (`humanizer`), then Render.
