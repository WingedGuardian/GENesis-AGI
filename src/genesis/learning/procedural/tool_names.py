"""One normalizer for every ``tool_trigger`` a procedure stores.

``tool_trigger`` is matched against the name of the tool Claude Code is about to
call, so only a real tool name can ever fire. The two LLM producers (the
extractor and the judge) were given no list and wrote CLI words (``ssh``), MCP
short names (``browser_navigate``) and prose (``review gate``) beside real names.
:func:`normalize_tool_trigger` keeps the real names, repairs the two classes that
mean a real tool, and drops the rest. It is pure and deterministic.

The built-in set is seeded from the lists the CC session builders already keep.
``experimentation.cc_router`` holds a third list and is deliberately NOT imported
(it loads the provider stack); a test pins it as a subset instead.
"""

from __future__ import annotations

import ast
import functools
import logging
import re
from pathlib import Path

from genesis.cc.session_config import _REFLECTION_DENY_BUILTINS
from genesis.cc.types import SPAWN_TOOL_NAMES

logger = logging.getLogger(__name__)

# fmt: off
BUILTIN_TOOLS = frozenset({
    *_REFLECTION_DENY_BUILTINS, *SPAWN_TOOL_NAMES,
    "Read", "Glob", "Grep", "LS", "MultiEdit", "WebFetch", "WebSearch", "TodoWrite",
    "NotebookRead", "ExitPlanMode", "EnterPlanMode", "AskUserQuestion", "ToolSearch",
    "LSP", "CronList", "TaskGet", "TaskList", "TaskOutput", "BashOutput", "KillShell",
    "ListMcpResourcesTool", "ReadMcpResourceTool", "ReadMcpResourceDirTool", "ListAgents",
    "Artifact", "ArtifactComments", "ArtifactData", "SendFeedback", "EndConversation",
})
# fmt: on

_MCP_NAME = re.compile(r"mcp__[A-Za-z0-9_-]+__[A-Za-z0-9_-]+")

# Shell commands an LLM names as if they were tools. Each becomes Bash plus a
# pattern on the command, which the trigger_spec tickets store; until then the
# pattern is only reported. Matched case-insensitively (`SSH`, `Pytest`), after
# the exact built-in check, so the Grep tool never reads as the CLI `grep`.
# fmt: off
_CLI_COMMANDS = (
    "ssh", "scp", "pytest", "ruff", "gitleaks", "pgrep", "incus", "gh", "gh pr create",
    "git push", "git commit", "git add", "git worktree add",
)
# fmt: on
_CLI_PATTERNS = {
    phrase: r"\b" + r"\s+".join(map(re.escape, phrase.split())) + r"\b" for phrase in _CLI_COMMANDS
}

# Genesis MCP servers and the modules that register their tools.
_MCP_MODULES = {
    "genesis-health": "genesis.mcp.health",
    "genesis-memory": "genesis.mcp.memory",
    "genesis-outreach": "genesis.mcp.outreach_mcp",
    "genesis-recon": "genesis.mcp.recon_mcp",
}

_GENESIS_PREFIXES = tuple(f"mcp__{server}__" for server in _MCP_MODULES)

TOOL_TRIGGER_GUIDANCE = (
    "Use only these tool names: "
    + ", ".join(sorted(BUILTIN_TOOLS))
    + ", or an MCP tool written in full as mcp__<server>__<tool>. For a shell command"
    " such as ssh or git push, use Bash. Anything else is discarded."
)


def _mcp_short_names() -> dict[str, str]:
    """Short MCP tool name -> full name, for names exactly one Genesis server has."""
    return {short: full[0] for short, full in _mcp_owners().items() if len(full) == 1}


def _genesis_mcp_names() -> frozenset[str]:
    """Every full ``mcp__genesis-*__<tool>`` name the Genesis servers declare."""
    return frozenset(name for names in _mcp_owners().values() for name in names)


@functools.cache
def _mcp_owners() -> dict[str, list[str]]:
    """Each Genesis MCP tool's short name -> its full name(s).

    Read from the servers' source rather than by importing them: importing all
    four takes seconds and registers every tool in the importing process. Every
    tool is declared with a bare ``@mcp.tool()``, so the function name is the
    tool name; a test pins every registered name to its entry here. The scan
    also finds tools a module registers only when the runtime starts, which is
    still their real full name. A file that cannot be read maps nothing, so its
    short names are dropped rather than guessed.
    """
    root = Path(__file__).resolve().parents[2]
    owners: dict[str, list[str]] = {}
    for server, module in _MCP_MODULES.items():
        base = root.joinpath(*module.split(".")[1:])
        files = sorted(base.glob("*.py")) if base.is_dir() else [base.with_suffix(".py")]
        for path in files:
            try:
                tree = ast.parse(path.read_text())
            except (OSError, SyntaxError):
                logger.warning("Cannot read MCP tool names from %s", path, exc_info=True)
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and any(
                    isinstance(d, ast.Call)
                    and isinstance(d.func, ast.Attribute)
                    and d.func.attr == "tool"
                    for d in node.decorator_list
                ):
                    owners.setdefault(node.name, []).append(f"mcp__{server}__{node.name}")
    return owners


def normalize_tool_trigger(values) -> tuple[list[str], list[str], list[str]]:
    """``(tools, patterns, dropped)`` for a raw tool_trigger value.

    ``tools`` holds built-in names and full ``mcp__<server>__<tool>`` names, in
    first-seen order without repeats; a unique MCP short name becomes its full
    name, and a known shell command becomes ``Bash`` with a word-boundary
    ``pattern``. Everything else is returned in ``dropped``.
    """
    if values is None:
        return [], [], []
    if isinstance(values, str):
        values = [values]
    short = _mcp_short_names()
    # A full name on a Genesis server must be one that server declares, so a
    # hallucinated tool is dropped; other servers can only be shape-checked.
    # An empty scan (unreadable source) validates nothing rather than
    # dropping every Genesis name.
    genesis_names = _genesis_mcp_names()
    tools: list[str] = []
    patterns: list[str] = []
    dropped: list[str] = []
    for raw in values:
        value = str(raw).strip()
        if value in BUILTIN_TOOLS:
            tool = value
        elif _MCP_NAME.fullmatch(value):
            if genesis_names and value.startswith(_GENESIS_PREFIXES) and value not in genesis_names:
                dropped.append(value)
                continue
            tool = value
        elif value in short:
            tool = short[value]
        elif value.lower() in _CLI_PATTERNS:
            tool = "Bash"
            pattern = _CLI_PATTERNS[value.lower()]
            if pattern not in patterns:
                patterns.append(pattern)
        else:
            dropped.append(value)
            continue
        if tool not in tools:
            tools.append(tool)
    return tools, patterns, dropped
