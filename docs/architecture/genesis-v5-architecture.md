# Genesis V5 Architecture

**Status:** Designed | **Last updated:** 2026-03-25

> V4 builds the nervous system. V5 teaches it to evolve.

---

## 1. V5 Vision: Self-Evolution Within Guardrails

V4 gives Genesis coherent behavior through the LIDA cycle, evidence-driven
weight adaptation, and coordinated action selection. But V4's adaptations are
bounded -- weights shift within +/-20%, proposals require user approval,
and the system cannot modify its own learning rules or identity.

V5 asks: what happens when a system that has demonstrated reliable judgment is
trusted to modify itself?

The answer is not unlimited autonomy. It is graduated trust -- precisely
scoped self-modification authority, earned through demonstrated competence,
always revocable, with mandatory check-ins that prevent silent drift. The
tension is real: a system that can improve itself is more capable, but a
system that modifies itself without oversight is dangerous. V5 resolves this
tension not by choosing one side but by building the machinery for both --
capability and oversight -- to coexist.

Three capabilities define V5:

1. **Autonomy Progression (L5-L7)** -- graduated trust for self-modification,
   from tuning parameters to proposing identity changes.
2. **Hybrid Agent Protocol** -- when multi-agent coordination standards mature,
   the ability to work with and delegate to other agent systems.
3. **GWT Maturation** -- replacing V4's skeleton implementations with first-
   principles cognitive components informed by months of operational data.

### What V5 Requires

V5 is not a calendar milestone. It requires:

- 3+ months of V4 operational data with positive GWT marker trends
- Demonstrated V4 weight adaptation stability (no oscillation, no drift)
- Proven shadow mode validation pipeline
- User trust established through V4's propose-and-approve cycle

Building V5 without this evidence base would mean building on assumptions.
We have already seen what that produces -- V3's deliberate conservatism exists
because the alternative is premature sophistication that looks good on paper
and fails in practice.

---

## 2. Autonomy Progression: L5-L7

V3 implements four autonomy levels, each earned through demonstrated
competence:

| Level | Name | V3 Status |
|-------|------|-----------|
| L1 | Simple Tool Use | Active |
| L2 | Known Pattern Execution | Active |
| L3 | Novel Task Execution | Active |
| L4 | Proactive Outreach | Active |

V5 extends the hierarchy with three levels that grant progressively more
self-modification authority:

### L5 -- System Configuration

**What it unlocks:** Adjusting awareness loop thresholds, modifying signal
weights (within +/-20% bounds), adjusting drive weights, tuning decay rates,
salience thresholds, and timing parameters.

**Default behavior:** Propose only, user approves. Grows to bounded self-
adjustment within session after high confidence. Changes beyond bounds still
require approval.

**Key constraint:** Cannot restructure fundamental mechanisms (replacing the
urgency scorer algorithm, for instance). Only parameter tuning within existing
mechanisms.

### L6 -- Learning System Modification

**What it unlocks:** Adjusting review schedules (reflection frequency,
calibration timing), modifying salience calibration thresholds, tuning drive
weight adaptation rates, adjusting quarantine thresholds, modifying engagement
heuristic parameters.

**Default behavior:** Propose only, always user review -- even after high
confidence. L6 is never fully autonomous.

**Why permanent oversight:** Learning modifications affect how the system
learns. A feedback loop where the learning system modifies its own learning
rules can diverge. Human oversight breaks potential divergence cycles. This is
not a temporary restriction to be relaxed later. It is a permanent
architectural commitment.

### L7 -- Identity Evolution

**What it unlocks:** Proposing changes to core values, drives, and self-model.
Suggesting new identity dimensions. Recommending priority reordering.

**Default behavior:** Draft only, user decides. Never autonomous. No amount
of evidence or operational history changes this.

**Why never autonomous:** Identity is the foundation that everything else
builds on. A system that autonomously modifies its own identity can drift in
ways that are invisible until they are severe. The user must remain the
ultimate authority on who Genesis is. This is both a safety constraint and a
philosophical commitment: Genesis's identity is ultimately the user's call.

### Evidence Requirements

Higher levels demand exponentially more evidence:

| Requirement | L4 to L5 | L5 to L6 | L6 to L7 |
|-------------|----------|----------|----------|
| Successful executions at current level | 20+ | 30+ | 50+ |
| Consecutive corrections | 0 in last 4 weeks | 0 in last 8 weeks | 0 in last 12 weeks |
| Weeks of operation at current level | 8+ | 12+ | 24+ |
| User explicit acknowledgment | Required | Required | Required |
| Shadow mode at next level | 4 weeks | 8 weeks | N/A (L7 is always shadow) |

### Regression Triggers

Trust, once broken, is rebuilt slowly:

- **2 consecutive corrections at a level:** Drop one level, require re-earning
  through standard advancement criteria.
- **1 user-reported harmful action:** Drop to L1 for that category, full
  re-earn. Requires explicit user restoration.
- **System detects systematic error** (e.g., 5 ignored outreach in a row):
  Self-proposes regression for that category.
- **Monthly check-in not confirmed:** Pause to propose-only until user confirms.

### Silence is Not Approval

The system periodically asks: "I have been handling [category] autonomously
for [period] with [X% success rate]. Would you like me to continue, or do you
want to adjust my autonomy for this?"

If the user does not explicitly confirm within 7 days, autonomy pauses for
that category. This prevents the dangerous pattern where a system accumulates
authority through inaction -- where the user's silence is taken as consent.

### Context-Dependent Trust Ceilings

Earned autonomy is not a global permission. Context restricts but never
expands effective autonomy:

| Context | Maximum Effective Autonomy |
|---------|--------------------------|
| Direct user session | Earned level (no cap) |
| Background cognitive | L3 |
| Sub-agent (irreversible) | L2 |
| Sub-agent (reversible) | Earned level |
| Outreach | L2 until calibrated |

A system with L6 earned autonomy still caps at L2 for irreversible sub-agent
actions in background tasks. The user's presence is the safety net; when the
user is absent, constraints tighten.

### Per-Category Independence

L5 for task execution does not imply L5 for outreach. Each domain earns
autonomy independently based on its own track record. The skill applicator
pattern -- already tagged as `GROUNDWORK(skill-autonomy-graduation)` in V3 --
demonstrates the model: each capability domain has its own autonomy category
with independent level tracking.

---

## 3. Hybrid Agent Protocol

### The Current State

Genesis runs on a subprocess invoker (`CCInvoker`) for Claude Code sessions.
V3 evaluated both the Agent Client Protocol (ACP) and the Python Agent SDK
extensively, finding critical bugs in both: multi-session hangs, zombie process
leaks, MCP stream closures, CPU spin after disconnect, parser crashes.

V3 chose the practical path: stay with the working invoker, but define an
`AgentProvider` protocol abstraction so alternatives can be slotted in later
without changing anything above the invoker layer.

### V5 Revisit Criteria

The hybrid architecture activates when the ecosystem matures:

1. ACP multi-session per adapter works reliably
2. Python SDK custom MCP tools work beyond the timeout cliff
3. SDK resource leaks (CLOSE_WAIT, zombie processes) are resolved
4. SDK reaches a stable API (v1.0+)
5. Genesis has a concrete use case for a non-Claude provider -- not
   hypothetical, but real

### Architecture

```
          +------------------------+
          |    AgentProvider        |  (protocol, defined in V3)
          |    .invoke()           |
          |    .invoke_streaming() |
          |    .interrupt()        |
          +------+----------+-----+
                 |          |
      +----------+---+ +---+----------+
      | ACP Backend  | | SDK Backend   |
      | (any agent)  | | (Claude-only) |
      | + system     | | + hooks       |
      |   prompt via | | + tool gates  |
      |   _meta      | | + model swap  |
      | + model swap | | + interrupt   |
      +--------------+ +--------------+
```

The value of ACP is provider flexibility: 19+ agents swappable via registry.
The value of the SDK is deep integration: hooks, tool gating, session
management. V5 uses both through the common `AgentProvider` interface,
choosing the right backend for each task.

### Agent-to-Agent Protocol (A2A)

V5 also positions Genesis for agent-to-agent coordination -- standardized
protocols for Genesis to work with other agent systems. The immediate
application is the handoff pattern (transfer context to another agent). We
explicitly do not build swarm orchestration in V5 -- that complexity
requires further maturation of both Genesis and the ecosystem.

---

## 4. GWT Maturation: From Skeleton to First Principles

V4 builds the LIDA cycle with simple implementations at each step. V5
replaces them with sophisticated implementations informed by months of
operational data.

### Coalition Mechanism (ATTEND step)

V4: Signals compete individually. Temporal co-occurrence grouping (signals
that fire within the same tick are grouped, groups compete as units).

V5: Full coalition mechanism with embedding similarity. Signals cluster by
semantic relatedness using Qdrant, not just temporal co-occurrence. Coalitions
compete as units, producing richer attention patterns.

Requires: Signal co-occurrence and outcome data from V4 to calibrate
clustering parameters.

### Learned SELECT Preference Model

V4: Single CC session with structured prompt evaluates proposals.

V5: Learned preference model trained on proposal/outcome pairs. The system
develops judgment about which proposals tend to succeed based on accumulated
evidence, not just per-instance LLM reasoning.

Requires: Proposal/outcome pairs from V4 cycles to train on.

### Adaptive Cycle Cadence

V4: Fixed cadence (light cycles every 5 minutes, full cycles every 15-30
minutes).

V5: Cadence adapts to activity. High-signal periods trigger faster cycles.
Idle periods extend cadence to save compute. The cycle's own frequency becomes
a tunable parameter.

Requires: Frequency vs. quality data from V4 to determine optimal cadence
ranges.

### Dynamic Context Scoping

V4: Universal context package with fixed composition.

V5: Context scoped per session type. Reflection sessions get reflection-
relevant context. Outreach sessions get user-model-heavy context. The
workspace controller learns which context items are referenced by which
session types and adjusts.

Requires: Reference tracking per session type from V4.

### Meta-Learning on the Cycle

V5 applies the autoresearch pattern (hypothesize, execute, measure) to the
cycle's own parameters. The system experiments with its own configuration:
"What happens if I increase the workspace capacity from 7 to 9?" It measures
the result against GWT markers and reverts if quality degrades.

This is the most powerful and most dangerous V5 capability. It is gated by L6
autonomy (learning system modification), which always requires user review.
The system proposes experiments; the user approves them; the system runs them;
both evaluate results.

Requires: Enough V4 cycles to establish baseline metrics to experiment against.

### Stable Interfaces

The eight-step cycle structure, the interfaces between steps (signal packages,
workspace entries, proposals, decisions), the measurement framework, and the
intent state structure all remain stable from V4 to V5. V5 is component
upgrades, not a rearchitecture. This is by design: the skeleton is built to
be filled in, not rebuilt.

---

## 5. Post-V5 Horizon

These are ideas worth tracking but too early to design. Each includes the
trigger condition that would move it from "tracking" to "designing."

### Embedding Model Migration

Current: local model at 1024 dimensions for text-only embeddings. Cloud
fallback for resilience.

Future: higher-quality embedding model, ideally local and multimodal. Full
re-index at migration time.

**Not now because:** 1024 dimensions are the sweet spot for Genesis's content
(the accuracy curve flattens between 768-1024 for conversational context and
procedures). Cloud embedding APIs send all memory content to third parties --
unacceptable for the memory corpus. Frontier multimodal embedding models are
in preview or have restrictive licenses.

**Trigger:** A local multimodal embedding model with ~1024 dimensions,
permissive license, and competitive quality becomes available; or retrieval
quality becomes a measurable bottleneck.

### Browser Automation with Live View

Current: Playwright via MCP server, process-level isolation, no live view.

Future: Browser automation with optional live view (watch Genesis navigate in
real-time) and human takeover (pause agent, take manual control, resume).

**Trigger:** Genesis dispatches browser tasks autonomously and the user wants
visibility; or a multi-agent architecture requires session isolation.

### Visual Workspace Dashboard

Current: Neural monitor dashboard showing health metrics, circuit breakers,
cost tracking, error logs.

Future: A richer workspace view where active tasks, research findings,
reflections, and outreach items are visual cards the user can rearrange,
annotate, and dismiss.

**Trigger:** Dashboard becomes a primary interaction surface; or user feedback
indicates need for better visibility into Genesis's working state.

---

## 6. Activation Criteria and Safety Invariants

### V5 Activation

| Prerequisite | Threshold |
|---|---|
| V4 operational data | 3+ months |
| GWT markers | Trending positive across all six |
| V4 weight adaptation | Stable (no oscillation or drift) |
| Shadow mode pipeline | Proven through V4 feature rollouts |
| User approval | Required per V5 feature |

### Safety Invariants (Non-Negotiable)

These hold across all V5 capabilities, regardless of earned autonomy level:

1. **L7 is never autonomous.** Identity evolution is always propose-and-wait.
   No exception, no override, no earned bypass.

2. **L6 is always reviewed.** Learning modifications that affect the learning
   system itself always require user review. Bounded self-adjustment (+/-20%)
   is possible for minor tuning; fundamental changes always need approval.

3. **Context ceilings are non-negotiable.** Background tasks cannot escalate
   beyond L3 regardless of earned level. The user's presence is the safety
   net.

4. **Regression is aggressive.** 2 corrections drops a level. 1 harmful action
   drops to L1. The cost of regression is low (re-earn with evidence). The
   cost of unchecked autonomy failure is high.

5. **Monthly check-ins are mandatory.** Autonomy is an ongoing grant, not a
   permanent award. The system cannot assume continued approval from past
   approval.

6. **Per-category independence.** No capability domain inherits trust from
   another. Each earns autonomy on its own track record.

7. **Feature-flag per level.** L5, L6, and L7 can be independently enabled
   or disabled without affecting lower levels. If any level causes problems,
   it can be reverted without touching the rest.

8. **User sovereignty is absolute.** This is not a V5 invariant -- it is the
   foundational principle that every version inherits. The user can revoke any
   autonomy instantly, override any decision, and modify any parameter. Genesis
   proposes; the user disposes.

---

## 7. References

### V5 Feature Specifications

- `docs/plans/v5-autonomy-progression-spec.md` -- L5-L7 detailed design,
  evidence requirements, regression triggers, context ceilings
- `docs/plans/v5-hybrid-agent-protocol.md` -- ACP/SDK evaluation findings,
  revisit criteria, architecture
- `docs/plans/post-v5-horizon.md` -- embedding migration, browser automation,
  visual workspace

### Cognitive Architecture

- `docs/architecture/genesis-v4-architecture.md` (GWT sections) —
  GWT/LIDA design, V4-to-V5 progression table, measurability framework
- `docs/architecture/genesis-v4-architecture.md` -- V4 architecture,
  LIDA cycle, six V4 features

### Foundation Documents

- `docs/architecture/genesis-v3-vision.md` -- core philosophy, four drives,
  identity model
- `docs/architecture/genesis-v3-autonomous-behavior-design.md` -- primary
  design, autonomy hierarchy, loop taxonomy

### Research

- Baars, B.J. (1988). *A Cognitive Theory of Consciousness.* Cambridge
  University Press.
- Franklin, S. et al. *LIDA: A Systems-level Architecture for Cognition,
  Emotion, and Learning.*
- Karpathy, A. *autoresearch* (2026). Triangular experiment loop -- applied
  to cycle meta-learning in V5.
- Lambert, N. "Lossy Self-Improvement." Interconnects AI (2026). Friction is
  structural, not engineering-solvable.
