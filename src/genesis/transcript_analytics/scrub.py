"""Secret scrubbing for stored text, reusing Genesis's own scrubber.

``scripts/hooks/secret_scrub.py`` is stdlib-only and fail-safe: on an internal
error it returns a placeholder, never the raw input (secret_scrub.py:426-443).
It is imported by path from the Genesis checkout (``GENESIS_REPO``, default
``~/genesis``) because this tool lives outside the repo.

Fail closed: if the scrubber cannot be imported, or it returns its error
placeholder, the text is NOT stored (None) and the row is flagged
``scrub_failed``. The scrubber covers credential shapes, not IPs/emails;
the store is local and mode 0700.
"""

from __future__ import annotations

import json
import os
import re
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
        return namespace["scrub"], hashlib.sha256(src).hexdigest()[:12]
    except Exception:  # noqa: BLE001 - any failure means: do not store text
        return None, None


_scrub, _VERSION = _load()


def version() -> str | None:
    """Content hash of the loaded scrubber, stamped into every stored source so a
    scrubber upgrade re-scrubs stored text on the next ingest (review SF-2)."""
    return _VERSION


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
    """Scrub plain text and embedded JSON; malformed structured input is withheld."""
    return _scrub_text(text, 0)


def _scrub_text(text, depth):
    if not text:
        return text, False
    if _scrub is None or depth > 64:
        return None, True
    try:
        # Decode objects before regex matching, including shell/log prefixes.
        if text.lstrip().startswith('"'):
            try:
                scalar, _ = load_json(text)
            except ValueError:
                scalar = None
            if isinstance(scalar, str):
                clean, failed = _scrub_text(scalar, depth + 1)
                return (
                    (None, True)
                    if failed
                    else (text if clean == scalar else json.dumps(clean, ensure_ascii=True), False)
                )
        for attempts, match in enumerate(re.finditer(r"[\{\[]", text), 1):
            if attempts > 64:
                return None, True
            start = match.start()
            pairs, duplicates = _object_pairs()
            decoder = json.JSONDecoder(object_pairs_hook=pairs)
            try:
                value, end = decoder.raw_decode(text, start)
            except ValueError:
                continue
            if isinstance(value, (dict, list)):
                clean, failed = scrub_json(value, _depth=depth + 1)
                if not failed and not duplicates[0] and json.loads(clean) == value:
                    clean = text[start:end]
                prefix, prefix_failed = _scrub_text(text[:start], depth + 1)
                suffix, suffix_failed = _scrub_text(text[end:], depth + 1)
                if failed or prefix_failed or suffix_failed:
                    return None, True
                return (prefix or "") + clean + (suffix or ""), False
        # Malformed quoted credential keys cannot be safely flattened.
        if _credential_candidate(text):
            return None, True
        out = _scrub(text)
        return (None, True) if out == _PLACEHOLDER else (out, False)
    except Exception:  # noqa: BLE001 - failures never expose original text
        return None, True


def _credential_candidate(text):
    for match in re.finditer(r'"((?:[^"\\]|\\.)*)"\s*:', text):
        key = json.loads('"' + match.group(1) + '"')
        probe = key + ": " + "x" * 32
        if _scrub(probe) != probe:
            return True
    return False


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


def scrub_json(value, *, preserve_identity=False, _depth=0):
    """Scrub decoded JSON before serialization; preserve only transcript ID paths.

    Credential labels use the loaded scrubber's own policy, with an opaque
    probe value, so JSON quoting cannot hide the key/value relationship.
    """
    if _scrub is None or _depth > 64:
        return None, True
    failed = False

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
                clean_key, error = _scrub_text(key, depth + 1)
                failed |= error
                probe = key + ": " + "x" * 32
                labeled = _scrub(probe)
                if labeled == _PLACEHOLDER:
                    failed = True
                out[clean_key or "[withheld]"] = (
                    "[REDACTED]"
                    if labeled != probe
                    else walk(child, (*path, key), depth + 1, item.get("type"))
                )
            return out
        if isinstance(item, list):
            return [walk(child, (*path, index), depth + 1) for index, child in enumerate(item)]
        if isinstance(item, str):
            clean, error = _scrub_text(item, depth + 1)
            failed |= error
            return clean
        return item

    try:
        clean = walk(value)
        return (None, True) if failed else (json.dumps(clean, ensure_ascii=True), False)
    except Exception:  # noqa: BLE001 - never return raw structured content on failure
        return None, True
