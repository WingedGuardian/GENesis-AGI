"""Guardrail: every privileged observation-consumer gates on origin.

The origin gate is a LIBRARY choke-point — nothing structurally forces a consumer
to apply it, so a refactor that drops the gate, or a NEW privileged consumer that
forgets it, would silently re-open the poisoning surface. This test pins the
enumerated consumer set AND — via AST — checks the gate appears inside the NAMED
function's own body (not merely somewhere in the module), so moving the call out
of the consumer fails CI.

The class was enumerated via a Serena ``find_referencing_symbols`` sweep of
``db/crud/observations.py::query`` PLUS a raw-SQL grep of ``FROM observations``
(2026-08-18 — the raw-SQL grep was added after review found ``observations.query``
alone missed raw ``db.execute`` consumers). Consumers that AUTO-CONSUME a
``user_model_delta`` / ``task_detected`` into privileged state:
- ``user_model.process_pending_deltas`` — accept into the user model.
- ``user_model.synthesize_narrative`` — resolved deltas → USER_KNOWLEDGE.md narrative.
- ``autonomy.dispatcher.dispatch_cycle`` — task_detected → autonomy dispatch.
Other ``observations`` readers surface content into LLM context WITHOUT the strict
write-gate (essential_knowledge / reflection / ego) — that broader injection surface
is a separate tracked follow-up, gated on the recall-side ``is_blockable`` predicate,
not this write-side one.
"""

from __future__ import annotations

import ast
from pathlib import Path

import genesis

_SRC = Path(genesis.__file__).parent

# Any of these tokens, appearing INSIDE the named function, counts as gating on
# origin: the per-row helper, the shared trusted-origin set, or the SQL filter arg.
_GATE_TOKENS = (
    "is_trusted_for_privileged_write",
    "TRUSTED_PRIVILEGED_WRITE_ORIGINS",
    "origin_class_in",
)

# (module relative path, the privileged-consumer function that MUST gate on origin)
_PRIVILEGED_CONSUMERS = [
    ("memory/user_model.py", "process_pending_deltas"),
    ("memory/user_model.py", "synthesize_narrative"),
    ("autonomy/dispatcher.py", "dispatch_cycle"),
]


def _function_ast_dump(module_src: str, func_name: str) -> str:
    """AST dump of the named function's own node.

    Using ``ast.dump`` (not the raw source segment) means COMMENTS are excluded —
    a comment mentioning a gate token can't fool the guardrail; only a real
    identifier / keyword-arg / string in the function's AST matches.
    """
    tree = ast.parse(module_src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == func_name:
            return ast.dump(node)
    raise AssertionError(f"function {func_name!r} not found in module")


def test_privileged_consumers_gate_on_origin_in_their_own_body():
    for rel, func in _PRIVILEGED_CONSUMERS:
        dump = _function_ast_dump((_SRC / rel).read_text(), func)
        assert any(tok in dump for tok in _GATE_TOKENS), (
            f"{rel}::{func} must gate on origin (one of {_GATE_TOKENS}) INSIDE its "
            f"own body — it auto-consumes observation content into privileged state."
        )


def test_gate_helper_and_trusted_set_are_importable_and_consistent():
    from genesis.security.immunity import (
        TRUSTED_PRIVILEGED_WRITE_ORIGINS,
        is_trusted_for_privileged_write,
    )

    # The trusted set is exactly owner/first_party; the helper agrees with it.
    assert set(TRUSTED_PRIVILEGED_WRITE_ORIGINS) == {"owner", "first_party"}
    for origin in TRUSTED_PRIVILEGED_WRITE_ORIGINS:
        assert is_trusted_for_privileged_write(origin) is True
    assert is_trusted_for_privileged_write("external_untrusted") is False
    assert is_trusted_for_privileged_write(None) is False
