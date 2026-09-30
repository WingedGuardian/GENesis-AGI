"""Decision question registry — the enumerable home for bounded-choice decisions.

Mirrors ``routing/config.py``: a shipped YAML plus a gitignored
``{stem}.local.yaml`` overlay, deep-merged. That seam is deliberate — the
question set is capability and ships; anything install-specific stays local.

The registry earns its place by making four otherwise-manual jobs mechanical:

1. **Label extraction** knows what to mine per site (``outcome_source``),
   once a site has a persisted signal. None does yet; the field stays unset
   until one exists.
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

import dataclasses
import re
from collections.abc import Hashable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from pydantic import ValidationError

from genesis.decisions.types import DecisionSpec

__all__ = ["RegistryError", "load_registry", "load_registry_from_string"]


class RegistryError(ValueError):
    """A spec violates a registry rule. Raised at load, never at call time."""


#: Every key a spec may carry, DERIVED from the model so the two cannot drift.
#: Checked before construction only to give a typo a readable message; the
#: model forbids extra fields on its own.
_ALLOWED_KEYS = frozenset(f.name for f in dataclasses.fields(DecisionSpec)) - {"name"}

#: The only top-level key. A misspelled `decision:` beside the real one would
#: otherwise load clean while everything under it is dropped.
_TOP_LEVEL_KEYS = frozenset({"decisions"})


def _describe(exc: ValidationError) -> str:
    """One line per failure: which field, and what was wrong with it."""
    parts = []
    for err in exc.errors(include_url=False):
        loc = ".".join(str(x) for x in err.get("loc", ()))
        msg = str(err.get("msg", "")).removeprefix("Value error, ")
        if isinstance(err.get("input"), bool):
            msg += " (YAML read a bare yes/no/true/false as a boolean; quote it)"
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


def _parse_one(name: Any, raw: Any) -> DecisionSpec:
    if not isinstance(raw, Mapping):
        raise RegistryError(f"decision {name!r}: spec must be a mapping")
    unknown = sorted(str(k) for k in set(raw) - _ALLOWED_KEYS)
    if unknown:
        raise RegistryError(
            f"decision {name!r}: unknown key(s) {unknown} — a typo here loads clean "
            f"and silently disables whatever it was meant to declare; allowed: "
            f"{sorted(_ALLOWED_KEYS)}"
        )
    try:
        return DecisionSpec(name=name, **raw)
    except ValidationError as exc:
        raise RegistryError(f"decision {name!r}: {_describe(exc)}") from None


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
    return MappingProxyType({name: _parse_one(name, spec) for name, spec in decisions.items()})


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader narrowed to the YAML a registry actually needs.

    Four YAML features let a file load clean while meaning something other
    than it says, so each is refused at construction time:

    - **Duplicate keys.** YAML keeps the last of two identical keys, dropping
      a spec with no diagnostic. Checked on RESOLVED keys, so every spelling
      (indent, quoting, flow style) collapses to one check; an earlier text
      scan was a denylist that five valid spellings walked past.
    - **Aliases and merge keys** (``*a``, ``<<:``). A registry never needs
      them, and they build self-referencing structures and hidden duplicates.
    - **Non-canonical numbers.** YAML 1.1 reads ``0200`` as octal 128,
      ``1:30`` as 90, ``0x10`` as 16, ``1_000`` as 1000, ``0:0.5`` as 0.5
      and ``0.0_5`` as 0.05. Integers and floats must be plain decimal.
    - **Unhashable keys** (``? [x]``), which would otherwise surface as a bare
      ``TypeError``.

    Subclassing ``SafeLoader`` inherits its constructor table, so no
    arbitrary-object tags are enabled by this.
    """

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(yaml.events.AliasEvent):
            event = self.peek_event()
            raise RegistryError(
                f"alias *{event.anchor} at line {event.start_mark.line + 1} — anchors "
                "and aliases are not supported in the decision registry"
            )
        return super().compose_node(parent, index)


_MERGE_TAG = "tag:yaml.org,2002:merge"
_CANONICAL_INT = re.compile(r"[-+]?(0|[1-9][0-9]*)")
_MAX_INT_DIGITS = 18
_CANONICAL_FLOAT = re.compile(r"[-+]?(0|[1-9][0-9]*)\.[0-9]+([eE][-+]?[0-9]+)?")


def _no_duplicate_keys(loader: _StrictLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        line = key_node.start_mark.line + 1
        if key_node.tag == _MERGE_TAG:
            raise RegistryError(f"merge key '<<' at line {line} is not supported")
        key = loader.construct_object(key_node, deep=False)
        if not isinstance(key, Hashable):
            raise RegistryError(f"key {key!r} at line {line} is not a scalar")
        if key in mapping:
            raise RegistryError(f"duplicate key {key!r} at line {line}")
        mapping[key] = loader.construct_object(value_node, deep=False)
    return mapping


def _canonical_int(loader: _StrictLoader, node: yaml.ScalarNode) -> int:
    text = loader.construct_scalar(node)
    if not _CANONICAL_INT.fullmatch(text):
        raise RegistryError(
            f"integer {text!r} at line {node.start_mark.line + 1} is not plain decimal — "
            "YAML 1.1 reads leading zeros as octal and colons as base 60"
        )
    # An explicit bound, not Python's int-string digit limit, which is an
    # interpreter setting (PYTHONINTMAXSTRDIGITS=0 disables it). No registry
    # integer needs more than 18 digits.
    if len(text.lstrip("+-")) > _MAX_INT_DIGITS:
        raise RegistryError(
            f"integer at line {node.start_mark.line + 1} has more than {_MAX_INT_DIGITS} digits"
        )
    return int(text)


def _canonical_float(loader: _StrictLoader, node: yaml.ScalarNode) -> float:
    text = loader.construct_scalar(node)
    if not _CANONICAL_FLOAT.fullmatch(text):
        raise RegistryError(
            f"number {text!r} at line {node.start_mark.line + 1} is not plain decimal — "
            "YAML 1.1 reads colons as base 60 and ignores underscores"
        )
    return float(text)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys)
_StrictLoader.add_constructor("tag:yaml.org,2002:float", _canonical_float)
_StrictLoader.add_constructor("tag:yaml.org,2002:int", _canonical_int)


def _load(text: str, where: str) -> Any:
    """The one YAML entry point: every parse failure becomes a RegistryError."""
    try:
        return yaml.load(text, Loader=_StrictLoader)  # noqa: S506 — SafeLoader subclass
    except RegistryError as exc:
        raise RegistryError(f"{where}: {exc}") from None
    except Exception as exc:
        # Deliberately broad. Parsing runs PyYAML's inherited constructors,
        # and what they raise on bad input is not a closed set: a bad date
        # gives ValueError, `!!timestamp nope` AttributeError, `!!bool abc`
        # KeyError, besides YAMLError and RecursionError. Listing types was
        # a denylist that each review extended; at this boundary ANY failure
        # means the file is not a valid registry.
        raise RegistryError(
            f"{where} is not valid registry YAML: {type(exc).__name__}: {exc}"
        ) from exc  # keep the cause: a bug in this loader's own constructors must stay traceable


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


def _reject_nulls(node: Any, where: str, trail: str = "") -> None:
    """Refuse a null ANYWHERE in an overlay.

    Deep-merge applies an overlay's ``null`` before validation, after which it
    is indistinguishable from a field the shipped spec never set, so a null
    silently deletes a declared constraint (``outcome_source``,
    ``latency_budget_ms``, ``cardinality_strategy``, an option). One walk over
    the whole overlay closes that class instead of guarding fields one by one.
    An overlay overrides values; it never deletes them.
    """
    if node is None:
        raise RegistryError(
            f"{where}: {trail or 'root'} is null — an overlay cannot delete a "
            "shipped field; override it with a value or remove the line"
        )
    if isinstance(node, Mapping):
        for key, value in node.items():
            _reject_nulls(value, where, f"{trail}.{key}" if trail else str(key))
    elif isinstance(node, (list, tuple)):
        for i, value in enumerate(node):
            _reject_nulls(value, where, f"{trail}[{i}]")


def _reject_additions(shipped: Any, overlay: Any, where: str) -> None:
    """An overlay overrides; it never grows the question set or an answer space.

    Adding a decision would let a retired one survive in an old overlay, and
    adding an option would change what a site can answer without the shipped
    spec ever saying so.
    """
    if not (isinstance(shipped, Mapping) and isinstance(overlay, Mapping)):
        return
    extra = sorted(str(k) for k in set(overlay) - set(shipped))
    if extra:
        raise RegistryError(
            f"{where} overrides decision(s) {extra} that the shipped registry does "
            "not define — an overlay may only override, so a retired decision "
            "cannot come back through it"
        )
    for name, spec in overlay.items():
        reshaped = sorted(_SHAPE_FIELDS & set(spec)) if isinstance(spec, Mapping) else []
        if reshaped:
            raise RegistryError(
                f"{where}: decision {name!r} overrides {reshaped} — those define what "
                "the question IS; an overlay may reword or retune it, not change it"
            )
        base_opts = shipped[name].get("options") if isinstance(shipped[name], Mapping) else None
        ov_opts = spec.get("options") if isinstance(spec, Mapping) else None
        if isinstance(base_opts, Mapping) and isinstance(ov_opts, Mapping):
            new = sorted(str(k) for k in set(ov_opts) - set(base_opts))
            if new:
                raise RegistryError(
                    f"{where}: decision {name!r} adds option(s) {new} — an overlay may "
                    "reword an option but not change the answer space"
                )


def _read(path: Path) -> str:
    """Read a registry file; a bad encoding or an unreadable file is a RegistryError."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RegistryError(f"{path.name} cannot be read: {exc}") from None


#: Fields that define WHAT a question is, rather than tune how it is asked.
#: An overlay may reword and retune; changing one of these would silently turn
#: a shipped question into a different one (a reversed score scale, an argmax
#: site promoted to threshold) under the same name.
_SHAPE_FIELDS = frozenset({"type", "consumes", "criteria"})


def load_registry_from_string(text: str) -> Mapping[str, DecisionSpec]:
    """Parse a registry from YAML text. No overlay support."""
    return _parse(_load(text, "registry"))


def load_registry(path: str | Path) -> Mapping[str, DecisionSpec]:
    """Load ``path``, deep-merging its ``{stem}.local.yaml`` overlay if present.

    The overlay is FOUND by the shared resolver, so it lives where every other
    config overlay does (``~/.genesis/config/`` first, then a repo sibling) and
    where the settings writers put it. It is PARSED strictly, unlike the shared
    merge, which degrades a bad overlay to a warning: a registry that silently
    drops an override has changed a gate's behaviour without saying so.

    An overlay may override shipped decisions, reword their options and retune
    them, but never add a decision or an option, nor change a question's
    type, what it consumes, or its criteria.
    """
    path = Path(path)
    base = _check_root(_load(_read(path), path.name), path.name)
    # The shipped registry must be valid ON ITS OWN. Validating only the
    # merged result let an overlay supply what the shipped file lacked (a
    # missing options map, or a whole decisions mapping), so whether the
    # shipped registry was valid depended on one install's local config.
    try:
        _parse(base)
    except RegistryError as exc:
        raise RegistryError(f"{path.name} (shipped, before any overlay): {exc}") from None

    # Function-local on purpose: a module-level alias would hold its own
    # reference that test isolation of the user config dir cannot reach.
    from genesis._config_overlay import _resolve_overlay_path

    overlay_path = _resolve_overlay_path(path)
    if overlay_path.is_file():
        overlay = _load(_read(overlay_path), overlay_path.name)
        if overlay is not None:  # an empty file is "no overrides"; [] / false / 0 are errors
            overlay = _check_root(overlay, overlay_path.name)
            _reject_nulls(overlay, overlay_path.name)
            _reject_additions(base.get("decisions"), overlay.get("decisions"), overlay_path.name)
            base = _deep_merge(base, overlay)

    return _parse(base)
