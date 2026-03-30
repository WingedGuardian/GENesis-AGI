# Phase 1: The Metronome — Building the Awareness Loop

*Completed 2026-03-03. 117 tests.*

---

## What We Built

The Awareness Loop is Genesis's heartbeat — a 5-minute tick that collects signals from every source (inbox, health monitors, recon feeds, calendar events), scores their composite urgency, and classifies what depth of reasoning the situation requires.

The counterintuitive design decision: **this entire system costs zero LLM tokens.**

Every signal collector is programmatic. The composite scorer is pure math. The depth classifier is threshold-based. The only thing the Awareness Loop produces is a signal — "the system should think at depth X about Y" — and that signal gets consumed by the Reflection Engine (Phase 4), which is where the actual LLM reasoning happens.

## Why Zero LLM Cost Matters

Most AI agent systems burn tokens constantly. They use LLMs for everything — scheduling, signal classification, priority scoring. That means every background tick costs money and introduces latency.

Genesis takes the opposite approach: **code handles structure; LLMs handle judgment.** The Awareness Loop is pure structure. It doesn't need to *reason* about whether a signal is important — it has weighted scores and thresholds for that. What it needs to do is reliably, cheaply, and frequently scan the environment and decide whether reasoning is warranted.

This means Genesis can tick every 5 minutes indefinitely at zero cost. The expensive reasoning only fires when the signals justify it.

## Key Design Decisions

**Hybrid event-driven + calendar guardrails.** The 5-minute tick is the baseline, but critical events bypass it entirely. If something urgent arrives, Genesis doesn't wait for the next tick — it escalates immediately. Calendar guardrails enforce minimum and maximum intervals to prevent both thrashing and neglect.

**Fixed signal weights.** In V3, the weights that determine how much each signal source matters are fixed — set once in the design doc, not adapted at runtime. Adaptive weights are a V4 feature that requires operational data to tune. We shipped with conservative defaults rather than building adaptation infrastructure we couldn't calibrate yet.

**Three categories of scheduled work.** The Awareness Loop coordinates three distinct types of scheduling:
1. **Event-driven reflection** — adaptive, signal-triggered, with calendar floors as safety nets
2. **Genesis's own rhythms** — internal cadence timers (morning report, calibration cycles)
3. **User-scheduled crons** — recon monitoring, future cron infrastructure

Each category has different governing principles but shares a single coordinator. This prevents scheduling conflicts and ensures the system has one coherent view of what needs attention.

**Depth classification, not action classification.** The Awareness Loop doesn't decide *what to do* — it decides *how hard to think about it.* The output is a depth level (Micro, Light, Deep, Strategic), not a task. This separation keeps the loop simple and testable while leaving judgment to the Reflection Engine.

## What We Learned

The biggest lesson from Phase 1 was about **the value of doing less with more discipline.** It would have been easy to make the Awareness Loop smarter — add an LLM to classify signals, use ML to predict urgency, build adaptive thresholds. Instead, we built a metronome. Simple, predictable, zero-cost, always running.

That simplicity paid dividends in every subsequent phase. When Phase 4 (Reflection Engine) was built, it could trust that the depth signal it received was reliable and cheap. When Phase 6 (Learning) was built, it could observe the Awareness Loop's behavior without worrying about confounding variables from LLM non-determinism.

The metronome doesn't think. It ticks. And that's exactly what it should do.
