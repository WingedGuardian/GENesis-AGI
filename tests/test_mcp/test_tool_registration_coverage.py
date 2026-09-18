"""Guardrail: an ``@mcp.tool()`` that nothing imports is a tool that does not exist.

``genesis.mcp.health`` registers its tools as an IMPORT SIDE EFFECT — the
``@mcp.tool()`` decorator runs when the defining module is imported, and
``__init__.py`` importing each sibling is the only thing that makes that happen
in the MCP server process. A module nobody imports therefore ships tools that
are absent from the live registry, and nothing anywhere fails: the file exists,
its tests pass, the decorator is right there in the source.

MEASURED 2026-09-08, which is why this guard exists: ``user_job_tools`` was
missing from ``__init__.py``, so **4 of 83 tools across 37 modules** were dead —
``user_job_create``, ``user_job_list``, ``user_job_control``, ``user_job_history``
— confirmed absent from a live session's genesis-health tool list. It had a
runtime caller (``runtime/init/user_jobs.py`` imports it to inject db/scheduler),
which is what made the omission survive: the module IS imported in the SERVER
process, just never in the MCP process that serves the tools.

The trap generalises to any registry populated by decorator side effects. The
fix is mechanical enforcement rather than a hand-maintained list, because the
failure is silent in every direction a human would look.
"""
from __future__ import annotations

import ast
from pathlib import Path

import genesis

_HEALTH_PKG = Path(genesis.__file__).parent / "mcp" / "health"


def _imported_submodules(init_path: Path) -> set[str]:
    """Sibling module names that ``__init__.py`` imports, in any import form."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(init_path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module == "genesis.mcp.health":
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("genesis.mcp.health."):
                    names.add(a.name.rsplit(".", 1)[-1])
    return names


def _declared_tools(path: Path) -> list[str]:
    """Functions decorated with ``@mcp.tool()`` (or a bare ``@tool``) in a module."""
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if (getattr(target, "attr", None) or getattr(target, "id", None)) == "tool":
                found.append(node.name)
                break
    return found


def test_every_module_declaring_mcp_tools_is_imported():
    """No module may declare tools without ``__init__.py`` importing it."""
    imported = _imported_submodules(_HEALTH_PKG / "__init__.py")

    orphaned: dict[str, list[str]] = {}
    for path in sorted(_HEALTH_PKG.glob("*.py")):
        if path.name == "__init__.py":
            continue
        tools = _declared_tools(path)
        if tools and path.stem not in imported:
            orphaned[path.stem] = tools

    assert not orphaned, (
        "These modules declare @mcp.tool() functions but are never imported by "
        "genesis/mcp/health/__init__.py, so their tools are ABSENT from the live "
        "MCP registry while looking perfectly registered in source:\n"
        + "\n".join(f"  {mod}: {', '.join(names)}" for mod, names in sorted(orphaned.items()))
        + "\nAdd the import to __init__.py (the decorator runs on import; nothing else registers it)."
    )


def test_the_scan_actually_finds_tools():
    """Guard the guard: a scanner that matches nothing passes vacuously.

    Without this, deleting the decorator-matching branch above would leave
    ``orphaned`` permanently empty and the real test permanently green.
    """
    total = sum(len(_declared_tools(p)) for p in _HEALTH_PKG.glob("*.py") if p.name != "__init__.py")
    assert total > 50, f"expected the health package to declare many tools, scanned {total}"


def test_user_job_tools_specifically_is_registered():
    """The acceptance bar: the exact defect this guard was built from."""
    imported = _imported_submodules(_HEALTH_PKG / "__init__.py")
    assert "user_job_tools" in imported
    assert _declared_tools(_HEALTH_PKG / "user_job_tools.py")
