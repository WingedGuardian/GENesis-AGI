


import pytest


@pytest.mark.asyncio
async def test_essential_knowledge_injected(tmp_path, monkeypatch):
    """Essential knowledge file should appear in diagnostic context."""
    from genesis.sentinel.context import assemble_diagnostic_context

    ek_dir = tmp_path / ".genesis"
    ek_dir.mkdir()
    (ek_dir / "essential_knowledge.md").write_text("Wing: memory\nActive: drift recall")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    result = await assemble_diagnostic_context(
        alarms=[],
        trigger_source="test",
        trigger_reason="test trigger",
    )
    assert "Essential Knowledge" in result
    assert "drift recall" in result


@pytest.mark.asyncio
async def test_missing_ek_does_not_fail(tmp_path, monkeypatch):
    """Missing essential_knowledge.md should not cause an error."""
    from genesis.sentinel.context import assemble_diagnostic_context

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    result = await assemble_diagnostic_context(
        alarms=[],
        trigger_source="test",
        trigger_reason="test trigger",
    )
    assert "Essential Knowledge" not in result
