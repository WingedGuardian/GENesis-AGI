# Capability Modules Framework — Design Summary

**Date:** 2026-03-12
**Status:** Implemented (Phase A-D)

## Overview

Framework for pluggable external domain capabilities that leverage Genesis's
cognitive infrastructure without modifying it. Analogy: Genesis's brain doesn't
change when it learns to trade or forecast — it acquires new hands.

## Architecture

### Knowledge Pipeline (`src/genesis/pipeline/`)

Always-on tiered research infrastructure:

```
Tier 0: Collection (free APIs, web search)    → high volume
Tier 1: Triage (surplus models, keyword)      → ~30% survives
Tier 2: Analysis (mid-tier models)            → ~10% survives
Tier 3: Judgment (strong models, CC sessions) → 5-10 items/day
```

- Research profiles (YAML config) define per-domain signal collection
- Pluggable collector registry — web search built-in, extensible
- Pipeline orchestrator ties tiers together
- Runs on surplus compute (SurplusScheduler integration)

### Module Framework (`src/genesis/modules/`)

- `CapabilityModule` protocol: register, deregister, handle_opportunity,
  record_outcome, extract_generalizable
- `ModuleRegistry`: lifecycle management (load/unload)
- `GeneralizationFilter`: LLM quality gate — only domain-agnostic lessons
  cross from module to core

### Design Principle: Hands, Not Brain

- Modules **use** Genesis cognitive services as shared tools
- Modules **do not** modify core identity, reflection, or learning
- Module outcomes tracked in **isolation** from Genesis self-model
- **Generalizable lessons** promoted via quality gate with low confidence
  (0.4) and `speculative=True` until validated
- Removing a module breaks nothing

### Data Flow

```
Pipeline signals → Module.handle_opportunity() → action proposal
User approves → Module executes → Module.record_outcome()
                                → Module.extract_generalizable()
                                → GeneralizationFilter → Core memory (if generalizable)
```

## Implemented Modules

### Prediction Markets (`prediction_markets/`)
- MarketScanner: fetch/filter/rank by volume, price, category
- CalibrationEngine: superforecasting methodology (Tetlock/GJP)
- PositionSizer: fractional Kelly criterion (1/4 Kelly default)
- OutcomeTracker: Brier scores, calibration curves, category breakdowns

### Crypto Token Operations (`crypto_ops/`)
- NarrativeDetector: LLM-driven detection from pipeline signals
- PositionMonitor: price/volume/holder tracking, exit signal detection
- CryptoOutcomeTracker: P&L, narrative accuracy, timing quality

## Future Modules

Same framework applies to:
- Work/career augmentation (prospecting, sales cycles, opportunity monitoring)
- Any external domain that follows: research → analyze → act → track → learn

## Test Coverage

170 tests across pipeline (45), module framework (21), prediction markets (62),
and crypto ops (42).
