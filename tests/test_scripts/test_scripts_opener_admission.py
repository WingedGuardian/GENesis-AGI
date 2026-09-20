"""Admission fencing for script-side raw SQLite openers (PR-0).

Two layers, matching ``tests/test_db/test_rw_connection_inventory.py`` for the
``src/`` tree:

* an AST inventory gate over ``scripts/`` — every production ``sqlite3.connect``
  / ``aiosqlite.connect`` site must sit in a scope that consults the admission
  fence (``database_is_fenced`` or a module wrapper of it), so an opener wired
  next year without the check fails by construction (allowlist polarity).
  Recognized spellings: attribute calls on the literal module names
  ``sqlite3``/``aiosqlite`` — plus, hardened outright, aliased imports
  (``import sqlite3 as s``) and from-imports of ``connect``, which are flagged
  as violations because they have no legitimate use in ``scripts/`` and would
  otherwise evade the matcher. A rebound module object or getattr call is NOT
  detected — treat the matcher as the spellings checked so far, not a closed
  set. The gate also checks PRESENCE of a fence call in an enclosing scope,
  not ordering or that the name resolves to the real fence — the behavior
  replay below is the compensating control for the write path;
* behavior tests driving the REAL hook entry points against a fenced scratch
  database — the 2026-09-18 incident replay: hook writers used to write to a
  QUARANTINED database, because nothing script-side consulted the marker.

Fixtures use ``GENESIS_HOME``/``GENESIS_DB_PATH`` env overrides (both resolved
at call time by ``genesis.env``), so nothing touches a live install.
"""

from __future__ import annotations

import ast
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO / "scripts"
_SRC = _REPO / "src"

# Spellings that count as consulting the fence. The module wrappers all funnel
# into genesis.db.admission.database_is_fenced — one implementation.
_FENCE_CALL_NAMES = {"database_is_fenced", "_db_is_fenced", "_db_fenced"}

# Openers allowed WITHOUT a fence check, each with the reason stated.
# Path is relative to scripts/. PR-0 fences the AUTOMATIC entry points (hooks,
# session-boundary workers, timer-driven maintenance) — the class the
# 2026-09-18 incident's bypass writers belong to. OPERATOR-RUN one-off CLIs
# are deliberately deferred to the raw-opener sweep PR (lease semantics,
# per-tool judgment: some legitimately examine a quarantined database). A NEW
# script added to scripts/ still fails this gate by default and must either
# take the fence or be consciously classified here in review.
_OPERATOR_ONE_OFF = (
    "operator-run one-off (backfill/migration/cleanup/report) — not an "
    "automatic entry point; the raw-opener sweep PR upgrades these to lease "
    "semantics with per-tool judgment"
)
_ALLOWLIST: dict[str, str] = {
    "dev/mw2_classifier_probe.py": _OPERATOR_ONE_OFF,
    "ambient_replay.py": _OPERATOR_ONE_OFF,
    "apply_entity_seed.py": _OPERATOR_ONE_OFF,
    "backfill_memory_taxonomy.py": _OPERATOR_ONE_OFF,
    "backfill_observation_embeddings.py": _OPERATOR_ONE_OFF,
    "backfill_observation_ttls.py": _OPERATOR_ONE_OFF,
    "backfill_origin_class_qdrant.py": _OPERATOR_ONE_OFF,
    "backfill_procedure_embeddings.py": _OPERATOR_ONE_OFF,
    "backfill_qdrant_metadata.py": _OPERATOR_ONE_OFF,
    "backfill_session_charters.py": _OPERATOR_ONE_OFF,
    "backfill_session_memories.py": _OPERATOR_ONE_OFF,
    "backfill_source_subsystem.py": _OPERATOR_ONE_OFF,
    "cleanup_fenced_knowledge_units.py": _OPERATOR_ONE_OFF,
    "cleanup_pipeline_noise.py": _OPERATOR_ONE_OFF,
    "cleanup_subsystem_qdrant.py": _OPERATOR_ONE_OFF,
    "dedup_memory_stores.py": _OPERATOR_ONE_OFF,
    "entity_backfill.py": _OPERATOR_ONE_OFF,
    "entity_cleanup_fake_commits.py": _OPERATOR_ONE_OFF,
    "gen_memory_quality_chart.py": _OPERATOR_ONE_OFF,
    "ledger_shadow_report.py": _OPERATOR_ONE_OFF,
    "lib/index_marker.py": (
        "opens its OWN code-intel queue database under marker_dir(), never "
        "genesis.db — outside the admission domain"
    ),
    "migrate_faiss_to_qdrant.py": _OPERATOR_ONE_OFF,
    "migrate_knowledge_to_episodic.py": _OPERATOR_ONE_OFF,
    "migrate_qdrant_collections.py": _OPERATOR_ONE_OFF,
    "migrate_reference_data.py": _OPERATOR_ONE_OFF,
    "mine_references_from_history.py": _OPERATOR_ONE_OFF,
    "regen_essential_knowledge.py": _OPERATOR_ONE_OFF,
    "reindex_fts_to_qdrant.py": _OPERATOR_ONE_OFF,
    "retrieval_efficacy_report.py": _OPERATOR_ONE_OFF,
    "run_memory_integrity_check.py": (
        "diagnostic tool — examining a suspect (possibly quarantined) "
        "database is its purpose; fencing it would blind the diagnosis"
    ),
    "seed_procedures.py": _OPERATOR_ONE_OFF,
    "wing_backfill.py": _OPERATOR_ONE_OFF,
    "wing_payload_resync.py": _OPERATOR_ONE_OFF,
}


def _connect_calls(tree: ast.AST):
    """Yield (node, ancestor_functions) for sqlite3/aiosqlite connect calls."""
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "connect"
            and isinstance(func.value, ast.Name)
            and func.value.id in {"sqlite3", "aiosqlite"}
        ):
            continue
        chain = []
        cursor = node
        while cursor in parents:
            cursor = parents[cursor]
            if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef)):
                chain.append(cursor)
        yield node, chain


def _scope_consults_fence(chain) -> bool:
    """True when any enclosing function's body contains a fence-check call."""
    for func in chain:
        for node in ast.walk(func):
            if isinstance(node, ast.Call):
                name = None
                if isinstance(node.func, ast.Name):
                    name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    name = node.func.attr
                if name in _FENCE_CALL_NAMES:
                    return True
    return False


def test_every_scripts_opener_consults_the_admission_fence():
    """ALLOWLIST polarity: a new raw opener without the fence check FAILS.

    The 2026-09-18 corruption incident's recurrence mechanism was exactly a
    script-side opener the src/-rooted inventory gate could not see. This gate
    roots at scripts/ and covers sqlite3 AND aiosqlite.
    """
    violations = []
    seen_allowlisted = set()
    for py in sorted(_SCRIPTS.rglob("*.py")):
        rel = str(py.relative_to(_SCRIPTS))
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # a broken script must be loud here too
            violations.append(f"{rel}: unparseable ({exc})")
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module in {"sqlite3", "aiosqlite"}
                and any(a.name == "connect" for a in node.names)
            ):
                violations.append(
                    f"{rel}:{node.lineno}: `from {node.module} import "
                    "connect` evades the opener matcher — import the "
                    "module and call it as an attribute"
                )
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name in {"sqlite3", "aiosqlite"} and a.asname:
                        violations.append(
                            f"{rel}:{node.lineno}: aliased `import {a.name} "
                            f"as {a.asname}` evades the opener matcher — "
                            "use the unaliased module name"
                        )
        for node, chain in _connect_calls(tree):
            if rel in _ALLOWLIST:
                seen_allowlisted.add(rel)
                continue
            if not chain:
                violations.append(
                    f"{rel}:{node.lineno}: module-level raw connect (no "
                    "enclosing function to carry the fence check)"
                )
                continue
            if not _scope_consults_fence(chain):
                violations.append(
                    f"{rel}:{node.lineno}: raw {ast.unparse(node.func)} without "
                    "an admission fence check in any enclosing function"
                )
    assert not violations, (
        "script-side SQLite openers must consult the admission fence "
        "(db_admission_check.database_is_fenced) before connecting:\n  " + "\n  ".join(violations)
    )
    stale = set(_ALLOWLIST) - seen_allowlisted
    assert not stale, f"allowlist entries no longer matching any opener: {stale}"


# ---------------------------------------------------------------------------
# admission module unit behavior
# ---------------------------------------------------------------------------


@pytest.fixture()
def admission(monkeypatch, tmp_path):
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / ".genesis"))
    if str(_SRC) not in sys.path:
        sys.path.insert(0, str(_SRC))
    import genesis.db.admission as adm

    return adm


def test_no_fence_on_fresh_state(admission, tmp_path):
    """Empty state: no admission dir, no markers -> not fenced."""
    db = tmp_path / "x.db"
    db.write_bytes(b"")
    assert admission.database_is_fenced(db) is False


def test_maintenance_marker_fences_and_survives_replacement(admission, tmp_path):
    """The maintenance fence is PATH-bound: replacing the file keeps it."""
    db = tmp_path / "x.db"
    db.write_bytes(b"a")
    marker = admission.maintenance_marker_path(db)
    marker.parent.mkdir(parents=True)
    marker.write_text("{}")
    assert admission.database_is_fenced(db) is True
    db.unlink()
    db.write_bytes(b"b")  # atomic-replacement stand-in: new inode, same path
    assert admission.database_is_fenced(db) is True, (
        "a maintenance fence must survive file replacement — path-bound, "
        "deliberately unlike the inode-bound quarantine marker"
    )


def test_quarantine_marker_fences(admission, tmp_path, monkeypatch):
    """The quarantine half routes through genesis.db.integrity."""
    db = tmp_path / "x.db"
    db.write_bytes(b"not a database")
    from genesis.db import integrity

    integrity.quarantine_database(db, source="test", detail="unit")
    assert admission.database_is_fenced(db) is True


def test_uri_and_path_spellings_share_one_fence(admission, tmp_path):
    """file: URI callers must land on the same admission domain as the path."""
    db = tmp_path / "x.db"
    db.write_bytes(b"a")
    marker = admission.maintenance_marker_path(db)
    marker.parent.mkdir(parents=True)
    marker.write_text("{}")
    assert admission.database_is_fenced(f"file:{db}?mode=ro") is True


# ---------------------------------------------------------------------------
# real-entry-path behavior: the incident replay
# ---------------------------------------------------------------------------


def _seed_audit_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE file_modifications (id INTEGER PRIMARY KEY, session_id, "
        "file_path, action, tool_name, file_hash, timestamp)"
    )
    conn.commit()
    conn.close()


def _run_audit_hook(tmp_path: Path, db: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["HOME"] = str(tmp_path / "home")
    env["GENESIS_HOME"] = str(tmp_path / ".genesis")
    env["GENESIS_DB_PATH"] = str(db)
    payload = {
        "session_id": "test-session",
        "tool_name": "Write",
        "tool_input": {"file_path": str(tmp_path / "some_file.txt")},
    }
    return subprocess.run(
        [sys.executable, str(_SCRIPTS / "file_modification_audit_hook.py")],
        input=json.dumps(payload),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _row_count(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM file_modifications").fetchone()[0]
    finally:
        conn.close()


def test_audit_hook_writes_when_unfenced(tmp_path):
    """CONTROL: the fixture genuinely produces a write when no fence exists.

    Without this, the fenced test below could pass because the hook never
    writes at all — the guard-the-guard leg.
    """
    db = tmp_path / "genesis.db"
    _seed_audit_db(db)
    proc = _run_audit_hook(tmp_path, db)
    assert proc.returncode == 0, proc.stderr
    assert _row_count(db) == 1, (
        f"control failed: the hook did not write on an unfenced database (stderr: {proc.stderr})"
    )


def test_audit_hook_refuses_quarantined_database(tmp_path):
    """INCIDENT REPLAY: a quarantined database takes no hook write.

    On the sibling install, syscall tracing attributed post-quarantine writes
    to exactly this hook. With the marker present the hook must exit 0 (never
    crash a session) and leave the database untouched.
    """
    db = tmp_path / "genesis.db"
    _seed_audit_db(db)
    home = tmp_path / ".genesis"
    home.mkdir()
    st = db.stat()
    (home / "db_quarantine.json").write_text(
        json.dumps(
            {
                "db_path": str(db.resolve()),
                "st_dev": st.st_dev,
                "st_ino": st.st_ino,
                "source": "test",
            }
        )
    )
    proc = _run_audit_hook(tmp_path, db)
    assert proc.returncode == 0, f"a fenced hook must not crash: {proc.stderr}"
    assert _row_count(db) == 0, (
        "the audit hook WROTE to a quarantined database — the incident class "
        "this fence exists to stop"
    )


def test_audit_hook_refuses_maintenance_fenced_database(tmp_path, monkeypatch):
    """The maintenance fence (path-scoped marker) refuses the same way."""
    db = tmp_path / "genesis.db"
    _seed_audit_db(db)
    if str(_SRC) not in sys.path:
        sys.path.insert(0, str(_SRC))
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / ".genesis"))
    import genesis.db.admission as adm

    marker = adm.maintenance_marker_path(db)
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"reason": "test maintenance"}))
    proc = _run_audit_hook(tmp_path, db)
    assert proc.returncode == 0, f"a fenced hook must not crash: {proc.stderr}"
    assert _row_count(db) == 0, "the audit hook wrote through an active maintenance fence"
