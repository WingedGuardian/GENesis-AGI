"""Tests for genesis.inbox.recommendation — YAML block parser."""

from __future__ import annotations

import pytest

from genesis.inbox.recommendation import (
    Recommendation,
    _extract_item_title,
    parse_recommendations,
)

# ---------------------------------------------------------------------------
# Fixtures — trimmed from real production output
# ---------------------------------------------------------------------------

GENESIS_SINGLE = """\
---
date: 2026-06-06
---

# Inbox Evaluation — 2026-06-06

## 1. Evo — Autoresearch Loop for Codebase Optimization

**Classification:** Genesis-relevant | **Decision:** Research

### Summary

Evo applies autoresearch methodology to automated codebase improvement.

### Recommendation

```yaml
action: ADAPT
next_step: "Prototype a metric-gated selection pattern for surplus code tasks"
effort: Medium
scope: V4
confidence: medium
architecture_impact: extends
```

### Lens 1: How It Helps Genesis

Some analysis here.
"""

GENESIS_WITH_TOOL_SCORES = """\
## 1. Some Agent Framework

**Classification:** Genesis-relevant

### Recommendation

```yaml
action: WATCH
next_step: "Track release cadence and revisit in Q3"
effort: Small
scope: V5
confidence: medium
architecture_impact: extends
tool_momentum: high
tool_activity: high
tool_maturity: low
```
"""

USER_SINGLE = """\
---
date: 2026-06-06
---

# Inbox Evaluation — 2026-06-06

## AI Engineer/architect

**Classification:** Personal note — career musing | **Decision:** Research

### Summary

A role-title refinement signal.

### Recommendation

```yaml
action: explore
next_step: "Audit your current application targets against AI Engineer vs AI Architect role titles"
effort: Small
timeline: Soon
relevance: Direct
confidence: high
```

### Lens 1: What This Is

Some analysis here.
"""

MULTI_ITEM = """\
---
date: 2026-06-06
---

# Inbox Evaluation — 2026-06-06

Genesis evaluated 4 items.

---

## 1. Evo — Autoresearch Loop

**Classification:** Genesis-relevant | **Decision:** Research

### Summary

Evo stuff.

### Recommendation

```yaml
action: ADAPT
next_step: "Prototype a metric-gated selection pattern"
effort: Medium
scope: V4
confidence: medium
architecture_impact: extends
```

### Lens 1: How It Helps Genesis

---

## 2. Memanto — Agent Memory

**Classification:** Genesis-relevant | **Decision:** Research

### Summary

Memanto stuff.

### Recommendation

```yaml
action: ADAPT
next_step: "Audit Genesis memory_store schema for supersession fields"
effort: Medium
scope: V3
confidence: high
architecture_impact: extends
```

---

## 3. Forkd — Fork-Based MicroVM Sandboxing

**Classification:** Genesis-relevant | **Decision:** Research

### Summary

Forkd stuff.

### Recommendation

```yaml
action: WATCH
next_step: "Bookmark Forkd for re-evaluation in V5"
effort: Trivial
scope: V5
confidence: medium
architecture_impact: irrelevant
```

---

## 4. EntityMap — Open Standard

**Classification:** Genesis-relevant | **Decision:** Research

### Summary

EntityMap stuff.

### Recommendation

```yaml
action: IGNORE
next_step: "No action needed — low relevance"
effort: Trivial
scope: Never
confidence: high
architecture_impact: irrelevant
```
"""

NO_RECOMMENDATION = """\
# Inbox Evaluation — 2026-06-06

## To-Do: Buy groceries

**Classification:** To-Do item

### Summary

This is a to-do item with no Recommendation block.
"""

MALFORMED_YAML = """\
## 1. Bad Item

### Recommendation

```yaml
action: ADAPT
next_step: "unbalanced quote
effort: [invalid
```

## 2. Good Item

### Recommendation

```yaml
action: WATCH
next_step: "This one is fine"
effort: Small
scope: V4
confidence: medium
architecture_impact: validates
```
"""

BOOKMARK_USER = """\
## Karpathy on Agentic Engineering

**Classification:** User-relevant

### Summary

Video breakdown of agentic engineering framing.

### Recommendation

```yaml
action: bookmark
next_step: "Use agentic engineering framing in networking"
effort: Trivial
timeline: Soon
relevance: Direct
confidence: high
```
"""

POTENTIAL_SKIP = """\
## Some Irrelevant Thing

### Recommendation

```yaml
action: potential_skip
next_step: "Probably not relevant"
effort: Trivial
timeline: Someday
relevance: Background
confidence: low
```
"""


BUILD_VERDICT = """\
## 1. Warpchart — star-velocity tracker

**Classification:** Genesis-relevant (capability-build directive)

### Recommendation

```yaml
action: BUILD
next_step: "Build a star-velocity module fed by gather_stars history"
effort: Small
scope: V4
confidence: high
architecture_impact: extends
verdict: build
verdict_reason: "Clear capability gap, fits module tree"
build_spec:
  requirements:
    - "Compute +N/day velocity from existing star history"
  steps:
    - type: code
      description: "Add velocity computation"
    - type: verification
      description: "Verify against recorded history"
  success_criteria:
    - "Velocity matches hand-computed value for fixture data"
  risks:
    - "Sparse history yields noisy velocity - require 3+ samples"
  intended_paths:
    - "src/genesis/modules/star_velocity/"
```

### Lens 1

Analysis.
"""

DONT_BUILD_VERDICT = """\
## 1. Proxmox VM provisioning MCP

**Classification:** Genesis-relevant (capability-build directive)

### Recommendation

```yaml
action: BUILD
next_step: "Report the veto"
effort: Trivial
scope: Never
confidence: high
architecture_impact: irrelevant
verdict: dont_build
verdict_reason: "No Proxmox cluster exists for Genesis to manage"
```
"""

NEEDS_DISCUSSION_VERDICT = """\
## 1. Hypothetical capability

**Classification:** Genesis-relevant (capability-build directive)

### Recommendation

```yaml
action: BUILD
next_step: "Discuss shape before building"
effort: Small
scope: V4
confidence: low
architecture_impact: challenges
verdict: needs_discussion
verdict_reason: "Ambiguous interface - needs a design decision before the executor can proceed"
```
"""

BAD_VERDICT_VALUE = """\
## 1. Something

**Classification:** Genesis-relevant

### Recommendation

```yaml
action: BUILD
next_step: "n"
effort: Small
scope: V4
confidence: low
verdict: maybe_later
build_spec: "not a mapping"
```
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestParseRecommendations:
    def test_genesis_single(self):
        recs = parse_recommendations(GENESIS_SINGLE)
        assert len(recs) == 1
        r = recs[0]
        assert r.action == "ADAPT"
        assert r.next_step == "Prototype a metric-gated selection pattern for surplus code tasks"
        assert r.effort == "Medium"
        assert r.scope == "V4"
        assert r.confidence == "medium"
        assert r.architecture_impact == "extends"
        assert r.classification == "genesis"
        assert r.item_title == "Evo — Autoresearch Loop for Codebase Optimization"
        assert r.is_actionable is True

    def test_parses_tool_scoring_fields(self):
        recs = parse_recommendations(GENESIS_WITH_TOOL_SCORES)
        assert len(recs) == 1
        r = recs[0]
        assert r.tool_momentum == "high"
        assert r.tool_activity == "high"
        assert r.tool_maturity == "low"

    def test_tool_scoring_fields_default_none_when_absent(self):
        # Backward compatible: existing recommendations without the rubric.
        r = parse_recommendations(GENESIS_SINGLE)[0]
        assert r.tool_momentum is None
        assert r.tool_activity is None
        assert r.tool_maturity is None

    def test_build_verdict_parses_full_spec(self):
        recs = parse_recommendations(BUILD_VERDICT)
        assert len(recs) == 1
        r = recs[0]
        assert r.action == "BUILD"
        assert r.verdict == "build"
        assert r.verdict_reason == "Clear capability gap, fits module tree"
        assert isinstance(r.build_spec, dict)
        assert r.build_spec["requirements"] == [
            "Compute +N/day velocity from existing star history"
        ]
        assert r.build_spec["steps"][0]["type"] == "code"
        assert r.build_spec["intended_paths"] == [
            "src/genesis/modules/star_velocity/"
        ]
        # BUILD never produces a follow-up — the build lane owns it.
        assert r.is_actionable is False

    def test_dont_build_verdict_without_spec(self):
        r = parse_recommendations(DONT_BUILD_VERDICT)[0]
        assert r.verdict == "dont_build"
        assert r.verdict_reason == "No Proxmox cluster exists for Genesis to manage"
        assert r.build_spec is None
        assert r.is_actionable is False

    def test_needs_discussion_verdict_parses(self):
        r = parse_recommendations(NEEDS_DISCUSSION_VERDICT)[0]
        assert r.verdict == "needs_discussion"
        assert r.verdict_reason is not None
        assert r.build_spec is None
        assert r.is_actionable is False  # BUILD is in _SKIP_ACTIONS

    def test_invalid_verdict_and_non_mapping_spec_degrade_to_none(self):
        # Shadow-safe: bad LLM output degrades to "no verdict", never invents one.
        r = parse_recommendations(BAD_VERDICT_VALUE)[0]
        assert r.verdict is None
        assert r.build_spec is None

    def test_verdict_fields_default_none_for_ordinary_evals(self):
        # Backward compatible: every pre-existing eval has no verdict fields.
        r = parse_recommendations(GENESIS_SINGLE)[0]
        assert r.verdict is None
        assert r.verdict_reason is None
        assert r.build_spec is None

    def test_user_single(self):
        recs = parse_recommendations(USER_SINGLE)
        assert len(recs) == 1
        r = recs[0]
        assert r.action == "explore"
        assert r.timeline == "Soon"
        assert r.relevance == "Direct"
        assert r.confidence == "high"
        assert r.classification == "user"
        assert r.scope is None
        assert r.architecture_impact is None
        assert r.item_title == "AI Engineer/architect"
        assert r.is_actionable is True

    def test_multi_item(self):
        recs = parse_recommendations(MULTI_ITEM)
        assert len(recs) == 4

        actions = [r.action for r in recs]
        assert actions == ["ADAPT", "ADAPT", "WATCH", "IGNORE"]

        # All are genesis classification
        assert all(r.classification == "genesis" for r in recs)

        # ADAPT items are actionable, WATCH is actionable, IGNORE is not
        assert recs[0].is_actionable is True
        assert recs[1].is_actionable is True
        assert recs[2].is_actionable is True
        assert recs[3].is_actionable is False

        # Titles extracted correctly
        assert recs[0].item_title == "Evo — Autoresearch Loop"
        assert recs[1].item_title == "Memanto — Agent Memory"
        assert recs[2].item_title == "Forkd — Fork-Based MicroVM Sandboxing"
        assert recs[3].item_title == "EntityMap — Open Standard"

    def test_no_recommendation(self):
        recs = parse_recommendations(NO_RECOMMENDATION)
        assert recs == []

    def test_empty_input(self):
        assert parse_recommendations("") == []
        assert parse_recommendations(None) == []  # type: ignore[arg-type]

    def test_malformed_yaml_skips_bad_keeps_good(self):
        recs = parse_recommendations(MALFORMED_YAML)
        # The malformed block should be skipped, good one kept
        assert len(recs) == 1
        assert recs[0].action == "WATCH"
        assert recs[0].item_title == "Good Item"

    def test_bookmark_user(self):
        recs = parse_recommendations(BOOKMARK_USER)
        assert len(recs) == 1
        r = recs[0]
        assert r.action == "bookmark"
        assert r.classification == "user"
        assert r.is_actionable is True  # bookmark creates follow-up

    def test_potential_skip_not_actionable(self):
        recs = parse_recommendations(POTENTIAL_SKIP)
        assert len(recs) == 1
        r = recs[0]
        assert r.action == "potential_skip"
        assert r.is_actionable is False


class TestExtractItemTitle:
    def test_numbered(self):
        assert _extract_item_title("1. Evo — Autoresearch\nSome text") == "Evo — Autoresearch"

    def test_unnumbered(self):
        assert _extract_item_title("AI Engineer/architect\nMore text") == "AI Engineer/architect"

    def test_single_line(self):
        assert _extract_item_title("Just a Title") == "Just a Title"


class TestRecommendationIsActionable:
    @pytest.mark.parametrize("action,expected", [
        ("ADOPT", True),
        ("ADAPT", True),
        ("WATCH", True),
        ("IGNORE", False),
        ("ignore", False),
        ("adopt", True),
        ("explore", True),
        ("bookmark", True),
        ("potential_skip", False),
        ("potential skip", False),
    ])
    def test_actionability(self, action: str, expected: bool):
        r = Recommendation(action=action, next_step="test")
        assert r.is_actionable is expected
