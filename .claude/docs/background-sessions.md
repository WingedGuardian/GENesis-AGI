# Background Sessions — Decision Guide

Genesis can run background CC sessions via the `direct_session_run` MCP tool.
Read this guide any time you're considering a background session or sub-agent.

## Background Session vs Sub-agent

| Situation | Use |
|---|---|
| Task > 20 minutes | Background session |
| Needs browser automation with a persistent profile | Background session |
| Quick research returning results to this conversation | Sub-agent |
| Parallel analysis with no memory writes needed | Sub-agent |

**Default heuristic:** If you'd need to resume it later, or if results need to
outlive this conversation → background session. If you just need an answer in
the next few minutes → sub-agent.

## Profiles

| Profile | Browser | observation_write | outreach_send | follow_up_create | Web search |
|---|---|---|---|---|---|
| `observe` | ✗ | ✗ | ✗ | ✗ | ✓ |
| `research` | ✗ | ✓ | ✗ | ✓ | ✓ |
| `interact` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `steward` | ✗ | ✓ | ✓ | ✓ | ✓ |

Most profiles block: Bash, Edit, Write, task_submit, settings_update,
direct_session_run, module_call. Use `interact` for workflows that operate
external platforms (publishing, form filling) and need to communicate with the
user. Use `research` for investigation that writes observations/follow-ups;
it also reaches the `genesis-recon` discovery tools (GitHub/model-intel/skill
scanning, findings storage) — the only profile that does.
Use `observe` for read-only investigation.

**`steward` is the one built-in Bash-enabled profile** — its Bash is restricted
to the `gh` CLI only, enforced by `scripts/bash_safety_hook.sh` via the
`GENESIS_BASH_ALLOWLIST` env var set from `CCInvocation.bash_allowlist`. It
still blocks Edit/Write/browser. Built for the upstream-PR stewardship
campaign: it reads/comments/reopens/closes Genesis's own PRs to external repos
and escalates code-change requests rather than editing or pushing itself. A
profile grants a scoped shell by appearing in `_PROFILE_BASH_ALLOWLIST`
(`src/genesis/cc/direct_session.py`); without an entry there, a Bash-granting
profile's shell is governed only by the global destructive-op blocks. The
allowlist matches the command's **first token** and blocks all
chaining/piping/substitution/redirection (`; && | $() ` ` > <`).

### Install-local profiles (overlay)

A deployment can register extra profiles — including Bash-scoped ones — without
editing the tracked `direct_session.py`, by adding an optional, gitignored
`genesis/cc/profile_overlay.py` exposing `register(ctx)`. The loader
(`_load_profile_overlays`) is a no-op when that module is absent (the default).
`ctx` is a `ProfileOverlayContext` that hands over the same building-block
disallow lists the built-ins use plus the venv-Python path, and an
`add_profile(name, *, disallow, addendum, bash_allowlist=(), mcp_profile=...,
skills=...)` method. `add_profile` refuses to redefine a built-in profile, so an
overlay can only add. This keeps install-specific session profiles (their names,
prompts, and tool scope) out of the shared repo while the generic mechanism
ships upstream. Note: allowlisting an interpreter (e.g. the venv Python) pins
only the command's first token — `python -c`/`python <file>` still pass — so an
interpreter-scoped overlay profile relies on its addendum for the
behavioural "only run module X" restriction, appropriate only for trusted
(Genesis-internal) sessions, not untrusted input.

## Memory Access Policy

Background sessions have strict memory isolation:

- **Vector store writes (Qdrant) are BLOCKED for ALL profiles.** No background
  session can call `memory_store`, `memory_synthesize`, or `memory_extract`.
  Episodic memory is exclusively for foreground user interactions.
- **Knowledge ingestion is BLOCKED for ALL profiles.** `knowledge_ingest`,
  `knowledge_ingest_batch`, and `knowledge_ingest_source` require explicit user
  authorization in an interactive session.
- **SQLite table writes are profile-gated.** `observation_write`,
  `reference_store`, `procedure_store` are available to research/interact but
  not observe. These write to structured tables, not vector stores.
- **Server-side code is unaffected.** Ego corrections, reflection output, and
  other server-side `MemoryStore.store()` calls bypass tool-level blocking
  because they don't go through MCP.
- **The session output IS the deliverable.** Background session findings belong
  in the final message (session transcript), not in vector stores. The
  foreground user reviews and decides what to persist.

## Key Parameters

- **`timeout_minutes`** — default 15, max 60. Use 60 for long research tasks.
  The clock runs the entire time, including during rate limit waits.
- **`model` / `effort`** — default Sonnet/High. Haiku for cheap bulk tasks.
- **`profile`** — see table above. Choose the minimum profile that covers the task.

## Always Write Progress Incrementally

**Rule: instruct every background session to write findings to memory as it
discovers them, not as a final batch at the end.**

Why: Background sessions are fragile and cannot be resumed. The timeout clock
runs continuously — including during rate limit waits. Any write committed
before a failure is preserved; any uncommitted work is lost permanently.

**Pattern to use in every prompt:**
> "Write each [finding/target/result] to memory as you find it, not all at the end."

Design your prompt around this constraint. A session that writes 15 of 20 targets
and then times out has delivered value. A session that batches and times out
delivers nothing.

## Rate Limits Are Shared

Background sessions share your account's Claude API rate limit with your
foreground session.

- Rate limit hits don't just block the background session — they block you too
- Rate limit wait time counts against `timeout_minutes` — 5 min waiting = 5 min less work
- Sessions that exhaust timeout during a wait fail with a Telegram failure notification
- Memory writes committed before failure are preserved

**Implication:** Don't run heavy background sessions during active foreground
work. Schedule long research sessions for idle periods.

## Failure Recovery

There is no resume path for failed background sessions. If a session fails:
1. Check memory for partial results (writes before failure are preserved)
2. Relaunch with a prompt that says: "Check memory for existing progress first,
   then continue from where it left off."

Failure modes:
- **Timeout** → Telegram notification + any committed memory writes preserved
- **Rate limit during wait** → countdown expires → same as timeout
- **Crash** → Telegram notification, same recovery path

## Memory Storage for Research Sessions

The core philosophy: **internal → episodic, external → knowledge base.**

**External research** (web sources, online data, third-party information):
- `memory_type`: `"knowledge"` (routes to `knowledge_base` collection)
- `confidence`: `0.5` (default — not vetted yet)
- After human review, promote via `knowledge_ingest` (0.85 confidence boost + dedup)

**Internal research** (about Genesis itself, codebase analysis, architecture):
- `memory_type`: `"episodic"` (routes to `episodic_memory` collection)
- This is Genesis reflecting on itself — internal context, not external facts

For both types, include a descriptive `source` tag (e.g. `"research_session"`,
`"podcast_research"`) and topic `tags` for recall.

## MCP Tool

```
direct_session_run(
    prompt="...",
    profile="research",      # observe | interact | research
    timeout_minutes=60,      # 15 default, up to 60 for long research
    model="sonnet",          # sonnet | opus | haiku | fable
    effort="high",
)
```
