"""Lock every OPEN-TIME connect site in connection.py to the admission seam.

Why this exists, stated plainly: the admission fence was first wired by
enumerating the connection factories BY HAND, and the hand-enumeration missed
one. ``get_raw_db`` — the factory ``zero_drop_worker`` opens through — was not
in the list, and a syscall-level replay caught it writing to a fenced database
while the three "known" factories were all correctly fenced.

So the enumeration is done here, mechanically, by AST, and it is the test that
fails when someone adds the seventh factory. A reviewer reading a diff cannot
reliably notice an unguarded ``aiosqlite.connect`` three hundred lines from the
guarded ones; this can.

Scope note — the SerializedConnection PER-CALL re-asserts
(``_retry_locked``, ``executescript``, ``cursor``) deliberately remain
quarantine-only and are asserted to stay that way below. They run on every
database operation on the server's shared connection, and promoting them to
the full fence belongs with the runtime corruption trip (which gives a fenced
server somewhere to go) rather than being smuggled in here.
"""

from __future__ import annotations

import ast
from pathlib import Path

_CONNECTION_PY = Path(__file__).resolve().parents[2] / "src" / "genesis" / "db" / "connection.py"

#: Functions holding an open-time ``sqlite3``/``aiosqlite`` connect. Every one
#: MUST assert admission. Adding a factory means adding it here deliberately,
#: which is the point — the failure is loud and names the function.
_EXPECTED_OPEN_TIME_SITES = {
    "connect_sqlite_rw",
    "_GuardedAiosqliteConnector._open",
    "get_db",
    "get_db._reconnect",
    "get_raw_db",
    "open_ro_connection",
}

#: Per-call re-assert sites, quarantine-only BY DESIGN (see module docstring).
_EXPECTED_PER_CALL_QUARANTINE_SITES = {
    "SerializedConnection._retry_locked",
    "SerializedConnection.executescript._locked",
    "SerializedConnection.cursor",
}

_ADMISSION_ASSERT = "assert_admitted"
_QUARANTINE_ASSERT = "assert_not_quarantined"


def _parse() -> tuple[ast.Module, dict[ast.AST, ast.AST]]:
    tree = ast.parse(_CONNECTION_PY.read_text())
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    return tree, parents


def _qualname(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    """Dotted name of the enclosing def/class chain, e.g. ``get_db._reconnect``."""
    current, names = node, []
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(current.name)
    return ".".join(reversed(names)) or "<module>"


def _connect_sites(tree: ast.Module, parents: dict[ast.AST, ast.AST]) -> dict[str, list[int]]:
    """Enclosing function -> line numbers of its sqlite3/aiosqlite connects."""
    sites: dict[str, list[int]] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "connect"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in {"sqlite3", "aiosqlite"}
        ):
            sites.setdefault(_qualname(node, parents), []).append(node.lineno)
    return sites


def _assert_calls(tree: ast.Module, parents: dict[ast.AST, ast.AST], name: str) -> set[str]:
    """Enclosing functions that call the named assertion helper."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name:
            found.add(_qualname(node, parents))
    return found


def test_open_time_connect_sites_are_exactly_the_expected_set():
    """A NEW connect site in connection.py fails here until it is classified."""
    tree, parents = _parse()
    actual = set(_connect_sites(tree, parents))
    assert actual == _EXPECTED_OPEN_TIME_SITES, (
        "connection.py's connect sites changed. Every open-time connect must "
        "assert admission (genesis.db.admission.assert_admitted) and be listed "
        "in _EXPECTED_OPEN_TIME_SITES. "
        f"unexpected={sorted(actual - _EXPECTED_OPEN_TIME_SITES)} "
        f"missing={sorted(_EXPECTED_OPEN_TIME_SITES - actual)}"
    )


def test_every_open_time_site_asserts_admission():
    """The fence must be present in the SAME function as the connect.

    Presence, not ordering — an AST check cannot prove the assertion dominates
    the connect on every path. The behavioural proof that it actually refuses
    is the incident-replay suite; this guarantees the call exists to be run.
    """
    tree, parents = _parse()
    admitted = _assert_calls(tree, parents, _ADMISSION_ASSERT)
    unguarded = sorted(_EXPECTED_OPEN_TIME_SITES - admitted)
    assert not unguarded, (
        f"open-time connect sites with no {_ADMISSION_ASSERT}() call: {unguarded}. "
        "A read-only or short-lived open is not an exemption: it still holds a "
        "descriptor on a database an operator may be replacing."
    )


def test_open_time_sites_do_not_use_the_quarantine_only_assert():
    """No open-time factory may call the quarantine assert directly.

    ``assert_admitted`` currently delegates to ``assert_not_quarantined``
    unchanged, so today the two are equivalent in effect — this is NOT a claim
    that the narrower one misses something right now. The rule exists so that
    every open-time factory goes through ONE named seam: when admission grows
    a second condition, it lands in a single function instead of needing six
    call sites to be found again. A factory reverted to the narrower assert
    would silently opt out of that.
    """
    tree, parents = _parse()
    quarantine_only = _assert_calls(tree, parents, _QUARANTINE_ASSERT)
    regressed = sorted(_EXPECTED_OPEN_TIME_SITES & quarantine_only)
    assert not regressed, (
        f"open-time sites using {_QUARANTINE_ASSERT} instead of "
        f"{_ADMISSION_ASSERT}: {regressed}. Route them through the single named "
        "seam so a later admission condition lands in one place."
    )


def test_per_call_reassert_sites_are_unchanged():
    """Pin the deliberate quarantine-only set so a change is a decision.

    If a later PR promotes these to the full fence (the runtime-corruption-trip
    work), this test is the thing that makes that an explicit edit rather than
    an accident — in either direction.
    """
    tree, parents = _parse()
    quarantine_only = _assert_calls(tree, parents, _QUARANTINE_ASSERT)
    assert quarantine_only == _EXPECTED_PER_CALL_QUARANTINE_SITES, (
        "the quarantine-only assertion set changed. These are the "
        "SerializedConnection per-call re-asserts, intentionally left on the "
        "direct quarantine assert rather than routed through the seam. "
        f"unexpected={sorted(quarantine_only - _EXPECTED_PER_CALL_QUARANTINE_SITES)} "
        f"missing={sorted(_EXPECTED_PER_CALL_QUARANTINE_SITES - quarantine_only)}"
    )
