# Earned Autonomy: How It Works

Trust-based, evidence-driven autonomy. 4 operational levels. Bayesian regression
on failure. Context-dependent ceilings that prevent unsupervised overreach.

---

## Why this subsystem exists

Binary "autonomy on/off" doesn't work for a system running continuously across
sessions. The right question isn't "does Genesis have permission to act?" — it's
"has Genesis demonstrated the competence to act in this specific domain, and is
this the right context for it to act?"

Autonomy in Genesis is a trust relationship. It's earned per action category
through demonstrated competence. It can regress. The regression is always
announced. The user always has override authority.

---

## 4 Autonomy Levels (V3)

```python
class AutonomyLevel(IntEnum):
    L1 = 1  # Simple tool use — health checks, status queries
    L2 = 2  # Pattern execution — running known procedures
    L3 = 3  # Novel task handling — unfamiliar requests within earned categories
    L4 = 4  # Proactive outreach — initiating communication based on observations
```

All categories start at L1. Genesis earns higher levels through demonstrated
competence, one level at a time.

---

## Context-Dependent Ceilings

The same earned level produces different effective authority depending on how
Genesis is running. A system that earns trust in supervised settings shouldn't
automatically act unsupervised at the same scope.

```python
CONTEXT_CEILING_MAP = {
    ContextCeiling.DIRECT_SESSION: 7,        # No cap — user is present
    ContextCeiling.BACKGROUND_COGNITIVE: 3,  # L3 max — thinking between conversations
    ContextCeiling.SUB_AGENT: 2,             # L2 max — isolated execution
    ContextCeiling.OUTREACH: 2,              # L2 until engagement proves calibration
}

async def effective_level(self, category: str) -> int:
    state = await self.get_state(category)
    ceiling = CONTEXT_CEILING_MAP[_CATEGORY_TO_CEILING[category]]
    return min(int(state.current_level), ceiling)
```

Effective level = `min(earned_level, context_ceiling)`.

---

## Bayesian Trust Scoring

Trust is data, not configuration. Genesis uses a Laplace-smoothed Beta
distribution posterior to compute competence per category:

```python
def bayesian_level(total_successes: int, total_corrections: int) -> int:
    """Posterior mean = (successes + 1) / (successes + corrections + 2)"""
    total = total_successes + total_corrections
    if total == 0:
        return 1  # no evidence yet
    posterior = (total_successes + 1) / (total + 2)

    if posterior >= 0.70: return 4
    if posterior >= 0.50: return 3
    if posterior >= 0.30: return 2
    return 1
```

**Why Laplace smoothing?** With zero interactions: `1 / 2 = 0.5` — uninformed
prior. Prevents immediate promotion after a single success while allowing
reasonable inference from sparse data.

**Trust progression:**

| Successes | Corrections | Posterior | Level |
|-----------|-------------|-----------|-------|
| 0 | 0 | 0.50 | L1 |
| 1 | 0 | 0.67 | L2 |
| 2 | 0 | 0.75 | L3 |
| 5 | 0 | 0.86 | L4 |
| 10 | 5 | 0.73 | L3 |
| 50 | 5 | 0.90 | L4 |

Promotion caps at +1 level per success — prevents overfitting to lucky streaks.
Regression has no such cap. A single correction can drop multiple levels if the
posterior crosses a threshold.

---

## Action Classification

Three irreversibility tiers drive different approval gates:

```python
class ActionClass(StrEnum):
    REVERSIBLE = "reversible"               # Edit file, create branch
    COSTLY_REVERSIBLE = "costly_reversible" # Send message, push code
    IRREVERSIBLE = "irreversible"           # Pay money, delete external, submit form
```

---

## Approval Gate Decision

```python
# Simplified from rule engine evaluation
if required_level > context_ceiling:
    return ApprovalDecision.BLOCK         # exceeds context ceiling

if required_level > earned_level:
    return ApprovalDecision.BLOCK         # exceeds earned authority

if action_class == ActionClass.IRREVERSIBLE:
    return ApprovalDecision.PROPOSE       # always require approval

if action_class == ActionClass.COSTLY_REVERSIBLE and required_level >= 3:
    return ApprovalDecision.PROPOSE       # L3+ gates costly actions

return ApprovalDecision.ACT              # proceed autonomously
```

**Key invariant:** No auto-approve path exists. Timeout always means reject.

```yaml
approval_timeouts:
  outreach: 3600         # 60 min
  task_proposal: 86400   # 24 hours
  irreversible: null     # wait forever
```

---

## Protected Paths

Three layers enforce that certain paths can't be modified via relay/background:

**Layer 1 — CC PreToolUse hooks** (architectural): Intercept file I/O before
it happens. Checked against `config/protected_paths.yaml`.

**Layer 2 — System prompt injection** (LLM): Relay sessions receive path
restrictions in the system prompt. Not foolproof alone.

**Layer 3 — Post-session git diff audit** (detective): Changes to protected
paths after any autonomous session are flagged and surfaced to the user.

**CRITICAL paths** (no relay modification):
- `src/genesis/channels/**` — relay infrastructure; modifying via Telegram could break Telegram
- `src/genesis/autonomy/protection.py` — Genesis cannot weaken its own guardrails
- `config/protected_paths.yaml` — same
- `config/autonomy.yaml` — level/threshold changes require CLI access

**SENSITIVE paths** (modifiable with explicit approval + self-review):
- `src/genesis/runtime.py` — bootstrap; affects all subsystem initialization
- `src/genesis/identity/*.md` — identity files shape how Genesis thinks

---

## 7-Layer Enforcement Taxonomy

Each enforcement mechanism has a defined layer so it's clear what can be
overridden, and by whom:

```python
class EnforcementLayer(IntEnum):
    HARD_BLOCK = 7        # Framework intercept, exit code 2 — unpromptable
    PERMISSION_GATE = 6   # Requires explicit user action
    PROPOSAL_GATE = 5     # Requires approval before acting
    ADVISORY = 4          # Soft system prompt injection — LLM can override
    DETECTION = 3         # Observation only, no enforcement
    AMBIENT = 2           # Always-on background classification
    BASELINE = 1          # No enforcement — default state
```

---

## Regression Mechanics

When `bayesian_level()` returns lower than current level:

```python
if target_level < current_level:
    new_level = target_level
    emit("autonomy.regression", {
        "category": category,
        "from": current_level,
        "to": new_level,
        "posterior": posterior,
        "reason": f"Bayesian regression (posterior={posterior:.3f})"
    })
    # notify user via Telegram
```

Regression is always announced. Silent regression is treated as worse than
the underlying failure.

`earned_level` ratchets up (historical maximum, never decreases). `current_level`
can drop. Rebuilding from L2 to L4 requires the same evidence a first-time
progression would require.

---

## Verification Gate

No autonomous task completes without a `CompletionArtifact`:

```python
@dataclass(frozen=True)
class CompletionArtifact:
    task_id: str
    what_attempted: str
    what_produced: str
    success: bool
    learnings: str = ""
    error: str | None = None
    outputs: dict = field(default_factory=dict)
```

`TaskVerifier` runs structural checks + task-type-specific validators (ruff +
pytest for code tasks). A task stays in-progress until verification passes.

---

## Key Files

| File | Purpose |
|------|---------|
| `src/genesis/autonomy/manager.py` | Autonomy state, effective_level, record_success/correction |
| `src/genesis/autonomy/classifier.py` | Action classification, timeout lookup |
| `src/genesis/autonomy/approval.py` | Approval lifecycle, timeout enforcement |
| `src/genesis/autonomy/protection.py` | Protected path registry, fnmatch classification |
| `src/genesis/autonomy/verifier.py` | CompletionArtifact verification gate |
| `src/genesis/autonomy/crud.py` | Bayesian posterior computation, level regression |
| `config/autonomy.yaml` | Rule engine, approval timeouts, category config |
| `config/protected_paths.yaml` | CRITICAL and SENSITIVE path definitions |

---

## Design Decisions

**Why Bayesian posterior over a fixed penalty system?**
Fixed "2 mistakes = drop 1 level" ignores prior history. A system with 50
successes and 2 corrections deserves different treatment than one with 3 successes
and 2 corrections. The posterior captures this. It's the probability that the
next action in this category succeeds — which is exactly the question.

**Why context ceilings on top of earned levels?**
Earned trust in supervised sessions shouldn't automatically apply in unsupervised
background sessions. The trust was earned with the user watching. The ceiling
encodes the difference between supervised and unsupervised operation.

**Why YAML rule engine instead of hard-coded logic?**
Policy can change without code changes. The rule engine reads `autonomy.yaml`
at startup. Adding a new rule is a config change, not a deployment.

**Why three layers for protected paths?**
No single layer is sufficient. PreToolUse hooks can be bypassed if the hook
configuration is changed. System prompt instructions can be overridden by a
determined LLM. Post-session diff audit is purely detective. All three together
create defense-in-depth.

---

## V4/V5 Targets

**V4:**
- Evidence-based auto-promotion (currently manual user assignment)
- Calibration-informed level decisions (V4 has operational data to draw from)

**V5 (L5-L7):**
- L5: System configuration — adjusting own thresholds and parameters
- L6: Learning modification — changing own review schedules and calibration
- L7: Identity evolution — proposing changes to own operating principles

L5-L7 require months of L4 operational data before they're safe to activate.
The schema supports them now. The governance doesn't activate them yet.
