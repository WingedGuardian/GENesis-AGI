"""Tests for the AST codebase indexer."""

from __future__ import annotations

import os
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


def test_parse_module_records_class_bases(tmp_path: Path) -> None:
    """A class signature names its bases and keywords, in a form that parses back."""
    import ast

    src = tmp_path / "shapes.py"
    src.write_text(
        "import typing\n"
        "from abc import ABC, ABCMeta\n"
        "class Plain:\n    pass\n"
        "class Child(Plain):\n    pass\n"
        "class Proto(typing.Protocol[T]):\n    pass\n"
        "class Meta(ABC, metaclass=ABCMeta):\n    pass\n"
    )

    info = parse_module(src, repo_root=tmp_path)
    sigs = {s.name: s.signature for s in info.classes if s.symbol_type == "class"}

    assert sigs == {
        "Plain": "class Plain",
        "Child": "class Child(Plain)",
        "Proto": "class Proto(typing.Protocol[T])",
        "Meta": "class Meta(ABC, metaclass=ABCMeta)",
    }
    for sig in sigs.values():
        assert isinstance(ast.parse(sig + ": ...").body[0], ast.ClassDef)


async def _null_class_rows(db: aiosqlite.Connection) -> int:
    async with db.execute(
        "SELECT COUNT(*) FROM code_symbols WHERE symbol_type = 'class' AND signature IS NULL"
    ) as cur:
        return (await cur.fetchone())[0]


@pytest.mark.asyncio
async def test_index_codebase_heals_class_rows_without_bases(tmp_path: Path) -> None:
    """Class rows from before bases were recorded are re-parsed once, mtime or not."""
    src = tmp_path / "src" / "genesis"
    src.mkdir(parents=True)
    (src / "has_class.py").write_text("class Thing(Base):\n    pass\n")
    (src / "no_class.py").write_text("def f(): pass\n")

    async with aiosqlite.connect(":memory:") as db:
        await index_codebase(db, tmp_path)
        # The state an index written by the old code leaves behind.
        await db.execute("UPDATE code_symbols SET signature = NULL WHERE symbol_type = 'class'")
        await db.commit()
        assert await _null_class_rows(db) == 1

        healed = await index_codebase(db, tmp_path)
        assert healed.modules_indexed == 1  # only the module that had a NULL class row
        assert healed.modules_unchanged == 1
        assert await _null_class_rows(db) == 0
        async with db.execute("SELECT signature FROM code_symbols WHERE name = 'Thing'") as cur:
            assert (await cur.fetchone())[0] == "class Thing(Base)"

        again = await index_codebase(db, tmp_path)
        assert again.modules_indexed == 0  # the heal does not repeat


@pytest.mark.asyncio
async def test_index_codebase_heal_does_not_loop_on_unparseable_module(tmp_path: Path) -> None:
    """A module that no longer parses drops its stale class rows instead of re-healing forever."""
    src = tmp_path / "src" / "genesis"
    src.mkdir(parents=True)
    broken = src / "broken.py"
    broken.write_text("class Ok:\n    pass\n")

    async with aiosqlite.connect(":memory:") as db:
        await index_codebase(db, tmp_path)
        await db.execute("UPDATE code_symbols SET signature = NULL WHERE symbol_type = 'class'")
        await db.commit()
        mtime = broken.stat().st_mtime
        broken.write_text("class Ok(:\n")
        os.utime(broken, (mtime, mtime))  # same mtime: only the heal can re-parse it

        first = await index_codebase(db, tmp_path)
        assert first.modules_indexed == 1
        assert await _null_class_rows(db) == 0

        second = await index_codebase(db, tmp_path)
        assert second.modules_indexed == 0
