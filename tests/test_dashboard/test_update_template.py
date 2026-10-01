"""Verify the orchestrator template is valid Python after substitution."""

from genesis.dashboard.routes.updates import _ORCHESTRATOR_TEMPLATE


def test_orchestrator_template_compiles():
    """Substituted template must be syntactically valid Python.

    Catches typos, indentation errors, or broken string literals that
    would otherwise only surface at runtime during an actual update.
    """
    code = _ORCHESTRATOR_TEMPLATE.format(
        summary_file="/tmp/test_summary.txt",
        escalation_file="/tmp/test_escalation.txt",
        pid_file="/tmp/test_pid",
        genesis_root="/tmp/test_genesis",
        tier1_prompt="test tier 1 prompt",
        tier2_prompt="test tier 2 prompt",
    )
    # compile() raises SyntaxError on invalid Python
    compile(code, "<orchestrator-template>", "exec")


def test_backup_tab_surfaces_not_restarted_success():
    """The Last Update status span must distinguish a not-restarted success."""
    from pathlib import Path

    from genesis.dashboard.routes import updates

    html = (
        Path(updates.__file__).parent.parent
        / "templates" / "partials" / "tabs" / "backup.html"
    ).read_text()
    assert "server not restarted" in html
    assert "server_restarted === false" in html
    # A failed row reconciled to success must be labelled, not plain green.
    assert "reconciled" in html
    assert " (reconciled)" in html
