"""Peer JSON disclosure checks, including serialized JSON within string values."""

import itertools
import json
import re

from genesis.security.output_scanner import scan_outbound

_MAX_JSON_BYTES = 2 * 1024 * 1024
_MAX_SCAN_WORK = 32 * _MAX_JSON_BYTES
_MAX_NESTING = 1024
_CONTAINER = re.compile(r'[\{\[]\s*(?=["\{\[\]\}\d-]|true\b|false\b|null\b)')
_ESCAPE = re.compile(r'\\(?:u([0-9a-fA-F]{4})|(["\\/bfnrt]))')
_SIMPLE_ESCAPES = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


class _Unescaped(str):
    """Escape-decoded text: scanned as a leaf, never parsed as JSON."""


def _unescape(text):
    """Text with its JSON escapes decoded, wherever they appear in prose."""
    return _ESCAPE.sub(
        lambda m: chr(int(m[1], 16)) if m[1] else _SIMPLE_ESCAPES.get(m[2], m[2]), text
    )


class _JSONPairs(list):
    """Keep every object member, including duplicate keys, when decoding strings."""


class _WorkBudget:
    def __init__(self, limit=_MAX_SCAN_WORK):
        self.remaining = limit

    def charge(self, size):
        self.remaining -= max(1, size)
        if self.remaining < 0:
            raise ValueError("Peer disclosure scan work limit exceeded")

    def scan(self, text):
        self.charge(len(text))
        return scan_outbound(text).safe


def json_strings_safe(value):
    """Refuse unsafe original or decoded JSON; fail closed on invalid or excessive work.

    Scope: accident hygiene for same-owner peers, at least as strict as the
    outbound scan on other egress. It is not a confidentiality boundary: a
    session a peer can steer can encode or paraphrase anything it can read, so
    what a peer may learn is bounded by the session profile's capabilities.

    Serialized disclosures are limited to 2 MiB; larger results are withheld.
    Aggregate scanning is bounded to 32 times that limit; unusually nested or
    repetitive structures can be refused even when their serialization fits.
    """
    budget = _WorkBudget()
    try:
        # Inspect primitive lengths without allocating their escaped JSON form.
        preflight = _WorkBudget(_MAX_JSON_BYTES)
        for scalar, _ in _json_leaves(value, preflight, decode=False):
            preflight.charge(len(scalar))
        size = 0
        for chunk in json.JSONEncoder(allow_nan=False).iterencode(value):
            size += len(chunk.encode("utf-8"))
            if size > _MAX_JSON_BYTES:
                return False
        for scalar, keys in _json_leaves(value, budget):
            if not budget.scan(scalar):
                return False
            for key in keys:
                budget.charge(len(key) + len(scalar) + 3)
                if not scan_outbound("".join((key, " = ", scalar))).safe:
                    return False
        return True
    except (TypeError, ValueError, RecursionError):
        return False


def _decode(item, budget):
    budget.charge(len(item))
    if item.lstrip().startswith(("{", "[", '"')):
        try:
            return json.loads(item, object_pairs_hook=_JSONPairs)
        except json.JSONDecodeError:
            pass  # Ordinary prose, including incomplete JSON, still gets scanned.
    return None


def _key_names(key, budget):
    """Preserve scalar key names; compound decoded keys are unsupported."""
    while isinstance(key, str):
        budget.charge(len(key))
        yield key
        unescaped = _unescape(key)
        if unescaped != key:
            yield from _key_names(unescaped, budget)
        key = _decode(key, budget)
        if isinstance(key, (dict, list)):
            raise ValueError("Compound peer JSON mapping keys are unsupported")


def _json_leaves(value, budget, *, decode=True):
    for item, keys in _json_nodes(value, budget, decode):
        if isinstance(item, str):
            yield item, keys
        elif type(item) in (int, float):
            yield json.dumps(item, allow_nan=False), keys
        # JSON validation rejects unsupported objects; booleans/null carry no secret value.


def _json_nodes(value, budget, decode):
    """Own bounded iterator frames and cycle detection independently of leaf policy."""
    pending = [(iter(((value, ()),)), None)]
    active = set()
    while pending:
        if len(pending) > _MAX_NESTING:
            raise ValueError("Peer JSON nesting limit exceeded")
        entry = next(pending[-1][0], None)
        if entry is None:
            _, identity = pending.pop()
            if identity is not None:
                active.remove(identity)
            continue
        item, keys = entry
        budget.charge(1 + len(keys))
        yield item, keys
        children = _descendants(item, keys, budget, decode)
        if children is not None:
            identity = id(item) if isinstance(item, (dict, list, tuple)) else None
            if identity in active:
                raise ValueError("Cyclic peer JSON")
            if identity is not None:
                active.add(identity)
            pending.append((children, identity))


def _descendants(item, keys, budget, decode):
    if isinstance(item, str):
        if not decode:
            return None
        if isinstance(item, _Unescaped):
            # Decoding can truncate quoted JSON; a fragment here is prose.
            children = _embedded(item, keys, budget, strict=False)
        else:
            decoded = _decode(item, budget)
            children = (
                iter(((decoded, keys),)) if decoded is not None else _embedded(item, keys, budget)
            )
        # Hygiene, not a boundary: JSON escapes in prose ("\/home") are also
        # scanned decoded. Each pass shortens the text, so this ends.
        budget.charge(len(item))
        unescaped = _unescape(item)
        if unescaped != item:
            return itertools.chain(children, ((_Unescaped(unescaped), keys),))
        return children
    if isinstance(item, (dict, _JSONPairs)):
        pairs = item.items() if isinstance(item, dict) else item
        return _members(pairs, keys, budget, decode)
    if isinstance(item, (list, tuple)):
        return _children(item, keys)
    return None


def _embedded(text, keys, budget, *, strict=True):
    """Inspect JSON containers within prose/code fences using the stdlib parser.

    Plausible but malformed JSON is withheld rather than allowing escaped
    credentials to escape the scalar checks (``strict``); escape-decoded text
    is already scanned whole, so there a fragment is prose. Ordinary non-JSON
    brackets remain prose. Cursor and aggregate work bounds prevent repeated
    suffix decoding.
    """
    decoder = json.JSONDecoder(object_pairs_hook=_JSONPairs)
    cursor = 0
    while match := _CONTAINER.search(text, cursor):
        budget.charge(len(text) - match.start())
        try:
            value, cursor = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            if strict:
                raise ValueError("Malformed embedded peer JSON") from None
            cursor = match.end()
            continue
        yield value, keys


def _members(pairs, keys, budget, decode):
    for key, child in pairs:
        yield key, ()
        names = tuple(_key_names(key, budget)) if decode else ()
        budget.charge(len(keys) + len(names))
        yield child, keys + names


def _children(items, keys):
    for child in items:
        yield child, keys
