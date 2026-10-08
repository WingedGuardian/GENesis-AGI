"""Internal immutable binding and reserved source-tag controls."""

import time
from dataclasses import FrozenInstanceError, replace

import pytest

from genesis.cc.direct_session import DirectSessionRequest
from genesis.cc.peer_segment import PeerSegment
from genesis.peers.session import PeerSessionBinding


@pytest.fixture
def binding(tmp_path):
    return PeerSessionBinding(
        task_id="a" * 32,
        segment=PeerSegment("b" * 32, time.time() + 60, ("mcp__genesis_peer__task_context",)),
        generation=0,
        facade_config=str(tmp_path / "facade.json"),
        working_dir=str(tmp_path),
    )


def test_binding_is_immutable_and_ceiling_is_bounded(binding):
    assert binding.metadata(4)["autonomy_ceiling"] == 3
    with pytest.raises(FrozenInstanceError):
        binding.generation = 1
    for changes in (
        {"generation": True},
        {"generation": -1},
        {"task_id": "../bad"},
        {"working_dir": "relative"},
        {"facade_config": "relative"},
    ):
        with pytest.raises(ValueError):
            replace(binding, **changes)


def test_reserved_source_tag_cannot_enter_the_legacy_path():
    with pytest.raises(ValueError, match="constrained internal binding"):
        DirectSessionRequest(prompt="request", source_tag="peer_api")


@pytest.mark.parametrize(
    "changes",
    [
        {"source_tag": "user_request"},
        {"profile": "interact"},
        {"notify": True},
        {"system_prompt": "owner"},
        {"skills": []},
        {"tool_exceptions": ("Bash",)},
        {"planning_instruction": "override"},
        {"origin_session_id": "owner"},
        {"origin_caller_context": "owner"},
        {"caller_context": "ego_proposal:other"},
        {"roster_model": "other"},
        {"timeout_s": True},
        {"timeout_s": 7201},
    ],
)
def test_peer_binding_rejects_owner_execution_overrides(binding, changes):
    request = DirectSessionRequest(
        prompt="request", source_tag="peer_api", notify=False, peer_binding=binding
    )
    with pytest.raises(ValueError, match="constrained internal binding"):
        replace(request, **changes)
