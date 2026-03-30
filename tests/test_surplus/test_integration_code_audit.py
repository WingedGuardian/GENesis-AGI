"""Integration test: CODE_AUDIT pipeline end-to-end."""

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_code_audit_pipeline_end_to_end(db):
    """Verify: enqueue → execute → bridge → surplus_insights."""
    from genesis.surplus.code_audit import CodeAuditExecutor
    from genesis.surplus.findings_bridge import FindingsBridge
    from genesis.surplus.types import ComputeTier, SurplusTask, TaskStatus, TaskType

    # Create mock router that returns a finding
    mock_router = AsyncMock()
    mock_result = MagicMock()
    mock_result.success = True
    mock_result.content = '[{"file": "src/test.py", "line": 10, "severity": "medium", "description": "test issue", "suggestion": "fix it", "confidence": 0.9}]'
    mock_result.provider_used = "test-provider"
    mock_router.route_call = AsyncMock(return_value=mock_result)

    # Create executor
    executor = CodeAuditExecutor(router=mock_router, db=db)

    # Create a task
    task = SurplusTask(
        id="test-audit-1",
        task_type=TaskType.CODE_AUDIT,
        compute_tier=ComputeTier.FREE_API,
        priority=0.5,
        drive_alignment="competence",
        status=TaskStatus.RUNNING,
        created_at="2026-03-18T00:00:00",
    )

    # Execute
    result = await executor.execute(task)
    assert result.success
    assert len(result.insights) >= 1

    # Bridge findings
    bridge = FindingsBridge(db=db)
    bridged = await bridge.bridge_findings(result.insights)
    assert bridged >= 1

    # Verify findings were staged in surplus_insights (not observations)
    cursor = await db.execute(
        "SELECT * FROM surplus_insights WHERE source_task_type = 'code_audit'"
    )
    rows = await cursor.fetchall()
    assert len(rows) >= 1
