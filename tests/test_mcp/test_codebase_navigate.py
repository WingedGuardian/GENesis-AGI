"""Tests for the codebase_navigate health MCP tool: the seams view and its wiring."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import aiosqlite
import pytest

import genesis.mcp.health as health_pkg
from genesis.codebase.indexer import index_codebase
from genesis.mcp.health.codebase import _impl_codebase_navigate, codebase_navigate


@pytest.fixture
async def indexed_service(tmp_path: Path, monkeypatch):
    pkg = tmp_path / "src" / "genesis" / "chan"
    pkg.mkdir(parents=True)
    (pkg / "base.py").write_text(
        "from abc import ABC\n"
        "class ChannelAdapter(ABC):\n"
        '    """Base interface for messaging channel adapters."""\n'
        "    async def send(self, text): ...\n"
    )
    (pkg / "mail.py").write_text(
        "from genesis.chan.base import ChannelAdapter\n"
        "class MailAdapter(ChannelAdapter):\n"
        "    async def send(self, text): return None\n"
    )
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await index_codebase(db, tmp_path)
    svc = MagicMock()
    svc._db = db
    monkeypatch.setattr(health_pkg, "_service", svc)
    yield db
    await db.close()


async def test_seams_view(indexed_service) -> None:
    out = await _impl_codebase_navigate(seams=True)
    assert out["level"] == "seams"
    assert out["complete"] is True
    [seam] = [s for s in out["seams"] if s["seam"] == "ChannelAdapter"]
    assert seam["implementers"] == [
        {
            "name": "MailAdapter",
            "module": "src/genesis/chan/mail.py",
            "match": "nominal",
            "confidence": "high",
        }
    ]


async def test_seam_drill_down(indexed_service) -> None:
    out = await _impl_codebase_navigate(seam="ChannelAdapter")
    assert [s["seam"] for s in out["seams"]] == ["ChannelAdapter"]


async def test_other_levels_unchanged(indexed_service) -> None:
    l0 = await _impl_codebase_navigate()
    assert l0["level"] == "L0"
    l2 = await _impl_codebase_navigate(module="src/genesis/chan/base.py")
    assert l2["level"] == "L2"
    [cls] = [s for s in l2["symbols"] if s["name"] == "ChannelAdapter"]
    assert cls["signature"] == "class ChannelAdapter(ABC)"


async def test_seams_view_without_db(monkeypatch) -> None:
    monkeypatch.setattr(health_pkg, "_service", None)
    out = await _impl_codebase_navigate(seams=True)
    assert "error" in out


def test_tool_exposes_the_seams_parameters() -> None:
    """The schema MCP clients see carries both new parameters, optional."""
    props = codebase_navigate.parameters["properties"]
    assert props["seams"]["default"] is False
    assert props["seam"]["default"] == ""
    assert "seams" not in codebase_navigate.parameters.get("required", [])
    assert "seam" not in codebase_navigate.parameters.get("required", [])
