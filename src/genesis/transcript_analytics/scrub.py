"""Scoped credential scrubbing for stored text and reconstructed evidence.

The trusted checkout's stdlib-only shared scrubber and credential-label helper
are loaded from the same bytes. Missing or failing policy withholds text and
flags ``scrub_failed``. Recognized JSON is decoded before applying that policy;
unrecognized malformed encodings retain the shared plain-text policy. This is
credential scrubbing, not a guarantee that text contains no secrets or PII.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path

_PLACEHOLDER = "[scrub-error: content withheld]"


def _path() -> Path:
    from genesis.env import repo_root

    repo = Path(os.environ.get("GENESIS_REPO", repo_root()))
    return repo / "scripts" / "hooks" / "secret_scrub.py"


def _load():
    """Load the scrubber from ONE read of its bytes and hash those same bytes, so
    the stamp always describes the code actually in use (re-audit SF-5)."""
    import hashlib

    path = _path()
    try:
        src = path.read_bytes()
        namespace: dict = {"__name__": "_ta_secret_scrub", "__file__": str(path)}
        exec(compile(src, str(path), "exec"), namespace)  # noqa: S102 - trusted local Genesis file
        return (
            namespace["scrub"],
            namespace["credential_label_kind"],
            hashlib.sha256(src).hexdigest()[:12],
        )
    except Exception:  # noqa: BLE001 - any failure means: do not store text
        return None, None, None


_scrub, _label_kind, _VERSION = _load()
_ADAPTER_VERSION = "scoped-json-4"


def version() -> str | None:
    """Content hash of the loaded scrubber, stamped into every stored source so a
    scrubber upgrade re-scrubs stored text on the next ingest (review SF-2)."""
    return f"{_VERSION}:{_ADAPTER_VERSION}" if _VERSION else None


def available() -> bool:
    return _scrub is not None


def _object_pairs():
    duplicates = [False]

    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                duplicates[0] = True
            out[key] = value
        return out

    return pairs, duplicates


def load_json(raw):
    """Decode JSON and report duplicate keys anywhere in its object population."""
    pairs, duplicates = _object_pairs()
    return json.loads(raw, object_pairs_hook=pairs), duplicates[0]


def scrub_text(text: str | None) -> tuple[str | None, bool]:
    """Scoped JSON/credential scrubbing; unrecognized malformed encodings remain.

    Recognized malformed objects/members, duplicate keys and exhausted budgets
    are withheld. Ordinary shell quoting and unrecognized malformed arrays or
    scalars retain the shared plain-text policy.
    """
    return _scrub_text(text, 0, [64])


def _scrub_text(text, depth, budget):
    if not text:
        return text, False
    if _scrub is None or _label_kind is None or depth > 64:
        return None, True
    try:
        pieces = []
        cursor = 0
        while cursor < len(text):
            match = re.search(r'[\{\["]', text[cursor:])
            if match is None:
                pieces.append(text[cursor:])
                break
            start = cursor + match.start()
            budget[0] -= 1
            if budget[0] < 0:
                return None, True
            pairs, duplicates = _object_pairs()
            try:
                value, end = json.JSONDecoder(object_pairs_hook=pairs).raw_decode(text, start)
            except ValueError:
                if text[start] == "{" and re.match(r'\{[ \t\r\n]*"', text[start:]):
                    return None, True
                pieces.append(text[cursor : start + 1])
                cursor = start + 1
                continue
            if duplicates[0]:
                return None, True
            # A detached quoted member cannot safely represent a full object.
            if isinstance(value, str) and re.match(r"[ \t\r\n]*:", text[end:]):
                return None, True
            clean, failed = scrub_json(value, _depth=depth + 1, _budget=budget)
            if failed:
                return None, True
            replacement = text[start:end] if json.loads(clean) == value else clean
            sensitive = _scrub(replacement)
            if not isinstance(sensitive, str) or sensitive == _PLACEHOLDER:
                return None, True
            protected = replacement != text[start:end] or sensitive != replacement
            pieces.extend(
                (
                    text[cursor:start],
                    (replacement, isinstance(value, str)) if protected else replacement,
                )
            )
            cursor = end
        out = _scrub_spans(text, pieces)
        return (None, True) if not isinstance(out, str) or out == _PLACEHOLDER else (out, False)
    except Exception:  # noqa: BLE001 - no raw fallback after sanitizer failure
        return None, True


def _scrub_spans(text, pieces):
    """Apply prose policy without exposing sanitized JSON delimiters to regexes.

    A private-use prefix has more occurrences of its anchor than the entire
    input, so neither literal data nor control removal can manufacture it.
    Prefer an absent anchor; if all 6,400 BMP private-use characters occur,
    choosing the least frequent bounds expansion across the 64-span budget.
    """
    protected = any(isinstance(piece, tuple) for piece in pieces)
    if not protected:
        return _scrub("".join(pieces))
    counts = Counter(char for char in text if "\ue000" <= char <= "\uf8ff")
    anchor = next(
        (chr(code) for code in range(0xE000, 0xF900) if chr(code) not in counts),
        None,
    )
    if anchor is None:
        anchor = min(counts, key=counts.get)
    prefix = anchor * max(6, counts[anchor] + 1)
    rendered, replacements = [], []
    for index, piece in enumerate(pieces):
        if isinstance(piece, tuple):
            replacement, quoted = piece
            marker = f"{prefix}{index}{anchor}"
            if quoted:
                marker = f'"{marker}"'
            rendered.append(marker)
            replacements.append((marker, replacement))
        else:
            rendered.append(piece)
    out = _scrub("".join(rendered))
    if not isinstance(out, str) or out == _PLACEHOLDER:
        raise ValueError("shared scrubber failed")
    for marker, replacement in replacements:
        out = out.replace(marker, replacement)
    if prefix in out or out.count(anchor) > counts[anchor]:
        raise ValueError("protected JSON marker altered")
    return out


_IDENTITY_ROOT = frozenset({"uuid", "parentUuid", "sessionId", "agentId", "requestId"})


def _identity_path(path, block_type):
    return (
        len(path) == 1
        and path[0] in _IDENTITY_ROOT
        or path == ("message", "id")
        or len(path) == 4
        and path[:2] == ("message", "content")
        and isinstance(path[2], int)
        and (block_type, path[3]) in (("tool_use", "id"), ("tool_result", "tool_use_id"))
        or path == ("attachment", "toolUseID")
    )


def scrub_json(value, *, preserve_identity=False, _depth=0, _budget=None):
    """Scrub decoded JSON before serialization; preserve only transcript ID paths.

    Credential labels use the helper loaded from the same bytes as the scrubber.
    """
    if _scrub is None or _label_kind is None or _depth > 64:
        return None, True
    failed = False
    budget = [64] if _budget is None else _budget

    def walk(item, path=(), depth=_depth, block_type=None):
        nonlocal failed
        if depth > 64:
            failed = True
            return None
        if preserve_identity and _identity_path(path, block_type) and isinstance(item, str):
            return item
        if isinstance(item, dict):
            out = {}
            for key, child in item.items():
                clean_key, error = _scrub_text(key, depth + 1, budget)
                failed |= error
                if clean_key in out:
                    failed = True
                out[clean_key] = (
                    "[REDACTED]"
                    if _label_kind(key) is not None
                    else walk(child, (*path, key), depth + 1, item.get("type"))
                )
            return out
        if isinstance(item, list):
            return [walk(child, (*path, index), depth + 1) for index, child in enumerate(item)]
        if isinstance(item, str):
            clean, error = _scrub_text(item, depth + 1, budget)
            failed |= error
            return clean
        return item

    try:
        clean = walk(value)
        return (None, True) if failed else (json.dumps(clean, ensure_ascii=True), False)
    except Exception:  # noqa: BLE001 - never return raw structured content on failure
        return None, True
