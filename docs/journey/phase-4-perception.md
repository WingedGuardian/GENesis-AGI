# Phase 4: First Thoughts — From Measuring to Perceiving

*Completed 2026-03-07. Tests integrated into cumulative suite.*

---

## What We Built

Phase 4 is where Genesis starts thinking. The Awareness Loop (Phase 1) collects signals and classifies urgency. The routing layer (Phase 2) knows which models to use. But until Phase 4, no LLM has ever been called. The system has been measuring. Now it perceives.

The Reflection Engine is a layered pipeline: ContextAssembler gathers relevant information for the depth of thinking required. PromptBuilder selects and renders a template from a rotating pool. LLMCaller routes the request through the routing infrastructure. OutputParser validates the response against a depth-specific schema. ResultWriter stores observations and emits events.

Two depth levels are operational: **Micro** reflections handle routine ticks — quick structured extraction that tags signals, identifies entities, and produces a JSON summary. **Light** reflections handle moderate urgency — situation assessment, memory queries, drive-relevant interpretation, and recommendations. Deep and Strategic depths are stubbed, waiting for Phases 7 and beyond.

## Why the Jump From Measuring to Perceiving Matters

Collecting signals is not the same as understanding them. The Awareness Loop can tell you that error rates spiked at 2:00 AM and that the inbox has three unread items and that the last user interaction was six hours ago. Those are measurements. They answer "what" but not "so what."

Perception is the bridge. A micro reflection takes those raw signals and produces structured observations: "Error spike correlates with the cloud provider outage that started at 1:45 AM — this is an infrastructure event, not a code problem." A light reflection takes accumulated micro observations and produces a situation assessment: "The user has been inactive for six hours during their normal working time, there are three inbox items accumulating, and system health has recovered from this morning's outage — the most useful action is to prepare a summary for when they return."

This is where the principle of **code for structure, LLM for judgment** becomes concrete. The pipeline architecture — context assembly, prompt building, output parsing — is all code. Reliable, testable, deterministic. The actual interpretation — what these signals mean, what matters, what to do about it — is the LLM's job. Structure handles the plumbing; intelligence handles the thinking.

## Key Design Decisions

**Context assembly by relevance, not by budget.** The ContextAssembler gathers information based on what the reflection depth needs, not based on a token limit. Micro gets identity context plus the signal batch. Light adds the user profile, cognitive state, and relevant memories. If the assembled context is unexpectedly large for the depth, that is a signal the depth classifier should have escalated — not a reason to truncate. Quality is non-negotiable. Cost management belongs to the routing layer (cheaper models for cheaper tasks) and the surplus scheduler (how often to reflect, not how well).

**Rotating prompt templates to prevent mode collapse.** Each depth has multiple templates (three micro, three light) that rotate based on tick count or focus area. One micro template plays the analyst: systematic signal classification. Another plays the contrarian: actively looking for overlooked risks. A third plays curiosity: looking for novel patterns. This rotation prevents the system from settling into a single interpretive lens and missing things that lens would not catch.

**Identity context in every reflection.** SOUL.md (who Genesis is, ~1100 tokens) and user.md (the user's self-description) are loaded into every reflection prompt at 20B parameters and above. Smaller models doing extraction work do not need identity context. Larger models doing interpretation must have it — without it, reflections are technically competent but philosophically ungrounded. The system must know who it is and who it serves in order to make judgments, not just process data.

**Structured output with validation and retry.** LLM outputs are validated against depth-specific schemas. A micro reflection must produce tagged signals and anomaly flags. A light reflection must produce a situation assessment, relevant memories, and action recommendations. If the output is malformed, the system retries with error feedback (up to two retries), then accepts a degraded partial result rather than failing entirely.

## What We Learned

The biggest lesson from Phase 4 was about the **qualitative difference between data and perception**. Before this phase, Genesis had a sophisticated measurement infrastructure — signals, scores, thresholds, circuit breakers, degradation levels. All useful. All meaningless without interpretation.

The moment the first LLM call fired and produced an observation that connected two unrelated signals into a coherent insight, the system changed category. It was no longer a monitoring dashboard that happened to use AI. It was a system that noticed things. The difference is not incremental — it is a phase transition from instrument to perceiver.

The architectural lesson: test the system's behavior in aggregate and at boundaries, not the exact text the LLM produces. If you are asserting exact strings from an LLM, you are testing the wrong thing. Test that micro reflections produce valid schemas. Test that anomalies trigger escalation. Test that malformed outputs get retried. The intelligence is non-deterministic by nature — the scaffolding around it must not be.

Phase 4 also established the cognitive state summary infrastructure — a ~600-token regenerated summary of active context and pending actions, stored in the database and loaded into every subsequent reflection. This gives the system continuity: each reflection knows what the previous one was focused on. The summary is not a log (append-only, unbounded growth). It is a living document, regenerated periodically to reflect the current state. What Genesis is thinking about changes; the summary changes with it. This is how the system maintains a coherent self-model across sessions without relying on the full conversation history.
