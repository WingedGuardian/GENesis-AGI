# ADR 003: No Salience Gates on Reflection Delivery

**Status:** Accepted
**Date:** 2026-04-17

## Context

The reflection engine produces observations at multiple depths (micro, light,
deep, strategic). These are delivered to the user via Telegram. The question:
should low-salience reflections be filtered before delivery?

## Decision

Every reflection gets delivered. No salience thresholds, no quality gates,
no "is this interesting enough?" filtering on the delivery path.

## Consequences

**Benefits:**
- User maintains full awareness of system thinking
- Prevents the system from making editorial judgments about what the user "needs to see"
- Low-salience observations sometimes contain early signals of important patterns
- Builds trust — nothing is hidden

**Costs:**
- Higher notification volume
- Some reflections are genuinely low-value
- User must do their own filtering (but prefers this to missing signals)

**Why not filter:** The user explicitly established this as a hard rule after
an incident where filtered reflections hid an early warning signal. The
principle: Genesis observes and reports; the user decides what matters. This
is a sovereignty decision, not an engineering one.

**Scope:** This applies to reflection delivery only. Other subsystems
(observation classification, memory storage) still use salience scoring for
their own prioritization — they just can't suppress delivery.
