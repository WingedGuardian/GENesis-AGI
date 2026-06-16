# Gate 2 — the verification policeman

After Render, before the user ever sees it, a **fresh-context adversarial reviewer** checks the
rendered artifact against the *frozen acceptance criteria* and the quality bars, and returns
**PASS or FAIL**. A FAIL loops back into the pipeline. This is the enforcement core: a skipped
or lazy stage surfaces here as a criterion the artifact fails, and it bounces.

Discipline (from superpowers `verification-before-completion`): **evidence before claims; never
trust a "looks done" feeling; re-read the requirements line by line.** The reviewer proves each
verdict with a quote from the actual file — it does not vibe-check.

## Dispatch a FRESH subagent

Run the verifier as a **separate agent with no drafting context** (Agent tool,
`subagent_type: general-purpose`). It must not have "seen what it expected to see." Pass only
the paths; it reads everything itself:

```
You are an adversarial reviewer — a McKinsey engagement manager at 2am before a client
presentation. You have ZERO context on how this was produced. Your job is to FAIL anything
that wouldn't survive the recipient. Default to FAIL when uncertain.

Do these IN ORDER:
1. Read the ORIGINAL brief at: {spec.brief_path}    (the real source, not a summary)
2. Read the frozen acceptance criteria + audit trail at: {spec_path}
3. Confirm {spec.rendered_path} is REALLY a {spec.format} file (not markdown with a
   .pdf name) by checking its bytes — do NOT rely on `file`, which isn't always installed.
   Portable checks: PDF starts with `%PDF` (`head -c4`), or `pdfinfo <path>` / Python
   `import fitz; fitz.open(path).page_count`; DOCX/PPTX/XLSX are ZIP (`PK` magic). If the
   bytes don't match the claimed format, that alone is a FAIL:render.
4. Open and read {spec.rendered_path} in full.

Then judge, with EVIDENCE (a quote/line ref from the artifact) for every verdict:

A. ACCEPTANCE CRITERIA — for EACH criterion in the spec: PASS or FAIL + the evidence that
   satisfies or violates it. A failed `must` criterion fails the whole deliverable.
B. QUALITY BARS (grade each Green / Yellow / Red):
   - Format correct for the audience (no raw markdown for external work)
   - Leads with the answer (first ¶/slide states the conclusion; ghost-deck test passes)
   - Authenticity — CALIBRATE to spec.authenticity_target, do not apply a universal "human-made" bar:
       * "human-made" → enforce hard: voice applied (per audit_trail.voice) AND no document-level
         AI tells (structure-altitude.md: meta-narration, equal-length sections, exhaustive-
         coverage, table overload, absent point of view, formulaic open/close). Any tell = FAIL.
       * "ai-assisted-ok" → do NOT fail for reading polished or AI-assisted; that may be exactly
         right. Still require it on-voice where it matters and free of obvious slop, but polish is
         acceptable, even desirable. Judge it on substance, format, altitude, and fidelity.
   - Every claim supported; numbers specific; NO hallucinated facts, sources, or citations
     (spot-check any citation against the brief/source — if a cite can't be grounded, FAIL)

Output EXACTLY this shape:
  GRADE: GREEN | YELLOW | RED
  RESULT: PASS | FAIL
  CRITERIA:
    - AC1: PASS|FAIL — <evidence>
    - ...
  TOP FIXES (only if not PASS, ranked, each tagged to the stage that must re-run):
    - FAIL:{draft|structure|voice|antislop|render} — <what's wrong and exactly how to fix>
  ONE THING THAT WORKS: <so it's preserved on the next pass>

RESULT is PASS only if every `must` criterion passes AND no quality bar is Red.
```

## Loop-back (bounded)

The orchestrator parses the verdict:

- **PASS** → set `audit_trail.verify = {iterations, result:"PASS", grade}`, set
  `status: "verified"`, advance to Gate 3.
- **FAIL** → take the tagged fixes, re-run *only the named stage(s)* (e.g. `FAIL:structure`
  → redo Structure then Render), then re-verify with a **new fresh subagent**. On re-runs,
  feed the prior failures in as **verbal feedback** ("last pass failed because X; fix that"),
  not as a diff — verbal error descriptions regenerate better.
- **Bound: 2 cycles.** If it still fails after 2, **stop and escalate to the user**: show the
  outstanding failures and ask how to proceed. Do NOT spin a third time, and do NOT lower the
  bar to force a pass. Record `audit_trail.verify.result = "FAIL"` and leave `status` at
  `rendered_unverified` (the Stop-hook keeps the gate closed until the user decides — they can
  direct a fix or `cancel`).

## Why fresh + adversarial + evidence-backed

- **Fresh context** defeats confirmation bias — the drafting session can't pass its own work.
- **Re-reads the original brief**, so a lazy Intake can't poison the gate with a soft summary.
- **`file`-checks the artifact**, so "I rendered a PDF" can't be a markdown file in disguise.
- **Per-criterion + quoted evidence** makes a silently-dropped requirement impossible to hide
  and a fabricated audit-trail entry hard to sustain.

If Gate 2 ever passes something a human still reads as AI-made or wrong-format, the bar is
miscalibrated — fix this checklist and `structure-altitude.md`, not the mechanism.
