"""Lock the canonical SQLite RW chokepoint inventory against new bypasses."""

from __future__ import annotations

import ast
import re
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
            if "mode=ro" in source_segment:
                continue
            key = f"{relative}:{_enclosing_function(node, parents)}"
            if key not in _CANONICAL_READERS_USING_WRITABLE_MODE:
                unexpected.append(f"{key}:{node.lineno}")

    assert unexpected == [], (
        "canonical RW SQLite opens must use connect_sqlite_rw/"
        f"connect_aiosqlite_rw; classify explicit readers instead: {unexpected}"
    )


#: The two shapes the deploy scripts use to run inline Python. Scoping to one
#: program matters: a guard in a DIFFERENT program must not vouch for an open in
#: this one, and a whole-file substring search cannot tell them apart.
#: `update.sh` uses
#: a heredoc, quoted or not; `bootstrap.sh` uses `python3 -c "..."`. The
#: delimiter line carries redirections (`<<'PYEOF' 2>&1`) and the terminator can
#: be indented -- both found by the coverage control below rather than by
#: reading it, which is why that control exists.
_HEREDOC_RE = re.compile(r"<<'?(\w+)'?[^\n]*\n(.*?)\n[ \t]*\1", re.S)
_DASH_C_RE = re.compile(r"python3? -c \"(.*?)\n\"", re.S)
#: A writable open. `mode=ro` opens are readers and out of scope here; the
#: readers-in-writable-mode question has its own inventory above.
_SQLITE_OPEN_RE = re.compile(r"sqlite3\.connect\(")
_WRITE_SQL_RE = re.compile(r"\b(INSERT|UPDATE|DELETE|CREATE|REPLACE)\b")
#: Either spelling of the admission assertion. `connect_sqlite_rw` performs it
#: internally; `assert_admitted` / `assert_not_quarantined` are the same check
#: called directly, which is what a stdlib-only interpreter has to do.
_ADMISSION_RE = re.compile(r"connect_sqlite_rw\(|assert_admitted\(|assert_not_quarantined\(")


def _inline_programs(script: str) -> list[tuple[str, str]]:
    """`(label, body)` for every inline Python program in a shell script."""
    return [(f"<<{delim}", body) for delim, body in _HEREDOC_RE.findall(script)] + [
        ("-c", body) for body in _DASH_C_RE.findall(script)
    ]


def test_update_path_inline_writers_enforce_quarantine():
    """Every inline WRITER in the deploy scripts asserts admission first.

    Asserted by BEHAVIOUR and by ORDER, not by helper name. `update.sh`
    deliberately does not import `genesis.db.connection` for its history write:
    it picks its interpreter from a fallback chain that can land on a non-venv
    `python3`, so a half-rebuilt venv cannot silently lose the write, and that
    module imports `aiosqlite` at module scope. The `genesis.db.admission` ->
    `genesis.db.integrity` chain is stdlib-only, so the writer inlines exactly
    what `connect_sqlite_rw` does: expanduser, assert, connect to the resolved
    path.

    The previous form of this test matched the literal string
    `connect_sqlite_rw(os.environ["GH_DB_PATH"]`, which made that semantically
    identical inline read as a regression -- and would equally have passed an
    inline that asserted admission AFTER opening the database, or not at all.
    """
    repo = _ROOT.parents[1]
    for name in ("update.sh", "bootstrap.sh"):
        script = (repo / "scripts" / name).read_text()
        programs = _inline_programs(script)

        # COVERAGE CONTROL. A block this extractor cannot see is a block it
        # silently does not check, and shell quoting has already defeated it
        # twice. So the opens found INSIDE extracted programs must account for
        # every open in the file: a new invocation shape fails loudly here
        # rather than quietly reducing what this test covers.
        in_file = len(_SQLITE_OPEN_RE.findall(script))
        in_programs = sum(len(_SQLITE_OPEN_RE.findall(body)) for _, body in programs)
        assert in_programs == in_file, (
            f"{name}: extraction reached {in_programs} of {in_file} database opens "
            f"-- an unextracted inline program is an UNCHECKED one"
        )

        blocks = [
            (label, body)
            for label, body in programs
            if _SQLITE_OPEN_RE.search(body) and _WRITE_SQL_RE.search(body)
        ]
        assert blocks, f"{name}: no inline writer found -- the fixture would be vacuous"
        for label, body in blocks:
            opened = _SQLITE_OPEN_RE.search(body)
            guard = _ADMISSION_RE.search(body)
            assert guard, (
                f"{name} {label}: a writable open with no admission assertion; "
                f"call connect_sqlite_rw, or assert_admitted before connecting"
            )
            assert guard.start() < opened.start(), (
                f"{name} {label}: admission is asserted AFTER the database is "
                f"opened, which checks nothing -- the open is the thing to guard"
            )
