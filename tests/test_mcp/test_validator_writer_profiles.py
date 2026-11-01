"""Server-selected writer authority must stay inside its admitted request."""

import asyncio

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from genesis.mcp import external_profiles as profiles

HEALTH_READS = frozenset({
    "health_status", "bootstrap_manifest", "subsystem_heartbeats", "job_health", "provider_activity",
})
MEMORY_READS = frozenset({
    "memory_recall", "memory_expand", "memory_core_facts", "memory_stats", "knowledge_recall",
    "knowledge_status", "procedure_recall", "observation_query",
})


@pytest.mark.parametrize("server,reads,writes", [
    ("health", HEALTH_READS, {"pr_verification_close", "follow_up_create", "follow_up_list", "follow_up_update"}),
    ("memory", MEMORY_READS, {"memory_store"}),
])
def test_closed_profile_sets_keep_external_unchanged(server, reads, writes):
    assert profiles.profile_tools(server, "external") == reads
    assert profiles.profile_tools(server, "validator") == reads | writes


@pytest.mark.parametrize("fails", [False, True])
async def test_request_context_isolated_and_reset_on_error_and_concurrency(fails):
    role = getattr(profiles, "current_external_profile", None)
    assert role is not None, "request-scoped server role is required"
    assert role.get() is None
    seen = []
    entered, release = asyncio.Event(), asyncio.Event()
    servers = []
    for profile in ("validator", "external"):
        mcp = FastMCP(profile)

        @mcp.tool(name="health_status", task=False)
        async def read() -> str:
            selected = role.get()
            seen.append(selected)
            if selected == "validator":
                entered.set()
                await release.wait()
                if fails:
                    raise ValueError("owned failure")
            assert role.get() == selected
            return str(selected)

        mcp.add_middleware(profiles.ExternalProfileMiddleware("health", profile))
        servers.append(mcp)
    async with Client(servers[0]) as validator, Client(servers[1]) as external:
        task = asyncio.create_task(validator.call_tool("health_status"))
        await entered.wait()
        assert (await external.call_tool("health_status")).data == "external"
        assert role.get() is None
        release.set()
        if fails:
            with pytest.raises(ToolError):
                await task
        else:
            assert (await task).data == "validator"
    assert seen == ["validator", "external"]
    assert role.get() is None


async def test_validator_writer_direct_call_does_not_enter_external_body():
    mcp = FastMCP("owned external")
    entered = []

    @mcp.tool(task=False)
    async def memory_store() -> str:
        entered.append(True)
        return "owned"

    mcp.add_middleware(profiles.ExternalProfileMiddleware("memory", "external"))
    async with Client(mcp) as client:
        assert await client.list_tools() == []
        with pytest.raises(ToolError, match="unavailable"):
            await client.call_tool("memory_store")
    assert entered == []
