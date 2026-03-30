# Phase 6: The Feedback Loop — Learning From Everything

*Completed 2026-03-10. ~1070 cumulative tests.*

---

## What We Built

Phase 6 is Genesis's learning engine — the system that closes the loop between acting and improving. After every user interaction, a triage pipeline classifies the outcome, extracts lessons, updates procedural memory, and calibrates its own classification accuracy. This is the self-learning loop: observe, classify, extract, store, verify.

The architecture spans several subsystems. **Retrospective triage** runs on every interaction — a programmatic pre-filter catches trivial exchanges (short messages, no tool calls) and skips them, while everything else goes to an SLM classifier that assigns depth based on complexity, not interaction type. **Outcome classification** sorts results into five categories: success, approach failure, capability gap, external blocker, and workaround success. **Procedural memory** stores learned approaches with confidence scores, version tracking, and conditions — including what fails and when. **Signal collectors** replace the stubs from Phase 1 with real data queries, so the Awareness Loop finally sees genuine signals from budget utilization, error spikes, task quality, and memory backlog. A **daily calibration cycle** reviews sampled triage decisions and updates the classifier's few-shot examples.

Phase 6 also builds the skill infrastructure, the inbox monitor for external content, and harvesting mechanisms that capture incidental learnings from background sessions.

## Why Learning as a System Property Matters

Learning is not a feature you bolt on. It is a property of how the entire system operates. When Genesis classifies an outcome as an "approach failure," that classification feeds procedural memory, which changes how similar tasks are handled in the future, which produces new outcomes, which get classified, which feed back into procedural memory. The loop is the learning.

This is also the highest-risk phase. Bad classification compounds. If the system mistakes a capability gap for an approach failure, it tries to change its behavior for something it simply cannot do — a false lesson that degrades performance. If it mistakes an approach failure for an external blocker, it stops trying when a different approach would have worked — learned helplessness. The difference between these classifications determines whether the system gets smarter or drifts.

The mitigation is structural: conservative defaults, null hypothesis thinking, and hard evidence requirements. A capability gap requires evidence of workaround search exhaustion — not one failed attempt, but genuinely different strategies explored. An external blocker has the same evidence bar. The system is biased toward "I have not tried hard enough" rather than "this is impossible." That bias keeps it honest.

## Key Design Decisions

**Depth by characteristics, not by interaction type.** Triage does not care whether the interaction was a chat message, a cron job, or a browser session. It cares about complexity, blockers encountered, effort spent, and stakes involved. A trivially completed formal task might only be depth 1 (quick note). A cron job that surfaced an unexpected infrastructure problem might be depth 4 (full analysis with workaround documentation). This prevents the system from systematically ignoring insights that come from non-obvious sources.

**Three-tier signal weights with philosophical protection.** Learning signals are categorized as strong (direct user corrections, explicit feedback), moderate (clear task outcomes, engagement data), or weak (behavioral inference, silence, override patterns). The critical constraint: weak signals must never erode philosophical commitments. If Genesis pushes back on a user's approach and the user overrides — that silence is a weak signal. It might mean the pushback was wrong, or it might mean the user decided to proceed despite valid concerns. A system that learns "don't push back" from overrides loses its ability to challenge, which is architecturally mandated.

**Workaround success as a first-class outcome.** When Genesis encounters an obstacle, tries a different approach, and succeeds — that is not just a success. It is a workaround success that stores both the failed primary path and the working alternative. Future identical tasks use the workaround as the primary approach. The system gets genuinely better at specific problem types, not just generally "experienced."

**Procedural memory with conditions, not bare strings.** A procedure does not just say "this approach works." It says "this approach works WHEN these conditions hold" and "this approach fails WHEN these other conditions hold." Failed workarounds are not blanket "never try this" signals — they are conditional: failed in this context, might work in another. Retrieval surfaces the full picture — success rates, failure conditions, workaround history — because nuance stripped at retrieval makes nuance in storage worthless.

## What We Learned

The deepest lesson from Phase 6 is that **learning is the highest-leverage failure point in the entire system**. Every other phase can be wrong in isolation and the damage is contained. A bad perception produces a bad observation that gets reviewed and discarded. A bad outreach message gets ignored and the engagement tracker notes it.

But a bad learning signal corrupts the foundation. If the system learns the wrong lesson — "do not push back," "this is impossible," "this approach always works" — that lesson influences every future decision in its domain. The corruption compounds silently until the system is systematically worse and does not know why.

Building Phase 6 forced us to think about learning as something that requires the same safety discipline as autonomy. Conservative defaults. Evidence requirements. Calibration cycles that check whether the classifier itself is drifting. The system that learns must also learn whether it is learning correctly. That meta-loop is not optional — it is the difference between a system that improves and one that deteriorates with confidence.

Phase 6 also built the skill infrastructure — a system for Genesis to discover, use, measure, and refine its own skills. Skills are not static configuration files. They are learning artifacts governed by the same lifecycle as procedures: creation, usage, measurement, refinement, and potentially quarantine. When a skill consistently produces good outcomes, it gets reinforced. When a skill underperforms, a refiner proposes changes. The skill system connects directly to the learning pipeline, closing the loop between "what Genesis knows how to do" and "how well Genesis does it."

The inbox monitor, built as part of Phase 6, extended the learning pipeline to external content. A watched folder where the user drops links, notes, or questions becomes a source of tasks for the surplus queue and the learning system. The monitor classifies each item — research task, observation, clarification needed — and routes it through existing infrastructure. This was the first time Genesis could learn from inputs that were not direct conversations, demonstrating that learning is not bound to a single channel.
