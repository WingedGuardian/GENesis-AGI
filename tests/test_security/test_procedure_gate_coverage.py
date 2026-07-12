"""WS-3 B1 gate-1 (procedure-promotion) coverage lock.

Every call site that promotes a procedure into the store — ``store_procedure``
or ``store_procedure_checked`` — must be classified here with an explicit gate
disposition. A NEW, unclassified promotion site fails this test (telling the
author to wire the shadow gate or exempt it with a reason); a REMOVED site fails
too, keeping the registry honest.

Mirrors the gate-4 pattern in ``test_recall_inject_coverage.py``, adapted for
procedure promotion: the store functions are called as BARE names
(``store_procedure(...)``), so discovery matches ``ast.Name``, not
``ast.Attribute``.

Dispositions:
  gated               — a real promotion path; its function emits a shadow
                        would-block via ``security.immunity_shadow`` with the
                        session/trace ``origin_class`` (owner/first_party
                        self-guard to no row; external_untrusted records).
  deferred-with-reason— a promotion path that intentionally does NOT emit yet,
                        with a rationale (e.g. no origin signal in scope, path
                        pending removal).
  internal            — not a promotion site of its own: an internal delegation
                        between the two store functions
                        (``store_procedure_checked`` -> ``store_procedure``),
                        which must not be double-counted.
"""

from __future__ import annotations

import ast
import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src" / "genesis"

_STORE_FUNCS = {"store_procedure", "store_procedure_checked"}
_GATE_VALID = {"gated", "deferred-with-reason", "internal"}

# file (relative to src/genesis) :: enclosing function -> (disposition, why)
PROCEDURE_GATE_SITES: dict[str, tuple[str, str]] = {
    "learning/procedural/judge.py::_store_judged_procedure": (
        "gated",
        "judge convergence for BOTH the struggle and rebuild callers; emits with "
        "derive_session_origin(spine)",
    ),
    "autonomy/executor/trace.py::_store_new_procedure": (
        "gated",
        "autonomy retrospective; emits initiated_by-derived origin (Genesis's own "
        "execution = first_party/owner). The ExecutionTrace exposes no source-tool "
        "spine, and proc_data['tools_used'] is the retrospective's replay tools, "
        "not source provenance — so external-research influence is a deferred "
        "source-provenance follow-up, not a tool-name signal here",
    ),
    "mcp/memory/procedural.py::procedure_store": (
        "gated",
        "explicit-teach; emits origin_from_tool_names(tools_used) — NOT hardcoded "
        "owner (the research profile exposes this tool alongside web tools, so a "
        "background session can teach externally-influenced content). PR-B upgrades "
        "to per-session origin",
    ),
    "learning/procedural/extractor.py::extract_procedure": (
        "gated",
        "legacy 500-char fallback (still live from pipeline.py on "
        "APPROACH_FAILURE / WORKAROUND_SUCCESS / autonomous SUCCESS — exactly the "
        "outcomes most likely to carry external content); emits session_origin, "
        "which the pipeline.py caller derives from the SOURCE session's tool spine "
        "(summary.tool_calls) — NOT data['tools_used'] (the procedure's replay "
        "tools). Full removal of this path is tracked as follow-up 3558802740d5",
    ),
    "learning/procedural/operations.py::store_procedure_checked": (
        "internal",
        "delegates to store_procedure after dedup/upsert checks — the checked "
        "wrapper's own call, not a distinct promotion site",
    ),
}


def _discover_store_sites() -> dict[str, int]:
    """{"relpath::func": lineno} for every BARE-NAME call to a procedure store
    function (``store_procedure`` / ``store_procedure_checked``)."""
    found: dict[str, int] = {}
    for path in _SRC.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError:
            continue
        funcs = [
            (n.lineno, getattr(n, "end_lineno", n.lineno), n.name)
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        rel = path.relative_to(_SRC).as_posix()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in _STORE_FUNCS
            ):
                best, enclosing = -1, "<module>"
                for start, end, name in funcs:
                    if start <= node.lineno <= (end or start) and start > best:
                        best, enclosing = start, name
                found.setdefault(f"{rel}::{enclosing}", node.lineno)
    return found


def _functions_calling(attrs: set[str]) -> set[str]:
    """{"relpath::func"} for every function calling a METHOD named in *attrs*
    (e.g. ``record_would_block`` — an attribute call on the shadow module)."""
    found: set[str] = set()
    for path in _SRC.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError:
            continue
        funcs = [
            (n.lineno, getattr(n, "end_lineno", n.lineno), n.name)
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        rel = path.relative_to(_SRC).as_posix()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in attrs
            ):
                best, enclosing = -1, "<module>"
                for start, end, name in funcs:
                    if start <= node.lineno <= (end or start) and start > best:
                        best, enclosing = start, name
                found.add(f"{rel}::{enclosing}")
    return found


def test_procedure_gate_dispositions_valid():
    for key, (disp, why) in PROCEDURE_GATE_SITES.items():
        assert disp in _GATE_VALID, f"{key}: invalid disposition {disp!r}"
        assert why.strip(), f"{key}: empty rationale"


def test_every_store_procedure_site_is_classified():
    discovered = set(_discover_store_sites())
    registered = set(PROCEDURE_GATE_SITES)

    unregistered = discovered - registered
    assert not unregistered, (
        "New procedure store site(s) not classified for the WS-3 gate-1:\n  "
        + "\n  ".join(sorted(unregistered))
        + "\n\nClassify each in PROCEDURE_GATE_SITES: wire "
        "security.immunity_shadow.record_would_block with the session origin and "
        "mark it 'gated', or mark 'deferred-with-reason'/'internal' with a reason."
    )

    stale = registered - discovered
    assert not stale, (
        "Registered procedure store site(s) no longer found (rename/removal?) — "
        "update PROCEDURE_GATE_SITES:\n  " + "\n  ".join(sorted(stale))
    )


def test_gated_sites_actually_emit():
    """Every 'gated' site must call record_would_block — so deleting/moving the
    emit fails CI."""
    emitters = _functions_calling({"record_would_block", "record_would_block_sync"})
    for key, (disp, _why) in PROCEDURE_GATE_SITES.items():
        if disp != "gated":
            continue
        assert key in emitters, (
            f"{key} is classified 'gated' but its function does not call "
            "immunity_shadow.record_would_block — the gate emit was removed or "
            "moved. Re-wire it or reclassify with a reason."
        )
