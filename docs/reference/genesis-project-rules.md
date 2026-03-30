# Genesis v3 — Transferable Project Rules

> Distilled from 3 months of building the nanobot copilot system. These are
> hard-won rules that apply to ANY LLM-agent project built across multi-session
> Claude Code development. Adapt specifics to Genesis; the principles are universal.

---

## Communication Style

Act like gravity for this idea. Your job is to pull it back to reality. Attack the
weakest points in my reasoning, challenge my assumptions, and expose what I might
be missing. Be tough, specific, and do not sugarcoat your feedback.

Always be trying to "one up" the user's ideas when there's a good opportunity
that's also grounded in reality. Don't just agree — improve, extend, or propose
better alternatives.

---

## Design Philosophy

- **LLM-first solutions**: Genesis is always piloted by an LLM. When fixing a gap
  or adding a feature, prefer an LLM-driven solution (better prompt, identity file
  guidance, workspace doc update) over a programmatic one (code guardrail, regex,
  heuristic). Code should handle structural concerns (timeouts, data validation,
  event wiring); judgment calls belong to the LLM. Don't build code that overrides
  LLM judgment.

- **Verify against actual code**: Before claiming a gap, bug, or missing feature
  exists, read the actual source files — not just design docs. Design docs describe
  intent; code describes reality. When the two conflict, trust the code.

- **Flexibility > rigidity & lock-in**: Prefer adapter patterns, generic interfaces,
  and pluggable components. Every external dependency should be swappable. Every
  channel, model provider, and storage backend should be behind an abstraction.

---

## Branch Discipline

- **Never commit directly to main.** All work happens on feature branches (`feat/`,
  `fix/`, `refactor/`, `chore/` prefixes). Main only receives code through merges/PRs.
- **One logical change per branch.** Don't mix unrelated work. If a second concern
  emerges mid-branch, stash it or note it for the next branch.
- **Commit on branch -> review diff -> merge.** Every merge to main is a deliberate
  decision, not a side effect of working.
- **Use git worktrees for parallel independent work** when multiple tasks have no
  shared state or sequential dependencies.

---

## Session Discipline

- **Session wrap-up ritual**: Before ending a session, produce a structured handoff:
  what changed, what's pending, what decisions were made, what was learned. Persist
  this somewhere durable (changelog, lessons file, memory).
- **Self-correction loop**: When the user corrects a mistake, extract the lesson and
  persist it with a concrete rule. Corrections that stay in conversation context
  evaporate — persistent rules compound.
- **Uncommitted changes are not changes**: If you implemented it but didn't commit
  it, it doesn't exist. Always commit before ending a session. The commit is the
  unit of durability, not the file edit.

---

## Process Discipline

- **Pre-commit verification**: Before committing, verify the specific code path
  changed. Query the DB table you referenced. Hit the endpoint you modified.
  Trigger the feature you added. "It looks right" is not verification.
- **Commit scope**: Keep commits under ~10 files. If a change touches more, break
  it into sequential atomic commits that each independently work. Large surface
  area = large blast radius.
- **Loud failure default**: New `try/except` blocks in background services must
  surface failures visibly (alerting, status endpoint, log-with-level). No silent
  `logger.warning` for conditions the user would want to know about. If you're
  catching an exception just to log it, that's a smell — either handle it or
  alert on it.
- **Stabilize before extending**: Don't start new features while the previous
  feature has uncommitted fixes or known broken paths. Finish the fix chain first.
- **Run linting before committing, not after**: Pre-commit hooks are a safety net,
  not the primary check. Catch lint errors before staging to avoid the re-stage ->
  re-commit -> re-check cycle.

---

## Coding Guidelines

- Every changed line should trace directly to the user's request.
- If you write 200 lines and it could be 50, rewrite it.
- For multi-step tasks, state a brief plan with verification steps:
  1. [Step] -> verify: [check]
  2. [Step] -> verify: [check]
  3. [Step] -> verify: [check]

---

## Groundwork Code Protection

Code is sometimes written as **intentional foundational infrastructure** for a
planned feature that isn't fully connected yet. This is deliberate forward-
engineering, not dead code.

**When writing groundwork code:**
- Tag it with an inline comment: `# GROUNDWORK(<feature-id>): <why this exists>`
- Example: `# GROUNDWORK(post-v3-knowledge-base): Collection param enables future KB retrieval`
- The `<feature-id>` must correspond to a documented feature in the project docs

**When you encounter GROUNDWORK-tagged code:**
- **NEVER delete or refactor it as "dead code."** It exists because a previous
  session laid infrastructure for a planned feature.
- **NEVER remove the GROUNDWORK comment** — it's cross-session memory.
- If the code appears unused, check the project docs for the referenced feature-id.
- If you're unsure whether the feature is still planned, **ASK the user**.
- Only remove GROUNDWORK code if: (a) the feature is now fully implemented (code
  is active, remove only the tag), or (b) the user explicitly cancels the feature.

**Why this matters:** Multi-session development means session N+1 has no memory of
session N's intent. Without these tags, foundational code looks like dead code and
gets cleaned up, destroying deliberate architectural preparation. This has happened
multiple times.

---

## LLM-Agent Development Rules (from experience)

### Background Services
- Background services must NOT share the user-facing context pipeline. Build their
  own targeted prompts. If a shared function is used by both interactive and
  autonomous callers, the interactive-specific enrichment must be opt-in/opt-out.
- Every background service that depends on a local model MUST have a cloud fallback.
  Pattern: local (free) -> cloud (cheap) -> queue for deferred -> heuristic.
- Every autonomous background service must produce a per-step checklist showing what
  ran, what was skipped (and why), and what failed. Silence is ambiguous.
- Match timer frequency to cost. Health checks (cheap) -> short interval. LLM calls
  (expensive) -> long interval. Don't confuse them.

### LLM Output Parsing
- LLMs NEVER return bare JSON. Build a multi-level fallback: (1) direct parse,
  (2) extract from markdown fence, (3) regex find outermost braces, (4) strip
  trailing commas. If all fail, store raw + alert. Never silently discard.
- Python `.format()` and JSON templates don't mix. Literal braces in prompt templates
  must be doubled (`{{`, `}}`). Or use f-strings / `string.Template`.

### Agent Loops
- On the final iteration of an agent loop, call the LLM without tools. This forces
  text-only completion — the LLM must summarize findings. The nudge at N-3 is good
  progressive pressure, but the toolless final call guarantees output.
- Don't inject "reflect on results" after every tool call. Unconditional reflection
  injection turns the LLM into an essay generator. The LLM knows how to chain tool
  calls without being told to reflect.

### Identity & Self-Model
- Identity file staleness creates capability blindness. An LLM's capabilities are
  bounded by what it believes it can do. Stale docs create artificial limitations.
  When refactoring a subsystem, the workspace identity files are part of the blast
  radius.
- Open feedback loops produce prose, not progress. If you build a system that
  generates insights but has no structured output format and no downstream consumer,
  you haven't closed a loop — you've built a log printer. Every analysis needs:
  (1) structured output, (2) a table to write to, (3) a downstream consumer.

### Error Handling
- Guard every memory pipeline entry point against error responses. If the response
  is an error, it has no business entering the memory system.
- `try/except -> logger.warning` is a code smell in background services. If an
  exception is worth catching, it's worth handling properly or alerting on.
- Bare `except: pass` hides signature bugs indefinitely. At minimum: log the error.
  Better: narrow the catch. Best: test the logging path.

### Multi-Provider Architecture
- Providers and models are not independent axes. When you switch providers, you must
  also switch models. Each provider needs a default model, context window, and pricing.
- Provider ordering matters in failover chains. Make ordering explicit and intentional,
  not based on dict declaration order.
- Put the model's native provider first in failover chains. Gateways should be
  fallbacks, not primary.
- When an API response contains authoritative data (actual model used, actual tokens),
  always extract and propagate it. The request says what you asked for; the response
  says what you got. These are not the same thing.

### Cron / Scheduled Tasks
- When an LLM fires into a cold session with just a bare string, assume it will
  interpret that string as a task, not a message to relay. Always frame the intent
  explicitly (e.g., `[SCHEDULED REMINDER — deliver as-is]`).
- When a background service delivers a message to a user's chat, inject a breadcrumb
  into the user's active session. This bridges the session boundary.
- Never interpret naive datetimes as UTC on a UTC server when the user is in a
  different timezone. Apply timezone info when available.

### Testing
- Tests belong in version control. Only gitignore generated directories.
- Use provider-agnostic test fixtures. Assert behavior (failover worked, circuit
  breaker fired), not implementation details (specific provider count/order).

### SQLite
- Never read SQLite rows by positional index when the schema can change via ALTER
  TABLE. Use explicit column names in SELECT. ALTER TABLE always appends columns.
- Verify table names against actual schema before writing queries.

### Observability
- Always add observability when adding a feature. If it runs in the background, it
  needs a line in the status endpoint. If you can't see it, you can't trust it.
- Display thresholds must match data reality. Check actual data distribution before
  setting thresholds.
- Silent failures are the most expensive bugs. Default to loud.

---

## User Working Style

- Prefers tough, direct feedback — no sugarcoating
- Wants to be challenged and have ideas improved
- Values concise communication over verbose explanations
- Runs multiple Claude Code sessions in parallel (expect parallel edits)
- Thinks in terms of systems and feedback loops, not features
- Cares deeply about cost efficiency (free tier first, cheap tier second, expensive only when needed)
- Expects session handoffs to be durable — if it's not written down, it didn't happen
- Will correct mistakes directly — extract the lesson and persist it
- Architecture-first thinker: wants to understand the system before building
- Iterates rapidly but expects each iteration to be clean and verified
