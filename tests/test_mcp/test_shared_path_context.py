"""Actual ingestion/resume wrappers refuse lost scope before constructors."""

from tests.shared_mcp_qualification import QUALIFIED_TEST_STACK as QUALIFIED_TEST_STACK

# isort: split
import asyncio
import importlib

import pytest

from genesis import caller_context
from genesis.mcp.shared_paths import SharedPathError


@pytest.mark.parametrize("tool,arguments", [
    ("knowledge_ingest_source", {"source": "/owned/nonexistent.txt", "project_type": "synthetic"}),
    ("knowledge_ingest_batch", {"directory": "/owned/nonexistent", "project_type": "synthetic"}),
    ("resume_review", {"resume_source": "literal resume"}),
])
@pytest.mark.parametrize("worker", [False, True], ids=["missing-request", "lost-worker-context"])
async def test_actual_wrappers_refuse_missing_scope_before_consumers(monkeypatch, tool, arguments, worker):
    knowledge = importlib.import_module("genesis.mcp.memory.knowledge")
    from genesis.runtime._core import GenesisRuntime

    reached = []

    def forbidden(*args, **kwargs):
        reached.append("consumer")
        raise AssertionError("missing shared scope reached a consumer")

    monkeypatch.setattr(caller_context, "_shared_service", True)
    monkeypatch.setattr(knowledge, "_get_orchestrator", forbidden)
    monkeypatch.setattr(GenesisRuntime, "instance", forbidden)
    actual = await knowledge.mcp.get_tool(tool)
    assert actual.fn.__module__ == knowledge.__name__
    with pytest.raises(SharedPathError, match="context is unavailable"):
        if worker:
            await asyncio.get_running_loop().run_in_executor(None, lambda: asyncio.run(actual.fn(**arguments)))
        else:
            await actual.fn(**arguments)
    assert reached == []
