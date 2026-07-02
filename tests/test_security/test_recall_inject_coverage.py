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
_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "genesis"

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
    "mcp/memory/core.py::memory_recall": (
        "wrapped", "external items wrapped after label_result_dicts (full path)"),
    "mcp/memory/core.py::memory_proactive": (
        "wrapped", "source=both default; external items wrapped in the return"),
    "mcp/memory/knowledge.py::knowledge_recall": (
        "wrapped", "all hits external-world; content wrapped (vector + FTS body)"),
    "knowledge/applications/resume_review.py::_query_knowledge_base": (
        "wrapped", "source=knowledge; both vector and FTS body wrapped"),
    "autonomy/executor/research.py::_memory_recall": (
        "wrapped", "source=both default; external items wrapped before triage prompt"),
    "cc/context_injector.py::inject": (
        "wrapped", "episodic today; defensive is_external guard future-proofs widening"),
    "autonomy/executor/resources.py::_search_observations": (
        "first-party", "source=episodic (past task executions)"),
    "autonomy/executor/resources.py::_search_past_executions": (
        "first-party", "source=episodic (past task executions)"),
    "ego/context.py::_user_corrections_section": (
        "first-party", "source=episodic, D12-pinned to first-party user corrections"),
    "mcp/memory/knowledge.py::reference_lookup": (
        "first-party", "source=episodic; takes unit_id/score only, not content"),
    "channels/voice/handler.py::handle": (
        "user-facing", "spoken output; already is_external label-handled, XML wrap wrong"),
    "dashboard/routes/memory.py::memory_search": (
        "display", "human HTTP display, not an LLM prompt (stored-XSS threat class)"),
    "memory/corrective.py::_augment": (
        "pipeline-internal", "CRAG re-retrieve; results flow back through wrapped recall"),
}

# NOTE: memory_expand (mcp/memory/core.py) is ALSO a wrapped inject site, but it
# retrieves via ``_qdrant.retrieve(...)`` (by-id), not ``.recall(...)``, so it is
# not captured by this AST sweep. Its wrapping is verified by its own unit test.
# Likewise the proactive_memory_hook.py script lives outside src/genesis and is
# covered by tests/test_hooks/test_proactive_provenance.py. If a future change
# routes either through ``.recall(...)``, this registry will demand it be listed.


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
