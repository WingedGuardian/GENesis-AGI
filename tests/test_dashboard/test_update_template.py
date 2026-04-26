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
