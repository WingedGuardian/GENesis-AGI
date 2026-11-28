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
            # The arms every scrubber version has: which JSON spans to protect is
            # decided with these, so arms added to ``scrub`` only add redaction.
            namespace.get("scrub_base", namespace["scrub"]),
            namespace["credential_label_kind"],
            hashlib.sha256(src).hexdigest()[:12],
        )
    except Exception:  # noqa: BLE001 - any failure means: do not store text
        return None, None, None, None


_scrub, _scrub_base, _label_kind, _VERSION = _load()
_ADAPTER_VERSION = "scoped-json-6"


def version() -> str | None:
    """Content hash of the loaded scrubber, stamped into every stored source so a
    scrubber upgrade re-scrubs stored text on the next ingest (review SF-2)."""
    return f"{_VERSION}:{_ADAPTER_VERSION}" if _VERSION else None


def available() -> bool:
    return _scrub is not None and _scrub_base is not None


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


def _scrub_text(text, depth, budget, apply=None, decide=None):
    """``apply`` redacts; ``decide`` picks which JSON spans to protect from the
    prose pass. A protected span hides its text, a label at its edge included,
    from the prose pass's arms, so protecting a span the base arms would not
    can lose a redaction. Deciding with the base arms keeps the protected set
    exactly the one the base scrubber produces, and ``apply`` only adds. A test
    that patches ``_scrub`` alone still decides with the real ``_scrub_base``."""
    if not text:
        return text, False
    apply = _scrub if apply is None else apply
    decide = _scrub_base if decide is None else decide
    if apply is None or decide is None or _label_kind is None or depth > 64:
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
            original = text[start:end]
            before = budget[0]
            clean, failed = scrub_json(
                value, _depth=depth + 1, _budget=budget, _apply=apply, _decide=decide
            )
            if failed:
                return None, True
            replacement = original if json.loads(clean) == value else clean
            basis = replacement
            if decide is not apply:
                # Same walk with the base arms on a budget copy, so the full
                # walk's own budget use is unchanged.
                clean, failed = scrub_json(
                    value, _depth=depth + 1, _budget=[before], _apply=decide, _decide=decide
                )
                if failed:
                    return None, True
                basis = original if json.loads(clean) == value else clean
            sensitive = decide(basis)
            # The full arms must also accept the span, or it is withheld: the
            # protected copy never meets them again whole.
            checked = sensitive if decide is apply else apply(replacement)
            if any(not isinstance(v, str) or v == _PLACEHOLDER for v in (sensitive, checked)):
                return None, True
            protected = basis != original or sensitive != basis
            pieces.extend(
                (
                    text[cursor:start],
                    (replacement, isinstance(value, str), original) if protected else original,
                )
            )
            cursor = end
        out = _scrub_spans(text, pieces, apply, decide)
        return (None, True) if not isinstance(out, str) or out == _PLACEHOLDER else (out, False)
    except Exception:  # noqa: BLE001 - no raw fallback after sanitizer failure
        return None, True


def _scrub_spans(text, pieces, apply, decide):
    """Apply prose policy without exposing sanitized JSON delimiters to regexes.

    A private-use prefix has more occurrences of its anchor than the entire
    input, so neither literal data nor control removal can manufacture it.
    Prefer an absent anchor; if all 6,400 BMP private-use characters occur,
    choosing the least frequent bounds expansion across the 64-span budget.
    """
    protected = any(isinstance(piece, tuple) for piece in pieces)
    if not protected:
        return apply("".join(pieces))
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
            replacement, quoted, _original = piece
            marker = f"{prefix}{index}{anchor}"
            if quoted:
                marker = f'"{marker}"'
            rendered.append(marker)
            replacements.append((marker, replacement, _original))
        else:
            rendered.append(piece)

    def restore(text):
        for marker, replacement, _original in replacements:
            text = text.replace(marker, replacement)
        if prefix in text or text.count(anchor) > counts[anchor]:
            raise ValueError("protected JSON marker altered")
        return text

    # The base arms' pipeline exactly: prose pass, boundary probe, marker check.
    # Only then do the full arms run over that output, before markers are
    # restored. Run first, a full arm could consume a marker and the probe would
    # skip the value after it; checked after them only, a full arm could hide a
    # marker the prose pass had damaged. Run last, they only add redaction (and
    # re-running the base arms over their own output only adds too).
    out = decide("".join(rendered))
    if not isinstance(out, str) or out == _PLACEHOLDER:
        raise ValueError("shared scrubber failed")
    out = _scrub_span_boundaries(out, [(m, o) for m, _r, o in replacements], decide)
    if apply is decide:
        return restore(out)
    restore(out)
    out = apply(out)
    if not isinstance(out, str) or out == _PLACEHOLDER:
        raise ValueError("shared scrubber failed")
    return restore(out)


#: How much of a protected span's ORIGINAL text the boundary probe keeps: enough
#: for a credential label, its quotes and escapes.
_BOUNDARY_TAIL = 64
#: The separators main's label arms read after a label and its closing quote.
_BOUNDARY_SEPARATOR = re.compile(r"[ \t]*[:=]")


def _scrub_span_boundaries(out, markers, apply):
    """Redact a value that follows a protected span and a separator on its line.

    ``out`` is the prose pass's output with the span markers still in place;
    ``markers`` is ``(marker, original span text)`` per protected span. The
    marker hid any credential label that ENDS its span ('"... cookie"=VALUE')
    from the arm that would redact VALUE. Probe the span's original tail plus
    the rest of its line (up to the next marker) and keep the probe's
    redaction of that trailing text. This runs AFTER the prose pass and only
    removes text from its output, so it can add redaction and never take any
    away. A marker the prose pass consumed needs no probe. When the probe's
    output cannot be split at the span boundary, the trailing text is withheld.
    Windows are disjoint, so the cost is linear.
    """
    found = sorted(
        (at, marker, original) for marker, original in markers if (at := out.find(marker)) >= 0
    )
    ends = [at for at, _, _ in found[1:]] + [len(out)]
    for (at, marker, original), stop in reversed(list(zip(found, ends, strict=True))):
        start = at + len(marker)
        newline = out.find("\n", start, stop)
        end = stop if newline < 0 else newline
        window = out[start:end]
        if not _BOUNDARY_SEPARATOR.match(window):
            continue
        tail = original[-_BOUNDARY_TAIL:]
        probed = apply(tail + window)
        if not isinstance(probed, str) or probed == _PLACEHOLDER:
            raise ValueError("shared scrubber failed")
        if probed == tail + window:
            continue
        head = next(
            (h for h in (tail, apply(tail)) if isinstance(h, str) and probed.startswith(h)),
            None,
        )
        kept = probed[len(head) :] if head is not None else None
        if kept is None or not _is_subsequence(kept.replace("[REDACTED]", ""), window):
            kept = "[REDACTED]"
        out = out[:start] + kept + out[end:]
    return out


def _is_subsequence(needle, haystack):
    """True when every character of ``needle`` appears in ``haystack`` in order:
    the kept text came from the window, never from the span's original."""
    chars = iter(haystack)
    return all(char in chars for char in needle)


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


def scrub_json(
    value, *, preserve_identity=False, _depth=0, _budget=None, _apply=None, _decide=None
):
    """Scrub decoded JSON before serialization; preserve only transcript ID paths.

    Credential labels use the helper loaded from the same bytes as the scrubber.
    """
    apply = _scrub if _apply is None else _apply
    decide = _scrub_base if _decide is None else _decide
    if apply is None or decide is None or _label_kind is None or _depth > 64:
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
                clean_key, error = _scrub_text(key, depth + 1, budget, apply, decide)
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
            clean, error = _scrub_text(item, depth + 1, budget, apply, decide)
            failed |= error
            return clean
        return item

    try:
        clean = walk(value)
        return (None, True) if failed else (json.dumps(clean, ensure_ascii=True), False)
    except Exception:  # noqa: BLE001 - never return raw structured content on failure
        return None, True
