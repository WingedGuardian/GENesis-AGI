# Phase 7: Dreaming — Consolidation Through Reflection

*Completed 2026-03-13. Tests integrated into cumulative suite.*

---

## What We Built

Phase 7 is where Genesis sleeps — not literally, but functionally. Deep reflection is a periodic, high-quality reasoning session that does what lighter processes cannot: consolidate scattered observations into coherent patterns, deduplicate memories, resolve contradictions, review surplus outputs, assess skill effectiveness, and regenerate the cognitive state summary that gives the system continuity between sessions.

The implementation uses a Sonnet-class model triggered by the Awareness Loop when composite urgency crosses the Deep threshold. Critically, deep reflection only runs when there is pending work — accumulated observations needing consolidation, new retrospectives needing lesson extraction, surplus staging items needing review. It is adaptive, not calendar-driven. The system does not dream on a schedule. It dreams when there is something worth dreaming about.

This phase also introduces the mandatory weekly self-assessment (six dimensions, real data sources, structured output), weekly quality calibration (sampling recent outputs to detect standards drift), and learning stability monitoring (procedure quarantine, contradiction detection, regression signals). Background Claude Code sessions become the primary mechanism for deep reflection, with per-session-type configurations for MCP servers, hooks, and skill injection.

## Why Dreaming Matters

Micro and light reflections are fast, cheap, and frequent. They process signals in real time and produce observations. But they cannot step back. They cannot see that three observations from Tuesday, two from Thursday, and one from Saturday are all manifestations of the same underlying pattern. They cannot notice that two stored procedures contradict each other. They cannot evaluate whether the system's learning velocity is healthy or whether its outreach calibration is drifting.

Deep reflection is the periodic consolidation that gives the lighter processes coherence. The parallel to human sleep is not accidental. Research on memory consolidation shows that sleep is when the brain reorganizes what it learned during the day — strengthening important connections, weakening irrelevant ones, integrating new information with existing knowledge. Genesis's deep reflection does the same thing: it takes the raw material produced by continuous micro and light reflections and turns it into structured knowledge.

Memory consolidation in deep reflection is not housekeeping. It is the primary defense against memory pollution. Deduplication, merging overlapping observations, resolving contradictions, pruning stale links — these are safety operations that keep the system's knowledge base sharp rather than noisy. Without periodic consolidation, the memory store would accumulate redundancies and contradictions that degrade retrieval quality over time.

## Key Design Decisions

**Adaptive scheduling, not cron.** The v2 dream cycle ran 13 fixed-interval cron jobs every night regardless of whether there was anything to process. Phase 7 replaces this with urgency-driven scheduling: deep reflection fires when the Awareness Loop detects enough pending work to justify a Sonnet-class call. Jobs with no pending work are skipped entirely. A single structured call with comprehensive context replaces many small jobs running blind.

**Weekly self-assessment with real data.** Every week, Genesis evaluates itself across six dimensions — reflection quality, procedure effectiveness, outreach calibration, learning velocity, resource efficiency, and blind spots. Each dimension draws from concrete data sources: observation retrieval counts, procedure success rates, outreach engagement ratios, topic distribution analysis. The assessment prompt explicitly forbids confabulation — if the data is insufficient for a dimension, the system reports "insufficient data" rather than generating plausible-sounding fiction.

**Learning stability as a first-class concern.** Deep reflection includes explicit stability monitoring. When a procedure's success rate drops below 40% after three or more uses, it gets quarantined — excluded from retrieval but not deleted, in case circumstances change. When observations contradict each other, deep reflection resolves them: keep the one with stronger evidence, merge them into a nuanced replacement, or flag for user review when evidence is ambiguous. When procedure effectiveness trends downward for two consecutive weeks, a learning regression event fires and enters the cognitive state, making the regression visible to every subsequent reflection.

**Cognitive state regeneration.** After deep reflection, the system regenerates a ~600-token summary of active context and pending actions. This summary is loaded into every subsequent reflection and conversation, providing continuity. It answers the question "what was I focused on?" without requiring the system to re-derive its context from raw memory every time.

## What We Learned

The fundamental insight from Phase 7 is that **intelligence requires periodic consolidation, not just continuous processing**. A system that only processes signals in real time is reactive. A system that periodically steps back, reviews what it has learned, resolves its own contradictions, and evaluates its own effectiveness is reflective.

The architectural lesson is about the relationship between depth and frequency. Micro reflections are cheap and constant — the system's peripheral vision. Light reflections are moderate and signal-driven — the system's focused attention. Deep reflection is expensive and periodic — the system's capacity for genuine introspection. Each layer depends on the ones below it, and the entire stack only works when the consolidation layer is functioning. Without dreaming, observations accumulate without integration, contradictions go unresolved, and the system's self-model slowly diverges from reality.

Building the self-assessment framework also taught us something about honesty. The first assessment scored 0.17 out of 1.0. That number was uncomfortable but accurate — it reflected a system in its first week of operation with most feedback loops not yet active. A system that would have reported 0.8 in the same conditions would have been useless. The value of structured introspection is proportional to its honesty.

The skill evolution system, also part of Phase 7, demonstrated a principle that applies to the whole architecture: things that improve themselves are qualitatively different from things that are maintained externally. When deep reflection analyzes skill effectiveness, identifies underperforming skills, and triggers refinement proposals — the system is editing its own instructions based on evidence. The proposals are governance-gated (minor changes auto-apply, moderate and major changes require review), but the initiative comes from the system, not from a developer noticing a problem. This is the beginning of genuine self-improvement: not just learning facts, but improving the processes that generate those facts.
