"""ProgressSummarizer: bounded lines, tool lines, text coalescing."""

from __future__ import annotations

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from genesis.live_agent.progress import MAX_LINE_BYTES, ProgressSummarizer, clip


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def delta(text: str, parent: str | None = None) -> StreamEvent:
    event = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}
    return StreamEvent(uuid="u", session_id="s", event=event, parent_tool_use_id=parent)


def result(**kw) -> ResultMessage:
    base = dict(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
    )
    return ResultMessage(**{**base, **kw})


def test_clip_bounds_utf8_bytes_and_says_what_it_left_out():
    text = "é" * 400  # 800 bytes
    out = clip(text)
    assert len(out.encode()) <= MAX_LINE_BYTES
    assert out.endswith("[+800 bytes total]")
    assert clip("short") == "short"


def test_tool_use_and_tool_failure_lines():
    s = ProgressSummarizer()
    use = ToolUseBlock(
        id="t1",
        name="Bash",
        input={"command": "pytest  tests/x.py", "description": "Run the x tests"},
    )
    assert s.feed(AssistantMessage(content=[use], model="m")) == ["tool: Bash — Run the x tests"]
    failed = UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="boom", is_error=True)])
    assert s.feed(failed) == ["tool failed: Bash"]


def test_text_is_coalesced_until_a_sentence_ends_or_the_max_interval():
    clock = Clock()
    s = ProgressSummarizer(clock=clock)
    s.feed(delta("Looking at the"))
    clock.now = 0.5
    assert s.flush() is None  # mid-sentence, under the 1 s ceiling
    s.feed(delta(" ledger now."))
    assert s.flush() == "Looking at the ledger now."
    s.feed(delta("Next I will"))
    clock.now = 1.6
    assert s.flush() == "Next I"  # ceiling reached: up to the last whole word
    assert s.flush(force=True) == "will"


def test_the_ceiling_never_splits_a_word():
    # Measured live: a 1 s ceiling flush split "KIWI" into "K" / "IWI".
    clock = Clock()
    s = ProgressSummarizer(clock=clock)
    s.feed(delta("K"))
    clock.now = 1.5
    assert s.flush() is None  # no word boundary yet
    s.feed(delta("IWI PL"))
    clock.now = 3.0
    assert s.flush() == "KIWI"
    s.feed(delta("UM."))
    clock.now = 3.5
    assert s.flush() == "PLUM."


def test_subagent_text_is_not_narrated():
    s = ProgressSummarizer()
    s.feed(delta("inner chatter.", parent="t1"))
    assert s.flush(force=True) is None


def test_result_flushes_pending_text_and_leaves_the_final_line_to_the_host():
    s = ProgressSummarizer()
    s.feed(delta("Almost there"))
    assert s.feed(result(result="done")) == ["Almost there"]  # never a second "finished"
    final = ProgressSummarizer.final_line
    assert final(result(result="All  three tests pass.")) == "finished: All three tests pass."
    assert final(result(terminal_reason="aborted_streaming")) == "interrupted"
    assert final(result(is_error=True, api_error_status=429)) == "error: success (HTTP 429)"


def test_completed_tools_leave_no_progress_cache():
    s = ProgressSummarizer()
    for error in (False, True):
        s.feed(AssistantMessage(content=[ToolUseBlock(id="t1", name="Bash", input={})], model="m"))
        lines = s.feed(UserMessage(content=[ToolResultBlock(tool_use_id="t1", is_error=error)]))
        assert lines == (["tool failed: Bash"] if error else [])
        assert s._tool_names == {}
    s.feed(AssistantMessage(content=[ToolUseBlock(id="aborted", name="Read", input={})], model="m"))
    s.feed(result(terminal_reason="aborted_tools"))
    assert s._tool_names == {}
