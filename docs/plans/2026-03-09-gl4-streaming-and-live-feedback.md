# GL-4: Streaming and Live Feedback

> **Status:** COMPLETE (2026-03-10) — Steps 1-4 implemented
> **Depends on:** GL-3 (COMPLETE 2026-03-10), Phase 7 (for triage pipeline)
> **Phase dependencies:** Phase 7 (required for full value; core streaming infra is unblocked)
> **Not in scope:** Web UI CC chat widget (Phase 8 deliverable)

---

## Problem

`CCInvoker` currently uses `await proc.communicate()` — it blocks until the
CC subprocess finishes entirely, then returns everything at once. For short
exchanges this is fine. For any task that involves real agentic work (file
search, subagent spawning, test execution, MCP calls), the user sees nothing
for minutes, then gets a wall of text.

This is inherent to `--output-format json` (final-result mode). CC also
supports `--output-format stream-json`, which emits newline-delimited JSON
events as work progresses — tool calls starting, text deltas, subagent
activity, errors — before the final result.

GL-4 switches to streaming, enabling:
- Live progress updates during long operations
- Intermediate tool-call feedback ("reading files...", "running tests...")
- Lower perceived latency for users on all channels

---

## What GL-4 Does NOT Change

- The `claude -p` subprocess model itself — still the same CLI invocation
- Background sessions (reflection, tasks) — they do not need streaming;
  latency does not matter there, results are stored async anyway
- The `ConversationLoop` contract — `handle_message()` still returns a string;
  streaming is surfaced below that layer via callbacks

---

## Architecture

### Stream-JSON Event Shape

The CC CLI emits these event types with `--output-format stream-json`:

```
{"type": "system", "subtype": "init", "session_id": "uuid", ...}
{"type": "assistant", "message": {"content": [{"type": "text", "text": "..."}]}}
{"type": "tool_use", "name": "Bash", "input": {"command": "..."}}
{"type": "tool_result", "content": [...]}
{"type": "result", "subtype": "success", "result": "...", "total_cost_usd": 0.04, ...}
```

Note: Verify exact shape against `scripts/test_cc_cli.sh` output (run outside
CC) before implementing. The test script captures stream-json at
`scripts/cc_cli_output/test_stream_json.txt`.

### Streaming Invoker

Replace `proc.communicate()` with line-by-line stdout reading:

```python
class StreamingCCInvoker(CCInvoker):
    async def run_streaming(
        self,
        invocation: CCInvocation,
        on_event: Callable[[StreamEvent], Awaitable[None]] | None = None,
    ) -> CCOutput:
        args = self._build_args(invocation)
        # Override output format to stream-json
        idx = args.index("json")
        args[idx] = "stream-json"

        proc = await asyncio.create_subprocess_exec(
            *args, stdout=PIPE, stderr=PIPE, env=self._build_env()
        )

        result_data = None
        async for raw_line in proc.stdout:
            line = raw_line.decode().strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if on_event:
                await on_event(StreamEvent.from_raw(event))

            if event.get("type") == "result":
                result_data = event

        await proc.wait()
        return self._build_output_from_result(result_data, invocation)
```

### StreamEvent Type

```python
@dataclass(frozen=True)
class StreamEvent:
    event_type: str          # "text_delta", "tool_use", "tool_result", "done"
    text: str | None         # for text_delta
    tool_name: str | None    # for tool_use
    tool_input: dict | None  # for tool_use
    raw: dict                # original event

    @classmethod
    def from_raw(cls, raw: dict) -> StreamEvent:
        ...
```

### Channel Adapters for Live Output

Each channel surfaces streaming events differently:

**Terminal (GL-2):**
```
you: search for all usages of CCInvoker in the codebase

genesis: [Bash: ls src/genesis/cc]
genesis: [Read: invoker.py]
genesis: [Read: test_invoker.py]
genesis: Found 4 usages across 2 files...
```

**Telegram (GL-3+):**
- Send a "working..." message immediately on first event
- Edit it in-place as progress updates arrive (Telegram supports message editing)
- Replace with final response when done

**Background sessions:**
- No streaming needed — `CCInvoker.run()` unchanged

---

## Implementation Steps

### Step 1: Verify stream-json event shapes (prerequisite)

Run `scripts/test_cc_cli.sh` in a plain terminal. Inspect
`scripts/cc_cli_output/test_stream_json.txt` to confirm exact event shapes.
Update the architecture above if shapes differ.

This MUST happen before writing the streaming parser. The event shape is
empirically determined.

### Step 2: StreamEvent types + run_streaming()

**Files:**
- `src/genesis/cc/types.py` — add `StreamEvent` dataclass
- `src/genesis/cc/invoker.py` — add `run_streaming()` method
- `tests/test_cc/test_invoker.py` — streaming tests with mock subprocess

Keep `run()` (non-streaming) intact. `run_streaming()` is additive.

### Step 3: Terminal live output

**Files:**
- `src/genesis/cc/terminal.py` — wire `run_streaming()` with progress callback

```python
async def on_event(event: StreamEvent) -> None:
    if event.event_type == "tool_use":
        print(f"\r[{event.tool_name}...]", end="", flush=True)
    elif event.event_type == "text_delta" and event.text:
        print(event.text, end="", flush=True)
```

### Step 4: ConversationLoop streaming mode

**Files:**
- `src/genesis/cc/conversation.py` — add `handle_message_streaming()` accepting
  an `on_event` callback

Non-streaming `handle_message()` stays for backward compat.

### Step 5: Telegram streaming (GL-3 dependency)

Wire streaming into the Telegram relay using Telegram's `editMessageText` API
to update a "working..." message in place as progress arrives. Lives in the
GL-3 plan but listed here as the downstream consumer GL-4 enables.

---

## Phase Dependencies

GL-4 is **not blocked by any build phase** for its core streaming
infrastructure (Steps 1-4). Those are pure CC integration improvements.

It becomes more valuable as more phases complete:

| Phase | What it adds to GL-4 streaming |
|-------|-------------------------------|
| **Phase 6** | Skill dispatching — foreground benefits from live skill-loading feedback |
| **Phase 7** | Deep reflection — streaming shows consolidation work in progress |
| **Phase 8** | Outreach pipeline — async task execution with live status |
| **Phase 9** | Autonomous tasks — streaming is essential UX for long-running work |

GL-4 is best implemented **after GL-2 is manually validated** but **before
GL-3**, so Telegram gets streaming from day one.

---

## Full GL Sequence and Phase Dependency Map

| GL | What | Phase Dependencies | Status |
|----|------|-------------------|--------|
| **GL-1** | Background reflections via CC | Phases 0-4 (COMPLETE) | COMPLETE |
| **GL-2** | Terminal conversation (foreground CC) | Phases 0-5 (COMPLETE) | COMPLETE (infra); needs manual activation |
| **GL-3** | Telegram relay | GL-2 validated + bot token | **COMPLETE** (2026-03-10) |
| **GL-4** | Streaming and live feedback | Phase 7 + stream-json verification | PLANNED |

**GL-2:** COMPLETE. Manually validated 2026-03-10.

**GL-3:** COMPLETE (2026-03-10). Telegram relay live via `genesis-bridge.service`
(systemd). Minimal-first: foreground conversation only. Background capabilities
wire in as Phase 7+ delivers them. `triage_pipeline=None` for now.

**What GL-4 adds (updated 2026-03-10):**
- Stream-json event shapes confirmed empirically
- `StreamingCCInvoker.run_streaming()` with event callbacks
- Terminal + Telegram live progress (tool calls, text deltas)
- Triage pipeline wiring (deferred from GL-3; Phase 7 `session_config.py` enables this)
- CCOutput enrichment (`tool_calls` field)

**NOT in GL-4 (stays in Phase 8):**
- Web UI CC chat widget — belongs in the Phase 8 Genesis dashboard panel.
  See `2026-03-07-cc-go-live-design.md` and `genesis-v3-build-phases.md` Phase 8.

---

## Background Session Visibility (NOT in GL-4 Scope)

Background sessions completing invisibly (output only to DB) is intentional —
Genesis should not automatically narrate its own internal work to the user.
Surfacing background session results is a judgment call, not a notification
mechanism.

The right home for this:

- **Morning report (Phase 8):** Genesis synthesizes overnight work and chooses
  what to surface in the daily brief. "Last night's reflection identified X"
  only if it's worth the user's attention.
- **Proactive outreach (Phase 8/9):** If a reflection surfaces something
  requiring urgent attention, Genesis decides to reach out — via Telegram or
  the next conversation. This is governed behavior, not automatic forwarding.

The `message_queue` table exists for Genesis-initiated communication that has
passed a significance threshold, not as a firehose of background activity logs.
Automatic "here's what just ran" notifications would be noise, not value.

---

## Progress Timeout Surface

Currently a timed-out CC session returns a silent error. With streaming,
partial output has already been accumulated. GL-4 adds:

- On timeout, return whatever text was collected before the cut-off
- Surface: "Genesis ran out of time but completed this much: [partial output]"

---

## Verification

- [ ] `run_streaming()` reads stdout line-by-line without blocking
- [ ] Tool-use events appear in terminal as work happens, not after
- [ ] Text deltas stream progressively, not as a single block
- [ ] Final `CCOutput` from streaming matches non-streaming for same prompt
- [ ] Background sessions still use non-streaming `run()` — no regression
- [ ] Timeout during streaming returns partial output, not empty string
- [ ] Triage pipeline fires on foreground conversations (Telegram + terminal)

---

*Created: 2026-03-09*
*Status: Planned — implement after Phase 7 complete*
