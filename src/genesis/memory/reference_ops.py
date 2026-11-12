"""Shared reference-store domain operations.

Neutral home for reference-store logic used by BOTH the MCP tool layer
(``genesis.mcp.memory.knowledge``) and the dashboard route layer
(``genesis.dashboard.routes.references``). Keeping it here avoids the
presentation layer importing the MCP tool module (wrong direction) and
gives a single source of truth for:

- the reference ``project_type`` + valid ``kind`` set,
- parsing a stored reference ``body`` back into its description / value
  (the inverse of the ``_format_reference_body`` formatters), and
- the delete sequence (SQLite row + FTS + Qdrant point across both
  collections).

Body shape (produced identically by the ``reference_store`` MCP tool and
the extraction-job formatter)::

    [reference.{kind}] {identifier}

    {description}

    Value: {value}
    Tags: a, b            # optional
    Captured: via=... ...  # optional

``parse_reference_body`` is the security boundary for the dashboard: the
list/search/detail views must render ONLY the parsed ``description`` (never
the raw body), and the reveal endpoint returns ONLY the parsed ``value``.
It fails CLOSED — if the canonical ``\\n\\nValue: `` marker is absent the
value can't be located, so both fields come back empty rather than risk
leaking an unparsed secret into a non-reveal payload.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import TYPE_CHECKING, Any, NamedTuple
from urllib.parse import unquote

from genesis.db.crud.knowledge import CREDENTIAL_DOMAIN as _CREDENTIAL_DOMAIN
from genesis.db.crud.knowledge import DETAIL_PLACEHOLDER as _CRUD_DETAIL_PLACEHOLDER
from genesis.db.crud.knowledge import SECRET_PLACEHOLDER as _CRUD_PLACEHOLDER
from genesis.db.crud.knowledge import (
    get_reference_pair,
    get_secret_row,
    holds_secret,  # noqa: F401 - one rule, shared with the db layer
)

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)

# Single source of truth — the MCP tool module imports these back.
REFERENCE_PROJECT = "reference"


class ReferenceStoreUnavailable(RuntimeError):
    """The vector store was unavailable so the reference delete was DEFERRED and
    must be retried — distinct from "no such reference" (which returns ``False``).
    Callers should surface a retryable failure, not a 404/absent result."""
REFERENCE_KINDS = frozenset({
    "credentials",
    "url",
    "network",
    "persona_pointer",
    "account",
    "fact",
})

# The marker separating the description block from the value, as emitted by
# both formatters (a blank line followed by the ``Value: `` line).
_VALUE_MARKER = "\n\nValue: "
_TRAILER_PREFIXES = ("Tags: ", "Captured: ")


def parse_reference_body(body: str | None) -> dict[str, str]:
    """Split a stored reference body into its description and value.

    Returns ``{"description": str, "value": str}``. Fails CLOSED: if the
    canonical ``\\n\\nValue: `` marker is missing (a body shape this code
    didn't write), both fields are empty and a warning is logged — the
    caller must never fall back to emitting the raw body.

    Handles multi-line descriptions and multi-line values; the trailing
    ``Tags:`` / ``Captured:`` lines are stripped from the value.
    """
    if not body:
        return {"description": "", "value": ""}

    idx = body.find(_VALUE_MARKER)
    if idx == -1:
        logger.warning(
            "parse_reference_body: no canonical value marker found — "
            "failing closed (body len=%d)", len(body),
        )
        return {"description": "", "value": ""}

    # Everything before the marker is "[header]\n\n{description}".
    # Drop the header line (first line), keep the rest as description.
    pre = body[:idx]
    description = pre.split("\n", 1)[-1].strip() if "\n" in pre else ""

    # Everything after the marker is "{value}" plus an optional trailer of
    # Tags:/Captured: lines at the very end.
    after = body[idx + len(_VALUE_MARKER):]
    value_lines = after.split("\n")
    while value_lines and value_lines[-1].startswith(_TRAILER_PREFIXES):
        value_lines.pop()
    value = "\n".join(value_lines).strip()

    return {"description": description, "value": value}



# ─── Credential values and descriptions live outside the indexed body (#2426) ─
#
# A credential's VALUE is the one part of a reference no recall surface needs:
# lookup goes by identifier, and leaving the value out of the indexed text does
# not cost identifier recall on either the vector or the keyword path. Its
# free-text DESCRIPTION is held out WITH it: a description can repeat the value
# in any spelling or position, and no parser scrubs free text of it reliably
# (owner ruling, every review round found another way through). Every store
# that indexes a body (knowledge_units + knowledge_fts, memory_fts, the Qdrant
# payload and its embedding) therefore gets only the header (the identifier),
# DETAIL_PLACEHOLDER, the SECRET_PLACEHOLDER value line and the Tags/Captured
# trailer. The value and the description go to ONE ``reference_secrets`` row
# (``value``, ``detail``), a table no recall path reads. In the other indexed
# fields (identifier, tags, authority, provenance) every span of the value is
# MASKED with ``HELD_MASK`` (``mask_value_spans``) and the write goes on: no
# capture is refused or degraded to keep a secret out of the index (owner
# ruling, #2426), since the original is already held in reference_secrets.

#: Domains whose value is held out. Identifier kinds (url, network, account,
#: fact) are not: their value is the thing a lookup searches for.
SECRET_DOMAINS = frozenset({_CREDENTIAL_DOMAIN})
# The placeholders live with the db layer's fail-closed check (crud/knowledge.py).
SECRET_PLACEHOLDER = _CRUD_PLACEHOLDER
DETAIL_PLACEHOLDER = _CRUD_DETAIL_PLACEHOLDER
#: What a masked span of a held-out value reads as in an indexed field. Short,
#: fixed, and no credential keyword, so it is no secret-scanner hit.
HELD_MASK = "[held]"

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MIN_TOKEN = 4
_TOKEN_EDGE = ".,;:!?()[]{}\"'"  # noqa: S105 - punctuation, not a secret


_ASCII_DIGIT = re.compile(r"[0-9]")
_ASCII_ALPHA = re.compile(r"[A-Za-z]")
_ASCII_SYMBOL = re.compile(r"[^0-9A-Za-z]")


def _secret_token(token: str) -> bool:
    """A whitespace token that reads as a secret rather than a word: at least
    four characters, letters or digits mixed with a digit or a symbol. Plain
    words ("password", "Router") and email addresses never qualify."""
    if len(token) < _MIN_TOKEN or _EMAIL.match(token):
        return False
    if token.isascii():
        # The same three tests on ASCII, in C (an upload holds out megabytes).
        has_digit = _ASCII_DIGIT.search(token) is not None
        has_symbol = _ASCII_SYMBOL.search(token) is not None
        has_alpha = _ASCII_ALPHA.search(token) is not None
        return (has_digit or has_symbol) and (has_alpha or (has_digit and has_symbol))
    has_digit = any(c.isdigit() for c in token)
    has_symbol = any(not c.isalnum() for c in token)
    has_alpha = any(c.isalpha() for c in token)
    return (has_digit or has_symbol) and (has_alpha or (has_digit and has_symbol))


def _normalise(text: str) -> str:
    """``text`` as the refusal check compares it: NFKC (a fullwidth or other
    compatibility variant reads as its plain form), every format character
    (Unicode category Cf: zero-width spaces and joiners, the soft hyphen)
    removed, then casefolded."""
    if text.isascii():
        # NFKC leaves ASCII as it is, and no ASCII character is a format one.
        return text.casefold()
    folded = unicodedata.normalize("NFKC", text)
    return "".join(c for c in folded if unicodedata.category(c) != "Cf").casefold()


def _decoded(text: str) -> set[str]:
    """``text`` and its percent-decoded forms, up to three rounds."""
    forms = {text}
    for _ in range(3):
        text = unquote(text)
        forms.add(text)
    return forms


#: Where a structured value (a URL, a DSN, a key=value line, a header, XML,
#: a pipe- or backslash-separated record) splits into its parts, applied to the
#: NFKC form so fullwidth punctuation splits too. One generic fragmentation, no
#: per-format parser.
_FRAGMENT_SPLIT = re.compile(r"[\s:/@?&=#;,|\\<>\"'`(){}\[\]]+")
#: A host name: dotted labels ending in an alphabetic top-level label.
_HOSTNAME = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}$")
_SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]{0,31})://")


def _value_probes(value: str, *, login_exempt: bool = False) -> list[tuple[str, bool]]:
    """(probe, whole-word only) for the refusal check: the whole value, and
    every secret-shaped fragment of it, normalised. The value is percent-decoded
    (up to three rounds), NFKC-normalised and split on whitespace and
    ``: / @ ? & = # ; , | \\ < > " ' ` ( ) { } [ ]``, so
    the password, token or key inside a connection URL, a DSN or a
    ``key=value`` line is a fragment of its own; a fragment that is a host name,
    a URL scheme, or all digits (a port) is not probed. A secret that reads as
    a plain word (all letters, or under four characters) is not secret-shaped,
    so it is matched only as the whole value, and only as a whole word: an
    ordinary word inside another is no match. That is a stated limit."""
    whole = value.strip()
    probes: dict[str, bool] = {}
    for form in _decoded(whole) if whole else ():
        probes.setdefault(_normalise(form), form.isalpha() or len(form) < _MIN_TOKEN)
    # "login / secret" is the shape the capture writers format: with
    # ``login_exempt`` (the identifier only, which names the account) the
    # exact login half is not probed. Every other field is probed with the
    # tokens of both halves.
    login, sep, secret = whole.partition(" / ")
    exempt = login.strip().strip(_TOKEN_EDGE) if login_exempt and sep and secret.strip() else None
    for form in _decoded(whole):
        schemes = {m.group(1).casefold() for m in _SCHEME.finditer(form)}
        # Each distinct fragment once (the same output; a long value repeats).
        for fragment in dict.fromkeys(_FRAGMENT_SPLIT.split(unicodedata.normalize("NFKC", form))):
            fragment = fragment.strip(_TOKEN_EDGE)
            folded = fragment.casefold()
            if (
                not fragment or fragment == exempt or fragment.isdigit()
                or folded in schemes or _HOSTNAME.match(folded)
            ):
                continue
            if _secret_token(fragment):
                probes.setdefault(_normalise(fragment), False)
    probes.pop("", None)
    return list(probes.items())


#: Probes up to this long are looked up by every substring length present;
#: longer ones by their first ``_SHORT`` characters, then verified.
_SHORT = 32
#: A run of the value a mask may grow over is at most this long, so growing
#: costs a bounded check however large the value is.
_RUN_MAX = 256
_RUNS_PER_PROBE = 64
#: Matches spanning more whitespace tokens than this are masked with the whole
#: field (only a percent-encoded copy of a long multi-word value can need it).
_WINDOW_MAX = 16
_TERM_JOINER = re.compile(r"[-_.]")


def _term_shaped(fragment: str) -> bool:
    """A technical term rather than a secret: parts joined by ``-``, ``_`` or
    ``.``, each all letters, all digits or at most four characters
    (``wpa2-psk``, ``5ghz-band``, ``802.11ac``)."""
    if not _TERM_JOINER.search(fragment) or not all(c.isalnum() or c in "-_." for c in fragment):
        return False
    return all(p.isalpha() or p.isdigit() or len(p) <= 4 for p in _TERM_JOINER.split(fragment) if p)


class ValueMask:
    """Every probe of one held value (``_value_probes``), built ONCE per write
    and reused for every field it masks, with a matcher whose cost is linear in
    the field: probes are looked up by substring in hash sets. The fields it
    masks are small (an identifier, capped tags): caller metadata is held out
    with the value, never masked (owner ruling c91c5b74).

    ``captured``: (probe, whole-word) of every secret the capture rules
    recognise in the value and the description
    (``reference_extraction.captured_secrets``: "password: X", "pin: 4821", a
    known key prefix, ...). They are always probes, a plain-word or all-digit
    one as a whole word.

    ``prose``: the value is free text the writer did not delimit (a credential
    body with no ``Value:`` line, a distilled unit, an uploaded file). Its whole
    text is a probe, and so is every secret-shaped fragment that is not
    term-shaped (``_term_shaped``). A term-shaped fragment (``WPA2-PSK``) is
    probed only when a capture rule took it: otherwise it is an ordinary word
    of the prose, and masking it in a label would only lose the label. A
    writer-delimited value probes every secret-shaped fragment. Stated limit:
    an UNLABELLED term-shaped password in free prose that a tag or the name
    repeats stays visible there; nothing tells it from a label."""

    def __init__(
        self, value: str, *, login_exempt: bool = False, prose: bool = False,
        probes: list[tuple[str, bool]] | None = None,
        captured: list[tuple[str, bool]] | None = None,
    ):
        whole = {_normalise(form) for form in _decoded(value.strip())} if value.strip() else set()
        self.short: dict[int, set[str]] = {}
        self.long: dict[str, list[str]] = {}
        self.words: set[str] = set()
        if probes is None:
            probes = _value_probes(value, login_exempt=login_exempt)
        captured = captured or []
        taken = {probe for probe, _ in captured}
        for probe, as_word in [*probes, *captured]:
            if prose and probe not in whole and probe not in taken and (
                "-" in probe or "_" in probe or "." in probe
            ) and _term_shaped(probe):
                continue
            if as_word:
                self.words.add(probe)
            if len(probe) <= _SHORT:
                self.short.setdefault(len(probe), set()).add(probe)
            else:
                self.long.setdefault(probe[:_SHORT], []).append(probe)
        self.lengths = sorted(self.short)
        self._whole = whole
        self._runs: dict[str, list[str]] = {}
        self.window = max((len(p.split()) for p in whole), default=1)

    def runs(self, probe: str) -> list[str]:
        """The short whitespace-delimited runs of the value that hold ``probe``,
        so a match can grow over a secret that spans a separator
        ("Demo@Fake123"). Found by ``str.find`` on first use, at most
        ``_RUNS_PER_PROBE`` of them, so a megabyte value costs no full pass."""
        if probe not in self._runs:
            found: list[str] = []
            for form in self._whole:
                at = form.find(probe)
                while at != -1 and len(found) < _RUNS_PER_PROBE:
                    start = max(form.rfind(" ", 0, at), form.rfind("\n", 0, at), form.rfind("\t", 0, at)) + 1
                    stop = len(form)
                    for sep in (" ", "\n", "\t"):
                        end = form.find(sep, at + len(probe))
                        if end != -1:
                            stop = min(stop, end)
                    if stop - start <= _RUN_MAX and form[start:stop] not in found:
                        found.append(form[start:stop])
                    at = form.find(probe, stop)
            self._runs[probe] = found
        return self._runs[probe]

    def _bounded(self, text: str, start: int, stop: int, probe: str) -> bool:
        if probe not in self.words:
            return True
        return not (start and text[start - 1].isalnum()) and not (
            stop < len(text) and text[stop].isalnum()
        )

    def _matches(self, text: str):
        """(start, stop, probe) of every probe occurrence in normalised ``text``."""
        if not self.lengths and not self.long:
            return
        for i in range(len(text)):
            for length in self.lengths:
                if i + length > len(text):
                    break
                sub = text[i : i + length]
                if sub in self.short[length] and self._bounded(text, i, i + length, sub):
                    yield i, i + length, sub
            for probe in self.long.get(text[i : i + _SHORT], ()):
                if text.startswith(probe, i) and self._bounded(text, i, i + len(probe), probe):
                    yield i, i + len(probe), probe

    def carries(self, text: str | None) -> bool:
        """Whether ``text`` (normalised, and percent-decoded) carries a probe.
        The body placeholders and the mask are no leak and are left out (the
        mask as a separator, so the text around it never joins into a match)."""
        if not text:
            return False
        held_out = text.replace(SECRET_PLACEHOLDER, "").replace(DETAIL_PLACEHOLDER, "")
        held_out = held_out.replace(HELD_MASK, "\x00")
        return any(next(self._matches(_normalise(form)), None) for form in _decoded(held_out))

    def mask(self, text: str) -> str:
        """``text`` with every span of the value replaced by ``HELD_MASK``, in
        the ORIGINAL text: a fullwidth or zero-width spelling is mapped back to
        the characters it came from, and a match grows over every adjacent
        character, short of whitespace, that keeps it a run of the value. A
        spelling that cannot be mapped (percent-encoded) masks the whole token
        that carries it, in one pass. Unchanged when nothing matches."""
        if not text or not self.carries(text):
            return text
        normalised, origins = _normalised_with_origins(text)
        spans = []
        for start, stop, probe in self._matches(normalised):
            runs = self.runs(probe)
            while (
                start
                and not normalised[start - 1].isspace()
                and any(normalised[start - 1 : stop] in run for run in runs)
            ):
                start -= 1
            while (
                stop < len(normalised)
                and not normalised[stop].isspace()
                and any(normalised[start : stop + 1] in run for run in runs)
            ):
                stop += 1
            spans.append((origins[start], origins[stop - 1] + 1))
        text = _mask_spans(text, spans)
        for width in range(1, self.window + 1):
            if not self.carries(text):
                return text
            if width > _WINDOW_MAX:
                break
            text = self._mask_token_runs(text, width)
        return text if not self.carries(text) else HELD_MASK

    def _mask_token_runs(self, text: str, width: int) -> str:
        """``text`` with every run of ``width`` whitespace-delimited tokens that
        carries a probe masked whole, left to right in one pass."""
        parts = re.split(r"(\s+)", text)
        i = 0
        while i + 2 * (width - 1) < len(parts):
            hi = i + 2 * width - 1
            if self.carries("".join(parts[i:hi])):
                parts[i:hi] = [HELD_MASK]
            i += 2
        return "".join(parts)

    def mask_field(self, field: object) -> object:
        """``field`` with every string inside it masked (dict keys included),
        its structure and other types kept. A non-string scalar whose text
        carries the value has nothing else to keep: it becomes the mask."""
        if field is None:
            return None
        if isinstance(field, str):
            return self.mask(field)
        if isinstance(field, dict):
            return {self.mask_field(k): self.mask_field(v) for k, v in field.items()}
        if isinstance(field, (list, tuple, set)):
            return type(field)(self.mask_field(v) for v in field)
        return HELD_MASK if self.carries(str(field)) else field


def _captured_probes(texts: list[str | None]) -> list[tuple[str, bool]]:
    """(probe, whole-word) of every secret the capture rules recognise in
    ``texts``: the secret whole (a plain word or digits as a whole word) and
    its secret-shaped fragments."""
    # Imported here: reference_extraction imports knowledge_ingest, whose
    # credential path imports this module.
    from genesis.memory.reference_extraction import captured_secrets

    probes: list[tuple[str, bool]] = []
    for secret in captured_secrets([t for t in texts if t]):
        probes += [(_normalise(form), not _secret_token(form)) for form in _decoded(secret.strip())]
        probes += _value_probes(secret)
    return probes


def value_masks(
    value: str, *, prose: bool = False, detail: str | None = None,
    also: tuple[tuple[str | None, str | None], ...] = (),
) -> tuple[ValueMask, ValueMask]:
    """(the strict probe set, the identifier's) of ``value`` and the secrets the
    capture rules find in it and in ``detail``, from ONE probe pass. ``also``:
    (value, description) of other held secrets the write must not repeat (the
    value it replaces, an entry its identifier names), probed the same way.
    Only the identifier exempts the login of a "login / secret" value (it names
    the account); the tags are probed strictly."""
    probes = _value_probes(value) + [
        probe for other, _ in also if other and other.strip() for probe in _value_probes(other)
    ]
    login, sep, secret = value.strip().partition(" / ")
    exempt = _normalise(login.strip().strip(_TOKEN_EDGE)) if sep and secret.strip() else None
    whole = {_normalise(form) for form in _decoded(value.strip())}
    named = [(p, w) for p, w in probes if p != exempt or p in whole]
    captured = _captured_probes([value, detail, *(t for pair in also for t in pair)])
    return (
        ValueMask(value, prose=prose, probes=probes, captured=captured),
        ValueMask(value, prose=prose, probes=named, captured=captured),
    )


def _normalised_with_origins(text: str) -> tuple[str, list[int] | range]:
    """``text`` normalised character by character as ``_normalise`` does, and
    for each normalised character the index of the original one it came from."""
    if text.isascii():
        # ASCII normalises to its lower case, character for character.
        return text.lower(), range(len(text))
    out: list[str] = []
    origins: list[int] = []
    for i, char in enumerate(text):
        for part in unicodedata.normalize("NFKC", char):
            if unicodedata.category(part) == "Cf":
                continue
            for folded in part.casefold():
                out.append(folded)
                origins.append(i)
    return "".join(out), origins


def _mask_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """``text`` with the (merged) spans replaced by the mask, in one join."""
    merged: list[list[int]] = []
    for start, stop in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], stop)
        else:
            merged.append([start, stop])
    pieces, at = [], 0
    for start, stop in merged:
        pieces += [text[at:start], HELD_MASK]
        at = stop
    return "".join(pieces) + text[at:]


def mask_value_spans(value: str, text: str, *, login_exempt: bool = False) -> str:
    """``text`` with every span of ``value`` masked (``ValueMask.mask``)."""
    if not text or not value or not value.strip():
        return text
    return ValueMask(value, login_exempt=login_exempt).mask(text)


def _as_mask(value: str | ValueMask) -> ValueMask:
    return value if isinstance(value, ValueMask) else ValueMask(value)


def _mask_tag(mask: ValueMask, tag: object) -> object | None:
    """A tag with its value spans masked, at any nesting. None for a tag that
    was nothing but the value: dropping it loses nothing, it had no other
    content."""
    masked = mask.mask_field(tag)
    if masked != tag and isinstance(masked, str) and not masked.replace(HELD_MASK, "").strip():
        return None
    return masked


def _trailer_tag(tag: str) -> str:
    """A tag as the body's one ``Tags:`` line can carry it: a line break in
    it would start a line of its own (a second ``Value:`` line, say)."""
    return " ".join(tag.splitlines())


#: A credential's tags are capped before they are masked; the originals are
#: held in the secret row's meta.
MAX_TAGS = 64
MAX_TAG_CHARS = 256


def cap_masked(text: str, limit: int) -> str:
    """``text`` (already masked) cut to ``limit`` characters, never through a
    mask: a cut that would split one cuts before it. Masking runs on the WHOLE
    text first, so the cut never leaves part of a secret."""
    if len(text) <= limit:
        return text
    # The last mask that starts before the cut: when it runs past it, cut before it.
    at = text.rfind(HELD_MASK, 0, limit + len(HELD_MASK) - 1)
    if at != -1 and at < limit < at + len(HELD_MASK):
        limit = at
    return text[:limit]


def cap_tag(tag: object) -> object:
    """A masked tag as a credential row indexes it: at most ``MAX_TAG_CHARS``
    (a non-string one longer than that as its capped JSON text)."""
    if isinstance(tag, str):
        return cap_masked(tag, MAX_TAG_CHARS)
    text = json.dumps(tag, ensure_ascii=False, default=str)
    return tag if len(text) <= MAX_TAG_CHARS else cap_masked(text, MAX_TAG_CHARS)


def mask_value_tags(
    value: str | ValueMask, tags_json: str | None, body: str,
) -> tuple[str | None, str]:
    """(tags_json, body) for a credential row: at most ``MAX_TAGS`` tags, each
    masked whole and then capped (``cap_tag``), with every span of the value masked strictly
    (strings, numbers, lists and dicts at any nesting), in the tags list and in
    the body's ``Tags:`` trailer line, which is also checked whole (a value
    holding ", " spans tags no piece of which matches alone). A tag that was
    nothing but the value is dropped. A ``Captured:`` line is dropped: the
    caller's provenance is held in the secret row's meta. Tags that are not a
    JSON list are decoded and masked as one value."""
    mask = _as_mask(value)
    if tags_json:
        try:
            tags = json.loads(tags_json)
        except ValueError:
            tags = tags_json
        if not isinstance(tags, list):
            tags = [tags]
        kept = [cap_tag(t) for t in (_mask_tag(mask, t) for t in tags[:MAX_TAGS]) if t is not None]
        tags_json = json.dumps(kept)
    idx = body.find(_VALUE_MARKER)
    if idx != -1:
        after = body[idx + len(_VALUE_MARKER):]
        trailer = _trailer(after)
        rebuilt = []
        for line in trailer:
            if not line.startswith("Tags: "):
                continue
            pieces = line[len("Tags: "):].split(", ")[:MAX_TAGS]
            joined = ", ".join(t for t in (_mask_tag(mask, t) for t in pieces) if t is not None)
            if mask.carries(joined):
                joined = mask.mask(joined)
            # Capped only once masked, so a cut never leaves part of a secret.
            joined = ", ".join(cap_tag(t) for t in joined.split(", "))
            if joined.replace(HELD_MASK, "").strip(", "):
                rebuilt.append("Tags: " + joined)
        if rebuilt != trailer:
            value_line = after[: len(after) - len("\n".join(trailer))].rstrip("\n")
            body = _held_body(body[:idx], rebuilt, value_line)
    return tags_json, body


class HeldCredential(NamedTuple):
    """What ``split_secret`` takes out of a credential body."""

    #: The indexed body: header, DETAIL_PLACEHOLDER, the value placeholder line, trailer.
    body: str
    #: The value, or None when the body carried the value placeholder (a re-ingest).
    value: str | None
    #: The description, or None when the body carried DETAIL_PLACEHOLDER.
    detail: str | None


def split_secret(
    body: str,
    *,
    header: str | None = None,
    tags: list[str] | None = None,
    value: str | None = None,
    detail: str | None = None,
) -> HeldCredential:
    """Take the value and the free-text description out of a credential body.

    The body is ``[reference.credentials] {identifier}``, a blank line, the
    description, a blank line, ``Value: {value}`` and the Tags/Captured trailer,
    the layout every credential writer formats (the reference_store and
    knowledge_ingest MCP tools, the extraction job, migrate_reference_data).
    ``header`` is the header line expected for the key (default: the body's own
    first line); a first line that is not it is part of the description. The
    returned body is that header, DETAIL_PLACEHOLDER where the description
    stood, the canonical value placeholder line and the trailer. A body that
    already carries a placeholder (a re-ingest of a held body) returns None for
    that part, so the stored one stays. ``value`` and ``detail``, when a writer
    passes them, are the value and description as it knows them (a multi-line
    value is held out whole, whatever its lines look like).

    A body whose split point is ambiguous (no ``Value:`` line, the marker twice,
    a value line shaped like a trailer line, a repeated trailer line, or a
    parsed value that is not the writer's) is never refused: MORE is held out.
    Everything after the header goes to the secret (the value: the text after
    the first marker, or the whole remainder when there is none), the
    description before the first marker stays the detail (None when there is
    no marker to separate it, so a stored one is kept), and the trailer is
    rebuilt only from the structured ``tags``, so no part of the ambiguous text
    is indexed. Raises ValueError, before anything is written, only when a
    payment card number is in the value or the description (#3172).
    """
    from genesis.security.card_shapes import contains_card_number

    first = body.partition("\n")[0]
    header = first if header is None else header
    rest = body[len(first):] if first == header else "\n" + body
    idx = rest.find(_VALUE_MARKER)
    clean = idx != -1 and rest.count(_VALUE_MARKER) == 1
    if clean:
        after = rest[idx + len(_VALUE_MARKER):]
        if value is not None and value.strip() and after.startswith(value):
            # The writer's value says exactly where the value line ends: what
            # follows it is the trailer, whatever the value's lines look like.
            tail = after[len(value):]
            trailer = tail.strip("\n").split("\n") if tail.strip() else []
            clean = tail[:1] in ("", "\n") and all(
                line.startswith(_TRAILER_PREFIXES) for line in trailer
            ) and _split_trailer("\n".join(trailer))[1]
            held = value
        else:
            trailer, clean = _split_trailer(after)
            held = parse_reference_body(rest)["value"]
            value_lines = held.split("\n")[1:]
            clean = clean and not any(
                line.startswith(_TRAILER_PREFIXES) for line in value_lines
            ) and (value is None or held == value.strip())
    if clean:
        description: str | None = rest[:idx].strip()
    else:
        # Hold out more: the remainder is the value, nothing of it indexed.
        held = (rest[idx + len(_VALUE_MARKER):] if idx != -1 else rest).strip()
        description = rest[:idx].strip() if idx != -1 else None
        trailer = ["Tags: " + ", ".join(_trailer_tag(t) for t in tags)] if tags else []
    if value is not None:
        # The writer's value, exactly as given (a whitespace-only one is empty).
        held = value if value.strip() else ""
    if detail is not None:
        description = detail.strip()
    if contains_card_number(held) or contains_card_number(description or ""):
        raise ValueError(
            "a credential reference carries a payment card number; cards are "
            "never stored as references"
        )
    return HeldCredential(
        _held_body(header + "\n\n" + DETAIL_PLACEHOLDER, trailer),
        # An empty value is an empty value, held like any other; only the
        # placeholder means "keep the stored one" (#2426 PR A).
        None if held == SECRET_PLACEHOLDER else held,
        None if description == DETAIL_PLACEHOLDER else description,
    )


def _split_trailer(after_marker: str) -> tuple[list[str], bool]:
    """(the trailing Tags:/Captured: lines of the text after the value marker,
    whether they are unambiguous). Writers emit each trailer line once; a
    repeated one means part of a multi-line value was read as trailer."""
    lines = after_marker.split("\n")
    trailer: list[str] = []
    while lines and lines[-1].startswith(_TRAILER_PREFIXES):
        trailer.insert(0, lines.pop())
    prefixes = [next(p for p in _TRAILER_PREFIXES if line.startswith(p)) for line in trailer]
    return trailer, len(prefixes) == len(set(prefixes))


def _trailer(after_marker: str) -> list[str]:
    """The trailing Tags:/Captured: lines of the text after the value marker."""
    return _split_trailer(after_marker)[0]


def _held_body(head: str, trailer: list[str], value: str = SECRET_PLACEHOLDER) -> str:
    """The canonical body: the head, ONE value line, then the trailer."""
    body = head + _VALUE_MARKER + value
    return body + "\n" + "\n".join(trailer) if trailer else body


def _put_detail_back(head: str, detail: str | None) -> str:
    """``head`` (header and description block) with a held-out description in
    place of DETAIL_PLACEHOLDER; unchanged when it carries no placeholder (a
    row written before the description was held out) or nothing is stored."""
    header, sep, rest = head.partition("\n\n")
    if detail is None or rest.strip() != DETAIL_PLACEHOLDER:
        return head
    return header + sep + detail


async def _held_pair(
    db: aiosqlite.Connection, row: dict,
) -> tuple[str, str | None, str | None]:
    """(body, value, detail) of a held-out credential row, as ONE writer's set.

    The secret row the snapshot's ``secret_ref`` names is read first: secret
    rows are never updated, so its value and detail are the very ones written
    beside the snapshot's body. When the snapshot names no secret any more (the
    unit was repointed since it was read, retiring the secret it named) or
    carries no ``secret_ref`` at all, the unit's CURRENT body, value and detail
    are read by one statement (``get_reference_pair``); the current secret is
    never put beside the snapshot's stale body. The value is None when no secret
    is stored for the unit (a partial restore, a hand edit), or the unit is gone.
    """
    body = row.get("body") or ""
    if row.get("secret_ref"):
        secret = await get_secret_row(db, row["secret_ref"])
        if secret is not None:
            return body, secret[0], secret[1]
    pair = await get_reference_pair(db, row["id"])
    if pair is None:
        return body, None, None
    return pair[0] or "", pair[1], pair[2]


def _credential(row: dict) -> bool:
    return holds_secret(row.get("project_type"), row.get("domain"))


def _held_row(row: dict, parsed_value: str) -> bool:
    """Whether the row's value (and description) live in reference_secrets: a
    credential whose body carries the placeholder, or any other reference row
    written with a held-out value (``ingest_knowledge_unit(held_value=...)``),
    which names it by ``secret_ref``; its body's value line is masked."""
    if _credential(row):
        return parsed_value == SECRET_PLACEHOLDER
    return bool(row.get("secret_ref"))


async def resolve_value(db: aiosqlite.Connection, row: dict) -> str | None:
    """The value of a reference row, or None when a held-out value is missing.

    A held-out credential's value comes from ``reference_secrets`` through
    ``_held_pair``, so it is the value of the same write as the body it pairs
    with; every other row (identifier kinds, and credentials written before
    #2426 that still carry their value inline, with no secret_ref) is parsed
    from the body as before, an empty value included. None means a held-out
    credential names no stored secret, which only a partial restore or a hand
    edit can produce: logged as an error, never guessed at.
    """
    return (await reveal_reference(db, row))[0]


async def reveal_reference(db: aiosqlite.Connection, row: dict) -> tuple[str | None, str]:
    """(value, description) of a reference row: for a held-out credential both
    come from one ``reference_secrets`` row (``_held_pair``); every other row
    parses both from its body. The value is None when a held-out value is
    missing (logged as an error)."""
    parsed = parse_reference_body(row.get("body"))
    if not _held_row(row, parsed["value"]):
        return parsed["value"], parsed["description"]
    body, secret, detail = await _held_pair(db, row)
    current = parse_reference_body(body)
    if secret is None:
        if _credential(row) and current["value"] and current["value"] != SECRET_PLACEHOLDER:
            # The unit's current body carries its value inline (a row written
            # before #2426): that body's own value, never a guess.
            return current["value"], current["description"]
        logger.error("reference %s holds its value out, but no secret is stored for it", row["id"])
        return None, current["description"]
    description = current["description"]
    if detail is not None and description == DETAIL_PLACEHOLDER:
        description = detail
    return secret, description


async def reveal_meta(db: aiosqlite.Connection, row: dict) -> dict | None:
    """The caller metadata held with a credential's value (#2426): the
    authority and provenance it was written with, purpose, ingestion source,
    relationships, caveats, and the identifier and tags as given. Read from the
    secret row the row names; None when none is held."""
    from genesis.db.crud.knowledge import get_secret_meta

    text = await get_secret_meta(db, row.get("secret_ref"))
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


async def compose_body(db: aiosqlite.Connection, row: dict) -> tuple[str, bool]:
    """(the row's body with its held-out description and value put back,
    whether the value was missing).

    For ``reference_lookup``, the one reader that returns a whole body. The
    placeholder is recognised by the value it parses to (the rule
    ``resolve_value`` and ``split_secret`` use), not by one exact spelling of
    its line, and the value goes back on a canonical value line. Body, value and
    description are one write's set (``_held_pair``): when the unit was
    repointed after ``row`` was read, the CURRENT body comes back with them.
    """
    body = row.get("body") or ""
    idx = body.find(_VALUE_MARKER)
    if idx == -1 or not _held_row(row, parse_reference_body(body)["value"]):
        return body, False
    body, value, detail = await _held_pair(db, row)
    idx = body.find(_VALUE_MARKER)
    if idx == -1 or (
        _credential(row) and parse_reference_body(body)["value"] != SECRET_PLACEHOLDER
    ):
        # The current body is not a held-out one (no marker, or a value inline):
        # it is returned as stored, its own pair.
        return body, False
    if value is None:
        logger.error("reference %s holds its value out, but no secret is stored for it", row["id"])
        return body, True
    head = _put_detail_back(body[:idx], detail)
    return _held_body(head, _trailer(body[idx + len(_VALUE_MARKER):]), value), False


async def delete_reference_entry(
    db: aiosqlite.Connection,
    store: Any | None,
    unit_id: str,
) -> bool:
    """Delete a reference entry: SQLite row + FTS row + Qdrant point.

    Mirrors the original ``reference_delete`` MCP sequence so both the tool
    and the dashboard route share one delete path. ``store`` is a
    ``MemoryStore`` (its ``.delete()`` tries both Qdrant collections and
    cascades metadata/links/pending); pass ``None`` for a SQLite-only
    fallback when no store is available.

    Refuses to delete non-reference rows (guards ``project_type``) so this
    can't be used as a generic knowledge-unit delete. Returns ``True`` if a
    row was deleted, ``False`` if no row existed with that id.
    """
    from genesis.db.crud import knowledge as knowledge_crud

    row = await knowledge_crud.get(db, unit_id)
    if row is None:
        return False
    if row.get("project_type") != REFERENCE_PROJECT:
        raise ValueError(
            f"delete_reference_entry: unit {unit_id} is not a reference entry "
            f"(project_type={row.get('project_type')!r})"
        )

    qdrant_id = row.get("qdrant_id")
    # store.store dedups by exact content, so two non-credential units can share
    # one point: never delete a point another unit still names. For a credential
    # the check is reliable: its writes never reuse a point (dedup=False in
    # ingest_knowledge_unit), so no other unit can name it. Two concurrent
    # deletes of the last owners of a SHARED (non-credential, or pre-change)
    # point can still both skip the delete.
    shared = bool(qdrant_id) and await knowledge_crud.qdrant_point_named_elsewhere(
        db, qdrant_id, exclude_id=unit_id,
    )
    if qdrant_id and store is not None and not shared:
        try:
            res = await store.delete(qdrant_id)
        except ReferenceStoreUnavailable:
            raise
        except Exception as exc:
            logger.error(
                "delete_reference_entry: Qdrant cleanup failed for unit %s "
                "(qdrant_id=%s) — deferring, leaving unit for retry",
                unit_id, qdrant_id, exc_info=True,
            )
            raise ReferenceStoreUnavailable(
                f"reference delete failed for unit {unit_id} (qdrant_id={qdrant_id})"
            ) from exc
        if res.get("deferred"):
            # Vector store unavailable → store.delete kept the point + metadata
            # intact. Do NOT drop the knowledge_units row, or we would orphan it
            # from its record. Signal a RETRYABLE failure (not "not found") so the
            # caller can report it correctly and retry the whole delete later.
            raise ReferenceStoreUnavailable(
                f"reference delete deferred for unit {unit_id} "
                f"(qdrant_id={qdrant_id}): vector store unavailable"
            )

    deleted = await knowledge_crud.delete(db, unit_id)
    logger.info("Reference entry %s deleted: %s", unit_id, deleted)
    return deleted
