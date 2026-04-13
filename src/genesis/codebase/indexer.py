"""AST-based codebase indexer — parses Python files and stores structural info in SQLite."""

from __future__ import annotations

import ast
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)


# ─── Dataclasses ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SymbolInfo:
    """A function, class, or method extracted from an AST."""

    name: str
    symbol_type: str  # "function", "async_function", "class", "method", "async_method"
    line_start: int
    line_end: int | None
    signature: str | None
    docstring: str | None
    is_public: bool
    parent_class: str | None = None


@dataclass(frozen=True)
class ImportInfo:
    """An import statement extracted from an AST."""

    target_module: str
    imported_names: str | None  # comma-separated, or None for bare imports
    is_relative: bool


@dataclass(frozen=True)
class ModuleInfo:
    """Parsed structural info for a single Python module."""

    path: str  # relative to repo root
    package: str
    module_name: str
    docstring: str | None
    loc: int
    functions: list[SymbolInfo] = field(default_factory=list)
    classes: list[SymbolInfo] = field(default_factory=list)
    imports: list[ImportInfo] = field(default_factory=list)


@dataclass(frozen=True)
class IndexResult:
    """Summary of an indexing run."""

    modules_indexed: int
    modules_skipped: int
    modules_unchanged: int
    total_symbols: int
    total_imports: int
    errors: list[str] = field(default_factory=list)


# ─── AST Extraction ─────────────────────────────────────────────────────────


def extract_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Build a signature string from an AST function node."""
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    args_parts: list[str] = []

    args = node.args

    # Positional-only args
    for _i, arg in enumerate(args.posonlyargs):
        part = arg.arg
        if arg.annotation:
            part += f": {ast.unparse(arg.annotation)}"
        args_parts.append(part)
    if args.posonlyargs:
        args_parts.append("/")

    # Regular positional args
    num_defaults = len(args.defaults)
    num_args = len(args.args)
    for i, arg in enumerate(args.args):
        part = arg.arg
        if arg.annotation:
            part += f": {ast.unparse(arg.annotation)}"
        default_idx = i - (num_args - num_defaults)
        if default_idx >= 0:
            part += f" = {ast.unparse(args.defaults[default_idx])}"
        args_parts.append(part)

    # *args
    if args.vararg:
        part = f"*{args.vararg.arg}"
        if args.vararg.annotation:
            part += f": {ast.unparse(args.vararg.annotation)}"
        args_parts.append(part)
    elif args.kwonlyargs:
        args_parts.append("*")

    # Keyword-only args
    for i, arg in enumerate(args.kwonlyargs):
        part = arg.arg
        if arg.annotation:
            part += f": {ast.unparse(arg.annotation)}"
        if i < len(args.kw_defaults) and args.kw_defaults[i] is not None:
            part += f" = {ast.unparse(args.kw_defaults[i])}"
        args_parts.append(part)

    # **kwargs
    if args.kwarg:
        part = f"**{args.kwarg.arg}"
        if args.kwarg.annotation:
            part += f": {ast.unparse(args.kwarg.annotation)}"
        args_parts.append(part)

    ret = ""
    if node.returns:
        ret = f" -> {ast.unparse(node.returns)}"

    return f"{prefix} {node.name}({', '.join(args_parts)}){ret}"


def _extract_symbols_and_imports(
    tree: ast.Module,
) -> tuple[list[SymbolInfo], list[SymbolInfo], list[ImportInfo]]:
    """Walk the top-level AST and extract functions, classes (with methods), and imports."""
    functions: list[SymbolInfo] = []
    classes: list[SymbolInfo] = []
    imports: list[ImportInfo] = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sym_type = "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
            functions.append(
                SymbolInfo(
                    name=node.name,
                    symbol_type=sym_type,
                    line_start=node.lineno,
                    line_end=node.end_lineno,
                    signature=extract_signature(node),
                    docstring=ast.get_docstring(node),
                    is_public=not node.name.startswith("_"),
                )
            )

        elif isinstance(node, ast.ClassDef):
            class_doc = ast.get_docstring(node)
            classes.append(
                SymbolInfo(
                    name=node.name,
                    symbol_type="class",
                    line_start=node.lineno,
                    line_end=node.end_lineno,
                    signature=None,
                    docstring=class_doc,
                    is_public=not node.name.startswith("_"),
                )
            )
            # Extract methods
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    m_type = "async_method" if isinstance(child, ast.AsyncFunctionDef) else "method"
                    classes.append(
                        SymbolInfo(
                            name=child.name,
                            symbol_type=m_type,
                            line_start=child.lineno,
                            line_end=child.end_lineno,
                            signature=extract_signature(child),
                            docstring=ast.get_docstring(child),
                            is_public=not child.name.startswith("_"),
                            parent_class=node.name,
                        )
                    )

        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(
                    ImportInfo(
                        target_module=alias.name,
                        imported_names=alias.asname,
                        is_relative=False,
                    )
                )

        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names = ", ".join(alias.name for alias in node.names) if node.names else None
            imports.append(
                ImportInfo(
                    target_module=module,
                    imported_names=names,
                    is_relative=bool(node.level and node.level > 0),
                )
            )

    return functions, classes, imports


def parse_module(path: Path, *, repo_root: Path | None = None) -> ModuleInfo:
    """Parse a single Python file and extract structural information.

    Args:
        path: Absolute path to the .py file.
        repo_root: If given, the stored path is relative to this root.

    Returns:
        ModuleInfo with all extracted symbols and imports.
    """
    rel_path = str(path.relative_to(repo_root)) if repo_root else str(path)
    source = path.read_text(encoding="utf-8", errors="replace")
    loc = source.count("\n") + (1 if source and not source.endswith("\n") else 0)

    # Derive package and module_name from path
    parts = Path(rel_path).with_suffix("").parts
    module_name = parts[-1]
    # Package: drop filename, join with dots (e.g. "src/genesis/memory" -> "genesis.memory")
    pkg_parts = parts[:-1]
    # Strip leading "src" if present
    if pkg_parts and pkg_parts[0] == "src":
        pkg_parts = pkg_parts[1:]
    package = ".".join(pkg_parts) if pkg_parts else ""

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        logger.warning("SyntaxError in %s: %s", rel_path, exc)
        return ModuleInfo(
            path=rel_path,
            package=package,
            module_name=module_name,
            docstring=None,
            loc=loc,
        )

    docstring = ast.get_docstring(tree)
    functions, classes, imports = _extract_symbols_and_imports(tree)

    return ModuleInfo(
        path=rel_path,
        package=package,
        module_name=module_name,
        docstring=docstring,
        loc=loc,
        functions=functions,
        classes=classes,
        imports=imports,
    )


# ─── Database ────────────────────────────────────────────────────────────────


async def ensure_tables(db: aiosqlite.Connection) -> None:
    """Create code index tables and indexes if they don't exist."""
    await db.execute("""
        CREATE TABLE IF NOT EXISTS code_modules (
            path             TEXT PRIMARY KEY,
            package          TEXT NOT NULL,
            module_name      TEXT NOT NULL,
            docstring        TEXT,
            loc              INTEGER NOT NULL,
            num_functions    INTEGER NOT NULL DEFAULT 0,
            num_classes      INTEGER NOT NULL DEFAULT 0,
            file_mtime       REAL NOT NULL,
            last_indexed_at  TEXT NOT NULL
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS code_symbols (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            module_path      TEXT NOT NULL REFERENCES code_modules(path) ON DELETE CASCADE,
            name             TEXT NOT NULL,
            symbol_type      TEXT NOT NULL,
            line_start       INTEGER NOT NULL,
            line_end         INTEGER,
            signature        TEXT,
            docstring        TEXT,
            is_public        INTEGER NOT NULL DEFAULT 1,
            parent_class     TEXT
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS code_imports (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path      TEXT NOT NULL REFERENCES code_modules(path) ON DELETE CASCADE,
            target_module    TEXT NOT NULL,
            imported_names   TEXT,
            is_relative      INTEGER NOT NULL DEFAULT 0
        )
    """)

    # Indexes
    for ddl in [
        "CREATE INDEX IF NOT EXISTS idx_code_symbols_module ON code_symbols(module_path)",
        "CREATE INDEX IF NOT EXISTS idx_code_symbols_name ON code_symbols(name)",
        "CREATE INDEX IF NOT EXISTS idx_code_symbols_type ON code_symbols(symbol_type)",
        "CREATE INDEX IF NOT EXISTS idx_code_imports_source ON code_imports(source_path)",
        "CREATE INDEX IF NOT EXISTS idx_code_imports_target ON code_imports(target_module)",
    ]:
        await db.execute(ddl)

    await db.commit()


async def index_codebase(
    db: aiosqlite.Connection,
    repo_root: Path,
    source_dir: str = "src/genesis",
) -> IndexResult:
    """Walk source_dir for *.py files, parse changed ones, and upsert into SQLite.

    Incremental: only re-parses files whose mtime has changed since last index.
    Removes DB entries for files that no longer exist on disk.
    """
    await ensure_tables(db)

    # Load existing mtimes
    existing: dict[str, float] = {}
    async with db.execute("SELECT path, file_mtime FROM code_modules") as cursor:
        async for row in cursor:
            existing[row[0]] = row[1]

    source_path = repo_root / source_dir
    if not source_path.is_dir():
        logger.warning("Source directory does not exist: %s", source_path)
        return IndexResult(0, 0, 0, 0, 0, errors=[f"Source dir missing: {source_path}"])

    # Collect all .py files
    current_files: dict[str, Path] = {}  # rel_path -> abs_path
    for root, _dirs, files in os.walk(source_path):
        for fname in files:
            if fname.endswith(".py"):
                abs_path = Path(root) / fname
                rel_path = str(abs_path.relative_to(repo_root))
                current_files[rel_path] = abs_path

    modules_indexed = 0
    modules_unchanged = 0
    modules_skipped = 0
    total_symbols = 0
    total_imports = 0
    errors: list[str] = []

    now = datetime.now(UTC).isoformat()

    for rel_path, abs_path in current_files.items():
        mtime = abs_path.stat().st_mtime

        # Compare as int milliseconds to avoid float precision issues across FSes
        if rel_path in existing and int(existing[rel_path] * 1000) == int(mtime * 1000):
            modules_unchanged += 1
            continue

        try:
            info = parse_module(abs_path, repo_root=repo_root)
        except OSError as exc:
            modules_skipped += 1
            errors.append(f"{rel_path}: {exc}")
            continue

        all_symbols = info.functions + info.classes
        num_functions = len(info.functions)
        num_classes = sum(1 for s in info.classes if s.symbol_type == "class")

        # Delete old data for this path
        await db.execute("DELETE FROM code_symbols WHERE module_path = ?", (rel_path,))
        await db.execute("DELETE FROM code_imports WHERE source_path = ?", (rel_path,))
        await db.execute("DELETE FROM code_modules WHERE path = ?", (rel_path,))

        # Insert module
        await db.execute(
            """INSERT INTO code_modules
               (path, package, module_name, docstring, loc, num_functions, num_classes,
                file_mtime, last_indexed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                rel_path,
                info.package,
                info.module_name,
                info.docstring,
                info.loc,
                num_functions,
                num_classes,
                mtime,
                now,
            ),
        )

        # Insert symbols
        for sym in all_symbols:
            await db.execute(
                """INSERT INTO code_symbols
                   (module_path, name, symbol_type, line_start, line_end,
                    signature, docstring, is_public, parent_class)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    rel_path,
                    sym.name,
                    sym.symbol_type,
                    sym.line_start,
                    sym.line_end,
                    sym.signature,
                    sym.docstring,
                    1 if sym.is_public else 0,
                    sym.parent_class,
                ),
            )

        # Insert imports
        for imp in info.imports:
            await db.execute(
                """INSERT INTO code_imports
                   (source_path, target_module, imported_names, is_relative)
                   VALUES (?, ?, ?, ?)""",
                (
                    rel_path,
                    imp.target_module,
                    imp.imported_names,
                    1 if imp.is_relative else 0,
                ),
            )

        modules_indexed += 1
        total_symbols += len(all_symbols)
        total_imports += len(info.imports)

    # Remove entries for deleted files
    deleted_paths = set(existing.keys()) - set(current_files.keys())
    for del_path in deleted_paths:
        await db.execute("DELETE FROM code_symbols WHERE module_path = ?", (del_path,))
        await db.execute("DELETE FROM code_imports WHERE source_path = ?", (del_path,))
        await db.execute("DELETE FROM code_modules WHERE path = ?", (del_path,))

    await db.commit()

    logger.info(
        "Code index: %d indexed, %d unchanged, %d skipped, %d deleted",
        modules_indexed,
        modules_unchanged,
        modules_skipped,
        len(deleted_paths),
    )

    return IndexResult(
        modules_indexed=modules_indexed,
        modules_skipped=modules_skipped,
        modules_unchanged=modules_unchanged,
        total_symbols=total_symbols,
        total_imports=total_imports,
        errors=errors,
    )
