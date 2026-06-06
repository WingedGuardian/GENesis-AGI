# Memory Quality Gates — Honest Scope

**Last updated:** 2026-06-06 (memory immune system PR 1)

This document explicitly states what our memory quality gates catch and
don't catch. Inspired by anneal-memory's "Honest scope" pattern — the
value is in naming the gaps, not hiding them.

## What Our Gates DO Catch

- **Source-overlap verification** — extractions that can't be grounded
  in the source transcript get confidence demoted by 0.3 and tagged
  `source_unverified`. No LLM call — pure lexical overlap check.
  *(Added: PR 1)*
- **Cross-session claim dedup** — FTS5 keyword search + Jaccard overlap
  (≥0.70) catches near-duplicate extractions across sessions before
  storage. *(Added: PR 1)*
- **Exact duplicate memories** — structural dedup in `store.py`
- **Sub-threshold confidence** — extractions below 0.4 confidence routed
  to FTS5-only (enforced)
- **Observation/reflection confidence gates** — min 0.5 confidence,
  enforced (was 0.3 shadow-mode). *(Activated: PR 1)*
- **Adversarial synthesis challenge** — different-provider LLM (Kimi)
  reviews synthesis output (DeepSeek) for information loss before
  deprecating originals. Blocks on FAIL. *(Added: PR 1)*
- **Adversarial entity challenge** — different-provider second opinion
  on entity "duplicate" verdicts. Both must agree for deprecation.
  *(Added: PR 1)*
- **Catastrophic-shrink gate** — synthesis <50% of originals' combined
  length is blocked. *(Added: PR 1)*
- **Confidence inheritance cap** — synthesis confidence uses median of
  sources with 0.85 ceiling, preventing inflation cascade. *(Added: PR 1)*
- **Trivial interaction filter** — prefilter skips <100 tokens + 0 tool calls
- **Subsystem write quarantine** — ego/triage/reflection writes are
  FTS5-only, excluded from vector retrieval
- **User model delta gate** — 0.90 confidence threshold (enforced)
- **Dream cycle cluster limits** — clusters >10 members skipped
- **Memory preflight** — dream cycle aborts at <256MB available RAM

## What Our Gates DON'T Catch

### Sycophancy and bias

- **LLM self-scoring** — extraction LLM assigns its own confidence.
  Positive-valence interactions systematically get higher confidence.
  Source-overlap checks grounding but not sycophantic framing.
- **Triage positive-valence bias** — interactions where user praised
  the response get systematically higher triage depth, biasing the
  learning pipeline toward feel-good interactions.
- **Procedure learning bias** — triage over-weights positive
  interactions, so procedures are extracted disproportionately from
  happy interactions — learning "what pleases" rather than "what works."

### Memory quality

- **Fact vs. opinion conflation** — "Genesis uses SQLite WAL" and
  "Genesis's memory system is sophisticated" are stored identically.
  No structural marker distinguishes verifiable facts from judgments.
- **Stale fact consolidation** — adversarial review now checks for
  temporal conflicts, but relies on LLM judgment to detect them.
  Structural temporal markers are not compared.
- **Slow vocabulary rotation** — same sycophantic claim restated with
  different words each session can evade lexical dedup if Jaccard drops
  below 0.70.

### Structural

- **Wing/room isolation** — cross-wing scan (PR 2) detects and links
  similar memories across wings, but does not merge them. Conflicting
  facts across wings are now linked as `contradicts` but resolution is
  manual. *(Partially addressed: PR 2)*
- **Connection pass as amplifier** — retrieval diversity penalty (PR 2)
  collapses echo clusters at retrieval time, but the underlying link
  density is unchanged. *(Partially addressed: PR 2)*
- **Rollback trigger** — dream cycle now flags runs with >50% blocked
  syntheses via `logger.critical()`. Manual rollback still required.
  *(Partially addressed: PR 2)*

### External

- **Adversarial transcript injection** — if a transcript contains
  manipulative content, extraction has no structural defense beyond
  source-overlap grounding.
- **Multi-session coordinated injection** — if multiple sessions
  independently reinforce the same false claim, confidence compounds.

## Known Bypass Patterns

These are specific ways the defenses can be circumvented. Documenting
them is part of the design — they represent known limitations, not bugs.

- **Semantic contradictions** — structural checks detect lexical
  similarity but not semantic opposition. "X works well" and "X doesn't
  work" share terms but contradict. Requires LLM judgment to detect.
- **Confidence ceiling arbitrage** — the 0.85 ceiling on synthesis
  confidence can be reached by any cluster with median ≥ 0.85. Over
  many cycles, most synthesized memories converge to 0.85.
- **Challenge prompt gaming** — adversarial prompts can theoretically
  be influenced by the content they review. Mitigated by using
  different providers, but not eliminated.

## Defense Changelog

| Date | PR | What Changed |
|------|-----|-------------|
| 2026-06-06 | Immune System PR 1 | Source-overlap, claim dedup, confidence cap, adversarial challenge (synthesis + entity), shrink gate, shadow gates activated |
| 2026-06-06 | Immune System PR 2 | Triage counter-bias, retrieval diversity penalty, cross-wing scan, temporal conflict detection, rollback flagging, activation-aware decay |
