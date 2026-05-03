# Trust That Has to Be Earned

Most autonomous systems offer two trust modes: full approval or full autonomy.
You configure which operations get a free pass and which require confirmation.
The problem with this is that it treats trust as a configuration decision rather
than an evidence question.

Genesis takes a different approach. Trust is earned per category through demonstrated
competence, tracked as a running Bayesian posterior, and can regress. You don't
configure it. The evidence configures it.

---

## The Scenario

A new Genesis installation starts at L1 everywhere: simple tool use only. Health
checks, status queries, read operations. Nothing that initiates contact, nothing
that sends a message, nothing that commits code.

Over the first week, Genesis completes dozens of routine operations correctly:
triage cycles, health probes, memory extractions, recon runs. Each correct
operation updates the posterior for the relevant category. After enough evidence,
the posterior crosses the threshold for L2. Genesis is now authorized to run
known procedures without per-step approval.

A few more weeks of successful triage and routing decisions push it to L3: novel
task handling within earned categories, without explicit approval for each action.

Then it makes a wrong call. A Telegram message goes out when it shouldn't have.
The signal read as urgent wasn't. That's a correction. The Bayesian posterior drops.
A correction carries more weight per event than a success, by design, because the
system is biased against overconfident authority. Genesis ratchets back to L2 for
that category.

The regression is announced on Telegram immediately: what changed, from which level
to which, the updated posterior, and why. Then it starts earning back.

---

## How It Works

Trust is calculated as a Bayesian posterior with Laplace smoothing:

```
(successes + 1) / (successes + corrections + 2)
```

At zero interactions, the posterior is 0.50, which maps to L1. After a few clean
successes with no corrections: 0.75, which is L3 territory. After a correction,
the posterior drops, and if it crosses below a threshold, the level drops with it.

Two values are tracked separately. The earned level is the historical maximum and
never decreases. The current level can drop. Rebuilding from L2 to L4 after a
regression takes the same evidence it took the first time. There's no shortcut back.

Context matters too. Even with an L4 earned level, Genesis doesn't act at full
autonomy in an unsupervised background session. The effective level is
`min(earned_level, context_ceiling)`. The ceiling is L3 for background cognitive
work and L2 for isolated sub-agent execution. Trust earned under supervision doesn't
transfer automatically to unsupervised contexts.

---

## The Outcome

Early in a Genesis installation, more operations require approval. You see what the
system is doing and why. Over time, as evidence accumulates in categories where
Genesis has a clean record, approvals get rarer. When something goes wrong, it
doesn't silently continue at the same authority level. It steps back, announces the
regression, and starts rebuilding.

The trust level isn't a number you set. It's a number the system earns, and the
evidence behind it is visible.

---

## Why This Matters

Static autonomy settings fail in both directions. Too high: Genesis acts confidently
in domains where it hasn't demonstrated competence, and you only find out when
something breaks. Too low: every routine operation requires approval, which defeats
the purpose of having an autonomous system.

The Bayesian model handles this naturally. Strong performance in triage doesn't
accelerate the trust curve for outreach. Each domain earns independently, which
means Genesis can be trusted to handle health checks at L4 while still requiring
approval for outreach messages at L2. Trust is proportional, not global.

You don't have to decide upfront how much to trust Genesis. The evidence decides.

---

*For the implementation details behind this case study, see
[`docs/architecture/autonomy-deep-dive.md`](../architecture/autonomy-deep-dive.md).*
