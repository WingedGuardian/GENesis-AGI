"""Coverage guardrail for ``claude -p`` model/effort selection.

Every surface that lets a caller pick the model or effort for a ``claude -p``
session must stay in lock-step with the ``CCModel`` / ``EffortLevel`` enums, and
every arg-builder must emit ``--model`` always and ``--effort`` only for models
that actually use one (Haiku does not).

This ties to the 2026-07-02 audit that:
  * added Fable 5 as a first-class tier (``CCModel.FABLE``),
  * corrected the effort ceilings — Sonnet 5 accepts the full low..max range
    (the old code capped Sonnet at HIGH), and Haiku uses no effort setting, so
    ``--effort`` is omitted for it entirely rather than clamped.

The AST scan at the bottom is the mechanical guardrail: if a *new* selection
surface hardcodes ``{"opus", "sonnet", "haiku"}`` instead of deriving from
``VALID_MODEL_NAMES``, this test fails until it's fixed — enforcement over a
hand-maintained site list.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import genesis
from genesis.cc.types import (
    VALID_EFFORT_NAMES,
    VALID_MODEL_NAMES,
    CCInvocation,
    CCModel,
    EffortLevel,
    clamp_effort,
    model_supports_effort,
)

# Models that do NOT use an effort setting — the CLI tolerates --effort but it's
# a no-op, so every arg-builder must omit it. Kept here so the expectation is
# asserted in one place.
_EFFORTLESS_MODELS = {CCModel.HAIKU}


def test_ccmodel_and_effort_enum_shape():
    assert {m.value for m in CCModel} == {"sonnet", "opus", "haiku", "fable"}
    assert {e.value for e in EffortLevel} == {"low", "medium", "high", "xhigh", "max"}


def test_canonical_sets_derive_from_enums():
    assert frozenset(m.value for m in CCModel) == VALID_MODEL_NAMES
    assert frozenset(e.value for e in EffortLevel) == VALID_EFFORT_NAMES
    assert "fable" in VALID_MODEL_NAMES
    assert {"xhigh", "max"} <= VALID_EFFORT_NAMES


def test_model_supports_effort_haiku_is_the_only_exception():
    for model in CCModel:
        assert model_supports_effort(model) is (model not in _EFFORTLESS_MODELS)


@pytest.mark.parametrize("model", [CCModel.OPUS, CCModel.SONNET, CCModel.FABLE])
@pytest.mark.parametrize("effort", list(EffortLevel))
def test_clamp_allows_full_range_for_effort_models(model, effort):
    # Opus / Sonnet 5 / Fable 5 accept the full low..max range — clamp is a no-op.
    assert clamp_effort(model, effort) == effort


@pytest.mark.parametrize(
    "full_name,expected",
    [
        ("claude-fable-5", CCModel.FABLE),
        ("claude-sonnet-5", CCModel.SONNET),
        ("claude-opus-4-8", CCModel.OPUS),
        ("claude-haiku-4-5", CCModel.HAIKU),
    ],
)
def test_from_full_name_current_ids(full_name, expected):
    assert CCModel.from_full_name(full_name) == expected


@pytest.mark.parametrize("model", list(CCModel))
def test_invoker_build_args_emits_model_and_gated_effort(model):
    from genesis.cc.invoker import CCInvoker

    invoker = CCInvoker(claude_path="claude")
    inv = CCInvocation(prompt="hi", model=model, effort=EffortLevel.MAX)
    args = invoker._build_args(inv)

    # --model is always emitted on the native path (no roster override).
    assert "--model" in args
    assert args[args.index("--model") + 1] == model.value

    # --effort is emitted iff the model uses one.
    assert ("--effort" in args) is model_supports_effort(model)
    if model_supports_effort(model):
        assert args[args.index("--effort") + 1] == "max"


@pytest.mark.parametrize(
    "model,expect_effort",
    [("sonnet", True), ("fable", True), ("opus", True), ("haiku", False)],
)
def test_ipc_remote_command_gates_effort(model, expect_effort):
    from genesis.modules.external.config import IPCConfig
    from genesis.modules.external.ipc import SshIPCAdapter

    adapter = SshIPCAdapter(IPCConfig(ssh_host="host", remote_claude_path="claude"))
    cmd = adapter._build_remote_command(model, "max")
    assert f"--model {model}" in cmd
    assert ("--effort" in cmd) is expect_effort


def test_module_level_validators_use_canonical_sets():
    import genesis.campaigns.control as campaigns
    import genesis.experimentation.cc_router as cc_router
    import genesis.mcp.health.session_control as session_control

    assert campaigns.VALID_MODELS == VALID_MODEL_NAMES
    assert campaigns.VALID_EFFORTS == VALID_EFFORT_NAMES
    assert cc_router._VALID_MODELS == VALID_MODEL_NAMES
    assert cc_router._VALID_EFFORTS == VALID_EFFORT_NAMES
    assert session_control._VALID_MODELS == VALID_MODEL_NAMES
    assert session_control._VALID_EFFORTS == VALID_EFFORT_NAMES


def test_campaign_resolvers_accept_full_roster():
    from genesis.campaigns.control import resolve_effort, resolve_model

    assert resolve_model("fable") is CCModel.FABLE
    assert resolve_model("sonnet") is CCModel.SONNET
    assert resolve_effort("max") is EffortLevel.MAX
    assert resolve_effort("xhigh") is EffortLevel.XHIGH


def test_ego_config_accepts_fable_and_full_effort_range():
    from genesis.ego.config import validate_ego_config

    assert validate_ego_config({"model": "fable"}) == []
    assert validate_ego_config({"default_effort": "max"}) == []
    assert validate_ego_config({"default_effort": "xhigh"}) == []
    assert validate_ego_config({"dispatch_model_overrides": {"investigate": "fable"}}) == []


def test_settings_validators_accept_fable_and_full_effort():
    from genesis.mcp.health.settings import _validate_channels

    assert _validate_channels(
        {"telegram": {"default_model": "fable", "default_effort": "max"}}
    ) == []


# ── Mechanical drift guard ──────────────────────────────────────────────────
# A set/frozenset/dict literal that enumerates the model tiers as strings is a
# hardcoded selection surface — it must instead derive from VALID_MODEL_NAMES /
# the CCModel enum. This AST scan catches those literal shapes. It CANNOT catch
# regex alternations (cc/intent.py) or if/elif comparison chains — those are
# guarded behaviorally below (test_intent_* / test_telegram_choices_*) and,
# structurally, by having those surfaces derive from the enum. HTML/JS selection
# surfaces (dashboard templates) are out of AST scope entirely.
# Files with a legitimate non-selection use of these tokens are allowlisted.
_MODEL_TOKENS = {"opus", "sonnet", "haiku"}
_ALLOWLIST = {
    # Contact-name stopword set — "opus"/"sonnet"/"haiku" are filtered as noise
    # words, not a model-selection surface.
    "memory/contact_tracker.py",
}


def _string_literal_elements(node: ast.AST) -> set[str] | None:
    """String elements of a set/frozenset/list/tuple/dict-keys literal, else None.

    Dict nodes contribute their string *keys* (the old telegram ``model_map`` /
    ``effort_map`` shape). Returns None for anything that isn't a pure
    string-literal collection.
    """
    elts: list[ast.expr] | None = None
    if isinstance(node, ast.Set):
        elts = node.elts
    elif isinstance(node, ast.Dict):
        elts = [k for k in node.keys if k is not None]  # skip **spread (key None)
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"set", "frozenset"}
        and len(node.args) == 1
        and isinstance(node.args[0], (ast.Set, ast.List, ast.Tuple))
    ):
        elts = node.args[0].elts
    if elts is None:
        return None
    values: set[str] = set()
    for e in elts:
        if isinstance(e, ast.Constant) and isinstance(e.value, str):
            values.add(e.value)
        else:
            return None  # not a pure string-literal collection
    return values


def _flags_tier_literal(node: ast.AST) -> bool:
    """True if node is a string-literal set/dict/frozenset whose elements are —
    CASE-FOLDED — a superset of the model tokens (the hardcoded-selection
    antipattern). Case folding matters: the routing registry used a Capitalized
    ``{"Haiku","Sonnet","Opus"}`` set that a case-sensitive scan missed."""
    values = _string_literal_elements(node)
    return values is not None and {v.lower() for v in values} >= _MODEL_TOKENS


def test_no_hardcoded_model_tier_literals_outside_allowlist():
    pkg_root = Path(genesis.__file__).resolve().parent
    offenders: list[str] = []
    for py in pkg_root.rglob("*.py"):
        rel = py.relative_to(pkg_root).as_posix()
        if rel in _ALLOWLIST:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        offenders += [
            f"{rel}:{getattr(node, 'lineno', '?')}"
            for node in ast.walk(tree)
            if _flags_tier_literal(node)
        ]
    assert not offenders, (
        "Hardcoded model-tier set/dict literal(s) found — derive from "
        "genesis.cc.types.VALID_MODEL_NAMES / the CCModel enum instead:\n  "
        + "\n  ".join(offenders)
    )


def test_ast_scan_self_check_flags_planted_literals():
    """The scan must catch dict-keyed AND Capitalized model maps — regressions
    for the two blind spots that shipped earlier (the telegram/cmd_effort dict,
    and the Capitalized routing ``_VALID_CC_MODELS`` set)."""
    for src in (
        'M = {"opus": 1, "sonnet": 2, "haiku": 3}',  # dict keys
        'M = {"Opus", "Sonnet", "Haiku"}',           # capitalized set
    ):
        tree = ast.parse(src)
        assert any(_flags_tier_literal(n) for n in ast.walk(tree)), (
            f"AST scan failed to flag: {src}"
        )


# JS/HTML dashboard templates are out of AST reach; scan them textually so a
# model-tier dropdown that omits a tier fails here too (regression for the
# neural_monitor.html routing selector missed in the first passes).
_JS_ARRAY_RE = re.compile(r"\[([^\[\]]*)\]")
_JS_QUOTED_RE = re.compile(r"""['"]([A-Za-z]+)['"]""")


def test_dashboard_templates_offer_every_model_tier():
    tpl_dir = Path(genesis.__file__).resolve().parent / "dashboard" / "templates"
    offenders: list[str] = []
    for html in tpl_dir.glob("*.html"):
        text = html.read_text(encoding="utf-8")
        for arr in _JS_ARRAY_RE.finditer(text):
            toks = {q.group(1).lower() for q in _JS_QUOTED_RE.finditer(arr.group(1))}
            if toks >= _MODEL_TOKENS and "fable" not in toks:
                offenders.append(f"{html.name}: [{arr.group(1).strip()[:60]}]")
    assert not offenders, (
        "Dashboard model-tier dropdown(s) missing 'fable' (JS array):\n  "
        + "\n  ".join(offenders)
    )


def test_intent_parser_recognizes_every_tier_and_effort():
    """Foreground /model + /effort regexes are AST-invisible; guard behaviorally."""
    from genesis.cc.intent import IntentParser

    parser = IntentParser()
    for m in CCModel:
        assert parser.parse(f"/model {m.value}").model_override == m
    for e in EffortLevel:
        assert parser.parse(f"/effort {e.value}").effort_override == e


def test_telegram_choice_strings_cover_the_roster():
    """Telegram /model + /effort help/usage text derive from the enums."""
    import genesis.channels.telegram._handler_commands as tg

    assert set(tg._MODEL_CHOICES.split("|")) == VALID_MODEL_NAMES
    assert set(tg._EFFORT_CHOICES.split("|")) == VALID_EFFORT_NAMES
