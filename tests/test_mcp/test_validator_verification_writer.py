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
from tests.fixtures.verification_sources import (
    CLOSE_CASES,
    reviewed_close,
    simple_receipt,
    stub_legacy_writer_review,
    tracking_closes_during_acquisition,
    use_real_receipt_assessment,
    writer_source,
)
from tests.test_scripts.test_pr_verification_closer import MERGE_SHA, _doc, _doc_for, _with_tracking


@pytest.fixture(autouse=True)
def _close_before_the_fixture_dates(monkeypatch):
    """Every close here is judged on a day before the fixtures' event-gate dates
    (2026-11-02, 2030-01-02), so a close may still store them as rechecks. The
    refusal of a due trigger is tested with its own date below."""
    from datetime import date

    from genesis.mcp.health import validator_writes

    monkeypatch.setattr(validator_writes, "_today", lambda: date(2026, 11, 1))


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
    stub_legacy_writer_review(monkeypatch)
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
    from genesis.session_awareness import pr_verification_sources

    path, _, config = recording
    config.with_suffix(".local.yaml").write_text("dry_run_only: false")
    monkeypatch.setattr(validator_writes, "gh_pr_metadata", lambda *a: PrMetadata(MERGE_SHA, "E2E: run timer"))
    monkeypatch.setattr(pr_verification_sources, "acquire_sources", lambda *a: pytest.fail("opened full sources"))
    result = await call(_doc(findings=[dict(summary="timer defect", disposition="unfiled")]), route=False)
    assert result["recorded"] and not result["would_close"]
    assert result["measurement_would_close"] and result["completeness"] == "tracking_pending"
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
    from genesis.session_awareness import pr_verification_sources

    path, _, config = recording
    config.with_suffix(".local.yaml").write_text("dry_run_only: false")
    before = row(path)
    step = "restart the timer and measure its next run"
    monkeypatch.setattr(validator_writes, "gh_pr_metadata", lambda *a: PrMetadata(MERGE_SHA, "E2E: " + step))
    monkeypatch.setattr(pr_verification_sources, "acquire_sources", lambda *a: writer_source(*a, "E2E: " + step))
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
    from genesis.session_awareness import pr_verification_sources

    path, _, config = recording
    config.with_suffix(".local.yaml").write_text("dry_run_only: false")
    monkeypatch.setattr(validator_writes, "gh_pr_metadata", lambda *a: PrMetadata(MERGE_SHA, "E2E: run the timer"))
    monkeypatch.setattr(pr_verification_sources, "acquire_sources", lambda *a: pytest.fail("opened full sources"))
    doc = _doc(gaps=[{"kind": "not_yet_done", "detail": "not run", "command": "run timer"}])
    result = await call(doc)
    assert result["verdict"] == "not-yet-done" and result["recorded"]
    assert result["completeness"] == "not_required" and not result["measurement_would_close"]
    assert row(path)["status"] == "open"


@pytest.mark.parametrize("declared", [True, False, None])
async def test_the_result_echoes_the_documents_no_progress(recording, monkeypatch, declared):
    from genesis.mcp.health import validator_writes

    path, _, config = recording
    config.with_suffix(".local.yaml").write_text("dry_run_only: false")
    monkeypatch.setattr(
        validator_writes, "gh_pr_metadata", lambda *a: PrMetadata(MERGE_SHA, "E2E: run the timer")
    )
    over = {} if declared is None else {"no_progress": declared}
    doc = _doc(gaps=[{"kind": "not_yet_done", "detail": "not run", "command": "run timer"}], **over)
    result = await call(doc)
    assert result["recorded"] and result["no_progress"] is bool(declared)
    assert json.loads(row(path)["evidence"])["no_progress"] is bool(declared)
    preview = await call(doc, dry_run=True)
    assert preview["recorded"] is False and preview["no_progress"] is bool(declared)


async def test_existing_mcp_close_stores_rechecks_without_a_new_caller_argument(recording, monkeypatch):
    path, _, config = recording
    config.with_suffix(".local.yaml").write_text("dry_run_only: false")
    gap = {"kind": "event_gated", "detail": "Synthetic future run",
           "trigger": "2030-01-02", "recheck": "echo synthetic"}
    use_real_receipt_assessment(monkeypatch)
    doc = simple_receipt(_with_tracking(_doc(gaps=[gap])))
    result = await call(doc, route=False)
    assert result["recorded"] is True
    assert result["completeness"] == "verified"
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


@pytest.mark.parametrize("verdict", ["pass-mechanical", "cannot-verify"])
async def test_the_sealed_validator_is_stamped_on_what_it_writes(recording, monkeypatch, verdict):
    # Even if the server inherited a Claude Code shell's markers and transcript id,
    # the external-client profile in force for the call decides the label, and a
    # Codex label never carries the parent transcript as its session.
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-transcript")
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    path, _shared, config = recording
    config.with_suffix(".local.yaml").write_text("dry_run_only: false")
    args = {"note": "Synthetic result"} if verdict != "pass-mechanical" else {}
    assert (await call(_doc_for(verdict), **args))["recorded"] is True
    stored = row(path)
    by = "closed_by" if verdict == "pass-mechanical" else "last_attempt_by"
    assert stored[by] == "codex-sealed" and stored["recorded_session"] is None


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


async def test_a_preview_returns_the_completeness_prompts(recording):
    doc = _doc(gaps=[{"kind": "method", "detail": "only CI ran it"}])
    result = await call(doc, dry_run=True)
    assert result["recorded"] is False
    assert result["prompts"] and result["prompts"][0].startswith("gaps/0 is a method gap")


async def test_a_clean_document_returns_no_prompts(recording):
    result = await call(_doc(), dry_run=True)
    assert result["prompts"] == []


@pytest.mark.parametrize("receipt", [False, True])
async def test_a_close_with_a_date_gate_already_come_is_refused(recording, monkeypatch, receipt):
    from datetime import date

    from genesis.mcp.health import validator_writes

    monkeypatch.setattr(validator_writes, "_today", lambda: date(2026, 11, 14))
    gate = {
        "kind": "event_gated",
        "detail": "weekly run",
        "trigger": "2026-11-14",
        "recheck": "gh run list",
    }
    path, _, _ = recording
    before = row(path)
    doc = _with_tracking(_doc(gaps=[gate]))
    if receipt:
        doc = simple_receipt(doc)
    use_real_receipt_assessment(monkeypatch)
    with pytest.raises(ToolError, match=r"gaps/0 \(trigger 2026-11-14\)"):
        await call(doc, route=False, dry_run=True)
    assert row(path) == before


@pytest.mark.parametrize("case", CLOSE_CASES)
@pytest.mark.parametrize("mode", ["write", "explicit-preview", "forced-preview"])
async def test_close_requires_an_independent_receipt(recording, tmp_path, monkeypatch, case, mode):
    from genesis.mcp.health import validator_writes

    path, _, config = recording
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    doc, source = reviewed_close(source_dir, monkeypatch, case)
    if mode != "forced-preview":
        config.with_suffix(".local.yaml").write_text("dry_run_only: false")
    monkeypatch.setattr(validator_writes, "gh_pr_metadata", lambda *a: PrMetadata(MERGE_SHA, source.body))
    before = row(path)
    flags = {"dry_run": mode == "explicit-preview", "route": False}
    if reason := CLOSE_CASES[case]:
        with pytest.raises(ToolError, match=reason) as refused:
            await call(doc, **flags)
        if mode != "write" and case == "missing":
            assert "Derived verdict: pass-mechanical (close refused)" in str(refused.value)
        assert row(path) == before
        return
    result = await call(doc, **flags)
    assert result["completeness"] == "verified" and result["would_close"]
    assert result["server_dry_run_only"] is (mode == "forced-preview")
    assert result["recorded"] is (mode == "write")
    stored = row(path)
    if mode != "write":
        assert stored == before
        return
    assert stored["status"] == "closed"
    receipt = json.loads(stored["evidence"])["completeness"]
    assert receipt["complete"] is True
    assert receipt["evidence_sha256"] == doc["completeness"]["evidence_sha256"]
    assert receipt["review_execution_ref"] == "fixture-only:no-model-ran"
    with pytest.raises(ToolError, match="no OPEN row"):
        await call(doc, **flags)
    assert row(path) == stored


@pytest.mark.parametrize("mode", ["write", "explicit-preview", "forced-preview"])
async def test_tracking_closed_during_source_acquisition(recording, monkeypatch, mode):
    path, _, config = recording
    if mode != "forced-preview":
        config.with_suffix(".local.yaml").write_text("dry_run_only: false")
    before = row(path)
    doc = simple_receipt(_with_tracking(_doc(
        findings=[{"summary": "reachable timer defect", "disposition": "filed"}],
    )))
    reads = tracking_closes_during_acquisition(monkeypatch)
    result = await call(doc, route=False, dry_run=mode == "explicit-preview")
    assert reads == ["tracking", "sources", "tracking"]
    assert result["recorded"] is (mode == "write")
    assert result["measurement_would_close"] and not result["would_close"]
    assert result["completeness"] == "tracking_pending"
    assert "public task closed while acquiring PR sources" in result["tracking"]["pending"][0]
    stored = row(path)
    if mode != "write":
        assert stored == before
        return
    assert stored["status"] == "open"
    assert stored["verdict"] == "pass-mechanical"
    assert stored["attempt_count"] == before["attempt_count"] + 1
    assert json.loads(stored["evidence"])["findings"] == doc["findings"]
