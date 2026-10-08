"""Registry authority is explicit and changes persist atomically."""

import asyncio
import sqlite3

import pytest


async def test_relationship_has_no_implicit_grants_and_revoke_preserves_identity(registry):
    assert (await registry.settings())["mode"] == "disabled"
    await registry.configure("fallback", service_url="https://genesis.example/v1/agent/a2a")
    await registry.register(
        "muse", same_owner=True, daily_allowance=3, token_name="GENESIS_PEER_MUSE_TOKEN"
    )
    initial = await registry.get("muse")
    assert await registry.grants("muse") == {}
    await registry.grant("muse", "conversation", "ask")
    assert await registry.grants("muse") == {"conversation": "ask"}
    await registry.revoke("muse")
    final = await registry.get("muse")
    assert final["active"] == 0 and final["epoch"] == initial["epoch"]
    assert final["revision"] == initial["revision"] + 2
    assert final["daily_allowance"] == 3


@pytest.mark.parametrize("allowance", [None, True, False, 0, -1, 2**63, 1.5])
async def test_admissions_require_owner_daily_allowance(registry, allowance):
    with pytest.raises(ValueError):
        await registry.register(
            "muse", same_owner=True, daily_allowance=allowance, token_name="GENESIS_PEER_MUSE_TOKEN"
        )
    assert await registry.rows() == []


@pytest.mark.parametrize(
    "name",
    [
        "GENESIS_MCP_HTTP_TOKEN",
        "GENESIS_DESK_TOKEN",
        "GENESIS_PEER_BACKEND_TOKEN",
        "bad",
        "GENESIS_PEER_ö_TOKEN",
    ],
)
async def test_credential_names_cannot_select_owner_or_backend_authority(registry, name):
    with pytest.raises(ValueError):
        await registry.register("muse", same_owner=True, daily_allowance=1, token_name=name)
    assert await registry.rows() == []


async def test_private_connection_enforces_foreign_keys_and_cancel_rolls_back(registry):
    await registry.register(
        "muse", same_owner=True, daily_allowance=1, token_name="GENESIS_PEER_MUSE_TOKEN"
    )
    with pytest.raises(sqlite3.IntegrityError):
        async with registry.transaction() as db:
            await db.execute("INSERT INTO peer_grants VALUES('unknown','conversation','allow')")
    with pytest.raises(asyncio.CancelledError):
        async with registry.transaction() as db:
            await db.execute("UPDATE peers SET active=0 WHERE peer_id='muse'")
            raise asyncio.CancelledError
    assert (await registry.get("muse"))["active"] == 1
    assert await registry.grants("unknown") == {}


async def test_node_and_credential_bindings_are_unique(registry):
    await registry.register(
        "muse",
        same_owner=True,
        daily_allowance=1,
        token_name="GENESIS_PEER_MUSE_TOKEN",
        sam_realm="realm",
        sam_node="node",
    )
    for fields in (
        {"token_name": "GENESIS_PEER_MUSE_TOKEN"},
        {"sam_realm": "realm", "sam_node": "node"},
    ):
        with pytest.raises(sqlite3.IntegrityError):
            await registry.register("other", same_owner=False, daily_allowance=1, **fields)
    assert len(await registry.rows()) == 1


@pytest.mark.parametrize(
    "url",
    [
        None,
        "http://genesis.example/v1/agent/a2a",
        "https://genesis.example:9443/v1/agent/a2a",
        "https://user@genesis.example/v1/agent/a2a",
        "https://genesis.example/other",
        "https://genesis.example/v1/agent/a2a?key=x",
        "https://genesis.example/v1/agent/a2a#fragment",
    ],
)
async def test_enabled_mode_requires_owner_pinned_service_url(registry, url):
    with pytest.raises(ValueError):
        await registry.configure("fallback", service_url=url)
    assert (await registry.settings())["mode"] == "disabled"


async def test_fresh_and_migrated_schema_are_identical_and_migration_owns_no_commit(tmp_path):
    import importlib

    import aiosqlite

    from genesis.db.schema import TABLES

    migration = importlib.import_module("genesis.db.migrations.20261008041356_peer_registry")
    schemas = []
    for variant in ("fresh", "upgrade"):
        async with aiosqlite.connect(tmp_path / variant) as db:
            await db.execute("BEGIN")
            if variant == "fresh":
                for key in ("peer_settings", "peers", "peer_grants"):
                    await db.execute(TABLES[key])
            else:
                await migration.up(db)
                await migration.up(db)
            assert db.in_transaction
            schemas.append(
                await (
                    await db.execute(
                        "SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"
                    )
                ).fetchall()
            )
            await db.rollback()
            assert not await (
                await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
    assert [(name, " ".join(sql.split())) for name, sql in schemas[0]] == [
        (name, " ".join(sql.split())) for name, sql in schemas[1]
    ]


async def test_registry_refuses_missing_database_without_creating_it(tmp_path):
    from genesis.peers.registry import PeerRegistry

    path = tmp_path / "absent.db"
    with pytest.raises(sqlite3.OperationalError):
        await PeerRegistry(path).settings()
    assert not path.exists()
