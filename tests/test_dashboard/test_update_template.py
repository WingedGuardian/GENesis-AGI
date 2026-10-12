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


def _rendered_spawn_cc_source() -> str:
    import ast
    import string

    from genesis.dashboard.routes import updates

    fields = {f for _, f, _, _ in string.Formatter().parse(updates._ORCHESTRATOR_TEMPLATE) if f}
    rendered = updates._ORCHESTRATOR_TEMPLATE.format(**{f: "x" for f in fields})
    [spawn] = [
        n
        for n in ast.walk(ast.parse(rendered))
        if isinstance(n, ast.FunctionDef) and n.name == "spawn_cc"
    ]
    return ast.unparse(spawn)


def test_orchestrator_sessions_carry_the_update_tier_stamp():
    """Tier 1/2 sessions resolve the update's merge in the main checkout; the
    main-checkout guard allows exactly the sessions carrying this stamp."""
    src = _rendered_spawn_cc_source()
    assert "'GENESIS_UPDATE_TIER': '1'" in src, src
    assert "env=env" in src, src


def test_the_tier3_session_carries_the_update_tier_stamp(monkeypatch, tmp_path):
    from genesis.dashboard.routes import updates

    captured = {}

    class _Proc:
        pid = 1

    def fake_popen(cmd, **kw):
        captured.update(kw)
        return _Proc()

    monkeypatch.setattr(updates, "_HOME", tmp_path)
    monkeypatch.setattr(updates.subprocess, "Popen", fake_popen)
    monkeypatch.delenv("GENESIS_UPDATE_TIER", raising=False)
    updates._spawn_detached_cc("p", "opus", "max")
    assert captured["env"]["GENESIS_UPDATE_TIER"] == "1"
