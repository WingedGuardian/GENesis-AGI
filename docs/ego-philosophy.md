# Genesis Ego Philosophy

**Status:** Active | **Last updated:** 2026-06-02

The ego is Genesis's executive function — the part that decides what to
DO. This document defines the dual-ego architecture, domain boundaries,
and coordination protocol.

---

## Two Perspectives, One System

The user ego and genesis ego are two lenses on the same system, not two
independent minds. They exist because a single ego conflated user-domain
and infrastructure-domain thinking — it would see infrastructure alerts
alongside career goals and produce muddled proposals that served neither
domain well. Separation forces specialization.

- **User Ego (CEO)**: Sees the user's world — goals, events, contacts,
  conversations. Creates value for the user. Channels Cooperation
  (deliver results, anticipate needs) and Curiosity (connect dots across
  the user's world).

- **Genesis Ego (COO)**: Sees the system's world — health, performance,
  costs, infrastructure observations. Keeps Genesis running. Channels
  Preservation (protect what works) and Competence (improve processes).

Both are the LIDA SELECT step for their domain: they evaluate options,
resolve conflicts, and make coherent decisions within their jurisdiction.

---

## Information Separation as Architecture

Each ego sees only its domain's data: its own proposals, its own
observations, its own outcomes. This is enforced at the **context builder
level** — the data layer — not by prompts or post-hoc filters. Prompts
reinforce what code already enforces.

If an ego generates a cross-domain proposal, that means something leaked
in its context, and the leak should be fixed at the source.

**Enforcement layers (in priority order):**

1. **Context builders** — what each ego sees. Primary enforcement.
2. **Code filters** — domain boundary checks on proposals. Safety net.
3. **Realist prompt rules** — LLM-level domain judgment. Defense-in-depth.

---

## Investigation Is Free, Proposals Are Gated

Both egos have full tool access (Bash, Read, MCP tools,
`skip_permissions=True`). They investigate during their cycle using MCP
tools. Proposals are a **write permission gate** — only for actions that
change state and need user approval.

A proposal to "investigate X" is the ego failing to use its own tools —
unless the investigation requires dedicated session time beyond what the
ego cycle allows (e.g., a 30-minute deep dive that would block the
cycle), in which case it is a dispatch proposal.

---

## Domain-Aware Realist

The realist gate serves different purposes for each ego:

**User Ego**: Full quality control — catch vague proposals, zombie
re-proposals, infeasible ideas, read-ops disguised as proposals, and
cross-domain leaks into infrastructure.

**Genesis Ego**: Focused quality control — catch zombie re-proposals and
infeasible proposals. No "read operation" filtering — the genesis ego's
investigate-and-dispatch pattern is a legitimate proposal type for work
that exceeds in-cycle capacity.

---

## Cross-Ego Communication

The coordination interface is narrow and typed:

- **Genesis ego → User ego**: Escalations, delivered as observations
  (`type='escalation_to_user_ego'`). This is the ONLY path for
  infrastructure data to reach the user ego.

- **Cross-domain redirect**: If an ego generates a proposal outside its
  domain (information leak), the proposal is redirected as an observation
  for the correct ego. The proposal is not stored.

Neither ego sees the other's proposals, history, or outcomes. They
coordinate through the observation interface, not by watching each
other work. When they see each other's work, they blend into one
generalist and neither does their specific job well.

---

## Scope

| Domain | User Ego (CEO) | Genesis Ego (COO) |
|--------|---------------|-------------------|
| Career, content, goals | Primary | Never |
| Outreach to user | Primary | Via escalation only |
| System health, performance | Never (sees escalations) | Primary |
| Cost tracking, maintenance | Never | Primary |
| Morning report | Primary | Never |

---

## Design Notes

**Why not one ego with two modes?** Two CC sessions force domain purity
at the architectural level. One session with mode switching relies on the
LLM to stay in its lane — and LLMs conflate domains when they see
cross-domain context. Keep the separation mechanical until better
attention control exists.

**Future: notifications.** Not everything the ego thinks is worth sharing
requires user approval. "Dream cycle needs fixing before June 7" is a
notification; "Dispatch a session to fix the dream cycle" is a proposal.
The notifications output mode (autonomous via outreach pipeline, with
rate limiting and dedup) is planned as a follow-up to this architecture.

**Future: unified workspace.** The V4 workspace controller concept — one
evaluation point with domain-aware routing — may eventually replace the
dual-ego split. The information separation and domain boundary patterns
established here are the foundation for that evolution.
