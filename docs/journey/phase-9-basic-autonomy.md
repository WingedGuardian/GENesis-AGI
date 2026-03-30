# Phase 9: Earned Autonomy — Not Toggled, Demonstrated

*Completed March 2026. ~350 tests.*

---

## What We Built

Phase 9 is where Genesis stops being a system that waits for commands and becomes one that acts on its own — within carefully designed boundaries.

The autonomy system has four levels, each earned through demonstrated competence:

| Level | Scope | Behavior |
|---|---|---|
| **L1** | Simple tool use | Fully autonomous. No confirmation needed. |
| **L2** | Known-pattern tasks | Mostly autonomous. Checkpoint on novel elements. |
| **L3** | Novel tasks | Propose plan, execute with checkpoints, verify results. |
| **L4** | Proactive outreach | Threshold-gated with governance checks before sending. |

L5-L7 (self-modification, cross-system orchestration, identity evolution) are deferred to V5 — they require months of operational data to grant responsibly.

## Why "Earned" Matters

Every other AI agent system handles autonomy with a toggle. "Agent mode: on/off." That's the wrong abstraction. It's like giving someone full admin access on day one because they asked nicely.

Genesis's approach: autonomy is **per-category and competence-gated.** The system might be L3 for research tasks (it's demonstrated it can plan and execute reliably) but only L1 for financial operations (it hasn't proven it can handle that domain safely). Trust is granular, not binary.

And trust can regress:
- **2 consecutive corrections** in a category → drop one level, re-earn through demonstrated competence
- **1 user-reported harmful action** → drop to default across all categories, full re-earn required
- **Regression is always announced**, never silent. If Genesis loses trust, it tells you why.

In V3, autonomy levels are set by the user — there's no automatic progression. V4 adds evidence-based progression where the system can request higher autonomy in a category after demonstrating consistent competence. But even then, the user has final authority. Always.

## Key Design Decisions

**Context-dependent trust ceilings.** The same task gets different autonomy limits depending on *how* Genesis is executing it:

- **Direct session** (user is present): earned level, no cap
- **Background cognitive** (thinking between conversations): L3 maximum
- **Sub-agent** (delegated task): L2 for irreversible actions, earned for reversible
- **Outreach** (sending messages to the user): L2 until engagement data proves calibration

This prevents the failure mode where a system earns trust in supervised mode and then does something regrettable when unsupervised.

**Hard verification gate.** Every autonomous task completion must include: all tests pass, lint clean, diff review, structured explanation of changes, and before/after state comparison. This isn't a suggestion — it's architectural enforcement. The post-session hook validates these artifacts before marking a task complete. If verification fails, the task stays in-progress with the failure reason attached.

No "trust me, it worked." Show the evidence.

**Calibration-informed decisions.** Phase 8 accumulated prediction accuracy data — when Genesis says "80% confidence," is it historically right 80% of the time? Phase 9 injects this calibration history into autonomy decisions: "When you report 80% confidence on outreach decisions, you're historically right ~60% of the time. Adjust accordingly." This is the symbolic + neural pairing validated by research — the LLM does the reasoning, but hard data keeps it honest.

**Disagreement gates.** When cross-vendor model review disagrees with the primary model's assessment, the action blocks until resolved — either through a third model tiebreaker or escalation to the user with both assessments. Disagreement rates above 30% signal calibration problems that no amount of gating can fix. The system tracks this and surfaces it.

**Approval timeout and auto-reject.** When Genesis proposes an action requiring user approval, the proposal can't hang indefinitely. If the user doesn't respond within a configured window, the action is auto-rejected (not auto-approved). Safe defaults over convenient defaults.

## What We Learned

The fundamental insight from Phase 9: **autonomy is a trust relationship, not a capability.**

Genesis could technically do anything at any autonomy level — the LLM is capable regardless of what permission level it's running at. The autonomy system doesn't limit *capability*; it limits *authority*. And authority must be earned through evidence, not granted through configuration.

The other major lesson: **regression announcements matter as much as the regression itself.** When Genesis loses autonomy in a category, the fact that it tells you (and tells you why) is what maintains trust. Silent regression — where the system quietly becomes less autonomous without explanation — would erode trust faster than the original mistake.

Building an autonomy system forced us to think about failure modes that most AI systems ignore entirely: What happens when the AI is wrong? What happens when it's confidently wrong? What happens when it acts without supervision and the outcome is bad? Most systems treat these as edge cases. Genesis treats them as the core design constraint.

Phase 9 is where Genesis's philosophy becomes operational: **user sovereignty is absolute, autonomy is delegated, and trust is earned through demonstrated competence.** Everything else is implementation detail.
