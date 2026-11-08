"""One frozen protocol-name policy for shared listing, calls and status."""

import hashlib
import json
from dataclasses import dataclass
from functools import partial
from types import MappingProxyType

from fastmcp.tools.function_tool import FunctionTool
from fastmcp.tools.tool_transform import TransformedTool
from fastmcp.utilities.async_utils import is_coroutine_function

from genesis.mcp.external_profiles import profile_tools

# Explicit defining modules of the current Genesis tool surface. New shared
# modules require qualification rather than being guessed from a namespace.
_HEALTH_MODULES = frozenset(["bench_status", "board_tools", "build_lane_status", "calibration_status", "campaign_tools", "codebase", "cognitive_ledger_tools", "contributor_issue", "db_schema", "deliberation_tools", "direct_session_tools", "ego_calibration", "ego_tools", "errors", "evo_run", "experiment_run", "experiment_status", "follow_up_tools", "immunity_status", "inbox_digest", "infrastructure_profile", "j9_eval", "loop_closure_status", "manifest", "open_question_tools", "provider", "reflex_resolve", "reflex_status", "session_charter_tools", "session_control", "settings", "skill_replay_run", "skill_usage_status", "status", "task_tools", "update_history", "user_job_tools", "validator_writes", "web_tools", "zero_drop_tools"])
_MEMORY_MODULES = frozenset(["bookmarks", "conversation", "core", "documents", "entity_adjudication", "knowledge", "locate", "observations", "procedural"])
_LOCAL_FRONTENDS = {"genesis.mcp.health.browser", "genesis.mcp.health.module_ops",
                    "genesis.mcp.health.payment_context"}
_LOCAL_ROOTS = ("genesis.browser", "genesis.modules", "genesis.payments")


def _location(module: str, service: str) -> str:
    if module in _LOCAL_FRONTENDS or any(module == root or module.startswith(root + ".") for root in _LOCAL_ROOTS):
        return "local"
    approved = ({f"genesis.mcp.health.{name}" for name in _HEALTH_MODULES} if service == "health"
                else {f"genesis.mcp.memory.{name}" for name in _MEMORY_MODULES} if service == "memory"
                else {f"genesis.mcp.{service}_mcp"})
    if module not in approved:
        raise ValueError("Unqualified shared tool defining module")
    return "shared"


def _ancestry(tool, service):
    queue, visited, capabilities, locations = [tool], set(), set(), set()
    while queue:
        current = queue.pop()
        if id(current) in visited or type(current) not in {FunctionTool, TransformedTool}:
            raise ValueError("Unqualified shared tool ancestry")
        visited.add(id(current))
        capabilities.add(current.name)
        if current.task_config.supports_tasks() or current.auth is not None:
            raise ValueError("Unqualified shared tool execution policy")
        if isinstance(current, TransformedTool):
            queue.append(current.parent_tool)
            if current.fn is current.forwarding_fn:
                continue
        function, seen = current.fn, set()
        while True:
            if id(function) in seen:
                raise ValueError("Cyclic shared tool function")
            seen.add(id(function))
            if isinstance(function, partial):
                function = function.func
                continue
            if not is_coroutine_function(function):
                raise ValueError("Shared tool synchronous execution is not qualified")
            locations.add(_location(getattr(function, "__module__", ""), service))
            if not hasattr(function, "__wrapped__"):
                break
            function = function.__wrapped__
    return "local" if "local" in locations else "shared", frozenset(capabilities)


@dataclass(frozen=True)
class _Entry:
    location: str
    capabilities: frozenset[str]
    schema: dict


class Catalog:
    def __init__(self, service, entries):
        self.service, self.entries = service, MappingProxyType(dict(entries))

    @classmethod
    async def capture(cls, server, service):
        entries = {}
        for tool in await server.list_tools(run_middleware=False):
            if tool.name in entries:
                raise ValueError("Ambiguous shared protocol name")
            location, capabilities = _ancestry(tool, service)
            schema = tool.to_mcp_tool(name=tool.name).model_dump(mode="json", by_alias=True, exclude_none=True)
            entries[tool.name] = _Entry(location, capabilities, schema)
        # Current Genesis exposes tools only. Do not accidentally expose new
        # resource/prompt/task surfaces without their own qualification.
        if (await server.list_resources(run_middleware=False)
                or await server.list_resource_templates(run_middleware=False)
                or await server.list_prompts(run_middleware=False)
                or await server.get_tasks()):
            raise ValueError("Unqualified shared capability surface")
        return cls(service, entries)

    def allowed(self, name, caller, *, enabled=True):
        entry = self.entries.get(name)
        if not enabled or entry is None or entry.location != "shared":
            return False
        if caller.profile is None:
            return True
        if caller.profile not in {"external", "validator"} or self.service not in {"health", "memory"}:
            return False
        return entry.capabilities <= profile_tools(self.service, caller.profile)

    def revision(self, caller, *, enabled=True):
        schemas = [entry.schema for name, entry in sorted(self.entries.items())
                   if self.allowed(name, caller, enabled=enabled)]
        return hashlib.sha256(json.dumps(schemas, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
