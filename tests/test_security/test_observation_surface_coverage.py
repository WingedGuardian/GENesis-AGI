"""Guardrail: every observation WRITE classifies origin (WS-3, fail-closed).

Two ways a row reaches the ``observations`` table:

1. The CRUD chokepoint ``db/crud/observations.py`` ``create()`` / ``upsert()`` —
   both call :func:`genesis.memory.provenance.derive_observation_origin` (via the
   local ``_resolve_origin`` helper), so EVERY caller is classified automatically
   (explicit arg → session env → source map → NULL fail-closed). This test pins
   that wiring: a refactor that drops the resolve call would silently mint
   NULL-origin rows from every writer.

2. A RAW ``INSERT INTO observations`` that bypasses the chokepoint. Only two
   stdlib-only hooks legitimately do this (they run without the genesis venv):
   the conversation-pivot writer and the post-commit bugfix-audit writer. Both
   MUST stamp ``origin_class`` in their INSERT. Any OTHER raw INSERT — a new
   writer that forgets the chokepoint — fails CI here (forcing classification).

Discovery is AST (string constants + f-string fragments) with a regex cross-net,
mirroring tests/test_memory/test_store_subsystem_coverage.py.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_SRC = _REPO / "src" / "genesis"
_SCRIPTS = _REPO / "scripts"
_DOT_CLAUDE_HOOKS = _REPO / ".claude" / "hooks"

# Files allowed to issue a raw ``INSERT INTO observations`` (bypassing crud).
# Relative to the repo root. The chokepoint itself lives in the crud module.
_ALLOWED_RAW_INSERT = {
    "src/genesis/db/crud/observations.py",  # THE chokepoint
    "scripts/proactive_memory_hook.py",  # conversation_pivot writer (stdlib-only hook)
    "scripts/hooks/emit_bugfix_audit.py",  # bugfix audit writer (stdlib-only hook)
}

# The two stdlib-only raw writers must stamp origin_class in their INSERT.
_RAW_WRITERS_MUST_STAMP = {
    "scripts/proactive_memory_hook.py",
    "scripts/hooks/emit_bugfix_audit.py",
}

# Both INSERT and REPLACE land a row; catch either verb (REPLACE INTO evasion).
_INSERT_RE = re.compile(r"(?:INSERT|REPLACE)\s+INTO\s+observations", re.IGNORECASE)


def _iter_py_files() -> list[Path]:
    files: list[Path] = []
    for base in (_SRC, _SCRIPTS, _DOT_CLAUDE_HOOKS):
        if not base.exists():
            continue
        files.extend(p for p in base.rglob("*.py") if "test" not in p.parts)
    return files


def _string_constants(tree: ast.AST) -> list[str]:
    """All string literals in a module, including f-string text fragments."""
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            for v in node.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    out.append(v.value)
    return out


def _rel(p: Path) -> str:
    return str(p.relative_to(_REPO))


def _files_with_raw_insert() -> set[str]:
    """Files containing a raw ``INSERT INTO observations`` string (AST-discovered)."""
    hits: set[str] = set()
    for path in _iter_py_files():
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        if any(_INSERT_RE.search(s) for s in _string_constants(tree)):
            hits.add(_rel(path))
    return hits


def test_no_unclassified_raw_observation_insert() -> None:
    """No raw INSERT INTO observations outside the crud chokepoint + 2 known hooks.

    A new raw writer must EITHER route through crud.observations.create/upsert
    (auto-classified) OR be added here AND stamp origin_class (see the sibling
    test). Fail-closed: an unlisted raw writer is a silent NULL-origin source.
    """
    discovered = _files_with_raw_insert()
    unexpected = discovered - _ALLOWED_RAW_INSERT
    assert not unexpected, (
        "Raw 'INSERT INTO observations' found outside the allow-list "
        f"(bypasses the origin chokepoint): {sorted(unexpected)}. "
        "Route through db.crud.observations.create/upsert, or (for a stdlib-only "
        "hook) add it here and stamp origin_class in the INSERT."
    )


def test_allowed_raw_insert_entries_are_live() -> None:
    """Bidirectional: every allow-listed raw writer still contains a raw INSERT
    (a stale entry — writer removed/rerouted — must be pruned from the list)."""
    discovered = _files_with_raw_insert()
    # The crud module is the chokepoint; the two hooks are the raw writers.
    for rel in _ALLOWED_RAW_INSERT:
        assert rel in discovered, (
            f"allow-listed raw writer {rel!r} no longer contains a raw "
            "'INSERT INTO observations' — prune it from _ALLOWED_RAW_INSERT."
        )


def test_regex_net_matches_ast_discovery() -> None:
    """Cross-net: a plain regex file scan finds no raw INSERT that the AST walk
    missed (split-string / unusual construction evasion guard)."""
    regex_hits: set[str] = set()
    for path in _iter_py_files():
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        if _INSERT_RE.search(text):
            regex_hits.add(_rel(path))
    ast_hits = _files_with_raw_insert()
    # Regex may see comments/docstrings the AST-constant walk also sees, so the
    # regex set should be a superset; anything ONLY in regex is worth surfacing.
    regex_only = regex_hits - ast_hits - _ALLOWED_RAW_INSERT
    assert not regex_only, (
        f"regex found raw observation-INSERT the AST walk missed: {sorted(regex_only)}"
    )


def test_raw_writers_stamp_origin_class() -> None:
    """The two stdlib-only raw writers stamp origin_class in their INSERT.

    AST-level: ``origin_class`` must appear as a string constant in the module
    (the INSERT column list) — a comment mentioning it does not count.
    """
    for rel in _RAW_WRITERS_MUST_STAMP:
        path = _REPO / rel
        tree = ast.parse(path.read_text())
        consts = _string_constants(tree)
        assert any("origin_class" in s for s in consts), (
            f"{rel} issues a raw observation INSERT but does not stamp "
            "origin_class — it would mint NULL-origin (fail-closed excluded) rows."
        )


def _function_dump(module_src: str, func_name: str) -> str:
    tree = ast.parse(module_src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == func_name:
            return ast.dump(node)
    raise AssertionError(f"function {func_name!r} not found")


def test_crud_chokepoint_resolves_origin() -> None:
    """create() and upsert() must invoke the origin resolver in their own body —
    this is what makes EVERY crud caller auto-classified."""
    src = (_SRC / "db" / "crud" / "observations.py").read_text()
    for func in ("create", "upsert"):
        dump = _function_dump(src, func)
        assert "_resolve_origin" in dump or "derive_observation_origin" in dump, (
            f"observations.{func}() no longer resolves origin — the write "
            "chokepoint is broken; all callers would mint unclassified rows."
        )


# ── Source-classification coverage (Codex PR #1431 P1#2) ────────────────────
# The chokepoint resolving origin is necessary but not sufficient: a writer whose
# `source` is unknown resolves to NULL → excluded from every laundering-critical
# read surface. This forces each STATIC observation source literal to be
# consciously classified (external / first-party / intake) OR listed as a
# user-content source whose origin is stamped explicitly at the write site.

# Receiver names that alias the observations crud module (observations.create,
# obs_crud.create, observation_crud.create/upsert).
_OBS_RECEIVERS = {"observations", "obs_crud", "observation_crud"}


def _static_observation_sources() -> dict[str, str]:
    """Map each STATIC ``source=`` literal on an observations.create/upsert Call
    to a file, via AST (a regex with ``[^)]`` can't skip the ``)`` in a preceding
    ``id=str(uuid.uuid4())`` arg — the reason the first version saw only ~9/46).
    f-strings / variable sources are dynamic and intentionally skipped."""
    found: dict[str, str] = {}
    for path in _iter_py_files():
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (
                isinstance(fn, ast.Attribute)
                and fn.attr in ("create", "upsert")
                and isinstance(fn.value, ast.Name)
                and fn.value.id in _OBS_RECEIVERS
            ):
                continue
            for kw in node.keywords:
                if (
                    kw.arg == "source"
                    and isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, str)
                ):
                    found.setdefault(kw.value.value, _rel(path))
    return found


def test_every_static_observation_source_is_classified() -> None:
    """Each static source literal classifies to a definite origin, OR is a
    documented user-content source (origin stamped by channel at the write site).
    A NEW writer with an unclassified source fails here — add it to the
    first-party allowlist / external set, or the user-content exceptions."""
    from genesis.memory.provenance import (
        _USER_CONTENT_OBS_SOURCES,
        _origin_from_source,
    )

    unclassified: list[str] = []
    for src, rel in _static_observation_sources().items():
        if src in _USER_CONTENT_OBS_SOURCES:
            continue  # origin stamped explicitly by channel (task_detected_origin)
        if src.startswith(("session:", "module:", "intake:")):
            continue  # resolved dynamically (env / cc_sessions JOIN / intake split)
        if _origin_from_source(src) is None:
            unclassified.append(f"{src!r} ({rel})")
    assert not unclassified, (
        "Observation source(s) resolve to NULL origin → excluded from reflection/"
        "L1 surfaces. Classify each in memory.provenance "
        "(_FIRST_PARTY_OBS_SOURCES / _EXTERNAL_OBS_SOURCES) or, if user-content, "
        "_USER_CONTENT_OBS_SOURCES:\n  " + "\n  ".join(sorted(unclassified))
    )
