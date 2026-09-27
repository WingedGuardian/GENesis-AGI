"""Decision question registry — one enumerable home for every bounded choice.

Mirrors ``routing/config.py``: a shipped YAML plus a gitignored
``{stem}.local.yaml`` overlay, deep-merged. That seam is deliberate — the
question set is capability and ships; anything install-specific stays local.

The registry earns its place by making four otherwise-manual jobs mechanical:

1. **Label extraction** knows what to mine per site (``outcome_source``).
2. **Calibration fitting** gets its natural unit — one temperature per site.
3. **Task-adapter training** learns question *shapes* without seeing content.
4. "How many decisions does Genesis make, and which are calibrated?" becomes a
   query rather than an archaeology project.

The question wording lives here rather than inline at call sites because it
is shared across backends: the same spec drives a hosted or a local decision
model unchanged, and wording materially moves accuracy — the same model scored
very differently on one egress question depending on how it was phrased.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from genesis._config_overlay import _resolve_overlay_path
from genesis.decisions.types import (
    CARDINALITY_SOFT_CAP,
    Consumes,
    DecisionSpec,
    Fallback,
    QuestionType,
    band_problem,
)

__all__ = ["RegistryError", "load_registry", "load_registry_from_string"]


class RegistryError(ValueError):
    """A spec violates a registry rule. Raised at load, never at call time."""


def _fail(name: str, msg: str) -> None:
    raise RegistryError(f"decision {name!r}: {msg}")


def _enum(name: str, field: str, raw: Any, enum_cls: type) -> Any:
    try:
        return enum_cls(str(raw))
    except ValueError:
        allowed = ", ".join(sorted(m.value for m in enum_cls))
        _fail(name, f"{field}={raw!r} is not one of: {allowed}")


#: Every key a spec may carry. ALLOWLIST, deliberately: a denylist cannot
#: catch `thrshold: 0.9`, which loads clean and silently disables the gate the
#: author believed they had declared.
_ALLOWED_KEYS = frozenset(
    {
        "type",
        "instructions",
        "consumes",
        "fallback",
        "owner",
        "options",
        "criteria",
        "threshold",
        "dead_band",
        "tie_rule",
        "latency_budget_ms",
        "outcome_source",
        "cardinality_strategy",
    }
)

#: Closed set. An unrecognised strategy satisfied the soft-cap rule while
#: naming a mechanism nothing implements.
_STRATEGIES = frozenset({"shortlist", "hierarchical"})


def _require_string_keys(owner: str, kind: str, mapping: Mapping[Any, Any]) -> None:
    """Refuse non-string keys rather than canonicalise them with ``str()``.

    YAML resolves ``1`` to an int and bare ``yes`` / ``no`` to booleans, so
    ``str()`` both collides distinct keys (``1`` and ``"1"`` become one entry,
    the later silently replacing the earlier) and rewrites labels (``no``
    becomes ``"False"``). Quote the key instead.
    """
    bad = [k for k in mapping if not isinstance(k, str)]
    if bad:
        raise RegistryError(
            f"{owner}: {kind} key(s) {bad!r} are not strings — quote them; YAML "
            "reads bare numbers and yes/no as non-strings"
        )


def _text(name: str, field: str, value: Any, *, required: bool = True) -> str | None:
    """A text field, which must BE text. The sibling of `_require_string_keys`.

    ``str()`` on a value rewrote exactly as it did on keys: ``owner: yes``
    became ``"True"``, an overlay's ``skip: null`` became the description
    ``"None"``, and ``outcome_source: 0`` quietly became no source at all.
    """
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        _fail(
            name,
            f"{field}={value!r} must be a non-empty string — quote it; YAML reads "
            "bare yes/no/numbers/null as non-strings, and an overlay cannot delete "
            "a field by setting it to null",
        )
    return value.strip()


def _parse_one(name: str, raw: Mapping[str, Any]) -> DecisionSpec:
    if not isinstance(raw, Mapping):
        _fail(name, "spec must be a mapping")

    unknown = sorted(str(k) for k in set(raw) - _ALLOWED_KEYS)
    if unknown:
        _fail(
            name,
            f"unknown key(s) {unknown} — a typo here loads clean and silently "
            f"disables whatever it was meant to declare; allowed: "
            f"{sorted(_ALLOWED_KEYS)}",
        )

    qtype = _enum(name, "type", raw.get("type"), QuestionType)
    consumes = _enum(name, "consumes", raw.get("consumes"), Consumes)

    # The wording IS the capability; the owner is where a finding gets routed.
    instructions = _text(name, "instructions", raw.get("instructions"))
    owner = _text(name, "owner", raw.get("owner"))

    fb = raw.get("fallback")
    if isinstance(fb, Mapping) and set(fb) - {"typed", "legacy"}:
        # An overlay's misspelled `tyepd:` merges beside the shipped `typed:`,
        # so without this the intended local fallback silently never applies.
        extra = sorted(str(k) for k in set(fb) - {"typed", "legacy"})
        _fail(name, f"fallback has unknown key(s) {extra}")
    if not isinstance(fb, Mapping) or not fb.get("typed") or not fb.get("legacy"):
        _fail(
            name,
            "fallback.typed and fallback.legacy are both required — a site that "
            "cannot say what it does without a model is not ready to be a "
            "decision site",
        )
    fallback = Fallback(
        typed=_text(name, "fallback.typed", fb["typed"]),
        legacy=_text(name, "fallback.legacy", fb["legacy"]),
    )

    options: Mapping[str, str] = MappingProxyType({})
    criteria: tuple[str, ...] = ()
    # Each arm rejects the OTHER arm's field. Accepting and discarding it
    # means a spec that reads as configured behaves as if it were not.
    if qtype is QuestionType.CHOICE:
        if raw.get("criteria") is not None:
            _fail(name, "a choice declares options, not criteria")
        opts = raw.get("options")
        if not isinstance(opts, Mapping) or len(opts) < 2:
            _fail(name, "a choice needs an options mapping with at least 2 entries")
        _require_string_keys(name, "option", opts)
        options = MappingProxyType({k: _text(name, f"options.{k}", v) for k, v in opts.items()})
    elif qtype is QuestionType.SCORE:
        if raw.get("options") is not None:
            _fail(name, "a score declares criteria, not options")
        crit = raw.get("criteria")
        if not isinstance(crit, (list, tuple)) or len(crit) < 2:
            _fail(name, "a score needs an ordered criteria list with at least 2 levels")
        criteria = tuple(_text(name, f"criteria[{i}]", x) for i, x in enumerate(crit))
        if len(set(criteria)) != len(criteria):
            # Two identical levels make the ordinal answer ambiguous.
            _fail(name, f"criteria must be distinct levels; got {list(criteria)}")
    else:  # NOUL
        if raw.get("options") is not None or raw.get("criteria") is not None:
            _fail(name, "a noul is binary and must not declare options or criteria")

    # The linter rule. `consumes` exists so this is enforced rather than noticed.
    # A bare cut is not enough: scores near it are not repeatable on replay, so
    # a thresholding site also declares the band it abstains in and the rule
    # for scores landing exactly on a band edge. The checks live in
    # `band_problem`, shared with `DecisionSpec.gate`, so a directly
    # constructed spec cannot skip them.
    threshold = raw.get("threshold", None)
    dead_band = raw.get("dead_band", None)
    tie_rule = raw.get("tie_rule", None)
    if consumes is Consumes.THRESHOLD:
        for key, value in (
            ("threshold", threshold),
            ("dead_band", dead_band),
            ("tie_rule", tie_rule),
        ):
            if value is None:
                _fail(
                    name,
                    f"consumes=threshold requires an explicit {key} — decisions near a "
                    "cut flip on an identical retry, so the site must declare its cut, "
                    "the band it abstains in, and its edge tie rule",
                )
        if isinstance(tie_rule, str):
            tie_rule = tie_rule.strip()
        problem = band_problem(threshold, dead_band, tie_rule)
        if problem is not None:
            _fail(name, problem)
        threshold, dead_band = float(threshold), float(dead_band)
    else:
        for key, value in (
            ("threshold", threshold),
            ("dead_band", dead_band),
            ("tie_rule", tie_rule),
        ):
            if value is not None:
                _fail(
                    name,
                    f"{key}={value!r} is set but consumes={consumes.value} — a stray "
                    f"{key} means gating was expected but never declared",
                )

    strategy = raw.get("cardinality_strategy")
    if strategy is not None:
        strategy = str(strategy).strip()
        if strategy not in _STRATEGIES:
            _fail(
                name,
                f"cardinality_strategy={strategy!r} is not one of: "
                f"{sorted(_STRATEGIES)} — an unrecognised value satisfied the "
                "soft-cap rule while naming a mechanism nothing implements",
            )

    # The token budget is shared by the answer space whatever the primitive,
    # so a 50-level score hits the same wall a 50-option choice does.
    answer_space = len(options) if qtype is QuestionType.CHOICE else len(criteria)
    if answer_space > CARDINALITY_SOFT_CAP and not strategy:
        _fail(
            name,
            f"cardinality {answer_space} exceeds the soft cap of "
            f"{CARDINALITY_SOFT_CAP}; declare a cardinality_strategy "
            f"({' | '.join(sorted(_STRATEGIES))}) — the answer space shares a "
            "fixed token budget, so labels stop being distinguishable as it grows",
        )

    budget = raw.get("latency_budget_ms")
    if budget is not None:
        # bool is an int subclass; float('inf') raises OverflowError rather
        # than ValueError; and a plain float SILENTLY TRUNCATES (3.9 -> 3).
        # All three escape a naive int() coercion, and the truncation is the
        # quiet one — it produces a budget the author never wrote.
        if isinstance(budget, bool) or not isinstance(budget, (int, str)):
            _fail(
                name,
                f"latency_budget_ms={budget!r} must be an integer — a float is "
                "rejected rather than truncated, so a budget is never silently "
                "changed to one nobody declared",
            )
        try:
            budget = int(budget)
        except (TypeError, ValueError, OverflowError):
            _fail(name, f"latency_budget_ms={budget!r} is not an integer")
        if budget <= 0:
            _fail(name, "latency_budget_ms must be positive")

    return DecisionSpec(
        name=name,
        type=qtype,
        instructions=instructions,
        consumes=consumes,
        fallback=fallback,
        owner=owner,
        options=options,
        criteria=criteria,
        threshold=threshold if consumes is Consumes.THRESHOLD else None,
        dead_band=dead_band if consumes is Consumes.THRESHOLD else None,
        tie_rule=tie_rule if consumes is Consumes.THRESHOLD else None,
        latency_budget_ms=budget,
        outcome_source=_text(name, "outcome_source", raw.get("outcome_source"), required=False),
        cardinality_strategy=str(strategy) if strategy else None,
    )


#: The only top-level key. A misspelled `decision:` beside the real one would
#: otherwise load clean while everything under it is dropped.
_TOP_LEVEL_KEYS = frozenset({"decisions"})


def _check_root(raw: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise RegistryError(f"{where} must be a mapping with a 'decisions' key")
    unknown = sorted(str(k) for k in set(raw) - _TOP_LEVEL_KEYS)
    if unknown:
        raise RegistryError(f"{where} has unknown top-level key(s) {unknown}; allowed: decisions")
    return raw


def _parse(raw: Any) -> Mapping[str, DecisionSpec]:
    raw = _check_root(raw, "registry")
    decisions = raw.get("decisions")
    if decisions is None:
        raise RegistryError(
            "registry has no 'decisions' mapping (missing, or set to null — an "
            "overlay cannot clear the shipped decisions)"
        )
    if not isinstance(decisions, Mapping):
        raise RegistryError("'decisions' must be a mapping of name -> spec")
    _require_string_keys("registry", "decision", decisions)
    out: dict[str, DecisionSpec] = {}
    for name, spec in decisions.items():
        out[name] = _parse_one(name, spec)
    return MappingProxyType(out)


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate mapping keys.

    YAML silently keeps the last of two identical keys, so a duplicated
    decision id would drop a spec with no diagnostic. An earlier version
    scanned the raw text for two-space-indented keys; that was a DENYLIST and
    five valid-YAML spellings walked straight past it (4-space indent, a
    quoted key, ``decisions :`` with a space before the colon, flow style, a
    trailing comment on the key line) while the overlay was never scanned at
    all.

    Checking at construction time is closed-set: it sees *resolved* keys after
    YAML has done its own parsing, so every spelling collapses to the same
    check. Subclassing ``SafeLoader`` inherits its constructor table, so no
    arbitrary-object tags are enabled by this.
    """


def _no_duplicate_keys(loader: _StrictLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=False)
        if key in mapping:
            raise RegistryError(f"duplicate key {key!r} at line {key_node.start_mark.line + 1}")
        mapping[key] = loader.construct_object(value_node, deep=False)
    return mapping


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys)


def _deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Recursive merge, matching what ``routing/config.py`` does.

    A per-decision *replace* would make a partial override — an overlay
    supplying only ``owner:`` — fail validation for every field it did not
    restate, which is the opposite of what an overlay is for.
    """
    out = dict(base)
    for key, value in overlay.items():
        prior = out.get(key)
        if isinstance(prior, Mapping) and isinstance(value, Mapping):
            out[key] = _deep_merge(prior, value)
        else:
            out[key] = value
    return out


def load_registry_from_string(text: str) -> Mapping[str, DecisionSpec]:
    """Parse a registry from YAML text. No overlay support."""
    return _parse(yaml.load(text, Loader=_StrictLoader))  # noqa: S506 — SafeLoader subclass


def load_registry(path: str | Path) -> Mapping[str, DecisionSpec]:
    """Load ``path``, deep-merging its ``{stem}.local.yaml`` overlay if present.

    The overlay is FOUND by the shared resolver, so it lives where every other
    config overlay does (``~/.genesis/config/`` first, then a repo sibling) and
    where the settings writers put it. It is PARSED strictly, unlike the shared
    merge, which degrades a bad overlay to a warning: a registry that silently
    drops an override has changed a gate's behaviour without saying so.

    An overlay may override shipped decisions but never add one, so a decision
    retired upstream cannot be resurrected by an overlay written before it was
    retired.
    """
    path = Path(path)
    base = _check_root(yaml.load(path.read_text(), Loader=_StrictLoader), path.name)  # noqa: S506

    overlay_path = _resolve_overlay_path(path)
    if overlay_path.is_file():
        overlay = yaml.load(overlay_path.read_text(), Loader=_StrictLoader)  # noqa: S506
        if overlay is not None:  # an empty file is "no overrides"; [] / false / 0 are errors
            overlay = _check_root(overlay, overlay_path.name)
            shipped = base.get("decisions")
            added = overlay.get("decisions")
            if isinstance(shipped, Mapping) and isinstance(added, Mapping):
                extra = sorted(str(k) for k in set(added) - set(shipped))
                if extra:
                    raise RegistryError(
                        f"{overlay_path.name} overrides decision(s) {extra} that the "
                        "shipped registry does not define — an overlay may only "
                        "override, so a retired decision cannot come back through it"
                    )
            base = _deep_merge(base, overlay)

    return _parse(base)
