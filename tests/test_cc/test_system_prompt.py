"""Tests for SystemPromptAssembler."""

import pytest

from genesis.cc.system_prompt import SystemPromptAssembler


@pytest.fixture
def assembler(tmp_path):
    (tmp_path / "SOUL.md").write_text("You are Genesis.")
    (tmp_path / "USER.md").write_text("User prefers concise answers.")
    (tmp_path / "CONVERSATION.md").write_text("Respond naturally.")
    return SystemPromptAssembler(identity_dir=tmp_path)


@pytest.fixture
def assembler_no_user(tmp_path):
    (tmp_path / "SOUL.md").write_text("You are Genesis.")
    (tmp_path / "CONVERSATION.md").write_text("Respond naturally.")
    return SystemPromptAssembler(identity_dir=tmp_path)


def test_assemble_sync_parts(assembler):
    result = assembler.assemble_static()
    assert "You are Genesis." in result
    assert "User prefers concise answers." in result
    assert "Respond naturally." in result


def test_assemble_without_user_profile(assembler_no_user):
    result = assembler_no_user.assemble_static()
    assert "You are Genesis." in result
    assert "Respond naturally." in result


@pytest.mark.asyncio
async def test_assemble_includes_cognitive_state(assembler, db):
    from genesis.db.crud import cognitive_state

    await cognitive_state.replace_section(
        db,
        section="active_context",
        id="cs-1",
        content="Working on vehicle registration.",
        generated_by="test",
        created_at="2026-03-08T12:00:00",
    )
    result = await assembler.assemble(db=db)
    assert "Working on vehicle registration." in result


@pytest.mark.asyncio
async def test_assemble_with_empty_cognitive_state(assembler, db):
    result = await assembler.assemble(db=db)
    assert "You are Genesis." in result
    assert len(result) > 50


def test_assemble_includes_date(assembler):
    assert "Date:" in assembler.assemble_static()


def test_sections_are_separated(assembler):
    assert "---" in assembler.assemble_static()
