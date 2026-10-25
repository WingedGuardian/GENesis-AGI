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
    # The card vault's own cards.db (0600, in the 0700 vault dir), never genesis.db.
    "payments/vault_store.py",
}
_ISOLATED_RECOVERY_WRITERS = {
    # Stdlib-only, existing staged file; restore/update publish only on success.
    # This is not a canonical reader or a whole-module exemption.
    "db/crud/peer_restore.py:reset_peer_authority",
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


def _first_arg_is_ro_uri_call(node: ast.Call) -> bool:
    """Whether the connect call's FIRST argument is itself a call to ``ro_uri``.

    ``integrity.ro_uri(...)`` always yields a ``?mode=ro`` URI (locked by
    tests/test_db/test_readonly_query.py::test_ro_uri_is_read_only). Only the
    direct form is trusted: ``ro_uri`` merely appearing somewhere in the call's
    source (a keyword argument, a comment, an unrelated name) is not.
    """
    if not node.args or not isinstance(node.args[0], ast.Call):
        return False
    func = node.args[0].func
    return (isinstance(func, ast.Name) and func.id == "ro_uri") or (
        isinstance(func, ast.Attribute) and func.attr == "ro_uri"
    )


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
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
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
            if "mode=ro" in source_segment or _first_arg_is_ro_uri_call(node):
                continue
            # A private in-memory database is never the canonical store.
            if (
                node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == ":memory:"
            ):
                continue
            key = f"{relative}:{_enclosing_function(node, parents)}"
            if key not in _CANONICAL_READERS_USING_WRITABLE_MODE | _ISOLATED_RECOVERY_WRITERS:
                unexpected.append(f"{key}:{node.lineno}")

    assert unexpected == [], (
        "canonical RW SQLite opens must use connect_sqlite_rw/"
        f"connect_aiosqlite_rw; classify explicit readers instead: {unexpected}"
    )


def test_update_path_inline_writers_enforce_quarantine():
    repo = _ROOT.parents[1]
    update = (repo / "scripts" / "update.sh").read_text()
    bootstrap = (repo / "scripts" / "bootstrap.sh").read_text()
    assert 'connect_sqlite_rw(os.environ["GH_DB_PATH"]' in update
    # bootstrap's crash-recovery insert runs under the system python3, before the
    # venv exists, so it cannot import connect_sqlite_rw (aiosqlite); it makes the
    # same admission check (assert_admitted delegates to it) before opening.
    start = bootstrap.index("INSERT INTO update_history")
    block = bootstrap[bootstrap.rindex("<<'PY'", 0, start) : bootstrap.index("\nPY\n", start)]
    assert "from genesis.db.integrity import assert_not_quarantined" in block
    check = block.index("assert_not_quarantined(db_path)")
    assert check < block.index("sqlite3.connect(db_path")


def test_ro_uri_exemption_is_structural_not_textual():
    def call(src: str) -> ast.Call:
        return ast.parse(src).body[0].value

    assert _first_arg_is_ro_uri_call(call("sqlite3.connect(ro_uri(p), uri=True)"))
    assert _first_arg_is_ro_uri_call(call("sqlite3.connect(integrity.ro_uri(p), uri=True)"))
    assert not _first_arg_is_ro_uri_call(call("sqlite3.connect(p, x=ro_uri(q))"))
    assert not _first_arg_is_ro_uri_call(call("sqlite3.connect(p)  # ro_uri(p)"))
    assert not _first_arg_is_ro_uri_call(call("sqlite3.connect(not_ro_uri(p))"))
    assert not _first_arg_is_ro_uri_call(call("sqlite3.connect(ro_uri_path)"))
