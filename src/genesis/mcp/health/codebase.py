"""codebase_navigate tool — progressive codebase exploration.

Provides hierarchical navigation of the Genesis codebase using the
AST-indexed code_modules and code_symbols tables:

  L0 (no params): Package index — top-level packages with module counts and LOC.
  L1 (package):   Module list — modules in that package with top symbols.
  L2 (module):    Symbol detail — all public symbols in a specific module.
"""

from __future__ import annotations

import logging

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)


async def _impl_codebase_navigate(package: str = "", module: str = "") -> dict:
    """Navigate the codebase progressively.

    Args:
        package: Top-level package to drill into (e.g. "genesis.awareness").
                 Empty returns L0 package index.
        module:  Full module path (e.g. "genesis/awareness/loop.py").
                 Returns L2 symbol detail for that module.
    """
    import genesis.mcp.health as health_mod

    _service = health_mod._service
    if _service is None or _service._db is None:
        return {"error": "DB not available — code index may not be populated"}

    db = _service._db

    # L2: Symbol detail for a specific module
    if module:
        cursor = await db.execute(
            "SELECT name, symbol_type, line_start, line_end, signature, "
            "parent_class, is_public "
            "FROM code_symbols WHERE module_path = ? "
            "ORDER BY line_start",
            (module,),
        )
        rows = await cursor.fetchall()
        if not rows:
            return {"error": f"No symbols found for module: {module}"}
        symbols = [
            {
                "name": r["name"],
                "type": r["symbol_type"],
                "line_start": r["line_start"],
                "line_end": r["line_end"],
                "signature": r["signature"],
                "parent_class": r["parent_class"],
                "public": bool(r["is_public"]),
            }
            for r in rows
        ]
        return {
            "level": "L2",
            "module": module,
            "symbols": symbols,
            "count": len(symbols),
        }

    # L1: Modules in a package
    if package:
        # Normalize: "genesis.awareness" → "src/genesis/awareness"
        pkg_path = package.replace(".", "/")
        if not pkg_path.startswith("src/"):
            pkg_path = f"src/{pkg_path}"

        cursor = await db.execute(
            "SELECT path, module_name, loc, num_functions, num_classes, docstring "
            "FROM code_modules WHERE path LIKE ? "
            "ORDER BY path",
            (f"{pkg_path}/%",),
        )
        rows = await cursor.fetchall()
        if not rows:
            return {"error": f"No modules found for package: {package}"}

        modules = []
        for r in rows:
            # Get top public symbols for each module
            sym_cursor = await db.execute(
                "SELECT name, symbol_type FROM code_symbols "
                "WHERE module_path = ? AND is_public = 1 "
                "ORDER BY line_start LIMIT 8",
                (r["path"],),
            )
            top_symbols = [
                f"{s['name']} ({s['symbol_type']})"
                for s in await sym_cursor.fetchall()
            ]
            modules.append({
                "path": r["path"],
                "name": r["module_name"],
                "loc": r["loc"],
                "functions": r["num_functions"],
                "classes": r["num_classes"],
                "docstring": (r["docstring"] or "")[:120],
                "top_symbols": top_symbols,
            })
        return {
            "level": "L1",
            "package": package,
            "modules": modules,
            "count": len(modules),
        }

    # L0: Package index
    cursor = await db.execute(
        "SELECT package, COUNT(*) as module_count, SUM(loc) as total_loc, "
        "SUM(num_functions) as total_functions, SUM(num_classes) as total_classes "
        "FROM code_modules "
        "GROUP BY package ORDER BY total_loc DESC",
    )
    rows = await cursor.fetchall()
    if not rows:
        return {"error": "Code index is empty — run the indexer first"}

    packages = [
        {
            "package": r["package"],
            "modules": r["module_count"],
            "loc": r["total_loc"],
            "functions": r["total_functions"],
            "classes": r["total_classes"],
        }
        for r in rows
    ]
    return {
        "level": "L0",
        "packages": packages,
        "total_modules": sum(p["modules"] for p in packages),
        "total_loc": sum(p["loc"] for p in packages),
    }


@mcp.tool()
async def codebase_navigate(package: str = "", module: str = "") -> dict:
    """Navigate the Genesis codebase progressively.

    Three levels of detail:
      - No params → L0: Package index (all packages with module counts, LOC)
      - package="genesis.awareness" → L1: Modules in that package with top symbols
      - module="src/genesis/awareness/loop.py" → L2: All symbols in that module

    Use L0 to orient, L1 to explore a package, L2 to find specific symbols.
    """
    return await _impl_codebase_navigate(package=package, module=module)
