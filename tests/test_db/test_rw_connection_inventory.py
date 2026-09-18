"""Lock the canonical SQLite RW chokepoint inventory against new bypasses."""

from __future__ import annotations

import ast
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2] / "src" / "genesis"
_IMPLEMENTATIONS = {
    "db/connection.py",
    "db/integrity.py",
}
_UNRELATED_OR_SNAPSHOT = {
    "browser/profile.py",
    "eval/bench/isolation.py",
}
_CANONICAL_READERS_USING_WRITABLE_MODE = {
    "attention/calibrate.py:load_labeled",
    "attention/differ.py:load_from_db",
    "dashboard/routes/updates.py:_query_db",
    "db/crud/session_heartbeats.py:count_active_sync",
    "db/crud/session_heartbeats.py:get_active_sync",
    "eval/cli.py:_cmd_compare",
    "eval/cli.py:_cmd_export",
    "eval/cli.py:_cmd_results",
    "eval/reflection_golden_set.py:generate_golden_set",
    "guardian/watchdog.py:_last_deployed_commit",
    "inbox/writer.py:_db_floor",
    "learning/procedural/session_inject.py:load_active_procedures",
    "mcp/health/manifest.py:_impl_job_health",
    "mcp/health/update_history.py:_impl_update_history_recent",
}


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
    return "<module>"


def test_canonical_rw_opens_use_guarded_factories():
    unexpected: list[str] = []
    for path in _ROOT.rglob("*.py"):
        relative = str(path.relative_to(_ROOT))
        if relative in _IMPLEMENTATIONS or relative in _UNRELATED_OR_SNAPSHOT:
            continue
        source = path.read_text()
        tree = ast.parse(source)
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            if (
                node.func.attr != "connect"
                or not isinstance(owner, ast.Name)
                or owner.id not in {"sqlite3", "aiosqlite"}
            ):
                continue
            source_segment = ast.get_source_segment(source, node) or ""
            if "mode=ro" in source_segment:
                continue
            key = f"{relative}:{_enclosing_function(node, parents)}"
            if key not in _CANONICAL_READERS_USING_WRITABLE_MODE:
                unexpected.append(f"{key}:{node.lineno}")

    assert unexpected == [], (
        "canonical RW SQLite opens must use connect_sqlite_rw/"
        f"connect_aiosqlite_rw; classify explicit readers instead: {unexpected}"
    )


def test_update_path_inline_writers_enforce_quarantine():
    repo = _ROOT.parents[1]
    update = (repo / "scripts" / "update.sh").read_text()
    bootstrap = (repo / "scripts" / "bootstrap.sh").read_text()
    assert "connect_sqlite_rw(os.environ[\"GH_DB_PATH\"]" in update
    assert "assert_not_quarantined('$DB_PATH')" in bootstrap
