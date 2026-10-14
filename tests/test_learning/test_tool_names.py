"""The tool-name normalizer applied to every stored tool_trigger."""

import json

import pytest

from genesis.learning.procedural import tool_names
from genesis.learning.procedural.operations import store_procedure, store_procedure_checked
from genesis.learning.procedural.tool_names import BUILTIN_TOOLS, normalize_tool_trigger


@pytest.mark.parametrize(
    ("values", "tools", "patterns", "dropped"),
    [
        (None, [], [], []),
        (["Bash", "Read", "Agent"], ["Bash", "Read", "Agent"], [], []),
        ("Edit", ["Edit"], [], []),
        (
            ["mcp__claude_ai_Gmail__search_threads"],
            ["mcp__claude_ai_Gmail__search_threads"],
            [],
            [],
        ),
        (["browser_navigate"], ["mcp__genesis-health__browser_navigate"], [], []),
        (["ssh", "SSH"], ["Bash"], [r"\bssh\b"], []),
        (["git push"], ["Bash"], [r"\bgit\s+push\b"], []),
        (["Bash", "pytest"], ["Bash"], [r"\bpytest\b"], []),
        (["Python", "Git", "review gate", "Grep"], ["Grep"], [], ["Python", "Git", "review gate"]),
        # Built-ins match exactly: the CLI word `grep` is not the Grep tool.
        (["grep"], [], [], ["grep"]),
        (["ListAgents", "Artifact"], ["ListAgents", "Artifact"], [], []),
        # A Genesis server's full name must be one it declares.
        (["mcp__genesis-health__not_a_tool"], [], [], ["mcp__genesis-health__not_a_tool"]),
        (["mcp__genesis-health__health_status"], ["mcp__genesis-health__health_status"], [], []),
    ],
)
def test_each_mapping_class(values, tools, patterns, dropped):
    assert normalize_tool_trigger(values) == (tools, patterns, dropped)


def test_the_builtin_set_covers_the_three_existing_lists():
    from genesis.cc.session_config import _REFLECTION_DENY_BUILTINS
    from genesis.cc.types import SPAWN_TOOL_NAMES
    from genesis.experimentation.cc_router import _CLI_DISALLOWED_TOOLS

    assert (
        set(_REFLECTION_DENY_BUILTINS) | set(SPAWN_TOOL_NAMES) | set(_CLI_DISALLOWED_TOOLS)
        <= BUILTIN_TOOLS
    )


def test_the_mcp_short_names_match_the_registered_tools():
    """The map is read from source without importing the servers; this pins it
    to what each server really registers, so drift fails here."""
    from genesis.cc.session_config import _registered_mcp_tool_names

    registered = {}
    for server, module in tool_names._MCP_MODULES.items():
        for name in _registered_mcp_tool_names(module):
            registered.setdefault(name, []).append(f"mcp__{server}__{name}")
    unique = {short: full[0] for short, full in registered.items() if len(full) == 1}
    mapped = tool_names._mcp_short_names()
    # Every registered tool maps to its real full name. The map may hold more:
    # a few modules register their tools when the runtime starts (user_job_tools),
    # which a plain import of the server does not do.
    assert {short: mapped.get(short) for short in unique} == unique


def test_the_prompt_guidance_names_the_tools_without_braces():
    text = tool_names.TOOL_TRIGGER_GUIDANCE
    assert "Bash" in text and "mcp__<server>__<tool>" in text
    assert "{" not in text and "}" not in text  # the extractor prompt is a str.format template


_ROW = dict(
    task_type="deploy",
    principle="launch deploys detached",
    steps=["detach it"],
    tools_used=["Bash"],
    context_tags=["deploy"],
)


async def test_store_procedure_stores_only_tool_names(db):
    from genesis.db.crud import procedural

    pid = await store_procedure(db, tool_trigger=["ssh", "Python", "Read"], **_ROW)
    row = await procedural.get_by_id(db, pid)
    assert json.loads(row["tool_trigger"]) == ["Bash", "Read"]


async def test_a_judge_shaped_store_reports_what_it_dropped(db):
    from genesis.db.crud import procedural

    result = await store_procedure_checked(db, tool_trigger=["ssh", "review gate"], **_ROW)
    row = await procedural.get_by_id(db, result.procedure_id)
    assert json.loads(row["tool_trigger"]) == ["Bash"]
    assert any("review gate" in w for w in result.warnings)
    # The mapped command is reported too: until its pattern is stored, the row
    # fires on any Bash call its tags match.
    assert any("ssh" in w and "Bash" in w for w in result.warnings)


@pytest.mark.parametrize("incoming", [None, ["Python", "fail-open"]])
async def test_a_refine_without_a_usable_trigger_keeps_the_stored_one(db, incoming):
    from genesis.db.crud import procedural
    from genesis.learning.procedural.embedding import pack_embedding

    blob = pack_embedding([1.0] + [0.0] * 1023)
    first = await store_procedure_checked(
        db, tool_trigger=["Bash"], principle_embedding=blob, **_ROW
    )
    second = await store_procedure_checked(
        db, tool_trigger=incoming, principle_embedding=blob, **_ROW
    )
    assert second.action == "updated" and second.procedure_id == first.procedure_id
    row = await procedural.get_by_id(db, first.procedure_id)
    assert json.loads(row["tool_trigger"]) == ["Bash"]


async def test_an_explicit_teach_can_clear_a_trigger_and_an_extraction_cannot(db):
    from genesis.db.crud import procedural
    from genesis.learning.procedural.embedding import pack_embedding

    blob = pack_embedding([1.0] + [0.0] * 1023)
    first = await store_procedure_checked(db, tool_trigger=["Bash"], principle_embedding=blob, **_ROW)
    # An auto-extraction's empty list keeps the trigger.
    await store_procedure_checked(db, tool_trigger=[], principle_embedding=blob, draft=1, **_ROW)
    row = await procedural.get_by_id(db, first.procedure_id)
    assert json.loads(row["tool_trigger"]) == ["Bash"]
    # An explicit teach's empty list clears it.
    await store_procedure_checked(db, tool_trigger=[], principle_embedding=blob, **_ROW)
    row = await procedural.get_by_id(db, first.procedure_id)
    assert row["tool_trigger"] is None
