"""WS-3 surface-coverage guardrail: every observations-table CONTENT consumer
is discovered mechanically and must carry a classified origin-policy verdict.

Two discovery passes over git-TRACKED src/genesis (tracked-only so install-local
modules — e.g. a git-excluded local module directory — cannot make this suite
diverge between an install and CI):

1. RAW SQL — any ``ast.Constant``/``ast.JoinedStr`` string matching
   ``from observations`` case/whitespace-insensitively (docstrings skipped),
   attributed to its enclosing function. Implicit adjacent-literal
   concatenation is covered because Python's parser merges adjacent literals
   into one Constant; a file-level regex net cross-checks the AST pass so a
   needle landing outside AST-visible strings fails CI. KNOWN LIMIT: explicit
   ``"FROM " + "observations"`` runtime concatenation evades both passes —
   write SQL as plain literals.
2. CRUD QUERY CALLERS — alias-aware ``observations.query(...)`` calls
   (``from genesis.db.crud import observations [as X]``, module imports,
   direct ``from genesis.db.crud.observations import query`` styles) PLUS any
   attribute-chained ``<expr>.observations.query(...)`` (over-approximate —
   catches the sys.modules-aliased ``memory_mod.observations.query`` idiom).

Every discovered site must appear in ``SURFACE_INVENTORY`` with a verdict:

- ``EXCLUDED`` — external_untrusted rows filtered out at the query (NULL kept);
  function must contain an exclusion token.
- ``WRAPPED`` — external content surfaced inside <external-content> markers;
  function must contain the wrap token (or the entry's override token when the
  producer/renderer are split functions).
- ``GATED`` — privileged consumer behind is_trusted_for_privileged_write /
  origin_class_in (the strict NULL-excluding predicates).
- ``SAFE_INTERNAL`` — content not surfaced into LLM/file/user context
  (counts, EXISTS, timestamps, dedup, self-written internal telemetry), or the
  crud/data layer itself. Reason string documents why.
- ``DISPLAY`` — human-facing dashboard JSON (origin_class included in payload;
  not an LLM context).

A NEW consumer fails this suite until its author classifies it — the same
forcing-function pattern as test_store_subsystem_coverage.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
# Case-insensitive + whitespace-tolerant: lowercase "from observations" is
# valid SQLite and a plausible honest style — it must not evade discovery.
_NEEDLE_RE = re.compile(r"(?i)from\s+observations\b")
_NEEDLE_DESC = "FROM observations (case/whitespace-insensitive)"

# site key: "path/relative/to/src/genesis::function" -> (verdict, reason, token_override)
# token_override (3rd element, optional) replaces the verdict's default token set —
# used when the discovered producer and the policy-carrying renderer are split
# across functions (behavior then pinned by a dedicated test).
_E = "EXCLUDED"
_W = "WRAPPED"
_G = "GATED"
_S = "SAFE_INTERNAL"
_D = "DISPLAY"

SURFACE_INVENTORY: dict[str, tuple] = {
    # ── crud / data layer (policy applied by callers; query() is the choke point)
    "db/crud/observations.py::create": (_S, "INSERT layer"),
    "db/crud/observations.py::query": (
        _S,
        "choke point — exposes origin_class_in + exclude_origin_class; callers classified individually",
    ),
    "db/crud/observations.py::get_by_id": (_S, "id-scoped lookup"),
    "db/crud/observations.py::exists_by_hash": (_S, "EXISTS dedup"),
    "db/crud/observations.py::exists_recent_by_type": (_S, "EXISTS check"),
    "db/crud/observations.py::distinct_unresolved_types": (_S, "DISTINCT type list"),
    "db/crud/observations.py::distinct_unresolved_sources": (_S, "DISTINCT source list"),
    "db/crud/observations.py::delete": (_S, "mutator"),
    "db/crud/observations.py::delete_by_source_and_type": (_S, "mutator"),
    "db/crud/observations.py::oldest_created_at": (_S, "timestamp aggregate"),
    "db/crud/observations.py::get_unsurfaced": (
        _S,
        "morning-report feed of Genesis-authored operational alerts (INTERNAL_OBS_TYPES excluded by callers)",
    ),
    "db/crud/observations.py::get_standing": (_S, "standing internal alerts feed"),
    "db/crud/observations.py::count_unsurfaced": (_S, "COUNT"),
    "db/crud/observations.py::count_unresolved": (_S, "COUNT"),
    "db/crud/observations.py::count_unresolved_by_types": (_S, "COUNT"),
    "db/crud/observations.py::count_external_by_ids": (
        _S,
        "COUNT of external rows (observability)",
    ),
    "db/crud/observations.py::count_recent_unresolved_by_type_and_source": (_S, "COUNT"),
    "db/crud/observations.py::unsurfaced_counts_by_priority": (_S, "COUNT by priority"),
    "db/crud/cognitive_state.py::compute_state_flags": (_S, "COUNT flags"),
    "db/crud/loop_closure.py::observation_funnel": (_S, "funnel COUNTs"),
    "db/crud/loop_closure.py::reflection_funnel": (_S, "funnel COUNTs"),
    "db/data_migrations/d0002_resolve_duplicate_session_alerts.py::verify": (
        _S,
        "migration verify",
    ),
    "db/data_migrations/d0010_backfill_skill_proposal_dampening.py::migrate": (_S, "migration"),
    "db/data_migrations/d0010_backfill_skill_proposal_dampening.py::verify": (
        _S,
        "migration verify",
    ),
    # ── privileged consumers (strict NULL-excluding gates; see test_privileged_write_coverage)
    "memory/user_model.py::process_pending_deltas": (_G, "user-model accept path"),
    "memory/user_model.py::synthesize_narrative": (_G, "narrative evidence"),
    "autonomy/dispatcher.py::dispatch_cycle": (_G, "task_detected pickup"),
    # ── hard exclusions (reflection pipeline + always-loaded L1 file)
    "reflection/context_gatherer.py::gather_evaluation_context": (_E, "deep-reflection evidence"),
    "reflection/context_gatherer.py::gather_for_calibration": (_E, "calibration prompt content"),
    "reflection/context_gatherer.py::_recent_observations": (
        _E,
        "deep-reflection recent-obs funnel",
    ),
    "perception/context.py::_build_memory_hits": (_E, "reflection memory-hits section"),
    "perception/context.py::_build_light_chain_context": (
        _E,
        "light-chain context (json.loads consumer — wrap incompatible)",
    ),
    "memory/essential_knowledge.py::_recent_decisions": (_E, "L1 Key Insights"),
    "memory/essential_knowledge.py::_active_session_pivots": (_E, "L1 Active Work"),
    "memory/essential_knowledge.py::_count_observations": (_S, "COUNT header stat"),
    # ── wrapped LLM-context surfaces
    "ego/context.py::_observations_section": (_W, "combined-ego observations section"),
    "ego/genesis_context.py::_observations_section": (_W, "genesis-ego observations section"),
    "ego/user_context.py::_recurring_patterns_section": (
        _W,
        "GROUP-BY sample; any_external taints",
    ),
    "ego/world_snapshot.py::build": (
        _W,
        "producer half — render() wraps; behavior pinned by test_observation_surface_wrap",
        ("origin_class",),
    ),
    "sentinel/context.py::assemble_diagnostic_context": (_W, "sentinel diagnostic context"),
    "guardian/briefing.py::build_dynamic_briefing": (_W, "guardian briefing"),
    "surplus/executor.py::_gather_context": (_W, "surplus generator prompt"),
    "mcp/memory/observations.py::observation_query": (
        _W,
        "MCP tool results land in calling session's context",
    ),
    "mcp/memory/core.py::memory_stats": (_S, "len() of pending deltas — count only"),
    # ── reflection-adjacent safe reads
    "reflection/context_gatherer.py::detect_pending_work": (_S, "backlog COUNT"),
    "reflection/context_gatherer.py::gather_for_assessment": (
        _S,
        "metric/count reads of cc_reflection_deep cohort",
    ),
    "reflection/context_gatherer.py::_intelligence_digest": (_S, "timestamp read only"),
    "reflection/context_gatherer.py::_intake_items_since_last_deep": (_S, "timestamp read only"),
    "reflection/question_gate.py::can_ask": (_S, "COUNT pending"),
    "reflection/scheduler.py::_already_ran_this_week": (_S, "existence check"),
    "reflection/stability.py::check_regression": (
        _S,
        "numeric score parse of self_assessment (source-pin residual: see PR body)",
    ),
    "cc/reflection_bridge/_prompts.py::_fetch_prior_light_summary": (
        _E,
        "forgeable source/type-pin \u2192 reflection prompt; external excluded",
    ),
    # ── ego safe reads (Genesis-authored sources)
    "ego/genesis_context.py::_execution_outcomes_section": (
        _E,
        "forgeable type/source-pin \u2192 genesis-ego prompt; external excluded",
    ),
    "ego/user_context.py::_execution_outcomes_section": (
        _E,
        "forgeable type/source-pin → user-ego prompt; external excluded",
    ),
    "ego/user_context.py::_genesis_escalations_section": (
        _E,
        "forgeable type-pin \u2192 user-ego prompt at priority=critical; external excluded",
    ),
    "ego/user_context.py::_backlog_summary_section": (_S, "COUNT"),
    "ego/session.py::_process_escalations": (_S, "dedup compare of self-written escalations"),
    # ── telemetry / signals / observability
    "awareness/loop.py::_check_user_model_staleness": (_S, "MAX/EXISTS"),
    "awareness/loop.py::perform_tick": (_S, "existence/dedup checks"),
    "observability/snapshots/awareness.py::awareness": (_S, "created_at/source read"),
    "learning/signals/error_spike.py::collect": (_S, "COUNTs"),
    "learning/signals/recon_findings.py::collect": (_S, "COUNT"),
    "learning/signals/genesis_version.py::_store_update_available": (_S, "version dedup/write"),
    "learning/signals/genesis_version.py::_check_failure_file": (_S, "version marker read"),
    "learning/signals/genesis_version.py::_get_baseline": (_S, "version baseline read"),
    "learning/signals/cc_version.py::_get_last_known_version": (_S, "version read"),
    "learning/signals/cc_version.py::_check_registry_version": (_S, "version read"),
    "learning/triage/calibration.py::_query_recent_observations": (
        _S,
        "retrospective-source metric parse (source-pin residual)",
    ),
    "runtime/_degradation.py::record_init_degradation": (_S, "SELECT 1 existence"),
    "mcp/health/errors.py::_compute_alerts": (_S, "type-pinned genesis_update_* internal alerts"),
    "surplus/cc_memory_staleness.py::_write_observations": (_S, "writer + resolved-flag read"),
    "surplus/code_audit.py::_get_previous_findings": (
        _S,
        "self-written recon code_audit dedup (source-pin residual)",
    ),
    "recon/gatherer.py::_get_previous_star_count": (_S, "self-written recon marker"),
    "recon/gatherer.py::_get_last_release_timestamp": (_S, "self-written recon marker"),
    "recon/account_activity.py::_drain_pending": (_S, "self-written github markers"),
    # ── eval harness (offline, not a live LLM/user surface)
    "eval/j9_aggregator.py::_compute_system_composite": (_S, "offline eval metric"),
    "eval/j9_aggregator.py::_compute_dev_quality": (_S, "offline eval metric"),
    "eval/j9_aggregator.py::_grade_reflection": (_S, "offline eval metric"),
    "eval/reflection_golden_set.py::_sample_observations": (_S, "offline eval sampling"),
    # ── dashboard (human display; origin_class present in JSON payload)
    "dashboard/routes/observations.py::observations_list": (
        _D,
        "owner dashboard list; origin_class in payload",
    ),
    "dashboard/routes/recon.py::recon_findings": (_D, "recon findings panel (self-written)"),
    "dashboard/routes/surplus.py::surplus_activity": (_D, "surplus activity panel"),
    "dashboard/routes/state.py::job_health_endpoint": (_S, "COUNTs"),
    "dashboard/routes/updates.py::update_status": (_S, "type-pinned genesis_update_* telemetry"),
    "dashboard/routes/vitals.py::_build_sqlite_section": (_S, "COUNTs"),
    "mcp/recon_mcp.py::recon_findings": (_S, "self-written recon findings tool"),
}

_VERDICT_TOKENS: dict[str, tuple[str, ...]] = {
    _E: ("exclude_origin_class", "EXCLUDE_EXTERNAL_ORIGIN_SQL"),
    _W: ("wrap_if_external",),
    _G: ("is_trusted_for_privileged_write", "origin_class_in", "TRUSTED_PRIVILEGED_WRITE_ORIGINS"),
}

# Files where the regex net may hit outside code strings (comments/docstrings
# discussing the observations table). Add "relpath" entries WITH a reason.
_REGEX_ONLY_ALLOWED: frozenset[str] = frozenset(
    {
        # prose docstrings ("... from observations [table]"), no SQL:
        "dashboard/routes/recon.py",
        "learning/signals/cc_version.py",
    }
)


def _tracked_py_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "src/genesis/**/*.py", "src/genesis/*.py"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    return [_REPO / line for line in out.stdout.splitlines() if line]


def _docstring_ids(tree: ast.AST) -> set[int]:
    """id()s of Constant nodes that are docstrings (first stmt of a body)."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


def _enclosing_function(tree: ast.AST, lineno: int) -> str:
    best = "<module>"
    best_span: int | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            end = getattr(node, "end_lineno", node.lineno)
            if node.lineno <= lineno <= end:
                span = end - node.lineno
                if best_span is None or span < best_span:
                    best, best_span = node.name, span
    return best


def _raw_sql_functions(tree: ast.AST) -> set[str]:
    hits: set[str] = set()
    doc_ids = _docstring_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _NEEDLE_RE.search(node.value) and id(node) not in doc_ids:
                hits.add(_enclosing_function(tree, node.lineno))
        elif isinstance(node, ast.JoinedStr):
            for part in node.values:
                if (
                    isinstance(part, ast.Constant)
                    and isinstance(part.value, str)
                    and _NEEDLE_RE.search(part.value)
                ):
                    hits.add(_enclosing_function(tree, node.lineno))
                    break
    return hits


def _crud_query_functions(tree: ast.AST) -> set[str]:
    mod_aliases: set[str] = set()
    direct: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "genesis.db.crud":
                for a in node.names:
                    if a.name == "observations":
                        mod_aliases.add(a.asname or a.name)
            elif node.module == "genesis.db.crud.observations":
                for a in node.names:
                    if a.name == "query":
                        direct.add(a.asname or a.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "genesis.db.crud.observations":
                    mod_aliases.add(a.asname or a.name)
    hits: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == "query":
            # alias-rooted: observations.query(...) / obs_crud.query(...)
            if (
                isinstance(f.value, ast.Name)
                and f.value.id in mod_aliases
                or isinstance(f.value, ast.Attribute)
                and f.value.attr == "observations"
            ):
                hits.add(_enclosing_function(tree, node.lineno))
        elif isinstance(f, ast.Name) and f.id in direct:
            hits.add(_enclosing_function(tree, node.lineno))
    return hits


def _discover() -> tuple[dict[str, set[str]], set[str]]:
    """Return ({site_key: {"raw"|"query"}}, regex_only_files)."""
    sites: dict[str, set[str]] = {}
    regex_only: set[str] = set()
    src_root = _REPO / "src" / "genesis"
    for py in _tracked_py_files():
        rel = str(py.relative_to(src_root))
        source = py.read_text()
        tree = ast.parse(source)
        raw = _raw_sql_functions(tree)
        q = _crud_query_functions(tree)
        for fn in raw:
            sites.setdefault(f"{rel}::{fn}", set()).add("raw")
        for fn in q:
            sites.setdefault(f"{rel}::{fn}", set()).add("query")
        if _NEEDLE_RE.search(source) and not raw:
            regex_only.add(rel)
    return sites, regex_only


def test_every_discovered_consumer_is_classified():
    sites, _ = _discover()
    unclassified = sorted(set(sites) - set(SURFACE_INVENTORY))
    assert not unclassified, (
        "NEW observations-table consumer(s) with no origin-policy verdict:\n  "
        + "\n  ".join(unclassified)
        + "\nClassify each in SURFACE_INVENTORY "
        "(tests/test_security/test_observation_surface_coverage.py): does the "
        "content reach an LLM prompt / always-loaded file / user surface? "
        "Then apply EXCLUDED (exclude_origin_class — NULL kept), WRAPPED "
        "(wrap_if_external), or GATED (privileged consumers), or record why "
        "it is SAFE_INTERNAL/DISPLAY."
    )


def test_no_stale_inventory_entries():
    sites, _ = _discover()
    stale = sorted(set(SURFACE_INVENTORY) - set(sites))
    assert not stale, (
        "SURFACE_INVENTORY entries no longer discovered (moved/renamed/"
        "removed) — update the inventory:\n  " + "\n  ".join(stale)
    )


def test_regex_net_catches_ast_evasion():
    """Any file whose SOURCE mentions the needle but whose AST scan found no
    code-string hit is either a comment/docstring mention (allowlist it with a
    reason) or a string-construction the AST pass cannot see — fail loudly."""
    _, regex_only = _discover()
    unexplained = sorted(regex_only - _REGEX_ONLY_ALLOWED)
    assert not unexplained, (
        f"'{_NEEDLE_DESC}' appears outside AST-visible code strings in: "
        f"{unexplained} — comment/docstring mention (allowlist with reason) "
        "or an evasion-style string construction (rewrite it as a plain "
        "literal so the guardrail can see it)."
    )


def test_verdict_enforcement_tokens_present():
    """EXCLUDED/WRAPPED/GATED sites must carry their policy token INSIDE the
    discovered function's own AST (comments excluded via ast.dump)."""
    src_root = _REPO / "src" / "genesis"
    failures: list[str] = []
    trees: dict[str, ast.AST] = {}
    for key, entry in SURFACE_INVENTORY.items():
        verdict = entry[0]
        tokens = entry[2] if len(entry) > 2 else _VERDICT_TOKENS.get(verdict)
        if not tokens:
            continue
        rel, func = key.split("::")
        if rel not in trees:
            trees[rel] = ast.parse((src_root / rel).read_text())
        tree = trees[rel]
        dump = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == func:
                dump = ast.dump(node)
                break
        if dump is None:
            failures.append(f"{key}: function not found")
            continue
        if not any(tok in dump for tok in tokens):
            failures.append(f"{key}: none of {tokens} inside the function body")
    assert not failures, "Origin-policy verdict not enforced in code:\n  " + "\n  ".join(failures)


def test_crud_constant_matches_provenance():
    from genesis.db.crud import observations as crud
    from genesis.memory.provenance import ORIGIN_EXTERNAL_UNTRUSTED

    assert crud.EXTERNAL_UNTRUSTED == ORIGIN_EXTERNAL_UNTRUSTED
