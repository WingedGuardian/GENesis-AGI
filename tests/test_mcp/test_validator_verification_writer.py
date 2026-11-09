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
from genesis.session_awareness.pr_recording import PrMetadata
from tests.test_scripts.test_pr_verification_closer import MERGE_SHA, _doc, _doc_for, _with_tracking


@pytest.fixture
async def recording(tmp_path, monkeypatch):
    from genesis import _config_overlay
    from genesis.mcp.health import follow_up_tools, validator_writes
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
    monkeypatch.setattr(validator_writes, "gh_pr_metadata", lambda repo, pr: PrMetadata(MERGE_SHA, ""))
    from genesis.session_awareness import pr_tracking
    monkeypatch.setattr(pr_tracking, "public_receipt", lambda *a: "synthetic task covers concern")
    try:
        yield path, shared, config
    finally:
        await shared.close()


def row(path):
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        return dict(db.execute("SELECT * FROM pr_verifications WHERE pr_number=7").fetchone())


async def call(doc, *, route=True, **over):
    from genesis.mcp.health import mcp

    if route:
        _with_tracking(doc)
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert "pr_verification_close" in tools, "actual health MCP writer must be registered"
    owned = FastMCP("owned recording")
    owned.add_tool(tools["pr_verification_close"])
    owned.add_middleware(ExternalProfileMiddleware("health", "validator"))
    async with Client(owned) as client:
        return (await client.call_tool("pr_verification_close", dict(
            evidence_json=json.dumps(doc), pr=7,
        ) | over)).data


async def test_unlinked_finding_records_measurement_but_reports_pending(recording):
    path, _, config = recording
    config.with_suffix(".local.yaml").write_text("dry_run_only: false")
    doc = _doc(findings=[{"summary": "reachable defect", "disposition": "reported in chat"}])
    result = await call(doc, route=False)
    assert result["recorded"] is True
    assert result["tracking_complete"] is False
    assert result["would_close"] is False
    assert row(path)["status"] == "open"
    assert row(path)["verdict"] == "pass-mechanical"
    assert json.loads(row(path)["evidence"])["findings"] == doc["findings"]


async def test_pending_tracking_retains_measurement_before_close_only_e2e_gate(recording, monkeypatch):
    from genesis.mcp.health import validator_writes
    path, _, config = recording
    config.with_suffix(".local.yaml").write_text("dry_run_only: false")
    monkeypatch.setattr(validator_writes, "gh_pr_metadata", lambda *a: PrMetadata(MERGE_SHA, "E2E: run timer"))
    result = await call(_doc(findings=[dict(summary="timer defect", disposition="unfiled")]), route=False)
    assert result["recorded"] and not result["would_close"]
    assert row(path)["verdict"] == "pass-mechanical"


@pytest.mark.parametrize("preview", [False, True])
@pytest.mark.parametrize("failure", [False, True])
async def test_pending_readback_preserves_measurement_or_previews_without_writing(recording, preview, failure):
    path, _, config = recording
    config.with_suffix(".local.yaml").write_text("dry_run_only: false")
    doc = _doc_for("fail-intent") if failure else _doc()
    doc.update(findings=[dict(summary="defect", disposition="filed")], tracking=[
        dict(kind="public", target="findings/0", url="https://github.com/owner/repo/issues/12",
             quote="unrelated quote", rationale="Claimed coverage, but receipt does not substantiate it")])
    before = row(path)
    result = await call(doc, route=False, dry_run=preview, **({"note": "failure measured"} if failure else {}))
    assert result["tracking_complete"] is False and result["would_close"] is False
    if preview:
        assert row(path) == before
    else:
        stored = row(path)
        assert stored["status"] == "open" and stored["verdict"] == result["verdict"]
        assert json.loads(stored["evidence"])["claims"] == doc["claims"]


@pytest.mark.parametrize("inactive", [False, True])
async def test_private_receipt_reads_actual_eligible_follow_up(recording, inactive):
    from genesis.db.crud import follow_ups

    path, shared, config = recording
    config.with_suffix(".local.yaml").write_text("dry_run_only: false")
    identifier = await follow_ups.create_private(
        shared, request_key="private-receipt", content="Synthetic private acceptance task",
        reason="Synthetic disclosure decision", work_state="ready",
    )
    if inactive:
        await follow_ups.update_private(shared, identifier, status="completed")
    doc = _doc(findings=[dict(summary="private acceptance task", disposition="private")], tracking=[
        dict(kind="private", target="findings/0", id=identifier,
             quote="Synthetic private acceptance task", rationale="Task covers the acceptance measurement",
             disclosure_harm="The task necessarily identifies a private individual",
             redaction_insufficient="Removing identity removes the decision target")])
    result = await call(doc, route=False)
    assert result["tracking_complete"] is not inactive
    assert row(path)["status"] == ("open" if inactive else "closed")


@pytest.mark.parametrize("preview", [False, True])
@pytest.mark.parametrize("covered", [False, True])
async def test_mcp_enforces_e2e_mapping_before_preview_or_write(recording, monkeypatch, preview, covered):
    from genesis.mcp.health import validator_writes

    path, _, config = recording
    config.with_suffix(".local.yaml").write_text("dry_run_only: false")
    before = row(path)
    step = "restart the timer and measure its next run"
    monkeypatch.setattr(validator_writes, "gh_pr_metadata", lambda *a: PrMetadata(MERGE_SHA, "E2E: " + step))
    doc = _doc(e2e=[{"step": step, "claim": 0}]) if covered else _doc()
    if covered:
        result = await call(doc, dry_run=preview)
        assert result["would_close"] and result["verdict"] == "pass-mechanical"
        assert result["recorded"] is not preview
    else:
        with pytest.raises(ToolError, match=step):
            await call(doc, dry_run=preview)
    if preview or not covered:
        assert row(path) == before


async def test_missing_mapping_does_not_prevent_recording_an_incomplete_attempt(recording, monkeypatch):
    from genesis.mcp.health import validator_writes

    path, _, config = recording
    config.with_suffix(".local.yaml").write_text("dry_run_only: false")
    monkeypatch.setattr(validator_writes, "gh_pr_metadata", lambda *a: PrMetadata(MERGE_SHA, "E2E: run the timer"))
    doc = _doc(gaps=[{"kind": "not_yet_done", "detail": "not run", "command": "run timer"}])
    result = await call(doc)
    assert result["verdict"] == "not-yet-done" and result["recorded"]
    assert row(path)["status"] == "open"


async def test_a_recorded_not_yet_done_reports_whether_it_repeats_the_last_attempt(
    recording, monkeypatch
):
    from genesis.mcp.health import validator_writes

    path, _, config = recording
    config.with_suffix(".local.yaml").write_text("dry_run_only: false")
    monkeypatch.setattr(
        validator_writes, "gh_pr_metadata", lambda *a: PrMetadata(MERGE_SHA, "E2E: run the timer")
    )
    gap = {"kind": "not_yet_done", "detail": "not run", "command": "run timer"}
    assert (await call(_doc(gaps=[gap])))["nyd_repeat"] == 0
    assert (await call(_doc(gaps=[gap])))["nyd_repeat"] == 1
    assert row(path)["nyd_repeat"] == 1
    other = {**gap, "command": "run the other timer"}
    assert (await call(_doc(gaps=[other])))["nyd_repeat"] == 0


async def test_a_preview_carries_a_null_nyd_repeat(recording):
    _, _, config = recording
    config.with_suffix(".local.yaml").write_text("dry_run_only: false")
    gap = {"kind": "not_yet_done", "detail": "not run", "command": "run timer"}
    result = await call(_doc(gaps=[gap]), dry_run=True)
    assert result["recorded"] is False and result["nyd_repeat"] is None


async def test_existing_mcp_close_stores_rechecks_without_a_new_caller_argument(recording):
    path, _, config = recording
    config.with_suffix(".local.yaml").write_text("dry_run_only: false")
    gap = {"kind": "event_gated", "detail": "Synthetic future run",
           "trigger": "2030-01-02", "recheck": "echo synthetic"}
    result = await call(_doc(gaps=[gap]))
    assert result["recorded"] is True
    stored = row(path)
    assert stored["status"] == "closed"
    assert json.loads(stored["rechecks"]) == [
        {**{k: gap[k] for k in ("trigger", "recheck", "detail")},
         "results": [], "done_at": None}
    ]


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
