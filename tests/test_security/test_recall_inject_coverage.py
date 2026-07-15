"""Durable coverage guardrail for recall-side injection defense (PR2).

Context: recalled external-world (knowledge_base) content that reaches an LLM
prompt must be structurally wrapped in ``<external-content>`` markers at inject
time (see ``genesis.memory.provenance.wrap_external_recall``). During PR2 design,
three successive manual sweeps EACH found a new inject site the prior sweep
missed (resume_review, research, memory_expand, memory_proactive). Hand
enumeration does not converge — so this test converts residual completeness risk
into a PR-time forcing function.

Mechanism: statically enumerate every ``<something>.recall(...)`` call site under
``src/genesis`` and assert each enclosing function is registered below with an
explicit disposition. A NEW, unregistered recall consumer fails this test with a
message telling the author to classify it (wrap it, or exempt it with a reason).
A REMOVED site fails too, keeping the registry honest.

This guards the retriever ``.recall()`` consumers. It does NOT try to prove each
``wrapped`` site actually calls ``wrap_external_recall`` — that is covered by the
per-site unit tests. Its single job is: no recall consumer reaches an LLM prompt
unclassified.
"""

from __future__ import annotations

import ast
import pathlib

# Repo-root-relative source tree.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src" / "genesis"

# Valid dispositions for a recall call site.
#   wrapped          — external-world content can reach an LLM prompt here; it is
#                      wrapped via wrap_external_recall (or is behind the wrapped
#                      MCP recall tools).
#   first-party      — recalls source="episodic" only (Genesis's own memory), or
#                      takes only IDs/scores (not content) into a prompt.
#   user-facing      — output goes to the human (spoken/UI), not into an LLM
#                      prompt; structural XML wrapping is inappropriate.
#   display          — human display surface (HTTP/dashboard), not an LLM prompt
#                      (threat class is stored-XSS, handled elsewhere — not here).
#   pipeline-internal— an intermediate re-retrieval whose results flow BACK
#                      through a wrapped recall entrypoint; not a direct inject
#                      site of its own.
_VALID = {"wrapped", "first-party", "user-facing", "display", "pipeline-internal"}

# file (relative to src/genesis) :: enclosing function  ->  (disposition, why)
KNOWN_RECALL_SITES: dict[str, tuple[str, str]] = {
    "eval/longmemeval/runner.py::_run_arm": (
        "first-party",
        "eval-harness recall against an EPHEMERAL store of "
        "first_party LongMemEval haystack; no production trust boundary "
        "(extracted from run_question for the dual-store graph arm)",
    ),
    "mcp/memory/core.py::memory_recall": (
        "wrapped",
        "external items wrapped after label_result_dicts (full path)",
    ),
    "mcp/memory/core.py::memory_proactive": (
        "wrapped",
        "source=both default; external items wrapped in the return",
    ),
    "mcp/memory/knowledge.py::knowledge_recall": (
        "wrapped",
        "all hits external-world; content wrapped (vector + FTS body)",
    ),
    "knowledge/applications/resume_review.py::_query_knowledge_base": (
        "wrapped",
        "source=knowledge; both vector and FTS body wrapped",
    ),
    "autonomy/executor/research.py::_memory_recall": (
        "wrapped",
        "source=both default; external items wrapped before triage prompt",
    ),
    "cc/context_injector.py::inject": (
        "wrapped",
        "episodic today; defensive is_external guard future-proofs widening",
    ),
    "autonomy/executor/resources.py::_search_observations": (
        "first-party",
        "source=episodic (past task executions)",
    ),
    "autonomy/executor/resources.py::_search_past_executions": (
        "first-party",
        "source=episodic (past task executions)",
    ),
    "ego/context.py::_user_corrections_section": (
        "first-party",
        "source=episodic, D12-pinned to first-party user corrections",
    ),
    "mcp/memory/knowledge.py::reference_lookup": (
        "first-party",
        "source=episodic; takes unit_id/score only, not content",
    ),
    "channels/voice/handler.py::handle": (
        "wrapped",
        "full LLM path wraps external content into the system prompt; "
        "the spoken (raw_snippets) rendering keeps a soft label (can't speak XML)",
    ),
    "dashboard/routes/memory.py::memory_search": (
        "display",
        "human HTTP display, not an LLM prompt (stored-XSS threat class)",
    ),
    "memory/corrective.py::_augment": (
        "pipeline-internal",
        "CRAG re-retrieve; results flow back through wrapped recall",
    ),
}

# NOTE: memory_expand and memory_core_facts (mcp/memory/core.py) are ALSO
# wrapped inject sites, but they read Qdrant directly (by-id retrieve / scroll),
# not via ``.recall(...)`` — they are captured by the KNOWN_QDRANT_READ_SITES
# sweep below. The proactive_memory_hook.py script lives outside src/genesis and
# is covered by tests/test_hooks/test_proactive_provenance.py.


def _discover_recall_sites() -> dict[str, int]:
    """Return {"relpath::func": lineno} for every ``X.recall(...)`` call site."""
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
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "recall"
            ):
                enclosing = "<module>"
                best = -1
                for start, end, name in funcs:
                    if start <= node.lineno <= (end or start) and start > best:
                        best, enclosing = start, name
                found.setdefault(f"{rel}::{enclosing}", node.lineno)
    return found


# ── WS-3 B1: injection-gate (gate 4) coverage lock ─────────────────────────
# Every `wrapped` recall site (external-world content reaching an
# action-capable LLM prompt) must ALSO be classified here as `gated` (it emits
# a shadow would-block via ``security.immunity_shadow``) or
# `deferred-with-reason`. This converts gate coverage into a PR-time forcing
# function: a new wrapped site cannot silently bypass the injection gate, and
# removing an emit from a gated site fails CI (see
# ``test_gated_sites_actually_emit``).
_GATE_VALID = {"gated", "deferred-with-reason"}

# Two wrapped inject sites reach an LLM prompt but are NOT captured by the
# ``.recall()`` AST sweep above — ``memory_expand`` retrieves by-id via
# ``_qdrant.retrieve``, and the proactive hook lives outside ``src/genesis``.
# They are enumerated explicitly so the gate set is complete.
_EXTRA_WRAPPED_SITES: dict[str, tuple[str, str]] = {
    "mcp/memory/core.py::memory_expand": (
        "gated",
        "by-id retrieve (not .recall); wraps + emits external hits",
    ),
    "mcp/memory/core.py::memory_core_facts": (
        "gated",
        "episodic scroll (not .recall); wraps stored-external items "
        "+ emits (B4 — caught by the qdrant-read sweep below)",
    ),
    "scripts/proactive_memory_hook.py::_run": (
        "gated",
        "sync emit after stdout flush; lives outside src/genesis. B4 "
        "pushed-feed cut: dispatched sessions never RUN this hook (module-"
        "level GENESIS_CC_SESSION exit) — protection is total absence, not a "
        "per-item drop; only user-launched foreground terminal sessions reach "
        "it (wrap/label only). If that exit is ever narrowed, this surface "
        "re-enters the enforce-drop set.",
    ),
}

# file::func -> (gate disposition, why). All wrapped sites are gated now
# (enumeration-complete); `deferred-with-reason` stays as the explicit escape
# hatch for a future site that legitimately should not gate.
INJECTION_GATE_SITES: dict[str, tuple[str, str]] = {
    "mcp/memory/core.py::memory_recall": (
        "gated",
        "emits on BOTH the full (enriched) and compact-preview branches",
    ),
    "mcp/memory/core.py::memory_proactive": (
        "gated",
        "source=both default; emits per-call blockable count",
    ),
    "mcp/memory/knowledge.py::knowledge_recall": (
        "gated",
        "every hit external-world; emits per-call blockable count",
    ),
    "knowledge/applications/resume_review.py::_query_knowledge_base": (
        "gated",
        "source=knowledge; emits (user-facing but enumeration-complete)",
    ),
    "autonomy/executor/research.py::_memory_recall": (
        "gated",
        "highest write-capability path; emits",
    ),
    "cc/context_injector.py::inject": (
        "gated",
        "episodic today -> 0 rows; emit wired for a future source widening",
    ),
    "channels/voice/handler.py::handle": (
        "gated",
        "wraps external into the LLM system prompt; emits",
    ),
    **_EXTRA_WRAPPED_SITES,
}


# OUT OF GATE-4 SCOPE (external-tool-output, not recall-inject): document_query
# (mcp/memory/documents.py) sends a PDF to the external PageIndex QA service
# and returns its SYNTHESIZED answer to the prompt; web_fetch / web_search
# likewise return external tool output. These reach a prompt but carry no
# origin_class / wrap_external_recall model, so they are a DIFFERENT gate
# class (quarantine tool output), not part of the provenance-based recall
# gate this registry enforces. Tracked as a separate WS-3 follow-up.


def _functions_calling(attrs: set[str]) -> set[str]:
    """Return {"relpath::func"} for every function under src that calls a method
    whose attribute name is in *attrs* (e.g. record_would_block)."""
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


def test_injection_gate_dispositions_valid():
    for key, (disp, why) in INJECTION_GATE_SITES.items():
        assert disp in _GATE_VALID, f"{key}: invalid gate disposition {disp!r}"
        assert why.strip(), f"{key}: empty rationale"


def test_every_wrapped_recall_site_is_gate_classified():
    wrapped = {k for k, (disp, _) in KNOWN_RECALL_SITES.items() if disp == "wrapped"}
    classified = set(INJECTION_GATE_SITES)
    missing = wrapped - classified
    assert not missing, (
        "wrapped recall site(s) not classified for the WS-3 injection gate:\n  "
        + "\n  ".join(sorted(missing))
        + "\n\nClassify each in INJECTION_GATE_SITES: wire "
        "security.immunity_shadow.record_would_block and mark it 'gated', or "
        "mark 'deferred-with-reason' with a rationale."
    )


def test_gated_sites_actually_emit():
    """Every 'gated' site under src/genesis must call record_would_block[_sync]
    — so deleting/moving the emit fails CI. (The proactive hook is verified by
    test_proactive_hook_emits_sync; it lives outside src/genesis.)"""
    emitters = _functions_calling({"record_would_block", "record_would_block_sync"})
    for key, (disp, _why) in INJECTION_GATE_SITES.items():
        if disp != "gated" or key.startswith("scripts/"):
            continue
        assert key in emitters, (
            f"{key} is classified 'gated' but its function does not call "
            "immunity_shadow.record_would_block[_sync] — the gate emit was "
            "removed or moved. Re-wire it or reclassify with a reason."
        )


def test_proactive_hook_emits_sync():
    hook = _REPO_ROOT / "scripts" / "proactive_memory_hook.py"
    assert "record_would_block_sync" in hook.read_text(), (
        "the proactive-memory hook no longer emits the injection shadow gate"
    )


def test_all_registry_dispositions_valid():
    for key, (disp, why) in KNOWN_RECALL_SITES.items():
        assert disp in _VALID, f"{key}: invalid disposition {disp!r}"
        assert why.strip(), f"{key}: empty rationale"


def test_every_recall_site_is_classified():
    discovered = set(_discover_recall_sites())
    registered = set(KNOWN_RECALL_SITES)

    unregistered = discovered - registered
    assert not unregistered, (
        "New recall() call site(s) not classified for injection defense:\n  "
        + "\n  ".join(sorted(unregistered))
        + "\n\nClassify each in KNOWN_RECALL_SITES: if external-world content can "
        "reach an LLM prompt here, WRAP it with wrap_external_recall and mark it "
        "'wrapped'; otherwise mark 'first-party'/'user-facing'/'display'/"
        "'pipeline-internal' with a reason."
    )

    stale = registered - discovered
    assert not stale, (
        "Registered recall site(s) no longer found (rename/removal?) — update "
        "KNOWN_RECALL_SITES:\n  " + "\n  ".join(sorted(stale))
    )


# ── WS-3 B4: non-recall Qdrant content-read sweep ───────────────────────────
# ``memory_core_facts`` scrolled episodic and injected full content into a
# prompt while being INVISIBLE to the ``.recall()`` sweep above — the header\'s
# "hand enumeration does not converge" applied to the sweep itself. This second
# sweep closes that class mechanically: every direct Qdrant ``.scroll(...)`` /
# ``.retrieve(...)`` call site under src/genesis must be classified here.
#
#   prompt-gated         — payload CONTENT reaches an LLM prompt; the site wraps
#                          blockable items and emits the gate-4 shadow record.
#   infra                — counters/vectors/tags/existence/ops only; no content
#                          flows into any prompt.
#   library              — shared Qdrant helper; its CALLERS carry the
#                          classification.
#   deferred-with-reason — content DOES reach an LLM but gating is explicitly
#                          tracked work (rationale must name the follow-up
#                          concern).
_QDRANT_READ_VALID = {"prompt-gated", "infra", "library", "deferred-with-reason"}

KNOWN_QDRANT_READ_SITES: dict[str, tuple[str, str]] = {
    "eval/bench/isolation.py::_scroll_usage": (
        "infra",
        "bench snapshot usage-payload copy between collections",
    ),
    "mcp/memory/core.py::_increment_retrieved": (
        "infra",
        "retrieved_count writeback; reads the counter only",
    ),
    "mcp/memory/core.py::memory_core_facts": (
        "prompt-gated",
        "episodic confidence-scroll into the caller prompt; "
        "wraps stored-external items + emits gate 4 (B4)",
    ),
    "mcp/memory/core.py::memory_expand": (
        "prompt-gated",
        "by-id full-payload expand; wraps + emits gate 4",
    ),
    "memory/dream_cycle.py::_get_vector": ("infra", "vectors only (with_vectors, no content use)"),
    "memory/dream_cycle.py::_rehydrate_cluster": (
        "deferred-with-reason",
        "cluster member CONTENT feeds the consolidation "
        "LLM and the synthesized canonical memory does not inherit member "
        "origin_class — an origin-LAUNDERING path, not a session-inject path. "
        "Origin-aware consolidation is a tracked WS-3 follow-up; today only a "
        "handful of episodic rows are external and daily-slice runs shadow.",
    ),
    "memory/health.py::_scan_duplicates": (
        "infra",
        "duplicate-detection metric; content hashed, never prompted",
    ),
    "memory/intent.py::expand_query": ("infra", "tag-index rebuild; with_payload=[tags] only"),
    "qdrant/collections.py::batch_retrieve_vectors": (
        "library",
        "shared helper; callers classified individually",
    ),
    "qdrant/collections.py::get_point": (
        "library",
        "shared helper; callers classified individually",
    ),
    "qdrant/collections.py::scroll_points": (
        "library",
        "shared helper; callers classified individually",
    ),
    "resilience/embedding_recovery.py::_point_exists": (
        "infra",
        "existence check for recovery bookkeeping",
    ),
    "runtime/init/memory.py::_migrate_reference_vectors": ("infra", "boot-time vector migration"),
    "session_awareness/ranking.py::rank_candidates": (
        "deferred-with-reason",
        "candidate CONTENT reaches the ambient "
        "attention ARBITER prompt (judgment-only CC run, output = pick "
        "indices). Origin-aware arbiter handling is a tracked WS-3 follow-up; "
        "episodic-external volume is currently negligible.",
    ),
    "surplus/extraction_calibration.py::run_calibration": (
        "infra",
        "confidence/retrieved_count aggregation only",
    ),
}


def _discover_qdrant_read_sites() -> dict[str, int]:
    """{"relpath::func": lineno} for every ``X.scroll(...)``/``X.retrieve(...)``
    call site under src/genesis. Attribute-name match — a non-Qdrant object
    with a ``.retrieve``/``.scroll`` method would surface here too; classify it
    (usually ``infra``) rather than narrowing the sweep."""
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
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("scroll", "retrieve")
            ):
                best, enclosing = -1, "<module>"
                for start, end, name in funcs:
                    if start <= node.lineno <= (end or start) and start > best:
                        best, enclosing = start, name
                found.setdefault(f"{rel}::{enclosing}", node.lineno)
    return found


def test_qdrant_read_dispositions_valid():
    for key, (disp, why) in KNOWN_QDRANT_READ_SITES.items():
        assert disp in _QDRANT_READ_VALID, f"{key}: invalid disposition {disp!r}"
        assert why.strip(), f"{key}: empty rationale"


def test_every_qdrant_read_site_is_classified():
    discovered = set(_discover_qdrant_read_sites())
    registered = set(KNOWN_QDRANT_READ_SITES)

    unregistered = discovered - registered
    assert not unregistered, (
        "New Qdrant .scroll()/.retrieve() call site(s) not classified:\n  "
        + "\n  ".join(sorted(unregistered))
        + "\n\nClassify each in KNOWN_QDRANT_READ_SITES: if payload CONTENT "
        "reaches an LLM prompt, wrap blockable items + emit gate 4 and mark it "
        "'prompt-gated'; otherwise 'infra'/'library', or 'deferred-with-reason' "
        "naming the tracked concern."
    )

    stale = registered - discovered
    assert not stale, (
        "Registered Qdrant read site(s) no longer found (rename/removal?) — "
        "update KNOWN_QDRANT_READ_SITES:\n  " + "\n  ".join(sorted(stale))
    )


def test_prompt_gated_qdrant_sites_emit():
    """Every 'prompt-gated' Qdrant read site must call record_would_block[_sync]
    — deleting/moving the emit fails CI."""
    emitters = _functions_calling({"record_would_block", "record_would_block_sync"})
    for key, (disp, _why) in KNOWN_QDRANT_READ_SITES.items():
        if disp != "prompt-gated":
            continue
        assert key in emitters, (
            f"{key} is classified 'prompt-gated' but its function does not call "
            "immunity_shadow.record_would_block[_sync]. Re-wire the emit or "
            "reclassify with a reason."
        )


# ─── WS-3 B4 aftermath — stored-origin propagation locks ────────────────────
# The #1048 review grind was ONE defect class, found leaf-by-leaf across 11
# reviewer rounds: a gate-decision site consulting only the (collection,
# source_pipeline) re-derivation while the STORED origin_class existed but was
# not threaded — stored-external episodic items then crossed unwrapped /
# uncounted, or provenance labels disagreed with wrap decisions. These two
# locks turn the entire class into a PR-time failure instead of a
# review-round discovery:
#   1) every item_is_blockable / should_enforce_drop call passes
#      origin_class= (stored-first, never collection-only re-derivation);
#   2) every wrap_external_recall caller also consults item_is_blockable in
#      the same function (the wrap decision is keyed stored-first).

_ORIGIN_DECISION_ATTRS = {"item_is_blockable", "should_enforce_drop"}

# Definitions + internal delegation live here (should_enforce_drop calls
# item_is_blockable inside the module) — not consumer sites.
_ORIGIN_LOCK_EXCLUDED_FILES = {"security/immunity_shadow.py"}

# relpath::func -> reason, for a future wrap caller that legitimately never
# consults blockability. EMPTY today — every current wrap caller is keyed
# stored-first; add an entry only with a written rationale.
WRAP_WITHOUT_BLOCKABLE_OK: dict[str, str] = {}


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _functions_calling_any(attrs: set[str]) -> set[str]:
    """Like _functions_calling, but matches BOTH attribute calls
    (module.func(...)) and bare-name calls (from-imported func(...))."""
    found: set[str] = set()
    for path in _SRC.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError:
            continue
        rel = path.relative_to(_SRC).as_posix()
        funcs = [
            (n.lineno, getattr(n, "end_lineno", n.lineno), n.name)
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) in attrs:
                best, enclosing = -1, "<module>"
                for start, end, name in funcs:
                    if start <= node.lineno <= (end or start) and start > best:
                        best, enclosing = start, name
                found.add(f"{rel}::{enclosing}")
    return found


def test_gate_decision_calls_thread_stored_origin():
    """Lock 1: no decision call may omit origin_class= — omitting it silently
    falls back to (collection, source_pipeline) re-derivation, which is blind
    to stored-external episodic rows (the exact #1048 defect class)."""
    missing: set[str] = set()
    for path in _SRC.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError:
            continue
        rel = path.relative_to(_SRC).as_posix()
        if rel in _ORIGIN_LOCK_EXCLUDED_FILES:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and _call_name(node) in _ORIGIN_DECISION_ATTRS
                and "origin_class" not in {k.arg for k in node.keywords}
            ):
                missing.add(f"{rel}:{node.lineno} ({_call_name(node)})")
    assert not missing, (
        "Gate-decision call(s) omit origin_class= — thread the item's STORED "
        "origin (enrich via origin_class_by_ids / RetrievalResult.origin_class "
        "if the surface doesn't carry it yet); collection-only re-derivation "
        f"cannot see stored-external episodic rows: {sorted(missing)}"
    )


def test_every_wrap_caller_consults_blockability():
    """Lock 2: a function that wraps external recall content must key that
    decision stored-first (call item_is_blockable), or be registered with a
    written rationale — wrap-on-collection-alone is the #1048 defect class."""
    wrap_callers = {
        s
        for s in _functions_calling_any({"wrap_external_recall"})
        if s.split("::")[0] not in {"memory/provenance.py"}
    }
    blockable_callers = _functions_calling_any(_ORIGIN_DECISION_ATTRS)
    unkeyed = wrap_callers - blockable_callers - set(WRAP_WITHOUT_BLOCKABLE_OK)
    assert not unkeyed, (
        "wrap_external_recall caller(s) never consult item_is_blockable — key "
        "the wrap on stored-first blockability (see memory_core_facts for the "
        f"reference pattern) or register with a rationale: {sorted(unkeyed)}"
    )
    stale = set(WRAP_WITHOUT_BLOCKABLE_OK) - wrap_callers
    assert not stale, f"stale WRAP_WITHOUT_BLOCKABLE_OK entries: {sorted(stale)}"
