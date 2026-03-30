# Lessons Learned — Transferable to Genesis v3

> Hard-won insights from 3 months of building an LLM-agent system (nanobot copilot).
> Copilot-specific details have been abstracted. The principles apply to any
> LLM-agent project, especially one built on Agent Zero with multi-provider routing,
> background services, memory systems, and autonomous task execution.
>
> Lessons are numbered to match the original source. Some numbers have duplicates
> (the original file had numbering collisions). All are preserved.

---

## Architecture & Integration

### 1. Don't Build a Separate Proxy — Integrate

Built a FastAPI proxy between the agent framework and all LLMs. Realized it meant
maintaining a separate service, handling its own lifecycle, duplicating error handling,
and adding latency. Integrated directly instead — thin hooks in 5 existing files,
fat modules in a dedicated extension directory.

**Rule**: "Keep it stock" sounds good in theory, but when you're extending something,
integration beats interposition. A drop-in replacement is cleaner than a
man-in-the-middle.

### 10. Scaffold Everything, Wire Later — and Then Actually Wire It

Built code for all phases while focusing the hot path on core features. When completion
time came, the scaffolded modules were production-ready. BUT: scaffolded modules
silently degraded to no-ops because dependencies weren't declared. ImportError was
caught, logged once, and the system continued without the feature.

**Rule**: Scaffolding is an investment, but graceful degradation can mask the fact
that your investment isn't paying off. If a module can silently become a no-op,
add a health check that tells you it's actually working.

### 31a. Subclass Upstream Code — Don't Fork It

Needed to add cognitive context to a heartbeat LLM prompt. The upstream code was
in the framework's core. The instinct was to modify it directly. Instead: created a
subclass that overrides the method. Upstream stays pristine.

**Rule**: When extending upstream/framework code, subclass don't modify. The upstream
file is a contract you don't own — treat it like a library import. Your changes belong
in a derivative class, not in the original file.

---

## LLM Routing & Multi-Provider

### 2/28. The LLM Is the Best Routing Heuristic (Supersedes Heuristic Routing)

Originally: 11-rule heuristic classifier caused 20+ incidents — silent model switches,
cascading failures, wrong context windows. Replaced with: LLM receives a ground-truth
document containing provider health, costs, free tier limits, context windows, and
constraints. LLM proposes a routing plan, validates via API probes, user approves.

**Rule**: Per-message LLM classification is too slow/expensive. But for plan-level
routing — deciding which providers/models to use for a session — the LLM with proper
context makes better decisions than any heuristic. Routing plans are infrequent,
high-context decisions. Use the right tool for each.

### 3. Self-Escalation is the Safety Net That Makes Cheap Routing Safe

When routing to a cheap/local model, inject an instruction: "If this is beyond your
capabilities, start your response with `[ESCALATE]`." If the model does this, the
router automatically retries with a more capable model.

**Rule**: Build escape hatches, not perfect classifiers.

### 6. Failover Chains Need Multiple Cloud Providers

Single-provider fallback = single point of failure. Built FailoverChain with provider
tiers. Each tier tries multiple cloud providers in sequence. Different providers have
different failure modes (rate limits, availability, cold starts). Having 3-4 providers
means at least one is always up.

**Rule**: Multi-cloud isn't just for enterprises. For a system you rely on, provider
diversity is reliability.

### 14. Provider Ordering Matters in Failover Chains

`openai:` was first in the failover chain because it was declared first in the Pydantic
model. Every request first tried OpenAI (which can't route non-OpenAI models), failed,
then fell back. Wasted latency on every call.

**Rule**: Dict iteration order is implicit API. When a dict's order determines priority,
make the ordering explicit and intentional rather than relying on declaration order.

### 16/20. Providers and Models Are Not Independent Axes

`/use minimax` sent an Anthropic model to MiniMax's API. The router treated provider
selection and model selection as separate concerns. Non-gateway providers can't serve
other providers' models.

**Rule**: When adding a non-gateway provider, configure THREE things or it's broken:
(1) default model, (2) context window, (3) pricing. Missing any one causes cascading
silent failures.

### 35. Native Provider Preference in Failover Chains

**Rule**: When building a failover chain for a model, always put the model's native
provider first. Gateways should be fallbacks, not primary. A gateway may route the
request through an unknown intermediary.

### 47. Never Trust Default Values When Ground Truth Exists in the Response

`LLMResponse` was built with `model_used=""`. LiteLLM's response had a `model` field
containing the actual model the API returned — ground truth — and we never read it.
Downstream consumers echoed the requested model name. When a gateway served a different
model, the system reported the wrong one.

**Rule**: When an API response contains authoritative data (actual model used, actual
tokens consumed), always extract and propagate it. The request says what you asked
for; the response says what you got. These are not the same thing.

---

## Token Economics & Cost

### 4. Token-Conscious Storage is Non-Negotiable

Everything in identity/user/memory files gets loaded into every single LLM call.
A 2000-token user profile burns 2000 tokens on every message.

**Rule**: Every token in the system prompt is a recurring cost. Be surgical about what
gets injected. If it doesn't change the LLM's behavior for this specific message,
it doesn't belong in the prompt.

### 17. Unknown Model Metadata Costs Real Money

Model wasn't in the context window table. Default was 8K. Session hit 76% of 8K ->
router triggered overflow escalation -> routed through an expensive model at $0.32.
User didn't request it.

**Rule**: Default to 128K for unknown models. False overflow escalation (unexpected $$$)
is far worse than slightly overestimating context.

---

## LLM Output & Parsing

### 23. Python `.format()` and JSON Templates Don't Mix

Prompt template contained a JSON schema example with braces: `{"facts": [...]}`.
Python's `.format()` treated `{"facts"` as a format variable, throwing `KeyError`.
The error looked like a JSON parsing error, sending us looking at LLM responses.
In reality, `.format()` crashed before the LLM was contacted.

**Rule**: When a prompt template contains literal braces, double them: `{` -> `{{`.
Or use `string.Template` / f-strings. Test prompt templates in isolation.

### 24/32a. LLMs Never Return Bare JSON — Build a Four-Level Fallback

Cloud LLM returned JSON wrapped in preamble. `json.loads()` failed on every
extraction. Gemini regularly adds markdown fences, trailing commas, preamble.

**Rule**: Build `parse_llm_json(text)` with 4 levels: (1) direct `json.loads()`,
(2) extract from markdown fence, (3) regex find outermost braces, (4) strip
trailing commas. If all fail, store raw + alert. Never silently discard.

### 5. Let the LLM Conduct the Interview

Built a structured state machine for onboarding. User said: "It all goes through the
system anyway. Just inform the LLM in natural language."

**Rule**: If you have an LLM in the loop, let it do what LLMs are good at —
conversation, judgment, synthesis. Don't build a state machine when a prompt does
the job better.

---

## Background Services & Autonomy

### 15. Periodic Services Have Distinct Roles — Don't Confuse Them

Multiple services with overlapping names and histories. The cognitive heartbeat (LLM,
2h), health check (programmatic, 30min), and base heartbeat (upstream) had roles that
were inverted in early docs.

**Rule**: Match timer frequency to cost. Health checks are cheap and time-sensitive ->
short interval. LLM cognitive calls are expensive and latency-tolerant -> long interval.

### 19. Every Background Service Needs a Cloud Fallback

Extraction had no cloud fallback. When the local LLM went down, the entire extraction
pipeline fell through to a regex heuristic. Meanwhile, embeddings had a cloud fallback
and kept working fine.

**Rule**: Any background service that depends on a local model MUST have a cloud
fallback. Pattern: local (free) -> cloud (cheap) -> queue for deferred -> heuristic.

### 33a. Open Feedback Loops Produce Prose, Not Progress

Dream cycle had a `_self_reflect()` method producing free-form text. The summary was
delivered and stored, but nothing downstream consumed it. The LLM identified patterns
every night — then they evaporated.

**Rule**: If you build a system that generates insights but has no structured output
and no downstream consumer, you haven't closed a loop — you've built a log printer.
Every analysis needs: (1) structured output (JSON schema), (2) a table to write to,
(3) a downstream consumer that queries that table.

### 33b. Background Services Must Not Share the User Context Pipeline

The heartbeat "continued a philosophical discussion" instead of executing tasks.
Background service prompts about "thinking" and "awareness" semantically matched
philosophical user conversations, causing cross-session memory contamination.

**Rule**: When a shared function is used by both interactive and autonomous callers,
the interactive-specific enrichment must be opt-in or opt-out. Background services
build their own targeted prompts. Any new enrichment must be guarded by an isolation
flag (e.g., `skip_enrichment`).

### 34. Background Service Reports Need Per-Step Accountability

Dream cycle report showed "Quiet night. All systems healthy." when all counters were
zero. Ambiguous — did all jobs run cleanly or did half silently skip?

**Rule**: Every autonomous background service must produce a per-step checklist showing
what ran, what was skipped (and why), and what failed. Silence is ambiguous in
autonomous systems. Wrap every job in a helper that records status, timing, and skip
reason.

### 36. LLM vs Infrastructure Concerns

Provider outage alerts were fed to LLMs via heartbeat events. The LLM commented on
API health uselessly.

**Rule**: Provider outages are infrastructure concerns. Never feed them to LLMs.
Provider health belongs in a status endpoint, not in LLM context.

---

## Memory & Context

### 8. Background Extraction is the Key to Model Switching

When switching from one model to another mid-conversation, the new model has no
context. Background SLM extracts facts, decisions, constraints as structured JSON.
When context needs rebuilding, these serve as compressed briefing (~200 tokens
instead of 5000).

**Rule**: Extraction is compression. Structured extraction is the bridge that makes
multi-model conversations seamless.

### 27. Error Responses Must Be Guarded From Memory Pipeline

When an LLM call failed, the error message was passed through the extraction and
memory pipeline. Error strings were stored as "facts" — "connection refused" and
"API returned 503" as user preferences. Subsequent recall surfaced error artifacts.

**Rule**: Guard every memory pipeline entry point with `not is_error`. Errors are
never valid memory content.

---

## Error Handling & Observability

### 29. Silent Failures Are the Most Expensive Bugs

Audit found 11 silent swallows — `try/except` blocks that caught exceptions and only
logged them (or `pass`). Memory extraction could fail at three layers, each with its
own silent swallow. The 34% fix-commit rate was largely driven by discovering these
after deployment.

**Rule**: `try/except -> logger.warning` is a code smell in background services. If an
exception is worth catching, it's worth handling or alerting on. The cost of a false
alert is one dismissed notification. The cost of a silent failure is hours of debugging
the wrong thing. Default to loud.

### 48. Bare `except: pass` Hides Signature Bugs Indefinitely

`log_route()` required a parameter all callers omitted. Each call was wrapped in
`try: ... except Exception: pass`. TypeError fired on every single LLM call —
silently caught and discarded. Result: zero routing log entries ever written.

**Rule**: `except Exception: pass` around logging/telemetry is a trap. At minimum:
log the error. Better: narrow the catch to expected failures. Best: test the path.

### 21. Always Add Observability When Adding a Feature

After fixing a pipeline, there was no way to verify it was working from the status
endpoint. The fix was invisible to the user.

**Rule**: Every feature that runs in the background needs a line in the status output.
If you can't see it, you can't trust it.

### 26. Display Thresholds Must Match Data Reality

36 memory items existed at confidence 0.5. Display threshold was 0.6, requiring items
to appear twice with matching keys. With varied extraction wording, none matched —
all 36 were invisible.

**Rule**: When adding a display threshold, check the actual data distribution first.

### 37. Per-Provider Alert Dedup

**Rule**: Use `provider_failed:{name}` as alert dedup keys, not a shared
`provider_failed`. Each provider needs its own alert lifecycle so recovery can be
tracked independently.

---

## Agent Loops & Interaction

### 39. Agent Iteration Exhaustion Should Force Completion, Not Discard Work

Hit "Reached 10 iterations without completion." All intermediate tool results and
reasoning were discarded. The agent had done real work — user got a useless error.

**Rule**: On the final iteration, call the LLM without tools. This forces a text-only
completion — the LLM must produce a summary. The nudge at N-3 is good progressive
pressure, but the toolless final call is the safety net that guarantees output.

### 43. Interleaved CoT Reflection Must Not Fire on Every Tool Call

After every tool execution, the agent injected "Reflect on the results." For simple
1-tool requests, this caused verbose essays instead of confirming the action.

**Rule**: Unconditional reflection injection turns the LLM into an essay generator.
Only inject steering messages when they serve a purpose.

### 7. Natural Language Approvals > Structured Formats

When approvals come via chat (especially mobile/voice), forcing structured responses
creates friction. "yeah go for it" -> approve. "no too risky" -> deny.

**Rule**: Match your interaction model to your interface. Chat is a conversation —
let approvals be conversational.

---

## Cron & Scheduling

### 31b. Cron Reminders Need Delivery Framing

Reminder fired on time but the LLM interpreted "check in with Data" as a task (ran
a status check instead of delivering the reminder). Another reminder was never created
— the LLM hallucinated a successful tool call.

**Rule**: When an LLM fires into a cold session with a bare string, it will interpret
it as a task, not a message. Always frame intent explicitly (e.g.,
`[SCHEDULED REMINDER — deliver as-is]`). Also: require tool-result evidence (like a
job ID) in confirmations to make hallucinations detectable.

### 44. Cron `at` Parameter Must Respect Timezone

User set a reminder for 5:12 PM EST. The `at` parameter received a naive ISO string.
Python's `datetime.fromisoformat()` created a naive datetime, `.timestamp()`
interpreted it as UTC on the UTC server, causing 5-hour offset.

**Rule**: Never interpret naive datetimes as UTC on a UTC server when the user is in a
different timezone. Apply timezone info when available.

### 45. Cross-Session Message Delivery Needs Breadcrumbs

Reminder fired in a background session, delivered to user's chat. User replied. Bot
had zero context because the reply loaded the user's session, which had no record of
the delivered message.

**Rule**: When a background service delivers a message, inject a breadcrumb into the
user's active session. Bridges the session boundary.

### 38. Asyncio Timer Tasks Die Silently — Always Re-arm in Finally

A periodic timer silently stopped firing. The callback method called save then re-arm
sequentially — if save threw, re-arm never executed and the timer was permanently dead.

**Rule**: Any async callback that re-arms a timer MUST use `try/finally` with re-arm
in `finally`. Asyncio doesn't propagate task exceptions to the event loop by default.

---

## Identity & Self-Model

### 32b. Identity File Staleness Creates Capability Blindness

The agent told its user "I have basic tools" and "I need you to guide each step."
In reality it had 20+ tools including browser automation, git, task system. The
identity files were stale — describing removed features and missing entire tool
categories.

**Rule**: When refactoring a subsystem, the workspace identity files are part of the
blast radius. An LLM's capabilities are bounded by what it believes it can do.
Add "update workspace docs" as a mandatory step in refactor checklists.

---

## Infrastructure & Operations

### 13. Check for Existing Infrastructure Before Creating New

Gateway wouldn't stay up. SIGTERM killed it ~3 seconds after every startup. Root cause:
a systemd service already existed. We created a second one for the same process. Both
were enabled, both started on boot, each one's startup killed the other's process.

**Rule**: Before creating new process managers (systemd services, cron entries),
always check what already exists. `systemctl --user list-units --type=service --all`.

### 25. Always Restart Via the Process Manager — Never Manual

When restarting a service, the instinct is `kill <pid> && nohup ... &`. This creates
an orphan process outside the process manager's control. The manager's `Restart=always`
then starts its own instance, which can't bind the port, crash-loops, and you debug a
"won't start" problem you caused.

**Rule**: If a service has a process manager (systemd, supervisor), use it. Never
bypass it with manual process launches.

### 12. Don't Over-Engineer the Config

For a single-user system, simple config beats "proper" config. One JSON file, Pydantic
validates on load. You don't need YAML templating or environment variable overrides.

---

## Database

### 18. Verify Table Names Against Actual Schema

Status aggregator queried `route_log` table. Actual name was `routing_log`. The query
silently failed (caught exception), returning no data.

**Rule**: Always verify table names with `.tables` before writing queries. Silent
exception handling around DB queries can mask schema mismatches.

### 30. ALTER TABLE Column Ordering Breaks Positional Index Reads

Added a column to a table. Schema had it at position 4. `ALTER TABLE ADD COLUMN`
appends to the end. In existing databases it was at position 6. Status showed wrong
values.

**Rule**: Never read SQLite rows by positional index when schema can change via ALTER
TABLE. Use explicit column names in SELECT.

---

## Testing & Code Quality

### 40. Provider-Agnostic Test Fixtures Beat Hardcoded Assertions

Tests hardcoded `assert len(cloud_default) == 4`. When we added native provider
preference, tests broke because they assumed a specific provider order.

**Rule**: When tests depend on external mutable state, extract into fixtures that
query current state. Assert behavior, not implementation details.

### 41. Tests Belong in Version Control

`tests/` was in `.gitignore`. Test changes couldn't be reviewed in PRs.

**Rule**: Version control ALL non-generated files. Only gitignore generated
directories (`__pycache__`, `.pytest_cache`, `build/`).

### 42. Run Linting Before Committing, Not After

Built a 12-file feature, all tests passing, then hit 6 lint errors on commit.
Each fix required re-staging.

**Rule**: Run `ruff check` (or equivalent) on changed files before `git add`, not
after `git commit`.

### 46. Uncommitted Changes Are Not Changes

Previous session implemented 4 code changes, ran all tests, verified in production —
but never committed. Next session found all changes gone after a branch merge.

**Rule**: If you implemented it but didn't commit it, it doesn't exist. The commit
is the unit of durability, not the file edit.

---

## Misc

### 9. The Channel Layer Needs the Most Resilience

The WhatsApp bridge is the simplest code in the stack but needs the most resilience.
A brilliant routing system means nothing if messages don't arrive.

**Rule**: The channel layer is your user-facing surface. It can be simple code, but
it needs the most resilience.

### 11. Private Mode is a Trust Feature

`/private` routes everything through the local model only. No cloud calls.

**Rule**: Privacy isn't just compliance — it's a feature that changes how users
interact. Build it early.

### 22. Backfill Tools Pay for Themselves

When fixing a broken pipeline, always ask: "what about the data that was missed?"

**Rule**: Build backfill tools for any pipeline that was broken. Historical data
shouldn't be permanently lost.
