


from types import SimpleNamespace

import pytest


def _alarm(alert_id, *, tier=1, severity="critical", message="test"):
    return SimpleNamespace(tier=tier, alert_id=alert_id, severity=severity, message=message)


@pytest.mark.asyncio
async def test_available_remediation_tools_section_renders_from_scope():
    """The tool-inventory section lists the tools in the passed scope, with
    their descriptions, so the Sentinel knows what it can actually do."""
    from genesis.sentinel.context import assemble_diagnostic_context

    scope = frozenset({"container.services", "container.disk_reclaim"})
    result = await assemble_diagnostic_context(
        alarms=[],
        trigger_source="test",
        trigger_reason="test",
        scope=scope,
    )
    assert "## Available Remediation Tools" in result
    # descriptions come straight from remediation_map.TOOLS
    assert "restart systemd user services" in result
    assert "Reclaim container disk" in result
    # a tool NOT in scope must not appear
    assert "Restart the local Qdrant" not in result


@pytest.mark.asyncio
async def test_alarm_line_shows_actionable_remediation():
    """A firing alarm whose mapped tool is available shows the 'you can act on
    this with' rationale naming that tool — the 'why you were woken' link."""
    from genesis.sentinel.context import assemble_diagnostic_context

    # infra:disk_low → {container.disk_reclaim, host.resource_alloc}
    result = await assemble_diagnostic_context(
        alarms=[_alarm("infra:disk_low")],
        trigger_source="fire_alarm",
        trigger_reason="disk",
        scope=frozenset({"container.disk_reclaim"}),
    )
    assert "[infra:disk_low]" in result
    assert "you can act on this with: container.disk_reclaim" in result


@pytest.mark.asyncio
async def test_alarm_line_flags_unavailable_remediation():
    """A mapped alarm whose tool is NOT available on this install tells the
    Sentinel to escalate rather than invent a fix."""
    from genesis.sentinel.context import assemble_diagnostic_context

    # provider:qdrant_unreachable → {qdrant.local}, which we omit from scope
    result = await assemble_diagnostic_context(
        alarms=[_alarm("provider:qdrant_unreachable")],
        trigger_source="fire_alarm",
        trigger_reason="qdrant",
        scope=frozenset({"container.services"}),
    )
    assert "is not available on this install" in result
    assert "escalate" in result


@pytest.mark.asyncio
async def test_scope_none_defaults_to_live_available_tools(monkeypatch):
    """When scope is omitted, the function evaluates available_tools() itself so
    it stays self-contained for callers/tests that don't thread scope in."""
    from genesis.sentinel import context as context_mod

    monkeypatch.setattr(
        context_mod, "available_tools", lambda: frozenset({"container.services"})
    )
    result = await context_mod.assemble_diagnostic_context(
        alarms=[],
        trigger_source="test",
        trigger_reason="test",
    )
    assert "## Available Remediation Tools" in result
    assert "restart systemd user services" in result


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
