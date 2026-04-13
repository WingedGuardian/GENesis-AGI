"""Tests for the AST codebase indexer."""

from __future__ import annotations

import time
from pathlib import Path

import aiosqlite
import pytest

from genesis.codebase.indexer import index_codebase, parse_module


def test_parse_module_basic(tmp_path: Path) -> None:
    """Parse a file with a function and a class, verify ModuleInfo."""
    src = tmp_path / "example.py"
    src.write_text(
        '"""Module docstring."""\n'
        "\n"
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        "\n"
        "def greet(name: str) -> str:\n"
        '    """Say hello."""\n'
        "    return f'Hello, {name}'\n"
        "\n"
        "\n"
        "class Widget:\n"
        '    """A widget."""\n'
        "\n"
        "    def tick(self) -> None:\n"
        "        pass\n"
    )

    info = parse_module(src, repo_root=tmp_path)

    assert info.module_name == "example"
    assert info.docstring == "Module docstring."
    assert info.loc > 0

    # One top-level function
    assert len(info.functions) == 1
    assert info.functions[0].name == "greet"
    assert info.functions[0].symbol_type == "function"
    assert "name: str" in (info.functions[0].signature or "")
    assert "-> str" in (info.functions[0].signature or "")

    # Class + method
    class_symbols = [s for s in info.classes if s.symbol_type == "class"]
    method_symbols = [s for s in info.classes if s.symbol_type == "method"]
    assert len(class_symbols) == 1
    assert class_symbols[0].name == "Widget"
    assert len(method_symbols) == 1
    assert method_symbols[0].name == "tick"
    assert method_symbols[0].parent_class == "Widget"

    # Imports
    assert len(info.imports) == 2
    os_imp = next(i for i in info.imports if i.target_module == "os")
    assert not os_imp.is_relative
    path_imp = next(i for i in info.imports if i.target_module == "pathlib")
    assert path_imp.imported_names == "Path"


def test_parse_module_syntax_error(tmp_path: Path) -> None:
    """Gracefully handle a file with invalid Python syntax."""
    bad = tmp_path / "broken.py"
    bad.write_text("def oops(\n")

    info = parse_module(bad, repo_root=tmp_path)

    assert info.module_name == "broken"
    assert info.loc > 0
    # No symbols extracted from a broken file
    assert info.functions == []
    assert info.classes == []


@pytest.mark.asyncio
async def test_index_codebase_incremental(tmp_path: Path) -> None:
    """Index two files, modify one, re-index — only the modified file is re-parsed."""
    src = tmp_path / "src" / "genesis"
    src.mkdir(parents=True)

    (src / "__init__.py").write_text("")
    (src / "alpha.py").write_text("def a(): pass\n")
    (src / "beta.py").write_text("def b(): pass\n")

    async with aiosqlite.connect(":memory:") as db:
        await db.execute("PRAGMA foreign_keys = ON")
        r1 = await index_codebase(db, tmp_path)

        assert r1.modules_indexed == 3  # __init__ + alpha + beta
        assert r1.modules_unchanged == 0

        # Verify symbols in DB
        async with db.execute("SELECT COUNT(*) FROM code_symbols") as cur:
            count = (await cur.fetchone())[0]
        assert count >= 2  # at least a() and b()

        # Re-index without changes
        r2 = await index_codebase(db, tmp_path)
        assert r2.modules_indexed == 0
        assert r2.modules_unchanged == 3

        # Modify one file (must change mtime)
        time.sleep(0.05)
        (src / "alpha.py").write_text("def a_v2(): pass\ndef a_v3(): pass\n")

        r3 = await index_codebase(db, tmp_path)
        assert r3.modules_indexed == 1
        assert r3.modules_unchanged == 2

        # Verify updated symbols
        async with db.execute(
            "SELECT name FROM code_symbols WHERE module_path LIKE '%alpha.py'"
        ) as cur:
            names = {row[0] for row in await cur.fetchall()}
        assert names == {"a_v2", "a_v3"}


@pytest.mark.asyncio
async def test_index_codebase_deletes_removed(tmp_path: Path) -> None:
    """After a file is deleted from disk, its DB entries are cleaned up."""
    src = tmp_path / "src" / "genesis"
    src.mkdir(parents=True)

    gone = src / "gone.py"
    gone.write_text("def vanish(): pass\n")

    async with aiosqlite.connect(":memory:") as db:
        await db.execute("PRAGMA foreign_keys = ON")
        r1 = await index_codebase(db, tmp_path)
        assert r1.modules_indexed == 1

        async with db.execute("SELECT COUNT(*) FROM code_modules") as cur:
            assert (await cur.fetchone())[0] == 1

        # Delete the file and re-index
        gone.unlink()

        r2 = await index_codebase(db, tmp_path)
        assert r2.modules_indexed == 0
        assert r2.modules_unchanged == 0

        async with db.execute("SELECT COUNT(*) FROM code_modules") as cur:
            assert (await cur.fetchone())[0] == 0
        async with db.execute("SELECT COUNT(*) FROM code_symbols") as cur:
            assert (await cur.fetchone())[0] == 0
