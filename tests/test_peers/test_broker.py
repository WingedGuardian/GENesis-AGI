"""Real Unix transport, current grants, and broker effect ownership."""

import asyncio
import importlib
import json
import logging
import secrets
import sys
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from genesis.cc.peer_segment import PeerSegment
from genesis.db.schema import TABLES
from genesis.peers.broker import BrokerRefusal, EmptyArguments, PeerBroker
from genesis.peers.resources import PublishedResources
from genesis.peers.session import PeerSessionBinding
from genesis.peers.tasks import PeerTasks


class PrivateFixture(SimpleNamespace):
    def __repr__(self):
        return "PrivateBrokerFixture(credential fields hidden)"


@pytest.fixture
async def setup(registry, tmp_path):
    tmp_path.chmod(0o700)
    async with registry.connection() as db:
        for name in (
            "direct_session_queue",
            "peer_tasks",
            "peer_receipts",
            "peer_daily_admissions",
            "peer_resources",
        ):
            await db.execute(TABLES[name])
        await db.commit()
    await registry.register(
        "fixture", same_owner=True, daily_allowance=10, token_name="GENESIS_PEER_FIXTURE_TOKEN"
    )
    await registry.grant("fixture", "conversation", "allow")
    resources = PublishedResources(registry)
    published = await resources.publish("Approved fixture", "Approved immutable snapshot.")
    await registry.grant("fixture", "resource:" + published["resource_id"], "allow")
    task = await PeerTasks(registry).admit(
        await registry.get("fixture"),
        {"messageId": "one", "role": "ROLE_USER", "parts": [{"text": "Fixture request."}]},
    )
    async with registry.transaction() as db:
        await db.execute("UPDATE peer_tasks SET state='working' WHERE id=?", (task["id"],))
    binding = PeerSessionBinding(
        task["id"],
        PeerSegment(
            uuid.uuid4().hex,
            time.time() + 60,
            tuple(
                "mcp__genesis_peer__" + name
                for name in ("task_context", "resources_list", "resource_read")
            ),
        ),
        0,
        str(tmp_path / "facade.json"),
        str(tmp_path),
    )
    authorizations = []

    async def authorize(binding, capability, digest, decision):
        authorizations.append((capability, digest, decision))
        if decision != "allow":
            raise BrokerRefusal("approval_required", 409)

    broker = PeerBroker(registry, authorize)
    await broker.start(tmp_path / "socket")
    lease_path = tmp_path / "lease.json"
    await broker.issue(binding, lease_path)
    lease = json.loads(lease_path.read_text())
    async with httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=lease["socket_path"], trust_env=False),
        trust_env=False,
        base_url="http://broker",
    ) as client:
        s = PrivateFixture(
            registry=registry,
            resources=resources,
            published=published,
            binding=binding,
            broker=broker,
            client=client,
            lease_path=lease_path,
            authorizations=authorizations,
            headers={"Authorization": "Bearer " + lease["lease"]},
        )
        yield s
    await broker.close()


async def call(s, name="task_context", arguments=None):
    return await s.client.post(
        "/call", json={"operation": name, "arguments": arguments or {}}, headers=s.headers
    )


async def test_real_stdio_and_uds_context_resource_pipeline(setup):
    s = setup
    assert s.lease_path.stat().st_mode & 0o777 == 0o600
    assert s.broker._socket.stat().st_mode & 0o777 == 0o600
    assert s.broker._socket.parent.stat().st_mode & 0o777 == 0o700
    response = await call(s)
    assert response.status_code == 200
    assert "<external-content" in response.json()["context"]
    response = await call(s, "resource_read", {"resource_id": s.published["resource_id"]})
    assert response.json()["content"] == "Approved immutable snapshot."
    import os
    from pathlib import Path

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "genesis.peers.facade", "--lease-file", str(s.lease_path)],
        env={
            "HOME": str(Path.home()),
            "PATH": os.environ["PATH"],
            "PYTHONPATH": str(Path.cwd() / "src"),
        },
        cwd=str(s.lease_path.parent),
    )
    async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        assert {tool.name for tool in tools.tools} == {
            "task_context",
            "resources_list",
            "resource_read",
        }
        assert not (await session.call_tool("task_context", {})).isError


async def test_auth_first_body_limit_exact_and_fragmented(setup):
    s = setup
    response = await s.client.post("/call", content=b"x" * (256 * 1024 + 1))
    assert response.status_code == 401
    response = await s.client.post(
        "/call", json={}, headers={"Authorization": "Bearer " + secrets.token_urlsafe(32)}
    )
    assert response.status_code == 401
    body = b'{"operation":"task_context","arguments":{}}'
    exact = body + b" " * (256 * 1024 - len(body))
    assert (await s.client.post("/call", content=exact, headers=s.headers)).status_code == 200
    assert (
        await s.client.post("/call", content=exact + b" ", headers=s.headers)
    ).status_code == 413

    async def chunks():
        for _ in range(257):
            yield b"x" * 1024

    assert (await s.client.post("/call", content=chunks(), headers=s.headers)).status_code == 413


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b"{",
        b'{"operation":"task_context","operation":"resource_read","arguments":{}}',
        b'{"operation":"task_context","arguments":{"extra":true}}',
        b'{"operation":"task_context","arguments":{},"extra":true}',
        b'{"operation":{},"arguments":{}}',
        b'{"operation":"missing","arguments":{}}',
        b'{"operation":"task_context","arguments":{"extra":NaN}}',
        b'{"operation":"task_context","arguments":{"extra":1e999}}',
        b'{"operation":"resource_read","arguments":{"resource_id":"../private"}}',
    ],
)
async def test_malformed_unknown_overrides_refused_without_details(setup, body):
    response = await setup.client.post("/call", content=body, headers=setup.headers)
    assert response.status_code == 400
    assert response.json() == {"code": "operation_refused"}


async def test_snapshot_cannot_expand_and_current_grant_downgrade_refuses(setup):
    s = setup
    another = await s.resources.publish("Other fixture", "Not in accepted snapshot.")
    await s.registry.grant("fixture", "resource:" + another["resource_id"], "allow")
    assert (
        await call(s, "resource_read", {"resource_id": another["resource_id"]})
    ).status_code == 403
    await s.registry.grant("fixture", "resource:" + s.published["resource_id"], "deny")
    assert (
        await call(s, "resource_read", {"resource_id": s.published["resource_id"]})
    ).status_code == 403
    assert (await call(s, "resources_list")).json()["resources"] == []
    await s.registry.grant("fixture", "conversation", "ask")
    assert (await call(s)).status_code == 409
    assert s.authorizations[-1][2] == "ask"


@pytest.mark.parametrize("change", ["generation", "cancel", "revoke", "budget", "expiry"])
async def test_current_task_invalidation_blocks_old_lease(setup, change):
    s = setup
    if change == "revoke":
        await s.registry.revoke("fixture")
    else:
        assignment = {
            "generation": "generation=generation+1",
            "cancel": "cancel_requested=1",
            "budget": "work_elapsed_s=work_limit_s",
            "expiry": "expires_at='2000-01-01T00:00:00+00:00'",
        }[change]
        async with s.registry.transaction() as db:
            await db.execute(f"UPDATE peer_tasks SET {assignment} WHERE id=?", (s.binding.task_id,))
    assert (await call(s)).status_code == 403


async def test_retired_or_corrupt_resource_is_not_disclosed(setup):
    s = setup
    async with s.registry.transaction() as db:
        await db.execute(
            "UPDATE peer_resources SET content=? WHERE id=?",
            ("Changed fixture.", s.published["resource_id"]),
        )
    assert (
        await call(s, "resource_read", {"resource_id": s.published["resource_id"]})
    ).status_code == 400
    await s.resources.retire(s.published["resource_id"])
    assert (
        await call(s, "resource_read", {"resource_id": s.published["resource_id"]})
    ).status_code == 404


async def test_drain_waits_repeated_cancellation_invalidates_and_prevents_reissue(setup):
    s = setup
    entered, release = asyncio.Event(), asyncio.Event()

    async def operation(row, decisions, arguments):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            return {}

    s.broker._operations["task_context"] = (EmptyArguments, operation, "conversation")
    pending = asyncio.create_task(call(s))
    await entered.wait()
    draining = asyncio.create_task(s.broker.drain(s.binding))
    await asyncio.sleep(0)
    draining.cancel()
    draining.cancel()
    await asyncio.sleep(0)
    assert not draining.done()
    assert (await call(s)).status_code == 401
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await draining
    assert (await pending).status_code == 403
    with pytest.raises(BrokerRefusal):
        await s.broker.issue(s.binding, s.lease_path.parent / "reissued.json")


async def test_revocation_during_operation_discards_result(setup):
    s = setup
    entered, release = asyncio.Event(), asyncio.Event()

    async def operation(row, decisions, arguments):
        entered.set()
        await release.wait()
        return {"private_fixture": "Must not be delivered after revoke."}

    s.broker._operations["task_context"] = (EmptyArguments, operation, "conversation")
    pending = asyncio.create_task(call(s))
    await entered.wait()
    await s.registry.revoke("fixture")
    release.set()
    response = await pending
    assert response.status_code == 403
    assert response.json() == {"code": "lease_expired"}


@pytest.mark.parametrize("phase", ["authorization", "handler"])
@pytest.mark.parametrize("change", ["budget", "submitted"])
async def test_execution_authority_rechecked_after_every_await(setup, phase, change):
    s = setup
    entered, release = asyncio.Event(), asyncio.Event()
    handled = []

    async def authorize(*args):
        if phase == "authorization":
            entered.set()
            await release.wait()

    async def operation(*args):
        handled.append(True)
        if phase == "handler":
            entered.set()
            await release.wait()
        return {"context": "Fixture must remain withheld."}

    s.broker.authorize_operation = authorize
    s.broker._operations["task_context"] = (EmptyArguments, operation, "conversation")
    pending = asyncio.create_task(call(s))
    await entered.wait()
    assignment = "work_elapsed_s=work_limit_s" if change == "budget" else "state='submitted'"
    async with s.registry.transaction() as db:
        await db.execute(f"UPDATE peer_tasks SET {assignment} WHERE id=?", (s.binding.task_id,))
    release.set()
    response = await pending
    assert response.status_code == 403
    assert response.json() == {"code": "lease_expired"}
    assert bool(handled) == (phase == "handler")


@pytest.mark.parametrize("expired", ["segment", "task"])
async def test_expiry_during_final_database_await_withholds_disclosure(setup, monkeypatch, expired):
    s = setup
    clock = time.time()
    monkeypatch.setattr("genesis.peers.broker.time.time", lambda: clock)
    if expired == "task":
        from datetime import UTC, datetime

        binding = replace(
            s.binding,
            segment=PeerSegment(uuid.uuid4().hex, clock + 1200, s.binding.segment.tools),
        )
        path = s.lease_path.parent / "long-segment.json"
        await s.broker.issue(binding, path)
        s.headers = {"Authorization": "Bearer " + json.loads(path.read_text())["lease"]}
        async with s.registry.transaction() as db:
            await db.execute(
                "UPDATE peer_tasks SET expires_at=? WHERE id=?",
                (datetime.fromtimestamp(clock + 60, UTC).isoformat(), s.binding.task_id),
            )
    original = s.registry.connection
    count = 0

    @asynccontextmanager
    async def delayed_connection():
        nonlocal count, clock
        async with original() as db:
            yield db
        count += 1
        if count == 4:
            clock += 120

    monkeypatch.setattr(s.registry, "connection", delayed_connection)
    response = await call(s)
    assert count == 4
    assert response.status_code == 403
    assert response.json() == {"code": "lease_expired"}


async def test_final_authorization_cannot_mix_unauthorized_database_snapshots(setup, monkeypatch):
    s = setup
    original = s.registry.connection
    count = 0

    async def mutate(sqls):
        async with original() as db:
            await db.execute("BEGIN IMMEDIATE")
            for sql in sqls:
                await db.execute(sql)
            await db.commit()

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        async def fetchone(self):
            row = await self.cursor.fetchone()
            await mutate(
                [
                    "UPDATE peer_tasks SET cancel_requested=1",
                    "UPDATE peer_grants SET decision='allow' WHERE capability='conversation'",
                ]
            )
            return row

    class Connection:
        def __init__(self, db):
            self.db = db

        async def execute(self, sql, parameters=()):
            cursor = await self.db.execute(sql, parameters)
            return Cursor(cursor) if sql.startswith("SELECT t.*") else cursor

    @asynccontextmanager
    async def fractured_connection():
        nonlocal count
        count += 1
        if count == 4:
            await mutate(["UPDATE peer_grants SET decision='deny' WHERE capability='conversation'"])
        async with original() as db:
            yield Connection(db) if count == 4 else db

    monkeypatch.setattr(s.registry, "connection", fractured_connection)
    response = await call(s)
    # Final validation saw working+deny, then cancelled+allow: neither grants
    # authority. One SQL snapshot must never combine old task with new grants.
    assert count == 4
    assert response.status_code == 403


async def test_no_operation_authorizer_fallback(setup):
    with pytest.raises(ValueError, match="authorizer required"):
        PeerBroker(setup.registry, None)


async def test_canonical_and_upgrade_ddl_identical():
    migration = importlib.import_module("genesis.db.migrations.20261008073006_peer_resources")
    statements = []

    class Capture:
        async def execute(self, sql):
            statements.append(sql.strip())

    await migration.up(Capture())
    assert statements == [TABLES["peer_resources"].strip()]


async def test_exact_segment_tool_list_enforced(setup):
    s = setup
    limited = replace(
        s.binding,
        segment=PeerSegment(
            uuid.uuid4().hex, time.time() + 60, ("mcp__genesis_peer__task_context",)
        ),
    )
    lease_path = s.lease_path.parent / "limited.json"
    await s.broker.issue(limited, lease_path)
    lease = json.loads(lease_path.read_text())
    response = await s.client.post(
        "/call",
        json={
            "operation": "resource_read",
            "arguments": {"resource_id": s.published["resource_id"]},
        },
        headers={"Authorization": "Bearer " + lease["lease"]},
    )
    assert response.status_code == 403


async def test_extension_requires_admitted_capability(setup):
    s = setup

    class QueryArguments(EmptyArguments):
        query: str

    called = []

    async def research(row, decisions, arguments):
        called.append(True)
        return {}

    s.broker.register_operation("research_search", "research", QueryArguments, research)
    await s.registry.grant("fixture", "research", "allow")
    binding = replace(
        s.binding,
        segment=PeerSegment(
            uuid.uuid4().hex, time.time() + 60, ("mcp__genesis_peer__research_search",)
        ),
    )
    path = s.lease_path.parent / "research.json"
    await s.broker.issue(binding, path)
    lease = json.loads(path.read_text())
    response = await s.client.post(
        "/call",
        json={"operation": "research_search", "arguments": {"query": "fixture"}},
        headers={"Authorization": "Bearer " + lease["lease"]},
    )
    assert response.status_code == 403
    assert called == []


async def test_audit_contains_only_operational_metadata(setup, caplog):
    import logging

    s = setup
    with caplog.at_level(logging.INFO, logger="genesis.peers.broker"):
        assert (await call(s)).status_code == 200
    log = caplog.text
    contains_credential = s.headers["Authorization"][7:] in log
    contains_prompt = "Fixture request." in log
    assert not contains_credential
    assert not contains_prompt
    assert "credential=segment_lease" in log
    assert s.binding.task_id in log and s.binding.segment.segment_id in log
    assert "operation=task_context outcome=200" in log


async def test_operator_cli_publishes_and_retires_without_content_output(setup, tmp_path, capsys):
    import argparse

    from genesis.peers.cli import add_parser, execute

    s = setup
    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers())
    source = tmp_path / "document.txt"
    source.write_text("Explicit approved operator document.")
    await execute(
        parser.parse_args(
            ["peers", "resource-publish", "--title", "Document", "--file", str(source)]
        ),
        s.registry,
    )
    report = json.loads(capsys.readouterr().out)
    assert set(report) == {"resource_id", "sha256"}
    assert (await s.resources.get(report["resource_id"]))["content"] == source.read_text()
    await execute(
        parser.parse_args(["peers", "resource-retire", report["resource_id"]]), s.registry
    )
    assert await s.resources.get(report["resource_id"]) is None


@pytest.mark.parametrize("kind", ["oversize", "invalid_utf8", "symlink", "directory"])
async def test_operator_publication_refuses_invalid_files(setup, tmp_path, kind):
    import argparse

    from genesis.peers.cli import add_parser, execute

    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers())
    source = tmp_path / "invalid.txt"
    if kind == "oversize":
        source.write_bytes(b"x" * (256 * 1024 + 1))
    elif kind == "invalid_utf8":
        source.write_bytes(b"\xff")
    elif kind == "symlink":
        source.symlink_to(setup.lease_path)
    else:
        source.mkdir()
    with pytest.raises((ValueError, OSError)):
        await execute(
            parser.parse_args(
                ["peers", "resource-publish", "--title", "Fixture", "--file", str(source)]
            ),
            setup.registry,
        )
    async with setup.registry.connection() as db:
        count = (await (await db.execute("SELECT COUNT(*) FROM peer_resources")).fetchone())[0]
    assert count == 1


@pytest.mark.parametrize("malformed", ["nul", "control", "cr", "method", "long_header"])
async def test_framework_wire_errors_never_echo_or_log_leases(setup, malformed):
    s = setup
    lease = json.loads(s.lease_path.read_text())
    needle = lease["lease"].encode()
    leaks = []

    class SafeCapture(logging.Handler):
        def emit(self, record):
            # Never persist the actual credential or raw framework diagnostic.
            leaks.append(needle in self.format(record).encode())

    targets = [logging.getLogger(name) for name in ("aiohttp.server", "genesis.peers.transport")]
    saved = [(target.handlers[:], target.propagate, target.level) for target in targets]
    for target in targets:
        target.handlers, target.propagate, target.level = [SafeCapture()], False, logging.DEBUG
    try:
        suffix = {"nul": b"\x00", "control": b"\x01", "cr": b"\rX"}.get(malformed, b"")
        header = b"Authorization: Bearer " + needle + suffix
        method = b"POST"
        if malformed == "method":
            method = needle + b"\x00"
        elif malformed == "long_header":
            header += b"x" * 10000
        reader, writer = await asyncio.open_unix_connection(lease["socket_path"])
        try:
            writer.write(method + b" /call HTTP/1.1\r\nHost: broker\r\n" + header + b"\r\n\r\n")
            await writer.drain()
            async with asyncio.timeout(5):
                response = await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
        status = int(response.split(b"\r\n", 1)[0].split()[1])
        credential_echoed = needle in response
        assert status == 400
        assert not credential_echoed
        assert leaks and not any(leaks)
    finally:
        for target, (handlers, propagate, level) in zip(targets, saved, strict=True):
            target.handlers, target.propagate, target.level = handlers, propagate, level
