# Background Sessions — Decision Guide

Genesis can run background CC sessions via the `direct_session_run` MCP tool.
Read this guide any time you're considering a background session or sub-agent.

## Background Session vs Sub-agent

| Situation | Use |
|---|---|
| Task > 20 minutes | Background session |
| Needs `memory_store` writes that must persist across sessions | Background session |
| Needs browser automation with a persistent profile | Background session |
| Quick research returning results to this conversation | Sub-agent |
| Parallel analysis with no memory writes needed | Sub-agent |

**Default heuristic:** If you'd need to resume it later, or if results need to
outlive this conversation → background session. If you just need an answer in
the next few minutes → sub-agent.

## Profiles

| Profile | Browser click/fill | memory_store | outreach_send | follow_up_create | Web search |
|---|---|---|---|---|---|
| `observe` | ✗ | ✗ | ✗ | ✗ | ✓ |
| `research` | ✗ | ✓ | ✗ | ✓ | ✓ |
| `interact` | ✓ | ✓ | ✓ | ✓ | ✓ |

All profiles block: Bash, Edit, Write, task_submit, settings_update,
direct_session_run. Use `interact` for workflows that operate external
platforms (publishing, form filling) and need to communicate with the user.
Use `research` for data gathering that writes to memory. Use `observe` for
read-only investigation.

## Key Parameters

- **`timeout_s`** — default 900 (15min). Use 3600 for research tasks. The clock
  runs the entire time, including during rate limit waits.
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
- Rate limit wait time counts against `timeout_s` — 5 min waiting = 5 min less work
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
    timeout_s=3600,          # 900 default, 3600 for long research
    model="claude-sonnet-4-6",
    effort="high",
)
```
