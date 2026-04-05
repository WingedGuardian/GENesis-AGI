# Enforcement Spectrum

Genesis uses a 7-layer enforcement spectrum to classify all rules,
constraints, and guardrails. When adding a new rule, use this taxonomy
to decide where it belongs.

## The Spectrum

| Layer | Name | Mechanism | Can LLM Override? | Exit Code |
|-------|------|-----------|-------------------|-----------|
| 7 | **HARD_BLOCK** | Framework intercept | No | 2 |
| 6 | **PERMISSION_GATE** | Requires user action | No | 2 |
| 5 | **PROPOSAL_GATE** | Requires approval | No (waits) | N/A |
| 4 | **ADVISORY** | Context injection | Yes (soft steer) | 0 |
| 3 | **DETECTION** | Observation only | N/A (no enforcement) | 0 |
| 2 | **AMBIENT** | Background classification | N/A (no enforcement) | 0 |
| 1 | **BASELINE** | Default state | N/A | 0 |

## Layer Details

### Layer 7: HARD_BLOCK

Framework-level interception that no prompt can override. The hook script
returns exit code 2 and CC blocks the tool call entirely.

**Genesis components:**
- `scripts/behavioral_linter.py` — blocks Write/Edit containing anti-patterns
  (`no-hide-problems`, `no-unguarded-kill`)
- Inline bash hooks in `.claude/settings.json` — blocks `pip install -e`
  to worktrees, `rm -rf /`, `git push --force`, `git reset --hard`, etc.
- `scripts/review_enforcement_commit.py` — blocks commits without review

**When to use:** Safety-critical constraints that must never be bypassed,
regardless of prompt content or user instruction in the conversation.
Reserve for rules where violation causes data loss, system damage, or
security breach.

### Layer 6: PERMISSION_GATE

Requires explicit user action to proceed. Distinguished from HARD_BLOCK
in that a direct CLI session (not relay) can perform the action.

**Genesis components:**
- `scripts/pretool_check.py` — blocks Write/Edit to CRITICAL protected
  paths from relay/chat channels
- `config/protected_paths.yaml` — defines CRITICAL vs SENSITIVE paths

**When to use:** Actions that are dangerous from automated/relay contexts
but legitimate when the user is directly at the CLI. The gate is about
*who* is acting, not *what* is being done.

### Layer 5: PROPOSAL_GATE

The autonomy system classifies an action and decides whether to proceed
autonomously or propose it for user approval. The system waits (with
configurable timeout) for a response.

**Genesis components:**
- `src/genesis/autonomy/classification.py` — `ActionClassifier` maps
  action classes to approval decisions
- `src/genesis/autonomy/rules.py` — `RuleEngine` evaluates data-driven
  rules from `config/autonomy_rules.yaml`
- `config/autonomy.yaml` — approval policy and timeouts

**When to use:** Actions that are costly or irreversible. The user should
approve before Genesis sends a message, pushes code, or deletes data.

### Layer 4: ADVISORY

Soft context injection. The advisory text is added to Claude's context
for the current tool call. The LLM can choose to follow or ignore it.

**Genesis components:**
- `scripts/procedure_advisor.py` — surfaces learned procedures before
  tool execution (e.g., "use PYTHONPATH, not pip install -e")
- `src/genesis/cc/system_prompt.py` — injects STEERING.md rules and
  protected path awareness into system prompt
- `config/procedure_triggers.yaml` — trigger patterns for procedures

**When to use:** Best practices, learned lessons, and contextual guidance.
Use when the right action depends on context the LLM may not have.

### Layer 3: DETECTION

Observes and logs but does not enforce. Used for tuning thresholds before
enabling enforcement, and for metrics collection.

**Genesis components:**
- `src/genesis/perception/confidence.py` — confidence gates in shadow
  mode (log what would be filtered without filtering)
- `config/content_sanitization.yaml` — prompt injection pattern detection
  (log-only, never blocks)
- `scripts/content_safety_hook.py` — PostToolUse content analysis

**When to use:** When you need data before deciding whether to enforce.
Shadow mode lets you tune thresholds without disrupting operation.

### Layer 2: AMBIENT

Always-on background classification that feeds into higher layers.
No enforcement — just labeling and routing.

**Genesis components:**
- `classify_action()` in `classification.py` — keyword-based action
  classification (reversible/costly/irreversible)
- `config/procedure_triggers.yaml` — pattern matching for procedure
  relevance
- Confidence scoring from LLM output

**When to use:** Inputs to decision-making, not decisions themselves.

### Layer 1: BASELINE

The default state. All paths are NORMAL, all actions are REVERSIBLE,
no enforcement applies. This is what you get when no rule matches.

## Decision Guide

When adding a new rule, ask:

1. **Can violation cause data loss or system damage?** -> Layer 7 (HARD_BLOCK)
2. **Should it be allowed from direct CLI but not relay?** -> Layer 6 (PERMISSION_GATE)
3. **Should the user approve before it happens?** -> Layer 5 (PROPOSAL_GATE)
4. **Is it guidance the LLM should consider?** -> Layer 4 (ADVISORY)
5. **Do we need data before deciding to enforce?** -> Layer 3 (DETECTION)
6. **Is it a classification input for other layers?** -> Layer 2 (AMBIENT)

## Unified Feedback: SteerMessage

All enforcement layers emit feedback via the `SteerMessage` type
(`src/genesis/autonomy/steering.py`). This provides a common format
for hook scripts, linters, and gates:

```python
from genesis.autonomy.steering import SteerMessage
from genesis.autonomy.types import EnforcementLayer, ApprovalDecision

msg = SteerMessage(
    layer=EnforcementLayer.HARD_BLOCK,
    rule_id="no-hide-problems",
    decision=ApprovalDecision.BLOCK,
    severity="critical",
    title="Behavioral Rule Violated",
    context="Hiding element based on error state",
    suggestion="Fix the root cause instead of hiding",
)

# Output in the format the caller needs
exit_code = msg.to_exit_code()   # 2
stderr = msg.to_stderr()          # Human-readable block message
hook_json = msg.to_hook_json()    # CC hook contract JSON
```

## Data-Driven Rules

Layer 5 (PROPOSAL_GATE) rules are defined in `config/autonomy_rules.yaml`
and evaluated by the `RuleEngine`. Rules can be added, modified, or
reordered without code changes. See `src/genesis/autonomy/rules.py`.

## Competitive Context

This taxonomy was validated against external production architectures
(AWS booking agent pattern, 2026-04-04 evaluation). Multiple teams
building agent systems independently converge on the same layered
enforcement model, which suggests this is a stable architectural pattern.
