"""One normalizer for every ``tool_trigger`` a procedure stores.

``tool_trigger`` is matched against the name of the tool Claude Code is about to
call, so only a real tool name can ever fire. The two LLM producers (the
extractor and the judge) were given no list and wrote CLI words (``ssh``), MCP
short names (``browser_navigate``) and prose (``review gate``) beside real names.
:func:`normalize_tool_trigger` keeps the real names, repairs the two classes that
mean a real tool, and drops the rest. It is pure and deterministic.

The built-in set is seeded from the lists the CC session builders already keep,
plus the built-ins sessions were seen calling (Claude Code 2.1.280). It is a
snapshot: the authoritative inventory is the ``tools`` list of a session's init
event, which nothing persists, and a missing name costs only a dropped trigger.
MCP names on the Genesis servers are checked against what those servers
declare; any other server (user-scoped, plugin, connector) is install-local,
so its names are only shape-checked.
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
    "Artifact", "ArtifactComments", "ArtifactData", "ArtifactCheck", "SendFeedback",
    "EndConversation", "SendUserFile", "ReadNotifications", "StructuredOutput",
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
# Anchored to command position (start, or after ; & | ( ` or a newline, then any
# blanks), past wrappers and their flags (sudo -E, time -p, env, timeout N,
# command, VAR=x, ...), a path and `python -m`, so `.venv/bin/ruff` and
# `timeout 30 ssh` match while `~/.ssh/known_hosts`, `gh-pages` and
# `grep pytest` do not. A heuristic for an advisory nudge: a wrapper it does not
# know costs one missed nudge, never a wrong one.
# Blanks are [ \t], never \s: with \s every newline would be an anchor that
# re-scans every newline after it (8 KB of newlines took 9 s per pattern).
_CLI_PREFIX = (
    r"(?:^|[;&|(`\n])[ \t]*(?:(?:(?:sudo|env|time|nohup|xargs|command)(?:[ \t]+-\S+)*"
    r"|timeout[ \t]+\S+|\w+=\S*)[ \t]+)*(?:[\w.~-]*/)*(?:python3?[ \t]+-m[ \t]+)?"
)
_CLI_PATTERNS = {
    # The command must END there too, so ssh-agent and pytest-cov are not ssh/pytest.
    phrase: _CLI_PREFIX + r"[ \t]+".join(map(re.escape, phrase.split())) + r"(?=$|[\s;&|)`])"
    for phrase in _CLI_COMMANDS
}
# A real tool name in front of prose an LLM added: `Bash (running tests)`,
# `Edit{file_path: ...}`, `Tool:ego_proposal_resolve`.
_TOOL_PREFIX = re.compile(r"(?:Tool:\s*)?([\w-]+)\s*(?:[(:{].*)?")  # one line only

# Genesis MCP servers and the modules that register their tools.
_MCP_MODULES = {
    "genesis-health": "genesis.mcp.health",
    "genesis-memory": "genesis.mcp.memory",
    "genesis-outreach": "genesis.mcp.outreach_mcp",
    "genesis-recon": "genesis.mcp.recon_mcp",
    "discord-bot": "genesis.mcp.discord_bot_mcp",
}
# Modules that declare tools the standalone MCP server never imports, so a
# session can never call them (tests/test_mcp/test_docket_worker_suppressed.py
# keeps the same list). A test pins the scan to exactly what each server
# registers, so a new module like these fails there.
_NOT_SERVED = frozenset({"genesis.mcp.health.user_job_tools"})

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
    """Every full ``mcp__<server>__<tool>`` name the Genesis servers declare."""
    return frozenset(name for names in _mcp_owners().values() for name in names)


@functools.cache
def _mcp_owners() -> dict[str, list[str]]:
    """Each Genesis MCP tool's short name -> its full name(s).

    Read from the servers' source rather than by importing them: importing them
    takes seconds and registers every tool in the importing process. Every
    tool is declared with a bare ``@mcp.tool()``, so the function name is the
    tool name; a test pins the result to exactly what each server registers.
    Modules in ``_NOT_SERVED`` are skipped. A file that cannot be read maps
    nothing, so its short names are dropped rather than guessed.
    """
    root = Path(__file__).resolve().parents[2]
    owners: dict[str, list[str]] = {}
    for server, module in _MCP_MODULES.items():
        base = root.joinpath(*module.split(".")[1:])
        files = sorted(base.glob("*.py")) if base.is_dir() else [base.with_suffix(".py")]
        for path in files:
            if f"{module}.{path.stem}" in _NOT_SERVED:
                continue
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


def _annotated_cli(value: str) -> str | None:
    """The longest CLI phrase ``value`` starts with, when prose follows it."""
    lower = value.lower()
    for phrase in sorted(_CLI_PATTERNS, key=len, reverse=True):
        if lower.startswith(phrase) and re.match(r"\s*[(:{]", lower[len(phrase) :]):
            return phrase
    return None


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
    elif not isinstance(values, list | tuple):
        # A number, bool or mapping from an LLM is not a trigger list; drop it
        # rather than iterate it (or raise) and abort the caller's batch.
        return [], [], [str(values)]
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
            server, name = value[5:].split("__", 1)
            canon = server.lower().replace("_", "-")
            if genesis_names and (canon in _MCP_MODULES or canon.startswith("genesis-")):
                # A Genesis server, however it was spelled (the template owns the
                # genesis- prefix): keep only a tool one declares, under its real name.
                canonical = f"mcp__{canon}__{name}"
                if canonical not in genesis_names:
                    dropped.append(value)
                    continue
                value = canonical
            tool = value
        elif value in short:
            tool = short[value]
        elif value.lower() in _CLI_PATTERNS:
            tool = "Bash"
            pattern = _CLI_PATTERNS[value.lower()]
            if pattern not in patterns:
                patterns.append(pattern)
        elif cli := _annotated_cli(value):
            tool = "Bash"
            if _CLI_PATTERNS[cli] not in patterns:
                patterns.append(_CLI_PATTERNS[cli])
        elif (m := _TOOL_PREFIX.fullmatch(value)) and m.group(1) != value:
            # The name alone, judged by every rule above (a full MCP name is
            # validated, a CLI word gets its pattern); it holds no prose, so
            # this recurses at most once.
            inner, inner_patterns, _ = normalize_tool_trigger([m.group(1)])
            if not inner:
                dropped.append(value)
                continue
            tool = inner[0]
            patterns.extend(p for p in inner_patterns if p not in patterns)
        else:
            dropped.append(value)
            continue
        if tool not in tools:
            tools.append(tool)
    return tools, patterns, dropped
