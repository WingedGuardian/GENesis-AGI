# Competitive Landscape: Harness Engineering (2026-04-02)

Research evaluation of six external sources against Genesis architecture.
Conducted as part of the V3→V4 transition planning.

## Sources Evaluated

1. **Claude Code Source Leak** — NPM package exposed readable TypeScript source
2. **HackerNoon: Deterministic Navigation** — 14-index system, 100% accuracy on 500-page specs
3. **Karpathy autoresearch** — Autonomous LLM training loop (63.5k GitHub stars)
4. **Meta-Harness (Stanford/MIT)** — Self-evolving agent harnesses via coding agent proposer
5. **Matt Berman Analysis** — Video breakdown of Meta-Harness implications
6. **OpenClaw Memory-QMD** — Competitor memory system with reranking and query expansion

## Key Findings

### Claude Code Architecture (Source 1)

CC is a full agent runtime — CLI parser → query engine → LLM API → tool
execution loop → terminal render. Built on Bun + TypeScript + React.

**Unreleased features behind flags:**
- Multi-agent coordinator (team management, agent tools)
- Backend routing (AWS Bedrock, Google)
- Team memory synchronization

**Implication:** Some Genesis capabilities (multi-session orchestration, memory
sync) will eventually overlap with CC-native features. Design for composability
— delegate to CC's native capabilities when they ship, augment where they don't.

### Deterministic Knowledge Navigation (Source 2)

Paper: "Skill Without Training" (Chudinov 2026). Key results:
- 100% accuracy vs 70% baseline on 246 questions across 500-page spec
- 7-15x token reduction via 14 pre-compiled indices
- Ontological routing: same keyword → different reading plans based on
  WHAT/WHY/HOW/WHEN/WHERE intent

**Directly implemented:** Ontological routing added to Genesis `memory_recall`
in `src/genesis/memory/intent.py`. Rule-based intent classification + RRF
scoring bias. V4 will explore compiled codebase indices.

### Autoresearch Pattern (Source 3)

Karpathy's framing: "you are programming the `program.md` files that provide
context to the AI agents." The human writes meta-prompts, the AI evolves
within bounds.

**Validates:** Genesis's CLAUDE.md/SOUL.md as program, AI as executor. The
pattern of "markdown as organizational DNA" is becoming mainstream. Genesis
extends this with reflection, learning, and graduated autonomy.

### Meta-Harness: Self-Evolving Harnesses (Sources 4, 5)

Paper: "Meta-Harness: End-to-End Optimization of Model Harnesses" (Lee et al. 2026).

Key results:
- 6x performance gap from harness changes alone (same model)
- 10M tokens of diagnostic context per step vs max 26K for prior methods
- Filesystem-based full history — proposer navigates via grep/cat
- #2 on TerminalBench-2 for Opus 4.6 (76.4%), #1 for Haiku 4.5 (37.6%)
- Harness improvements transfer across models

**Critical insight for Genesis:** Compressed feedback (summaries of summaries)
removes the signal needed to trace failures to root causes. Full data access
via tools outperforms curated prompt injection.

**Directly implemented:** Reflection engine now includes data pointers —
file paths, MCP tool names, database query hints — so reflection sessions
can navigate to raw data instead of relying on pre-digested summaries.

**V4/V5 implications:**
- Ego sessions as "proposer" — propose system prompt changes, evaluate, keep/discard
- Meta-Harness-style optimization loop for Genesis behavior

### OpenClaw Memory-QMD (Source 6)

Competitor memory system with local GGUF reranking model.

**Gaps identified:**
- **Reranking** — Genesis lacks a model-based re-scoring step after retrieval.
  OpenClaw uses BGE-Reranker via node-llama-cpp. For Genesis, Ollama-served
  reranker would be the right approach. Tagged as V4.
- **Query expansion** — Generating related terms before search. Directly
  implemented via tag co-occurrence index in `src/genesis/memory/intent.py`.
- **Citation provenance** — returning source attribution with results. Genesis
  has provenance in RetrievalResult but doesn't surface it in prompt injection.
  Tagged as V4.

### CC 2.1.88 Source Code Examination (Source 7)

Detailed examination of the leaked v2.1.88 source (1,900 TypeScript files,
512K lines). Key findings beyond the initial surface-level analysis:

**KAIROS — Always-On Background Agent (150+ references):**
- Tick-based autonomous daemon that decides independently whether to act
- 15-second blocking budget per decision cycle
- Append-only daily logs the agent cannot self-erase
- Three exclusive tools: push notifications, file delivery, PR subscriptions
- autoDream sub-component for memory consolidation during idle time
- Gated behind internal flags, not yet shipped publicly
- **Direct overlap with Genesis awareness loop + reflection engine**

**autoDream — Memory Consolidation (already active):**
- Three-gate trigger: 24h since last, 5+ sessions, consolidation lock
- Four phases: Orient → Gather → Consolidate → Prune
- MEMORY.md hard cap: 200 lines / ~25KB with silent truncation
- Genesis status: MEMORY.md at 149 lines, safely under cap. Architecture
  is correct — lightweight index in MEMORY.md, content in separate files
  that dream cycle doesn't touch.

**Operational Limits:**
- Auto-compaction at ~167K tokens (destructive, 5 strategies). Less
  relevant with Opus 1M context, but Sonnet sessions still hit this.
- 2,000-line file read ceiling — beyond this, the agent hallucinates.
  Validates our 1000 LOC hard cap.
- 14 tracked cache-break vectors (not publicly documented)
- Silent Opus-to-Sonnet downgrade on server errors — risk for autonomous
  sessions depending on Opus-level reasoning

**Multi-Agent Architecture:**
- Three subagent models: Fork (child), Teammate (parallel), Worktree (isolated)
- COORDINATOR_MODE: multi-agent swarm with mailbox routing
- ULTRAPLAN: remote Cloud Container with 30-minute Opus planning window

**Other Notable Findings:**
- Undercover mode for Anthropic employees (strips attribution, hides codenames)
- Anti-distillation: fake tool injection + cryptographically signed summaries
- 108 gated feature modules total
- Model codenames: Fennec=Opus 4.6, Capybara=new model family, Numbat=unreleased
- Opus 4.7 and Sonnet 4.8 referenced in forbidden strings list

## Competitive Positioning

| Dimension | Genesis | CC (native) | OpenClaw | Meta-Harness |
|-----------|---------|-------------|----------|--------------|
| Memory | SQLite + Qdrant + observations + CC memory | CLAUDE.md + session + user + team | BM25 + GGUF reranker | N/A (benchmark-focused) |
| Multi-agent | Live (cc_relay, task executor, surplus) | Behind flags | N/A | Claude Code as proposer |
| Autonomy | Graduated (autonomy manager + approval gates) | Binary (permissions) | N/A | Fully autonomous search |
| Reflection | Awareness loop + reflection engine | None | N/A | N/A |
| Self-improvement | Planned (V4 ego) | None | None | Core capability |
| Retrieval quality | FTS5 + Qdrant + intent routing + expansion | N/A | + reranking | N/A |

## Action Items by Phase

### V3 (Implemented)
- Ontological routing in memory_recall (intent classification + scoring bias)
- Query expansion via tag co-occurrence
- Reflection engine: fix observation truncation, add data pointers, reorient prompts

### V4 (Design Input)
- Model-based reranking (OpenClaw insight) — in-process cross-encoder ruled out
  (CPU-bound on 7-core Xeon E5-2680, load already hits 2x core count; 300-600ms
  of pure CPU per rerank pass with no GPU). Ollama also wrong serving layer
  (optimized for generative models, not cross-encoders). Best candidates:
  Jina Rerank API (zero local compute, ~$0.0001/call) or measure whether
  intent routing + query expansion (V3) are sufficient before adding complexity.
  Revisit 2026-04-09.
- Auto-compiled codebase indices (HackerNoon insight)
- Ego sessions as Meta-Harness-style proposer
- Citation provenance in prompt injection
- cc_relay composability with CC's upcoming coordinator mode + KAIROS
- Silent model downgrade detection for critical autonomous paths
- Study autoDream 4-phase process for memory system compatibility

### V5 (Long-term)
- Full Meta-Harness-style optimization loop for Genesis behavior
- Self-evolving system prompts
- KAIROS composability — when shipped, determine whether Genesis delegates
  perception to it or maintains independent awareness loop

## References

- Chudinov, Y. (2026). "Skill Without Training: Deterministic Knowledge Navigation
  for LLMs over Structured Documents." DOI: 10.5281/zenodo.18944351
- Lee, Y., Nair, R., Zhang, Q., Lee, K., Khattab, O., Finn, C. (2026).
  "Meta-Harness: End-to-End Optimization of Model Harnesses." arXiv: 2603.28052
- Karpathy, A. (2026). autoresearch. github.com/karpathy/autoresearch
- OpenClaw. Memory-QMD documentation. docs.openclaw.ai
