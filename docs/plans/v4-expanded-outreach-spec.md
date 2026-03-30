# V4 Feature Spec: Expanded Outreach

**Status:** DESIGNED — repositioned within GWT architecture. Activates after
calibrated user model (20+ surplus outreach data points, engagement rate > 40%).
**Dependency:** Phase 8 (Basic Outreach), V4 Strategic Reflection
**V3 Groundwork:** outreach_history table (7 categories including V4 types in
CHECK constraint), outreach MCP stub (5 tools), engagement heuristics
(`src/genesis/learning/engagement.py`), channel base class with
`GROUNDWORK(outreach-pipeline)` engagement stub, 1/day surplus outreach rule.
**GWT Integration:** Outreach categories become proposal types that modules
submit to the LIDA cycle. The workspace controller (SELECT step) replaces
independent outreach decisions with coordinated selection. See
`docs/architecture/genesis-v4-architecture.md` §8.

---

## What This Is

V3 outreach is limited to two categories (Blocker + Alert) plus exactly 1/day
surplus-driven outreach. V4 expands to five additional categories that require
a calibrated user model and engagement data to deliver well:

| Category | Source | Priority | Channel | Example |
|----------|--------|----------|---------|---------|
| **Blocker** (V3) | Task needs user decision | Immediate | Fastest available | "I found two architectures — which trade-off do you prefer?" |
| **Alert** (V3) | Health/budget threshold | High | Fastest available | "Budget at 85% of weekly limit" |
| **Finding** (V4) | Recon + salience threshold | Medium | Learned channel or digest | "New framework relevant to your project" |
| **Insight** (V4) | Reflection pattern detection | Medium-low | Next session or digest | "You've built 3 similar pipelines — template?" |
| **Opportunity** (V4) | User model + new info + capability cross-ref | Low | Next session or digest | "Based on your skills + goals, high-leverage idea: ..." |
| **Digest** (V4) | Scheduled batch of low-priority items | Low | Learned channel | "Here's what happened while you were away" |
| **Surplus** (V3) | Daily brainstorm staging area | Low | Governance-gated | Labeled as surplus-generated |

## Current V3 State

### Outreach Categories

Phase 8 implements Blocker and Alert only. The outreach_history table already
has a CHECK constraint including all 7 categories:

```sql
category TEXT NOT NULL CHECK (category IN (
    'blocker', 'alert', 'finding', 'insight', 'opportunity', 'digest', 'surplus'
))
```

### 1/Day Surplus Rule

From the autonomous behavior design (lines 2742–2744):

> Day 1 rule: Exactly 1 surplus-driven outreach per day. Not "up to 1" —
> exactly 1. The system can't learn to be proactive without being given
> opportunities to try.

### Engagement Heuristics (Fixed per Channel)

From `src/genesis/learning/engagement.py` (lines 8–15):

| Channel | Engaged Threshold | Ignored Threshold |
|---------|-------------------|-------------------|
| WhatsApp | Reply < 4h | Nothing after 24h |
| Telegram | Reply < 4h | Nothing after 24h |
| Web | Click-through < 5min | No interaction after 24h |
| Terminal | Substantive reply < 1min | Nothing after 1h |

V3 uses fixed heuristics. V4 learns optimal thresholds from data.

### Governance Gate (Deterministic, Pre-Outreach)

From build phases doc (lines 1071–1076):

1. Within autonomy permissions?
2. Passes salience threshold? (fixed in V3)
3. Timing appropriate? (quiet hours from config)
4. Similar outreach sent recently? (dedup)
5. Budget check for paid channels

### Outreach MCP Stub

`src/genesis/mcp/outreach_mcp.py` — 5 tool stubs ready for Phase 8:
- `outreach_send()` — queue message for delivery
- `outreach_queue()` — view pending messages
- `outreach_engagement()` — record engagement event
- `outreach_preferences()` — get/set channel preferences
- `outreach_digest()` — generate digest of queued items

## V4 Growth Ramp

From autonomous behavior design (lines 2752–2763):

| Phase | Frequency | Trigger |
|-------|-----------|---------|
| **Bootstrap** | Exactly 1/day | Default from day 1 |
| **Calibrating** | 1–2/day | 20+ surplus data points AND engagement > 40% |
| **Calibrated** | 1–3/day, self-regulated | 50+ data points AND engagement > 50% AND user explicitly approves |
| **Autonomous** | Self-determined (bounded by daily cap) | 100+ data points AND consistent engagement AND Strategic reflection confirms |

**Regression:** If surplus outreach engagement drops below 25% over a 2-week
window, frequency drops one phase. The system announces the regression and
reason.

## V4 Self-Rating Mechanism

From autonomous behavior design (lines 2763–2765):

Before sending outreach, the system predicts engagement probability. After
engagement data arrives, it computes prediction error. Over time, the system
tracks its prediction accuracy ("I'm 70% accurate at predicting which ideas
the user finds valuable") — and that accuracy number determines autonomy.

**Implementation:**
1. Pre-send: LLM predicts `engagement_probability` (0.0–1.0)
2. Store in `outreach_history.prediction_error` (computed after outcome)
3. Rolling accuracy metric over last 30 outreach events
4. Accuracy feeds into growth ramp qualification

**World Model Integration** (lines 2097–2101):

> World model predicts engagement for a signal → Reflection Engine decides
> outreach → outreach delivered → user engages or ignores → Self-Learning Loop
> computes prediction error → world model updated → future predictions improve.

The learning happens in the prediction model (world model); the salience
threshold is fixed or very slowly adjusted at Strategic depth.

## V4 Channel Learning

V3 uses config-driven fixed channel preferences. V4 learns which channel gets
the fastest/most-positive engagement per outreach type.

**Design:**
- Track `channel × category × engagement_outcome` statistics
- After 20+ outreach events per category, recommend optimal channel
- User can set explicit overrides ("alerts always go to WhatsApp")
- System recommendations are proposals, not auto-applied

**Channel adapter interface** (from `src/genesis/channels/base.py` lines 38–45):

```python
# GROUNDWORK(outreach-pipeline): Phase 8 engagement tracking.
@abstractmethod
async def get_engagement_signals(self, delivery_id: str) -> dict:
    """Check engagement signals for a sent message."""
```

## V4 Morning Report Upgrade

V3: Static prompt template for daily morning report.
V4: Meta-prompted adaptive content selection.

From build phases doc (lines 1433–1439):

1. Cheap model asks: "What does the user most need to hear this morning?"
   based on journal, user model, engagement patterns on previous reports
2. Capable model generates the report with adaptive section selection
3. Morning report engagement data feeds back into content selection model

**Question seam** (design doc lines 786–832): Morning reports may include
1–2 questions from recent self-reflection. Each question is asked at most
once via outreach — if unanswered, the outreach attempt expires but the
question persists in memory as an open observation.

## What V4 Must Build

### New Code

1. **`genesis.outreach.categories` module:**
   - `FindingDetector` — monitors recon findings against salience threshold
   - `InsightDetector` — monitors reflection patterns for cross-cutting insights
   - `OpportunityDetector` — cross-references user model + new info + capabilities
   - `DigestAssembler` — batches low-priority items into periodic digests

2. **`genesis.outreach.growth_ramp` module:**
   - `GrowthRampManager` — tracks current phase, evaluates advancement criteria,
     handles regression
   - `FrequencyController` — enforces daily cap based on current growth phase

3. **`genesis.outreach.prediction` module:**
   - `EngagementPredictor` — pre-send prediction (LLM call)
   - `PredictionTracker` — rolling accuracy computation, error logging
   - Feeds into `outreach_history.prediction_error`

4. **`genesis.outreach.channel_learner` module:**
   - `ChannelRecommender` — per-category channel optimization
   - Respects explicit user overrides from `outreach_preferences`

5. **`MORNING_REPORT.md` rewrite:**
   - V4 meta-prompted version with adaptive content selection
   - Engagement feedback integration

### Modifications to Existing Code

6. **Outreach MCP** — implement the 5 stub tools (Phase 8 deliverable,
   expanded for V4 categories)
7. **Engagement heuristics** — replace fixed rules with learned thresholds
   (keep fixed rules as bootstrap defaults)
8. **Governance gate** — add category-specific salience thresholds
   (V3 uses single fixed threshold)
9. **SurplusScheduler** — integrate growth ramp frequency control
10. **Strategic reflection** — include outreach quality metrics in MANAGER
    inputs, propose preference adjustments

### Fresh-Eyes Review

From build phases doc (line 1088):

> Fresh-eyes review on outreach before sending: cross-model check on the
> 1/day surplus outreach.

V4 extends this to all Finding/Insight/Opportunity outreach — a second model
reviews the outreach draft before delivery.

## Activation Criteria

| Prerequisite | Threshold |
|---|---|
| V3 Phase 8 complete | Basic outreach operational |
| Surplus outreach data points | 20+ (for Calibrating phase entry) |
| Engagement rate | > 40% on surplus outreach |
| User model quality | Sufficient user profile for cross-referencing |
| Recon MCP operational | Required for Finding category |

## Design Constraints

- **Surplus outreach is labeled.** The user always knows which outreach is
  autonomous vs triggered. This sets correct expectations.
- **Growth ramp requires explicit user approval** at the Calibrated phase
  (frequency increase beyond 2/day). Strategic reflection can propose but
  not auto-approve.
- **Outreach delivery is idempotent.** Same `outreach_id` → same message.
  Duplicate delivery is worse than non-delivery (design doc resilience patterns).
- **Channel learning is advisory.** Recommendations are proposals. The user
  can always override with explicit preferences.
- **Feature-flag per category.** Each V4 category (Finding, Insight,
  Opportunity, Digest) can be independently enabled/disabled.
- **Question seam is non-nagging.** Each question asked at most once via
  outreach. If unanswered, the question persists in memory but is not re-sent.

## References

- Autonomous behavior design: `docs/architecture/genesis-v3-autonomous-behavior-design.md`
  §Outreach Categories (lines 1027–1034), §Growth Ramp (lines 2742–2765),
  §World Model Prediction (lines 2097–2101), §Question Seam (lines 786–832),
  §Governance (lines 371–383)
- Build phases: `docs/architecture/genesis-v3-build-phases.md` §Phase 8
  (lines 1063–1230), §V4 Upgrades (lines 1424–1450)
- Strategic reflection spec: `docs/plans/v4-strategic-reflection-spec.md`
  §MANAGER Outputs (lines 35–41)
- Engagement heuristics: `src/genesis/learning/engagement.py`
- Channel base: `src/genesis/channels/base.py` (GROUNDWORK tag lines 38–45)
- Outreach MCP: `src/genesis/mcp/outreach_mcp.py`
- Schema: `src/genesis/db/schema.py` (outreach_history table)
- Outreach CRUD: `src/genesis/db/crud/outreach.py`
