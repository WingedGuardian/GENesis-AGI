"""Tests for surplus package exports."""


def test_surplus_exports():
    from genesis.surplus import (
        SurplusScheduler,
        TaskType,
    )

    assert TaskType.BRAINSTORM_USER == "brainstorm_user"
    assert SurplusScheduler is not None
