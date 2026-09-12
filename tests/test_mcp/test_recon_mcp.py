"""Tests for recon-mcp server — findings CRUD, triage, watchlist."""

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables
from genesis.mcp.recon_mcp import (
    _load_watchlist,
    init_recon_mcp,
    mcp,
)


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture
async def tools(db):
    init_recon_mcp(db=db)
    return await mcp.get_tools()


# ── watchlist (unchanged) ────────────────────────────────────────────────────


async def test_all_tools_registered(tools):
    for name in [
        "recon_config",
        "recon_findings",
        "recon_triage",
        "recon_store_finding",
        "recon_github_search",
        "recon_github_read",
    ]:
        assert name in tools, f"Missing tool: {name}"


async def test_github_search_distinguishes_empty_results_from_failure(tools, monkeypatch):
    async def empty(*args, **kwargs):
        return True, '{"total_count":0,"incomplete_results":false,"items":[]}'

    monkeypatch.setattr("genesis.mcp.recon_mcp.run_gh_checked", empty)
    result = await tools["recon_github_search"].fn(kind="repositories", query="unlikely")
    assert result == {
        "ok": True,
        "kind": "repositories",
        "query": "unlikely",
        "total_count": 0,
        "accessible_count": 0,
        "incomplete_results": False,
        "items": [],
        "page": 1,
        "per_page": 30,
        "has_more": False,
    }

    async def failed(*args, **kwargs):
        return False, ""

    monkeypatch.setattr("genesis.mcp.recon_mcp.run_gh_checked", failed)
    failed_result = await tools["recon_github_search"].fn(
        kind="repositories", query="unlikely"
    )
    assert failed_result["ok"] is False
    assert "failed" in failed_result["error"].lower()


async def test_github_read_file_reports_truncation(tools, monkeypatch):
    async def fake(*args, **kwargs):
        import base64
        import json

        return True, json.dumps({
            "type": "file",
            "size": 12,
            "sha": "abc",
            "html_url": "https://github.com/o/r/blob/main/a.txt",
            "download_url": "https://raw.example/a.txt",
            "encoding": "base64",
            "content": base64.b64encode(b"abcdefghijkl").decode(),
        })

    monkeypatch.setattr("genesis.mcp.recon_mcp.run_gh_checked", fake)
    result = await tools["recon_github_read"].fn(
        repository="o/r", operation="file", path="a.txt", max_chars=5
    )
    assert result["ok"] is True
    assert result["content"] == "abcde"
    assert result["truncated"] is True
    assert result["total_chars"] == 12


async def test_github_file_read_rejects_directory_response_without_crashing(tools, monkeypatch):
    async def directory(*args, **kwargs):
        return True, '[{"type":"file","name":"child.txt"}]'

    monkeypatch.setattr("genesis.mcp.recon_mcp.run_gh_checked", directory)
    result = await tools["recon_github_read"].fn(
        repository="o/r", operation="file", path="directory"
    )
    assert result["ok"] is False
    assert "not a file" in result["error"]


async def test_github_search_has_more_respects_api_1000_result_cap(tools, monkeypatch):
    async def many(*args, **kwargs):
        return True, '{"total_count":5000,"incomplete_results":false,"items":[]}'

    monkeypatch.setattr("genesis.mcp.recon_mcp.run_gh_checked", many)
    result = await tools["recon_github_search"].fn(
        kind="repositories", query="popular", page=10, per_page=100
    )
    assert result["has_more"] is False
    assert result["accessible_count"] == 1000


def test_load_watchlist():
    projects = _load_watchlist()
    assert isinstance(projects, list)
    assert len(projects) >= 5
    names = [p["name"] for p in projects]
    assert "Claude Code" in names


def test_load_watchlist_has_required_fields():
    projects = _load_watchlist()
    for p in projects:
        assert "name" in p
        assert "repo" in p
        assert "track" in p
        assert "priority" in p


async def test_recon_config_watchlist_returns_all(tools):
    result = await tools["recon_config"].fn(aspect="watchlist")
    assert len(result) >= 5


async def test_recon_config_watchlist_filters_by_priority(tools):
    high = await tools["recon_config"].fn(aspect="watchlist", priority="high")
    assert all(p["priority"] == "high" for p in high)
    assert len(high) >= 1


# ── findings CRUD ────────────────────────────────────────────────────────────


async def test_store_and_query_roundtrip(tools):
    result = await tools["recon_store_finding"].fn(
        title="Test Finding",
        summary="Some details",
        job_type="github_landscape",
        priority="high",
    )
    assert "finding_id" in result

    findings = await tools["recon_findings"].fn()
    assert len(findings) == 1
    assert findings[0]["category"] == "github_landscape"
    assert findings[0]["priority"] == "high"
    assert "Test Finding" in findings[0]["content"]


async def test_findings_filter_by_job_type(tools):
    await tools["recon_store_finding"].fn(
        title="A", summary="", job_type="github_landscape", priority="medium",
    )
    await tools["recon_store_finding"].fn(
        title="B", summary="", job_type="email_recon", priority="medium",
    )

    gh = await tools["recon_findings"].fn(job_type="github_landscape")
    assert len(gh) == 1
    assert "A" in gh[0]["content"]

    email = await tools["recon_findings"].fn(job_type="email_recon")
    assert len(email) == 1
    assert "B" in email[0]["content"]


async def test_findings_filter_by_priority(tools):
    await tools["recon_store_finding"].fn(
        title="High", summary="", job_type="web_monitoring", priority="high",
    )
    await tools["recon_store_finding"].fn(
        title="Low", summary="", job_type="web_monitoring", priority="low",
    )

    high = await tools["recon_findings"].fn(priority="high")
    assert all(r["priority"] == "high" for r in high)


async def test_findings_filter_by_triaged(tools):
    result = await tools["recon_store_finding"].fn(
        title="Triage me", summary="", job_type="test", priority="medium",
    )
    fid = result["finding_id"]

    untriaged = await tools["recon_findings"].fn(triaged=False)
    assert len(untriaged) == 1

    await tools["recon_triage"].fn(finding_id=fid, notes="done", action="dismiss")

    untriaged = await tools["recon_findings"].fn(triaged=False)
    assert len(untriaged) == 0

    triaged = await tools["recon_findings"].fn(triaged=True)
    assert len(triaged) == 1


# ── triage ───────────────────────────────────────────────────────────────────


async def test_triage_dismiss(tools):
    result = await tools["recon_store_finding"].fn(
        title="Dismiss me", summary="", job_type="test", priority="low",
    )
    fid = result["finding_id"]

    triage = await tools["recon_triage"].fn(finding_id=fid, notes="not relevant", action="dismiss")
    assert triage["success"] is True
    assert triage["action"] == "dismiss"


async def test_triage_acknowledge(tools):
    result = await tools["recon_store_finding"].fn(
        title="Ack me", summary="", job_type="test", priority="medium",
    )
    fid = result["finding_id"]

    triage = await tools["recon_triage"].fn(finding_id=fid, notes="noted", action="acknowledge")
    assert triage["success"] is True
    assert triage["action"] == "acknowledge"


async def test_triage_defer(tools):
    result = await tools["recon_store_finding"].fn(
        title="Defer me", summary="", job_type="test", priority="medium",
    )
    fid = result["finding_id"]

    triage = await tools["recon_triage"].fn(finding_id=fid, notes="check later", action="defer")
    assert triage["success"] is True
    assert triage["action"] == "defer"

    # Should still be untriaged (not resolved)
    untriaged = await tools["recon_findings"].fn(triaged=False)
    assert any(r["id"] == fid for r in untriaged)


async def test_triage_invalid_action(tools):
    result = await tools["recon_store_finding"].fn(
        title="Invalid", summary="", job_type="test", priority="medium",
    )
    fid = result["finding_id"]

    triage = await tools["recon_triage"].fn(finding_id=fid, notes="", action="delete")
    assert triage["success"] is False
    assert "Invalid action" in triage["error"]


async def test_store_finding_with_source_url(tools):
    await tools["recon_store_finding"].fn(
        title="URL Finding",
        summary="Details",
        job_type="web_monitoring",
        source_url="https://example.com",
    )
    findings = await tools["recon_findings"].fn()
    assert "https://example.com" in findings[0]["content"]
