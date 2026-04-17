# Part 6A: Automatic Session-End Synthesis Detection

**Status:** Deferred
**Created:** 2026-04-13
**Blocked by:** No `memory_recall` usage in foreground sessions yet

## What

Automatically detect when a session produces a synthesis (combining multiple
recalled memories into a new insight) and store it back as a higher-confidence
memory. This closes the "compounding loop" — knowledge builds on itself.

## Why deferred

Due diligence (2026-04-13) revealed the detection premise is invalid:

1. **No `memory_recall` calls in any foreground transcript.** Zero across 14
   sessions. The memory system works via passive L2 injection (`[Memory]` tags
   from the proactive hook), not active `memory_recall` tool calls.

2. **JSONL reader drops tool-only messages.** `read_transcript_messages()` skips
   assistant messages with no text content — tool_use blocks on their own lines
   are silently dropped, losing tool_names metadata.

3. **No signal to detect synthesis from.** Without explicit recall calls, there's
   no way to distinguish "assistant answered using injected memory context" from
   "assistant answered from its own knowledge."

## Prerequisite to unblock

Either:
- **A)** Sessions start using `memory_recall` explicitly (L3 deep search becomes
  common), giving transcript-scannable signal
- **B)** Redesign detection to match `[Memory]` tags in user messages (L2 proactive
  injection) paired with substantive assistant responses — but this is fuzzy and
  would produce many false positives
- **C)** Skip automatic detection entirely — the explicit `memory_synthesize` MCP
  tool (Part 6B, already built) covers the compounding loop when the LLM is
  instructed to use it

## Also fix when unblocked

- `src/genesis/util/jsonl.py` line 104: tool-only assistant messages should still
  be captured (with empty text) to preserve tool_names metadata for downstream
  consumers like synthesis detection.

## Original design (for when this becomes viable)

```
SessionEnd hook (1500ms budget)
  └─ _trigger_synthesis_detection()
      └─ subprocess.Popen(session_extract.py)  # fire-and-forget
          ├─ read transcript via read_transcript_messages()
          ├─ scan for synthesis candidates (deterministic)
          ├─ for each candidate: LLM extract synthesis content
          └─ store via MemoryStore.store(source_pipeline="synthesis")
```

File to create: `src/genesis/memory/session_extract.py`
File to modify: `scripts/genesis_session_end.py` (add trigger)
