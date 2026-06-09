"""Stream 1: Programmatic struggle detection.

Parses JSONL transcripts into a compact "action spine" (tool calls +
outcomes), computes a struggle score via heuristics. High score triggers
the Judge LLM for procedure extraction.

Zero LLM cost — purely programmatic. 95%+ of sessions have no struggle
and get skipped entirely.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Struggle score threshold for triggering the Judge.
# Generous (fail-open). Tune after 2 weeks of data.
STRUGGLE_THRESHOLD = 0.3

# Signal weights for score_struggle()
_WEIGHTS = {
    "error_rate": 0.30,
    "retry_count": 0.25,
    "approach_pivots": 0.20,
    "user_corrections": 0.15,
    "length_with_errors": 0.10,
}


def build_action_spine(transcript_path: Path) -> list[dict]:
    """Parse JSONL into a compact action spine.

    Each entry: {
        "turn": int,            # sequential turn number
        "type": "tool"|"user",  # tool call or user message
        "tool": str | None,     # tool name (for tool entries)
        "args_summary": str,    # truncated args or user text
        "outcome": "ok"|"error",# tool result outcome
        "error_text": str,      # error content (if any)
    }

    Reads raw JSONL line-by-line to capture tool_use + tool_result blocks
    that read_transcript_messages() intentionally strips.
    """
    spine: list[dict] = []
    pending_tools: dict[str, dict] = {}  # tool_use_id → spine entry (awaiting result)
    turn = 0

    try:
        with open(transcript_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg = entry.get("message", {})
                content_blocks = msg.get("content", [])
                msg_type = entry.get("type", "")

                if not isinstance(content_blocks, list):
                    # Some entries have string content (user text)
                    if msg_type == "user" and isinstance(content_blocks, str):
                        text = content_blocks[:200]
                        if text.strip():
                            turn += 1
                            spine.append({
                                "turn": turn,
                                "type": "user",
                                "tool": None,
                                "args_summary": text,
                                "outcome": "ok",
                                "error_text": "",
                            })
                    continue

                for block in content_blocks:
                    if not isinstance(block, dict):
                        continue

                    block_type = block.get("type", "")

                    if block_type == "tool_use":
                        turn += 1
                        tool_name = block.get("name", "unknown")
                        tool_input = block.get("input", {})

                        # Compact args summary
                        if isinstance(tool_input, dict):
                            args_text = json.dumps(tool_input, ensure_ascii=False)
                        else:
                            args_text = str(tool_input)
                        args_summary = args_text[:80]

                        spine_entry = {
                            "turn": turn,
                            "type": "tool",
                            "tool": tool_name,
                            "args_summary": args_summary,
                            "outcome": "ok",  # default, updated by tool_result
                            "error_text": "",
                        }
                        spine.append(spine_entry)

                        tool_id = block.get("id", "")
                        if tool_id:
                            pending_tools[tool_id] = spine_entry

                    elif block_type == "tool_result":
                        tool_id = block.get("tool_use_id", "")
                        is_error = block.get("is_error", False)
                        result_content = block.get("content", "")
                        if isinstance(result_content, list):
                            result_content = " ".join(
                                str(b.get("text", "")) for b in result_content
                                if isinstance(b, dict)
                            )

                        if tool_id in pending_tools:
                            entry_ref = pending_tools.pop(tool_id)
                            if is_error:
                                entry_ref["outcome"] = "error"
                                entry_ref["error_text"] = str(result_content)[:200]

                    elif block_type == "text" and msg_type == "user":
                        text = block.get("text", "")
                        if text.strip():
                            turn += 1
                            spine.append({
                                "turn": turn,
                                "type": "user",
                                "tool": None,
                                "args_summary": text[:200],
                                "outcome": "ok",
                                "error_text": "",
                            })

    except FileNotFoundError:
        logger.warning("Transcript not found: %s", transcript_path)
    except Exception:
        logger.warning("Failed to parse transcript for action spine", exc_info=True)

    # Mark orphaned tool_use entries (no matching tool_result) as errors.
    # This happens when sessions crash mid-execution.
    for entry in pending_tools.values():
        entry["outcome"] = "error"
        entry["error_text"] = "no result received (session may have crashed)"

    return spine


def score_struggle(spine: list[dict]) -> float:
    """Score 0-1 based on heuristic struggle signals.

    Signals (weighted):
    - error_rate: proportion of tool calls that errored
    - retry_count: same tool called with different args within 10 turns
    - approach_pivots: distinct tool sequences after error clusters
    - user_corrections: user messages matching correction patterns
    - length_with_errors: long sessions with high error rates
    """
    if not spine:
        return 0.0

    tool_entries = [e for e in spine if e["type"] == "tool"]
    user_entries = [e for e in spine if e["type"] == "user"]
    total_tools = len(tool_entries)

    if total_tools < 3:
        return 0.0  # Too few tool calls to detect patterns

    # ── Signal 1: Error rate ──
    errors = sum(1 for e in tool_entries if e["outcome"] == "error")
    error_rate = errors / total_tools
    error_signal = min(1.0, error_rate / 0.3)  # Normalize: 0.3+ → 1.0

    # ── Signal 2: Retry count ──
    retries = 0
    for i, entry in enumerate(tool_entries):
        if i == 0:
            continue
        # Same tool, within 10 turns of each other
        prev = tool_entries[i - 1]
        if (entry["tool"] == prev["tool"]
                and entry["args_summary"] != prev["args_summary"]
                and entry["turn"] - prev["turn"] <= 10):
            retries += 1
    retry_signal = min(1.0, retries / 3)  # Normalize: 3+ retries → 1.0

    # ── Signal 3: Approach pivots ──
    # Detect tool changes after error clusters
    pivots = 0
    in_error_cluster = False
    last_tool_after_error = None
    for entry in tool_entries:
        if entry["outcome"] == "error":
            in_error_cluster = True
        elif in_error_cluster:
            if entry["tool"] != last_tool_after_error:
                pivots += 1
                last_tool_after_error = entry["tool"]
            in_error_cluster = False
    pivot_signal = min(1.0, pivots / 2)  # Normalize: 2+ pivots → 1.0

    # ── Signal 4: User corrections ──
    # Reuse patterns from failure_detector
    from genesis.learning.failure_detector import _USER_CORRECTION_PATTERNS

    corrections = 0
    for entry in user_entries:
        text = entry.get("args_summary", "")
        for _, pattern in _USER_CORRECTION_PATTERNS:
            if pattern.search(text):
                corrections += 1
                break
    correction_signal = min(1.0, corrections / 1)  # Normalize: 1+ → 1.0

    # ── Signal 5: Session length with errors ──
    length_signal = 0.0
    if len(spine) > 100 and error_rate > 0.2:
        length_signal = 1.0
    elif len(spine) > 50 and error_rate > 0.15:
        length_signal = 0.5

    # ── Weighted combination ──
    score = (
        _WEIGHTS["error_rate"] * error_signal
        + _WEIGHTS["retry_count"] * retry_signal
        + _WEIGHTS["approach_pivots"] * pivot_signal
        + _WEIGHTS["user_corrections"] * correction_signal
        + _WEIGHTS["length_with_errors"] * length_signal
    )

    return min(1.0, score)


def format_spine_for_judge(spine: list[dict]) -> str:
    """Format action spine as compact text for the Judge LLM.

    Output:
    [T=1] TOOL: Bash {"command": "ssh root@..."} -> OK
    [T=2] TOOL: Read {"file_path": "/home..."} -> ERR: not found
    [T=5] USER: "that didn't work, try again"
    """
    lines: list[str] = []
    for entry in spine:
        turn = entry["turn"]
        if entry["type"] == "tool":
            outcome = "OK" if entry["outcome"] == "ok" else f"ERR: {entry['error_text'][:60]}"
            lines.append(
                f"[T={turn}] TOOL: {entry['tool']} {entry['args_summary']} -> {outcome}"
            )
        elif entry["type"] == "user":
            text = entry["args_summary"][:80]
            lines.append(f'[T={turn}] USER: "{text}"')
    return "\n".join(lines)
