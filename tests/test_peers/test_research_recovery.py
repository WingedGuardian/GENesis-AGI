"""Historical research validation grants publication telemetry, never execution."""

import json
from types import SimpleNamespace

import pytest

from genesis.peers.digests import operation_digest
from genesis.peers.recovery import PeerRecovery, recovered_binding
from genesis.peers.research import RESEARCH_PUBLICATION_TOOLS, validate_receipt
from tests.test_peers.test_recovery import publication as _publication
from tests.test_peers.test_recovery import recorded, snapshot

publication = _publication


async def research_record(s, operation, tools=None):
    await s.registry.grant("fixture", "research", "allow")
    if operation == "research_search":
        arguments = {"query": "public query", "max_results": 5}
        data = {"backend": "searxng", "results": [], "source": "external_untrusted"}
    else:
        arguments = {"url": "https://example.com/public"}
        data = {
            "original_url": arguments["url"],
            "final_url": arguments["url"],
            "title": "Public fixture",
            "content": "Public content",
            "source": "external_untrusted",
        }
    receipt = {"operation": operation, "arguments": arguments, "data": data}
    digest = operation_digest(operation, arguments)
    validate_receipt(receipt, digest)
    async with s.registry.transaction() as db:
        grants = json.loads(
            (
                await (
                    await db.execute(
                        "SELECT grants_json FROM peer_tasks WHERE id=?", (s.task["id"],)
                    )
                ).fetchone()
            )[0]
        )
        grants["research"] = "allow"
        await db.execute(
            "UPDATE peer_tasks SET grants_json=? WHERE id=?", (json.dumps(grants), s.task["id"])
        )
        await db.execute(
            "UPDATE peer_operations SET capability='research',operation_digest=?,result_json=? WHERE id=?",
            (digest, json.dumps(receipt), s.receipt["id"]),
        )
    s.result["tools_summary"] = (
        tools if tools is not None else {"mcp__genesis_peer__" + operation: 1}
    )
    await recorded(s)


@pytest.mark.parametrize("operation", ["research_search", "research_fetch"])
@pytest.mark.parametrize("mode", ["absent", "validator_only", "complete"])
async def test_research_recovery_requires_validator_and_telemetry(publication, operation, mode):
    s = publication
    await research_record(s, operation)
    kwargs = {}
    if mode != "absent":
        kwargs["research"] = SimpleNamespace(validate=validate_receipt)
    if mode == "complete":
        kwargs["publication_tools"] = RESEARCH_PUBLICATION_TOOLS
    recovery = PeerRecovery(s.registry, s.state.directory, **kwargs)
    _, segment, _, _ = await snapshot(s)
    binding = recovered_binding(segment, s.state.directory)
    assert not set(RESEARCH_PUBLICATION_TOOLS).intersection(binding.segment.tools)
    assert await recovery.run()
    task, _, _, count = await snapshot(s)
    assert (task["state"], count) == (("completed", 1) if mode == "complete" else ("failed", 0))
    assert await recovery.run()
    repeated, _, _, repeated_count = await snapshot(s)
    assert repeated == task and repeated_count == count


@pytest.mark.parametrize(
    "tools",
    [
        {"mcp__genesis_peer__unregistered": 1},
        {"Bash": 1},
        {"mcp__genesis_peer__research_search": 101},
        {"mcp__genesis_peer__research_search": True},
    ],
)
async def test_recovery_telemetry_keeps_name_and_count_constraints(publication, tools):
    s = publication
    await research_record(s, "research_search", tools)
    recovery = PeerRecovery(
        s.registry,
        s.state.directory,
        research=SimpleNamespace(validate=validate_receipt),
        publication_tools=RESEARCH_PUBLICATION_TOOLS,
    )
    assert await recovery.run()
    task, _, _, count = await snapshot(s)
    assert task["state"] == "failed" and count == 0


def test_recovery_telemetry_cannot_replace_receipt_validation(tmp_path):
    with pytest.raises(ValueError, match="receipt validation"):
        PeerRecovery(None, tmp_path, publication_tools=RESEARCH_PUBLICATION_TOOLS)


async def test_research_recovery_does_not_restore_revoked_permission(publication):
    s = publication
    await research_record(s, "research_search")
    await s.registry.grant("fixture", "research", "deny")
    recovery = PeerRecovery(
        s.registry,
        s.state.directory,
        research=SimpleNamespace(validate=validate_receipt),
        publication_tools=RESEARCH_PUBLICATION_TOOLS,
    )
    assert await recovery.run()
    task, _, _, count = await snapshot(s)
    assert task["state"] == "failed" and count == 0
