"""Exercise actual MCP recording into an owned migrated physical ledger."""

import json
import sqlite3

import aiosqlite
import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from genesis.db.connection import SerializedConnection
from genesis.db.crud import pr_verifications
from genesis.db.schema import create_all_tables, seed_data
from genesis.mcp.external_profiles import ExternalProfileMiddleware
from tests.test_scripts.test_pr_verification_closer import _doc, _doc_for


@pytest.fixture
async def recording(tmp_path, monkeypatch):
    from genesis import _config_overlay
    from genesis.mcp.health import follow_up_tools
    from genesis.session_awareness import codex_validator_config

    path = tmp_path / "owned.sqlite"
    raw = await aiosqlite.connect(path)
    raw.row_factory = aiosqlite.Row
    await create_all_tables(raw)
    await seed_data(raw)
    await raw.commit()
    shared = SerializedConnection(raw, db_path=path)
    await pr_verifications.open_verification(
        shared, repo="owner/repo", pr_number=7, pr_title="Synthetic check",
        merged_at="2030-01-01", now="2030-01-01",
    )
    config = tmp_path / "codex_validator.yaml"
    config.write_text("dry_run_only: true")
    user = tmp_path / "user-config"
    user.mkdir()
    monkeypatch.setattr(_config_overlay, "_user_config_dir", lambda: user)
    monkeypatch.setattr(codex_validator_config, "_base_path", lambda: config)
    monkeypatch.setattr(follow_up_tools, "_get_db", lambda: shared)
    try:
        yield path, shared, config
    finally:
        await shared.close()


def row(path):
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        return dict(db.execute("SELECT * FROM pr_verifications WHERE pr_number=7").fetchone())


async def call(doc, **over):
    from genesis.mcp.health import mcp

    tools = await mcp.get_tools()
    assert "pr_verification_close" in tools, "actual health MCP writer must be registered"
    owned = FastMCP("owned recording")
    owned.add_tool(tools["pr_verification_close"])
    owned.add_middleware(ExternalProfileMiddleware("health", "validator"))
    async with Client(owned) as client:
        return (await client.call_tool("pr_verification_close", dict(
            evidence_json=json.dumps(doc), pr=7,
        ) | over)).data


@pytest.mark.parametrize("verdict", ["pass-mechanical", "fail-intent", "cannot-verify"])
async def test_server_gate_previews_every_mutation(recording, verdict):
    path, shared, config = recording
    before = row(path)
    result = await call(_doc_for(verdict), **({"note": "Synthetic result"} if verdict != "pass-mechanical" else {}))
    assert result["dry_run"] is True and result["server_dry_run_only"] is True
    assert result["verdict"] == verdict
    assert row(path) == before
    assert not shared.in_transaction


@pytest.mark.parametrize("verdict", ["pass-mechanical", "fail-intent", "cannot-verify"])
async def test_local_opt_in_records_derived_result_and_closed_rows_are_immutable(recording, verdict):
    path, shared, config = recording
    config.with_suffix(".local.yaml").write_text("dry_run_only: false")
    args = {"note": "Synthetic result"} if verdict != "pass-mechanical" else {}
    result = await call(_doc_for(verdict), **args)
    stored = row(path)
    assert result["dry_run"] is False and result["recorded"] is True
    assert stored["verdict"] == verdict and stored["attempt_count"] == 1
    assert stored["status"] == ("closed" if verdict == "pass-mechanical" else "open")
    assert json.loads(stored["evidence"])["pr"] == 7
    if verdict == "pass-mechanical":
        with pytest.raises(ToolError):
            await call(_doc())
        assert row(path) == stored
    else:
        assert stored["last_attempt_note"] == "Synthetic result"
    assert not shared.in_transaction


@pytest.mark.parametrize("over", [
    {"pr": 8}, {"verdict": "fail-intent"}, {"park": True}, {"note": "Discarded note"},
    {"dry_run_only": False}, {"evidence_json": '{"pr":7,"pr":8}'},
])
async def test_refusals_leave_every_field_unchanged(recording, over):
    path, _, config = recording
    config.with_suffix(".local.yaml").write_text("dry_run_only: false")
    before = row(path)
    with pytest.raises(ToolError):
        await call(_doc(), **over)
    assert row(path) == before


@pytest.mark.parametrize("bad_pr", [True, 1.0, "1"])
async def test_independent_pr_type_is_checked_before_framework_coercion(recording, bad_pr):
    path, shared, config = recording
    await pr_verifications.open_verification(
        shared, repo="owner/repo", pr_number=1, pr_title="Synthetic strict target",
        merged_at="2030-01-01", now="2030-01-01",
    )
    config.with_suffix(".local.yaml").write_text("dry_run_only: false")
    try:
        await call(_doc(pr=1), pr=bad_pr)
    except ToolError:
        with sqlite3.connect(path) as fresh:
            assert fresh.execute("SELECT status FROM pr_verifications WHERE pr_number=1").fetchone()[0] == "open"
    else:
        with sqlite3.connect(path) as fresh:
            actual = fresh.execute("SELECT status FROM pr_verifications WHERE pr_number=1").fetchone()[0]
        pytest.fail(f"Noninteger {type(bad_pr).__name__} reached recording; physical row is {actual}")
