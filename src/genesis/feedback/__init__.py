"""feedback — the Self-Improvement Outcome Bus.

Connective tissue over Genesis's existing, siloed self-improvement signals.
An append-only, attributed, quality-weighted ledger (``outcome_events``) of what
actually happened after Genesis acted, so downstream calibration, deficiency
modelling, and the objective function can read one neutral source of truth
instead of six buried ones.

Layers:
- ``db/crud/outcome_events`` — strict storage primitive (raises on bad input).
- ``feedback.bus`` — fire-and-forget write path + signal taxonomy (tier policy).
- ``feedback.harvest`` — scheduled idempotent folding of existing signals
  + one-shot backfill.

Neutral framing: ``outcome_events``, not ``reward_signals`` — this is
observation, not reinforcement-learning training.
"""

from __future__ import annotations
