"""Classifier tests. Fixtures are synthetic stand-ins shaped like the real
error texts measured in the corpus (see plan §4-§5, §14); none is a real
transcript excerpt."""

import pytest

from genesis.transcript_analytics.classify import classify, parse_hook_block

# --- flagged errors (is_error=True) -------------------------------------------


@pytest.mark.parametrize(
    "text,denial,expected",
    [
        (
            "PreToolUse:Bash hook error: [${CLAUDE_PROJECT_DIR}/.claude/hooks/genesis-hook "
            "review_enforcement_commit.py]: BLOCKED: no review marker",
            "permission-rule",
            "hook_block",
        ),
        (
            "PreToolUse:Bash hook error: [bash /x/scripts/hooks/bash_safety_hook.sh]: nope",
            None,
            "hook_block",
        ),
        ("The user doesn't want to proceed with this tool use.", "user-rejected", "user_rejected"),
        ("anything at all", "user-rejected", "user_rejected"),
        (
            "<tool_use_error>String to replace not found in file.</tool_use_error>",
            None,
            "tool_use_error",
        ),
        ("2 validation errors for call[memory_store]\nquery\n  Missing", None, "validation"),
        ("Exit code 1\nTraceback (most recent call last):", None, "exit_nonzero"),
        ("ENOSPC: no space left on device, open '/x'", None, "enospc"),
        ("EISDIR: illegal operation on a directory, read", None, "fs_error"),
        ("File does not exist. Note: your current working directory is /x", None, "fs_error"),
        ("This Claude Code session is running stale code", None, "stale_session"),
        (
            "Output does not match required schema: root: must have required property",
            None,
            "schema_mismatch",
        ),
        (
            "Permission for this action was denied by the Claude Code auto mode classifier",
            None,
            "automode_denied",
        ),
        ("File content (270.1KB) exceeds maximum allowed size (256KB).", None, "file_too_large"),
        ("Ripgrep search timed out after 20 seconds.", None, "timeout"),
        (
            "This command could not be parsed safely (e.g. ANSI-C $'...' quoting)",
            None,
            "parse_refused",
        ),
        ("some-model is temporarily unavailable, so auto mode cannot", None, "model_unavailable"),
        ("Error calling tool 'memory_recall': fts5: syntax error", None, "mcp_error"),
        ('{"error":"project not found or not indexed"}', None, "mcp_error"),
        ("POSSIBLY a malformed tool call rather than a missing value", None, "malformed_call"),
        ("Not run: the response that made this tool call was stopped", None, "stopped"),
        ("Permission to use Bash has been denied.", None, "permission_denied"),
        (
            "Error: result (82,026 characters) exceeds maximum allowed tokens. Output has been saved to /x",
            None,
            "oversize_result",
        ),
        ("something nobody has seen before", None, "other"),
        ("something nobody has seen before", "permission-rule", "denied_permission-rule"),
    ],
)
def test_flagged_error_classes(text, denial, expected):
    cls, source = classify(text, is_error=True, denial_kind=denial)
    assert cls == expected
    assert source == "flag"


def test_long_inline_hook_command_is_still_a_hook_block():
    # Real inline `bash -c` hooks put the closing "]:" past char 1,500
    # (measured 1,555-2,309 on 71 rows, 2026-10-04).
    cmd = "bash -c 'IN=$(cat); " + "x" * 1600 + "'"
    text = f"PreToolUse:Bash hook error: [{cmd}]: blocked"
    assert classify(text, is_error=True, denial_kind="permission-rule") == ("hook_block", "flag")
    assert parse_hook_block(text)["hook_script"] == "<inline>"


def test_hook_block_wins_over_denial_kind():
    # A PreToolUse block carries toolDenialKind=permission-rule; the hook text decides.
    text = "PreToolUse:Read hook error: [~/.claude/hooks/gate]: denied"
    assert classify(text, is_error=True, denial_kind="permission-rule") == ("hook_block", "flag")


# --- unflagged results (is_error absent=None or explicitly False) -------------


@pytest.mark.parametrize("flag", [None])
def test_unflagged_oversize_result_is_detected(flag):
    text = "Error: result (135,207 characters) exceeds maximum allowed tokens. Output has been saved to /x"
    assert classify(text, is_error=flag, denial_kind=None) == ("oversize_result", "text")


@pytest.mark.parametrize("flag", [None])
@pytest.mark.parametrize(
    "text",
    [
        "Exit code 1\nfoo",  # success output never starts like this unflagged? stays unclassified
        "Error: something generic",  # generic 'Error' prefix is too weak unflagged
        "File created successfully at: /x",
        "Traceback (most recent call last):",  # e.g. a Read of a log file
        "",
    ],
)
def test_unflagged_weak_text_is_not_an_error(text, flag):
    assert classify(text, is_error=flag, denial_kind=None) == (None, None)


@pytest.mark.parametrize("flag", [None])
def test_unflagged_strong_patterns(flag):
    assert classify("<tool_use_error>x</tool_use_error>", is_error=flag, denial_kind=None) == (
        "tool_use_error",
        "text",
    )
    assert classify("Error calling tool 'x': boom", is_error=flag, denial_kind=None) == (
        "mcp_error",
        "text",
    )


@pytest.mark.parametrize("flag", [None])
@pytest.mark.parametrize(
    "text", ['{"error":null,"results":[1]}', '{"error": false}', '{"errors":[]}']
)
def test_unflagged_json_without_error_message_is_not_an_error(text, flag):
    assert classify(text, is_error=flag, denial_kind=None) == (None, None)


def test_flagged_classification_ignores_the_toolUseResult_prefix_form():
    # CC's toolUseResult is usually "Error: " + content; callers classify the
    # CONTENT block. This pins that the content form classifies correctly and
    # the prefixed form does NOT (so a caller passing the wrong text is caught).
    content = "<tool_use_error>String to replace not found</tool_use_error>"
    assert classify(content, is_error=True, denial_kind=None) == ("tool_use_error", "flag")
    assert classify("Error: " + content, is_error=True, denial_kind=None) == ("other", "flag")


def test_unflagged_with_denial_is_still_detected():
    # A denial record is a refusal even if the flag is missing.
    assert classify("whatever", is_error=None, denial_kind="user-rejected") == (
        "user_rejected",
        "text",
    )


# --- hook block parsing -------------------------------------------------------


def test_parse_hook_block_genesis_dispatcher():
    h = parse_hook_block(
        "PreToolUse:Bash hook error: [${CLAUDE_PROJECT_DIR}/.claude/hooks/genesis-hook "
        "git_push_guard.py --mode x]: BLOCKED: reason here"
    )
    assert h == {
        "hook_event": "PreToolUse",
        "hook_tool": "Bash",
        "hook_command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/genesis-hook git_push_guard.py --mode x",
        "hook_script": "git_push_guard.py",
    }


@pytest.mark.parametrize(
    "command,script",
    [
        ("bash /x/scripts/hooks/bash_safety_hook.sh", "bash_safety_hook.sh"),
        ("python3 /x/scripts/hooks/full_suite_guard.py", "full_suite_guard.py"),
        ("~/.claude/hooks/cbm-code-discovery-gate", "cbm-code-discovery-gate"),
        ("bash -c 'IN=$(cat); echo $IN'", "<inline>"),
        ("/abs/path/guard.sh --flag", "guard.sh"),
    ],
)
def test_parse_hook_script_attribution(command, script):
    h = parse_hook_block(f"PreToolUse:Bash hook error: [{command}]: msg")
    assert h["hook_script"] == script


def test_parse_hook_block_without_tool_and_non_hook_text():
    h = parse_hook_block("UserPromptSubmit hook error: [x/y.py]: m")
    assert (
        h["hook_event"] == "UserPromptSubmit"
        and h["hook_tool"] is None
        and h["hook_script"] == "y.py"
    )
    assert parse_hook_block("Exit code 1") is None
