"""WS-3 B1 gates 2-3 (identity / autonomy) coverage locks.

Same forcing-function idea as ``test_recall_inject_coverage.py`` (gate 4) and
``test_procedure_gate_coverage.py`` (gate 1): discovery is AST-based, every
discovered site needs an explicit disposition, and every 'gated' site must
actually emit — so a new writer/caller can't ship provenance-unclassified and
a removed emit fails CI.

GATE-2 (identity): the choke methods are identity/loader.py's ``write_text``
enclosers, pinned by SET-EQUALITY (a new/moved raw write in loader.py fails).
Callers of the public writers are discovered and must be registered. The
dashboard config-file PUT writer (outside loader.py, generic ``write_text``)
is pinned MANUALLY — an AST sweep scoped to loader can't see it.

GATE-3 (autonomy): discovery is IMPORT-ALIAS-RESOLVED — only ``Attribute``
calls on names bound to ``genesis.db.crud.capability_grants`` count, which
excludes the many unrelated ``record_success`` collisions (telegram typing
breakers, routing circuit breaker, AutonomyManager's legacy autonomy_state
path). The emits live INSIDE the crud choke (``_emit_autonomy_gate``); callers
THREAD origin_class (a REQUIRED kwarg — locked below — so a future caller must
state provenance or fail loudly at call time).

OUT OF GATE-3 SCOPE: the legacy ``autonomy_state`` evidence store
(db/crud/autonomy.py record_success/record_correction via AutonomyManager —
audit.py, surplus/dispatch.py, learning/pipeline.py). Same threat shape on a
parallel LIVE store, but all writers are Genesis's own execution telemetry
(first_party) and the store is slated to stay authoritative for non-email
readers (capability_grants.py module docstring). Documented exclusion, not
silent — revisit if any external-influenced writer appears.
"""

from __future__ import annotations

import ast
import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src" / "genesis"

_GATE_VALID = {"gated", "gated-via", "deferred-with-reason", "exempt-with-reason"}


def _parse(path: pathlib.Path) -> ast.AST | None:
    try:
        return ast.parse(path.read_text(), filename=str(path))
    except SyntaxError:
        return None


def _funcs(tree: ast.AST) -> list[tuple[int, int, str]]:
    return [
        (n.lineno, getattr(n, "end_lineno", n.lineno), n.name)
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _enclosing(funcs: list[tuple[int, int, str]], lineno: int) -> str:
    best, enclosing = -1, "<module>"
    for start, end, name in funcs:
        if start <= lineno <= (end or start) and start > best:
            best, enclosing = start, name
    return enclosing


def _functions_calling_attr(attrs: set[str]) -> set[str]:
    """{"relpath::func"} for every function calling a METHOD named in *attrs*."""
    found: set[str] = set()
    for path in _SRC.rglob("*.py"):
        tree = _parse(path)
        if tree is None:
            continue
        funcs = _funcs(tree)
        rel = path.relative_to(_SRC).as_posix()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in attrs
            ):
                found.add(f"{rel}::{_enclosing(funcs, node.lineno)}")
    return found


# ═══════════════════════════════ GATE 2 — identity ══════════════════════════

# The complete raw-write surface of identity/loader.py. write_user_md is
# comment-disabled with zero callers, but fully callable — pinning it means a
# future caller that re-enables USER.md synthesis must come through this
# registry.
_LOADER_WRITE_METHODS = {
    "write_user_md",
    "write_user_knowledge_md",
    "_write_narrative_knowledge",
    "_write_steering",
}

# Public writer entry points whose CALLERS must be classified.
_IDENTITY_WRITER_NAMES = {"add_steering_rule", "write_user_knowledge_md", "write_user_md"}

# discovered caller "relpath::func" -> (disposition, emitter_ref | None, why)
# emitter_ref: for 'gated-via', the enclosing function that carries the emit
# (the write call and the emit may live in different frames).
IDENTITY_GATE_SITES: dict[str, tuple[str, str | None, str]] = {
    "learning/pipeline.py::_extract_steering_rule": (
        "gated-via",
        "learning/pipeline.py::_run_pipeline",
        "steering write; the async caller emits ONLY on a real write (the "
        "directive-filter reject is a non-event), origin from _CHANNEL_ORIGIN "
        "(owner allow-map, unknown/voice -> external_untrusted fail-closed)",
    ),
    "runtime/init/learning.py::_evolve_user_model": (
        "gated",
        None,
        "USER_KNOWLEDGE synthesis; first_party by authorship (reflection-"
        "derived deltas). FLIP BLOCKER: observations carry no origin_class, so "
        "externally-planted user-facts stay first_party until delta-level "
        "provenance lands",
    ),
}

# Identity-file writers OUTSIDE loader.py that a loader-scoped sweep cannot
# see — pinned manually (the gate-4 _EXTRA_WRAPPED_SITES pattern).
IDENTITY_EXTRA_SITES: dict[str, tuple[str, str | None, str]] = {
    "dashboard/routes/config.py::config_file_update": (
        "exempt-with-reason",
        None,
        "owner-direct edit surface (HTTP-auth'd dashboard PUT into "
        "_IDENTITY_DIR) — owner origin by construction; never a would-block",
    ),
}


def test_loader_write_surface_is_pinned():
    """SET-EQUALITY on identity/loader.py's write_text enclosers — a new or
    moved raw write method fails CI and must be classified here."""
    tree = _parse(_SRC / "identity" / "loader.py")
    assert tree is not None
    funcs = _funcs(tree)
    writers = {
        _enclosing(funcs, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "write_text"
    }
    assert writers == _LOADER_WRITE_METHODS, (
        f"identity/loader.py raw-write surface changed: {sorted(writers)} != "
        f"{sorted(_LOADER_WRITE_METHODS)}. Update _LOADER_WRITE_METHODS and "
        "classify any new writer's callers in IDENTITY_GATE_SITES."
    )


def test_every_identity_writer_caller_is_classified():
    discovered = _functions_calling_attr(_IDENTITY_WRITER_NAMES)
    # The loader's own internals (write_user_knowledge_md delegating to
    # _write_narrative_knowledge etc.) are covered by the surface pin above.
    discovered = {d for d in discovered if not d.startswith("identity/loader.py::")}
    registered = set(IDENTITY_GATE_SITES)
    unregistered = discovered - registered
    assert not unregistered, (
        "Identity-writer caller(s) not classified for WS-3 gate-2:\n  "
        + "\n  ".join(sorted(unregistered))
        + "\n\nClassify each in IDENTITY_GATE_SITES (wire "
        "security.immunity_shadow.record_would_block with a derived origin and "
        "mark 'gated'/'gated-via', or exempt with a reason)."
    )
    stale = registered - discovered
    assert not stale, (
        "Registered identity site(s) no longer found — update "
        "IDENTITY_GATE_SITES:\n  " + "\n  ".join(sorted(stale))
    )


def test_identity_gated_sites_actually_emit():
    emitters = _functions_calling_attr({"record_would_block", "record_would_block_sync"})
    for key, (disp, emitter_ref, _why) in {
        **IDENTITY_GATE_SITES,
        **IDENTITY_EXTRA_SITES,
    }.items():
        assert disp in _GATE_VALID, f"{key}: invalid disposition {disp!r}"
        if disp == "gated":
            assert key in emitters, f"{key} is 'gated' but does not emit"
        elif disp == "gated-via":
            assert emitter_ref in emitters, (
                f"{key} is 'gated-via' {emitter_ref}, which does not emit — "
                "the gate emit was removed or moved"
            )


def test_dashboard_identity_writer_still_exists():
    """The manual pin must track reality: the dashboard PUT writer's enclosing
    function must still exist (rename/removal updates the registry)."""
    tree = _parse(_SRC / "dashboard" / "routes" / "config.py")
    assert tree is not None
    names = {name for _, _, name in _funcs(tree)}
    assert "config_file_update" in names, (
        "dashboard/routes/config.py::config_file_update not found — update "
        "IDENTITY_EXTRA_SITES to the new identity-file write surface"
    )


# ═══════════════════════════════ GATE 3 — autonomy ══════════════════════════

_CG_MODULE = "genesis.db.crud"
_CG_NAME = "capability_grants"
_CG_MUTATORS = {"record_success", "record_correction", "apply_event"}
# Non-evidence mutations, exempt by design: touch_used (usage telemetry),
# ensure_cell (mechanical row creation), decay_stale_cells (time decay).

# discovered caller "relpath::func" -> (disposition, why). All callers THREAD
# origin_class into the crud choke, where the single emit lives.
AUTONOMY_GATE_SITES: dict[str, tuple[str, str]] = {
    "autonomy/email_gate.py::check": (
        "gated-via",
        "CLASSIFY apply_event + scope-guard record_correction; both thread "
        "origin_class='first_party' (Genesis's own deterministic guards) into "
        "the crud choke emit",
    ),
    "autonomy/email_gate_watcher.py::drain_pending_email_sends": (
        "gated-via",
        "owner approve -> record_success / owner reject -> record_correction; "
        "threads origin_class='owner' (owner decisions are the evidence)",
    ),
    "dashboard/routes/autonomy.py::autonomy_flag_send": (
        "gated-via",
        "owner flags an autonomous send from the dashboard; threads origin_class='owner'",
    ),
    "ego/cell_promotion.py::handle_cell_promotion_resolution": (
        "gated-via",
        "executes an OWNER-approved promotion proposal (APPROVE apply_event); "
        "threads origin_class='owner'",
    ),
}


def _cg_bound_names(tree: ast.AST) -> set[str]:
    """Local names bound to the capability_grants module in this file."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == _CG_MODULE:
            for alias in node.names:
                if alias.name == _CG_NAME:
                    bound.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == f"{_CG_MODULE}.{_CG_NAME}":
                    # `import genesis.db.crud.capability_grants` binds the top
                    # package; calls would be fully dotted — treat the full
                    # dotted tail as the bound name marker (none exist today).
                    bound.add(alias.asname or _CG_NAME)
    return bound


def _discover_cg_mutation_callers() -> set[str]:
    """{"relpath::func"} for alias-resolved capability_grants mutation calls."""
    found: set[str] = set()
    for path in _SRC.rglob("*.py"):
        tree = _parse(path)
        if tree is None:
            continue
        bound = _cg_bound_names(tree)
        if not bound:
            continue
        funcs = _funcs(tree)
        rel = path.relative_to(_SRC).as_posix()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _CG_MUTATORS
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in bound
            ):
                found.add(f"{rel}::{_enclosing(funcs, node.lineno)}")
    return found


def test_every_grant_mutation_caller_is_classified():
    discovered = _discover_cg_mutation_callers()
    registered = set(AUTONOMY_GATE_SITES)
    unregistered = discovered - registered
    assert not unregistered, (
        "capability_grants mutation caller(s) not classified for WS-3 gate-3:\n  "
        + "\n  ".join(sorted(unregistered))
        + "\n\nEach caller must thread origin_class (required kwarg) and be "
        "registered in AUTONOMY_GATE_SITES."
    )
    stale = registered - discovered
    assert not stale, (
        "Registered gate-3 site(s) no longer found — update "
        "AUTONOMY_GATE_SITES:\n  " + "\n  ".join(sorted(stale))
    )


def test_autonomy_dispositions_valid():
    for key, (disp, why) in AUTONOMY_GATE_SITES.items():
        assert disp in _GATE_VALID, f"{key}: invalid disposition {disp!r}"
        assert why.strip(), f"{key}: empty rationale"


def test_crud_choke_emits_for_all_three_mutators():
    """The single emit helper must be called by ALL THREE mutation functions —
    deleting/moving an emit fails CI."""
    tree = _parse(_SRC / "db" / "crud" / "capability_grants.py")
    assert tree is not None
    funcs = _funcs(tree)
    emitting = {
        _enclosing(funcs, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_emit_autonomy_gate"
    }
    assert emitting >= _CG_MUTATORS, (
        f"capability_grants mutators missing the gate-3 emit: {sorted(_CG_MUTATORS - emitting)}"
    )
    # And the helper itself performs the shadow emit.
    assert "db/crud/capability_grants.py::_emit_autonomy_gate" in (
        _functions_calling_attr({"record_would_block"})
    )


def test_origin_class_is_required_on_all_mutators():
    """origin_class must stay a REQUIRED kwarg (no default) on all three
    mutators — a silent first_party default would be a permanently inert gate."""
    tree = _parse(_SRC / "db" / "crud" / "capability_grants.py")
    assert tree is not None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _CG_MUTATORS:
            kwonly = {a.arg for a in node.args.kwonlyargs}
            assert "origin_class" in kwonly, f"{node.name}: origin_class missing"
            defaults = {
                a.arg: d
                for a, d in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True)
                if d is not None
            }
            assert "origin_class" not in defaults, f"{node.name}: origin_class must have NO default"
