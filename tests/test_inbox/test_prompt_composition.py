"""Inbox evaluator prompt composition contracts."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from genesis.db.crud.prompt_versions import compute_prompt_hash
from genesis.inbox.monitor import InboxMonitor, InboxPromptLoadError
from genesis.inbox.types import InboxConfig


def _monitor(prompt_dir: Path, *, watch_dir: Path | None = None) -> InboxMonitor:
    return InboxMonitor(
        db=AsyncMock(),
        invoker=AsyncMock(),
        session_manager=AsyncMock(),
        config=InboxConfig(watch_path=watch_dir or prompt_dir),
        prompt_dir=prompt_dir,
    )


def test_system_prompt_composes_policy_then_both_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = "CANONICAL INBOX POLICY\n"
    skills = {
        "evaluate": "GENESIS FRAMEWORK BODY\n",
        "user_evaluate": "PERSONAL FRAMEWORK BODY\n",
    }
    (tmp_path / "INBOX_EVALUATE.md").write_text(policy, encoding="utf-8")
    monkeypatch.setattr(
        "genesis.learning.skills.wiring.load_skill",
        lambda name: skills.get(name),
    )

    monitor = _monitor(tmp_path)
    prompt = monitor._load_system_prompt()

    assert prompt.index("CANONICAL INBOX POLICY") < prompt.index("## Skill: evaluate")
    assert prompt.index("## Skill: evaluate") < prompt.index("## Skill: user_evaluate")
    assert prompt.count(skills["evaluate"]) == 1
    assert prompt.count(skills["user_evaluate"]) == 1
    assert prompt.endswith("Do not try to load either skill again.\n")
    assert monitor._prompt_hash == compute_prompt_hash(prompt)


@pytest.mark.parametrize("content", [None, "", "  \n\t"])
def test_system_prompt_rejects_missing_or_empty_policy(
    tmp_path: Path,
    content: str | None,
) -> None:
    if content is not None:
        (tmp_path / "INBOX_EVALUATE.md").write_text(content, encoding="utf-8")

    with pytest.raises(InboxPromptLoadError, match="INBOX_EVALUATE.md"):
        _monitor(tmp_path)._load_system_prompt()


def test_system_prompt_wraps_unreadable_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "INBOX_EVALUATE.md"
    path.write_text("policy", encoding="utf-8")
    monkeypatch.setattr(
        Path,
        "read_text",
        Mock(side_effect=OSError("synthetic read failure")),
    )

    with pytest.raises(InboxPromptLoadError, match="INBOX_EVALUATE.md"):
        _monitor(tmp_path)._load_system_prompt()


@pytest.mark.parametrize("invalid_kind", ["invalid_utf8", "directory"])
def test_system_prompt_rejects_non_text_policy(
    tmp_path: Path,
    invalid_kind: str,
) -> None:
    path = tmp_path / "INBOX_EVALUATE.md"
    if invalid_kind == "invalid_utf8":
        path.write_bytes(b"\xff")
    else:
        path.mkdir()

    with pytest.raises(InboxPromptLoadError, match="INBOX_EVALUATE.md"):
        _monitor(tmp_path)._load_system_prompt()


@pytest.mark.parametrize("skill_name", ["evaluate", "user_evaluate"])
@pytest.mark.parametrize("failure", [None, "", "  \n"])
def test_system_prompt_rejects_missing_or_empty_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    skill_name: str,
    failure: str | None,
) -> None:
    (tmp_path / "INBOX_EVALUATE.md").write_text("policy", encoding="utf-8")

    def load_skill(name: str) -> str | None:
        if name == skill_name:
            return failure
        return f"{name} body"

    monkeypatch.setattr("genesis.learning.skills.wiring.load_skill", load_skill)

    with pytest.raises(InboxPromptLoadError, match=skill_name):
        _monitor(tmp_path)._load_system_prompt()


@pytest.mark.parametrize("skill_name", ["evaluate", "user_evaluate"])
@pytest.mark.parametrize(
    "failure",
    [OSError("synthetic read failure"), UnicodeError("synthetic decode failure")],
)
def test_system_prompt_wraps_unreadable_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    skill_name: str,
    failure: Exception,
) -> None:
    (tmp_path / "INBOX_EVALUATE.md").write_text("policy", encoding="utf-8")

    def load_skill(name: str) -> str:
        if name == skill_name:
            raise failure
        return f"{name} body"

    monkeypatch.setattr("genesis.learning.skills.wiring.load_skill", load_skill)

    with pytest.raises(InboxPromptLoadError, match=skill_name):
        _monitor(tmp_path)._load_system_prompt()


def test_failed_composition_does_not_cache_partial_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_path = tmp_path / "INBOX_EVALUATE.md"
    policy_path.write_text("policy", encoding="utf-8")
    skills = {"evaluate": "evaluate body", "user_evaluate": None}
    monkeypatch.setattr(
        "genesis.learning.skills.wiring.load_skill",
        lambda name: skills[name],
    )
    monitor = _monitor(tmp_path)

    with pytest.raises(InboxPromptLoadError):
        monitor._load_system_prompt()

    assert monitor._system_prompt is None
    assert monitor._prompt_hash == ""
    skills["user_evaluate"] = "user body"
    assert "user body" in monitor._load_system_prompt()


def test_successful_composite_is_cached_for_monitor_lifetime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy_path = tmp_path / "INBOX_EVALUATE.md"
    policy_path.write_text("policy v1", encoding="utf-8")
    calls: list[str] = []

    def load_skill(name: str) -> str:
        calls.append(name)
        return f"{name} v1"

    monkeypatch.setattr("genesis.learning.skills.wiring.load_skill", load_skill)
    monitor = _monitor(tmp_path)
    first = monitor._load_system_prompt()
    first_hash = monitor._prompt_hash

    policy_path.write_text("policy v2", encoding="utf-8")
    second = monitor._load_system_prompt()

    assert second == first
    assert monitor._prompt_hash == first_hash
    assert calls == ["evaluate", "user_evaluate"]


@pytest.mark.asyncio
async def test_composition_failure_preflights_before_any_state_change(tmp_path: Path) -> None:
    watch_dir = tmp_path / "watch"
    prompt_dir = tmp_path / "prompts"
    watch_dir.mkdir()
    prompt_dir.mkdir()
    (watch_dir / "new-item.md").write_text("new inbox item", encoding="utf-8")
    monitor = _monitor(prompt_dir, watch_dir=watch_dir)
    monitor._autonomous_dispatcher = Mock()

    with pytest.raises(InboxPromptLoadError, match="INBOX_EVALUATE.md"):
        await monitor.check_once()

    assert monitor._db.mock_calls == []
    assert monitor._invoker.mock_calls == []
    assert monitor._session_manager.mock_calls == []
    assert monitor._autonomous_dispatcher.mock_calls == []
