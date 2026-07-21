"""Structural tests for the skill-replay arm builder.

No live CC — assert the built CCInvocation carries the bare-arm isolation
recipe and that the ONLY delta between arms is the pinned skill content.
"""

from __future__ import annotations

import pytest

from genesis.cc.types import CCModel, EffortLevel
from genesis.eval.bench.types import BenchTask
from genesis.eval.skill_replay.arms import build_skill_arm_invocation
from genesis.eval.skill_replay.types import ARM_NEW, ARM_OLD

_TASK = BenchTask(
    id="t1",
    category="drafting",
    prompt="Write a two-sentence intro in the user's voice.",
    expected="First person; no AI-tell constructions.",
)


def _arm(tmp_path, arm_label, content):
    return build_skill_arm_invocation(
        _TASK,
        tmp_path,
        CCModel.SONNET,
        EffortLevel.MEDIUM,
        skill_name="voice-master",
        skill_content=content,
        bare_config_dir=tmp_path / "bare-config",
        run_id="run1",
        arm_label=arm_label,
    )


def test_arm_carries_bare_isolation_recipe(tmp_path):
    inv = _arm(tmp_path, ARM_OLD, "OLD BODY")
    assert inv.safe_mode is True
    assert inv.strict_mcp_config is True
    assert inv.mcp_config.endswith("no_mcp.json")
    assert inv.skip_permissions is True
    assert inv.env_overrides["CLAUDE_CONFIG_DIR"].endswith("bare-config")
    # Neutral per-arm cwd, outside any repo checkout.
    assert inv.working_dir.endswith(f"work/{_TASK.id}/{ARM_OLD}")


def test_pinned_content_is_the_system_prompt(tmp_path):
    inv = _arm(tmp_path, ARM_NEW, "NEW BODY HERE")
    assert inv.system_prompt == "## Skill: voice-master\nNEW BODY HERE"
    # Prompt is the task + shared fairness envelope (out-of-bounds rule etc.).
    assert inv.prompt.startswith(_TASK.rendered_prompt())


def test_only_delta_between_arms_is_pinned_content(tmp_path):
    old = _arm(tmp_path, ARM_OLD, "OLD BODY")
    new = _arm(tmp_path, ARM_NEW, "NEW BODY")
    # Identical fairness surface.
    assert old.prompt == new.prompt
    assert old.model == new.model
    assert old.effort == new.effort
    assert old.timeout_s == new.timeout_s
    assert old.safe_mode == new.safe_mode
    # The intended deltas.
    assert old.system_prompt != new.system_prompt
    assert old.working_dir != new.working_dir
    assert old.session_key != new.session_key
    assert old.session_key.endswith(ARM_OLD)
    assert new.session_key.endswith(ARM_NEW)


def test_invalid_arm_label_raises(tmp_path):
    with pytest.raises(ValueError, match="arm_label must be"):
        _arm(tmp_path, "treatment", "body")
