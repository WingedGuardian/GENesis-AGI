"""Durable coverage guardrail for memory ``source_subsystem`` tagging.

Context: internal-subsystem memory writes (ego, triage, reflection, autonomy)
must set ``source_subsystem=`` so their decisional output is excluded from
default recall and stored FTS5-only. Hand enumeration of the writers drifted
(a whole class of legacy writes leaked into recall), so this test converts
residual completeness risk into a PR-time forcing function.

Mechanism: statically enumerate every ``<something>.store(...)`` call under
``src/genesis`` that looks like a MemoryStore write (it passes one of
``_MEMORY_KWARGS`` — the distinctive MemoryStore.store parameters). Each
enclosing function must EITHER pass ``source_subsystem=`` (an internal
subsystem writer) OR be registered in ``USER_CONTEXT_ALLOWLIST`` with a reason
(user-sourced / knowledge / consolidated content that must stay recallable).
A NEW, unregistered untagged writer fails with a message telling the author to
classify it. A stale allowlist entry (writer now tagged or removed) fails too.

PLUS a module invariant: a ``module`` (``src/genesis/modules/**``) is an
external capability, never a Genesis subsystem — no module ``.store()`` call
may set ``source_subsystem`` (see ``modules/base.py``). This is a HARD fail so
a future module cannot be mis-tagged as a subsystem.

Blind spot (documented, like test_recall_inject_coverage): a memory writer
that passes NONE of ``_MEMORY_KWARGS`` (e.g. only positional args) is not
discovered. Every current writer passes ``memory_type=`` or similar; if a
future one does not, add a distinctive kwarg or extend ``_MEMORY_KWARGS``.
"""

from __future__ import annotations

import ast
import pathlib

# Repo-root-relative source tree.
_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "genesis"

# Distinctive MemoryStore.store(...) keyword parameters. A ``.store(...)`` call
# passing any of these is treated as a memory write (filters out unrelated
# ``.store()`` APIs like ``conn.store``).
_MEMORY_KWARGS = frozenset(
    {
        "memory_type",
        "source_pipeline",
        "source_subsystem",
        "wing",
        "room",
        "auto_link",
        "source_session_id",
        "invalid_at",
        "memory_class",
    }
)

# file (relative to src/genesis) :: enclosing function  ->  why it is NULL.
# These writers deliberately leave source_subsystem NULL: the content is
# user-sourced, external-world knowledge, or a consolidated memory that must
# remain in default recall.
USER_CONTEXT_ALLOWLIST: dict[str, str] = {
    "bookmark/manager.py::create_explicit": "user-saved bookmark (user content)",
    "bookmark/manager.py::create_micro": "user-saved bookmark (user content)",
    "bookmark/manager.py::create_topic": "user-saved bookmark (user content)",
    "bookmark/manager.py::enrich": "user-saved bookmark enrichment (user content)",
    "channels/telegram/_handler_messages.py::_try_ego_correction_store": "user-authored ego correction (user content; ego must recall it)",
    # NOTE: s2s_session.py::close no longer writes memory — voice conversations
    # now land as extractable transcripts (W0.5), so there is no .store() call
    # here to classify.
    "eval/longmemeval/ingest.py::ingest_haystack": "LongMemEval benchmark haystack ingest into an EPHEMERAL throwaway store "
    "(first_party user-history content; never touches prod; not a subsystem)",
    "knowledge/ingest_upload.py::_store_as_is": "user-uploaded knowledge_base content (external-world, recallable)",
    "knowledge/orchestrator.py::_store_units": "ingested knowledge units (external-world, recallable)",
    "mcp/memory/core.py::memory_store": "user-invoked MCP store",
    "mcp/memory/core.py::memory_extract": "user-invoked MCP extraction",
    "mcp/memory/core.py::memory_synthesize": "user-invoked MCP synthesis",
    "memory/dream_cycle.py::_synthesize_and_deprecate": "consolidated memory meant FOR recall (tagging would break update_payload)",
    "memory/knowledge_ingest.py::ingest_knowledge_unit": "knowledge_base ingest (external-world, recallable)",
    "memory/session_observer.py::process_pending_observations": "conversation-derived observations (~48% of recall pool; must stay)",
    "recon/cc_update_analyzer.py::_ingest_to_knowledge": "external-world CC-update knowledge (recallable)",
}


def _enclosing(funcs: list[tuple[int, int, str]], lineno: int) -> str:
    best, name = -1, "<module>"
    for start, end, fname in funcs:
        if start <= lineno <= (end or start) and start > best:
            best, name = start, fname
    return name


def _discover_store_sites() -> tuple[set[str], set[str], set[str]]:
    """Enumerate memory-store call sites.

    Returns ``(untagged_funcs, tagged_module_funcs, all_funcs)`` where keys are
    ``"relpath::func"``:
      * untagged_funcs      — has ≥1 memory-store call WITHOUT source_subsystem
      * tagged_module_funcs — a ``modules/**`` site that DOES set source_subsystem
      * all_funcs           — every memory-store enclosing func (for sanity)
    """
    untagged: set[str] = set()
    tagged_module: set[str] = set()
    all_funcs: set[str] = set()
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
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "store"
            ):
                continue
            kwargs = {k.arg for k in node.keywords if k.arg}
            if not (kwargs & _MEMORY_KWARGS):
                continue
            key = f"{rel}::{_enclosing(funcs, node.lineno)}"
            all_funcs.add(key)
            has_ss = "source_subsystem" in kwargs
            if not has_ss:
                untagged.add(key)
            if has_ss and rel.startswith("modules/"):
                tagged_module.add(key)
    return untagged, tagged_module, all_funcs


def test_allowlist_entries_have_reasons():
    for key, why in USER_CONTEXT_ALLOWLIST.items():
        assert why.strip(), f"{key}: empty rationale"


def test_every_untagged_writer_is_classified():
    untagged, _, _ = _discover_store_sites()
    allow = set(USER_CONTEXT_ALLOWLIST)

    unclassified = untagged - allow
    assert not unclassified, (
        "New memory writer(s) that do NOT pass source_subsystem= and are not "
        "classified:\n  "
        + "\n  ".join(sorted(unclassified))
        + "\n\nIf this is an internal-subsystem writer (ego/triage/reflection/"
        "autonomy), add source_subsystem=. If it is user-sourced / knowledge / "
        "consolidated content that must stay recallable, register it in "
        "USER_CONTEXT_ALLOWLIST with a reason."
    )

    stale = allow - untagged
    assert not stale, (
        "Allowlisted writer(s) no longer found untagged (now tagged, renamed, "
        "or removed) — update USER_CONTEXT_ALLOWLIST:\n  " + "\n  ".join(sorted(stale))
    )


def test_no_module_sets_source_subsystem():
    """Modules are external capabilities, never Genesis subsystems."""
    _, tagged_module, _ = _discover_store_sites()
    assert not tagged_module, (
        "module .store() call(s) set source_subsystem — modules are external "
        "capabilities ('hands, not brain', see modules/base.py), NEVER Genesis "
        "subsystems, and must never tag a memory write:\n  " + "\n  ".join(sorted(tagged_module))
    )
