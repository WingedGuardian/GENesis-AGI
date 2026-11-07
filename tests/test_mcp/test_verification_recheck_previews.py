"""MCP close preview owes the same rechecks as CLI and real recording."""

import json

import pytest
from fastmcp.exceptions import ToolError

from tests.test_mcp.test_validator_verification_writer import call, row
from tests.test_mcp.test_validator_verification_writer import recording as recording
from tests.test_scripts.test_pr_verification_closer import EVENT_GAP, _doc


@pytest.mark.parametrize("forced", [True, False])
async def test_mcp_preview_lists_rechecks_without_mutation(recording, forced):
    path, _, config = recording
    if not forced:
        config.with_suffix(".local.yaml").write_text("dry_run_only: false")
    before = row(path)
    result = await call(_doc(gaps=[EVENT_GAP]), dry_run=True)
    assert result["dry_run"] is True and result["recorded"] is False
    assert result["rechecks"] == [{k: EVENT_GAP[k] for k in ("trigger", "recheck", "detail")}]
    assert row(path) == before


@pytest.mark.parametrize("preview", [True, False])
async def test_mcp_refuses_owed_rechecks_before_migration_even_in_preview(recording, preview):
    path, shared, config = recording
    config.with_suffix(".local.yaml").write_text("dry_run_only: false")
    await shared.execute("ALTER TABLE pr_verifications DROP COLUMN rechecks")
    await shared.commit()
    before = row(path)
    with pytest.raises(ToolError, match="column is unavailable"):
        await call(_doc(gaps=[EVENT_GAP]), dry_run=preview)
    assert row(path) == before


async def test_mcp_real_close_keeps_rechecks_and_original_evidence(recording):
    path, _, config = recording
    config.with_suffix(".local.yaml").write_text("dry_run_only: false")
    result = await call(_doc(gaps=[EVENT_GAP]))
    assert result["recorded"] is True
    stored = row(path)
    assert json.loads(stored["rechecks"])[0]["trigger"] == EVENT_GAP["trigger"]
    assert json.loads(stored["evidence"])["gaps"] == [EVENT_GAP]
