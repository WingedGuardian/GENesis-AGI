"""Masking a held-out credential value in the fields that are indexed (#2426).

Split out of ``reference_ops`` (which re-exports the public names): the probes
of a value (``_value_probes``), ``ValueMask`` and its span matcher, the probe
sets a write builds (``value_masks``), and the caps that cut a masked field
never through a mask. Every indexed field of a credential (its identifier and
tags) is masked with these before it is stored; the original is held in
``reference_secrets``.
"""

from __future__ import annotations

import json
import re
import unicodedata
from urllib.parse import unquote

from genesis.db.crud.knowledge import DETAIL_PLACEHOLDER, SECRET_PLACEHOLDER

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


#: Whitespace ``_normalise`` changes: a character other than a space, or a run.
_SPACING = re.compile(r"[^\S ]|  ")


def _one_space(text: str) -> str:
    """``text`` with every whitespace run one space (``str.split`` is the fast
    path: an upload holds out megabytes)."""
    if not _SPACING.search(text):
        return text
    words = text.split()
    if not words:
        return " "
    return (" " if text[0].isspace() else "") + " ".join(words) + (" " if text[-1].isspace() else "")


def _normalise(text: str) -> str:
    """``text`` as the refusal check compares it: NFKC (a fullwidth or other
    compatibility variant reads as its plain form), every format character
    (Unicode category Cf: zero-width spaces and joiners, the soft hyphen)
    removed, then casefolded, and every whitespace run read as one space (a
    multi-word value spaced another way is the same value)."""
    if text.isascii():
        # NFKC leaves ASCII as it is, and no ASCII character is a format one.
        return _one_space(text.casefold())
    folded = unicodedata.normalize("NFKC", text)
    plain = "".join(c for c in folded if unicodedata.category(c) != "Cf")
    return _one_space(plain.casefold())


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
                not fragment
                or fragment == exempt
                or fragment.isdigit()
                or folded in schemes
                or _HOSTNAME.match(folded)
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
_RUN_SEPARATORS = (" ", "\n", "\t")
#: Matches spanning more whitespace tokens than this are masked with the whole
#: field (only a percent-encoded copy of a long multi-word value can need it).
_WINDOW_MAX = 16
_TERM_JOINER = re.compile(r"[-_.]")
#: A percent escape (``%40``).
_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")


def _term_shaped(fragment: str) -> bool:
    """A technical term rather than a secret: parts joined by ``-``, ``_`` or
    ``.``, each all letters, all digits or at most four characters
    (``wpa2-psk``, ``5ghz-band``, ``802.11ac``)."""
    if not _TERM_JOINER.search(fragment) or not all(c.isalnum() or c in "-_." for c in fragment):
        return False
    return all(p.isalpha() or p.isdigit() or len(p) <= 4 for p in _TERM_JOINER.split(fragment) if p)


def _forms(text: str) -> set[str]:
    """The normalised forms of ``text`` (``_decoded``), without an empty one."""
    if not text.strip():
        return set()
    return {form for form in map(_normalise, _decoded(text.strip())) if form.strip()}


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
    repeats stays visible there; nothing tells it from a label.

    ``exempt``: the probes skipped as such prose words, decided by the source of
    each probe (``value_masks``) rather than by ``prose``: a known held value or
    a recorded masked span is never exempt, whatever the incoming text is.

    ``secrets``: the other held secrets the probes came from (``value_masks``'s
    ``also`` values): a match grows over runs of their text too, not only of
    ``value``'s.

    A probe given both unbounded and as a whole word is unbounded (the stronger
    requirement wins), and an empty or whitespace-only probe is no probe."""

    def __init__(
        self,
        value: str,
        *,
        login_exempt: bool = False,
        prose: bool = False,
        probes: list[tuple[str, bool]] | None = None,
        captured: list[tuple[str, bool]] | None = None,
        exempt: frozenset[str] | None = None,
        secrets: list[str] | None = None,
    ):
        whole = _forms(value)
        self.short: dict[int, set[str]] = {}
        self.long: dict[str, list[str]] = {}
        if probes is None:
            probes = _value_probes(value, login_exempt=login_exempt)
        captured = captured or []
        if exempt is None:
            exempt = (
                _prose_words(probes, whole, {probe for probe, _ in captured})
                if prose
                else frozenset()
            )
        kept = [(p, w) for p, w in [*probes, *captured] if p.strip() and p not in exempt]
        unbounded = {probe for probe, as_word in kept if not as_word}
        self.words: set[str] = {probe for probe, as_word in kept if as_word} - unbounded
        for probe in dict.fromkeys(probe for probe, _ in kept):
            if len(probe) <= _SHORT:
                self.short.setdefault(len(probe), set()).add(probe)
            else:
                self.long.setdefault(probe[:_SHORT], []).append(probe)
        self.lengths = sorted(self.short)
        # A match grows over runs of every secret source, not only of ``value``.
        self._whole = whole.union(*(_forms(text) for text in secrets or ()))
        self._runs: dict[str, list[str]] = {}
        self.window = max((len(p.split()) for p in self._whole), default=1)
        # A value that spells the mask text: the mask is then not only a
        # separator (``carries``).
        self._spells_mask = any(HELD_MASK in probe for probe, _ in kept)

    def runs(self, probe: str) -> list[str]:
        """The short whitespace-delimited runs of the value that hold ``probe``,
        so a match can grow over a secret that spans a separator
        ("Demo@Fake123"). Found by ``str.find`` on first use: at most
        ``_RUNS_PER_PROBE`` of them, each occurrence looking no further than
        ``_RUN_MAX`` characters either side (a longer run is never kept), so a
        value of repeated tokens costs a linear pass."""
        if probe not in self._runs:
            found: list[str] = []
            for form in self._whole:
                at = form.find(probe)
                while at != -1 and len(found) < _RUNS_PER_PROBE:
                    end = at + len(probe)
                    low, high = max(0, at - _RUN_MAX), min(len(form), end + _RUN_MAX + 1)
                    start = max(form.rfind(sep, low, at) for sep in _RUN_SEPARATORS) + 1
                    stops = [
                        i for i in (form.find(sep, end, high) for sep in _RUN_SEPARATORS) if i != -1
                    ]
                    stop = min(stops) if stops else (len(form) if high == len(form) else None)
                    whole_run = stop is not None and (start > 0 or low == 0)
                    if whole_run and stop - start <= _RUN_MAX and form[start:stop] not in found:
                        found.append(form[start:stop])
                    at = form.find(probe, stop if stop is not None else end)
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
        mask as a separator, so the text around it never joins into a match).
        A value that itself spells the mask text is matched in the text as it
        stands too, so a literal copy of it never escapes as "a mask"."""
        if not text:
            return False
        held_out = text.replace(SECRET_PLACEHOLDER, "").replace(DETAIL_PLACEHOLDER, "")
        candidates = [held_out.replace(HELD_MASK, "\x00")]
        if self._spells_mask and HELD_MASK in held_out:
            candidates.append(held_out)
        return any(
            next(self._matches(_normalise(form)), None)
            for candidate in candidates
            for form in _decoded(candidate)
        )

    def mask(self, text: str) -> str:
        """``text`` with every span of the value replaced by ``HELD_MASK``, in
        the ORIGINAL text: a fullwidth or zero-width spelling is mapped back to
        the characters it came from, and a match grows over every adjacent
        character, short of whitespace, that keeps it a run of the value. A
        spelling that cannot be mapped (percent-encoded) masks the whole token
        that carries it, in one pass. Unchanged when nothing matches."""
        return _mask_spans(text, self.mask_spans(text)) if text else text

    def mask_and_spans(self, text: str) -> tuple[str, list[str]]:
        """(``mask(text)``, the original text of every span it replaced, in
        order): what a write records of the secrets its identifier carried."""
        spans = self.mask_spans(text) if text else []
        return _mask_spans(text, spans), [text[start:stop] for start, stop in spans]

    def mask_spans(self, text: str) -> list[tuple[int, int]]:
        """The merged (start, stop) spans of ``text`` that ``mask`` replaces, in
        the original text's offsets."""
        if not text or not self.carries(text):
            return []
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
        # A percent-encoded spelling cannot be mapped back character by
        # character: the whole token that carries it decoded is masked, so no
        # fragment of the secret around the escapes stays visible.
        spans += [
            m.span()
            for m in re.finditer(r"\S+", text)
            if _ESCAPE.search(m.group()) and self.carries(m.group())
        ]
        spans = _merged(spans)
        for width in range(1, self.window + 1):
            if not self.carries(_mask_spans(text, spans)):
                return spans
            if width > _WINDOW_MAX:
                break
            rendered, ostart, oend = _rendered(text, spans)
            spans = _merged(spans + self._token_run_spans(rendered, ostart, oend, width))
        return spans if not self.carries(_mask_spans(text, spans)) else [(0, len(text))]

    def _token_run_spans(
        self,
        rendered: str,
        ostart: list[int],
        oend: list[int],
        width: int,
    ) -> list[tuple[int, int]]:
        """The original spans of every run of ``width`` whitespace-delimited
        tokens of ``rendered`` (the text as masked so far) that carries a
        probe, left to right in one pass: a run that carries one is masked
        whole and the next run starts after it."""
        tokens = [m.span() for m in re.finditer(r"\S+", rendered)]
        found = []
        i = 0
        while i + width <= len(tokens):
            start, stop = tokens[i][0], tokens[i + width - 1][1]
            if self.carries(rendered[start:stop]):
                found.append((ostart[start], oend[stop - 1]))
                i += width
            else:
                i += 1
        return found

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


def _prose_words(
    probes: list[tuple[str, bool]], whole: set[str], taken: set[str]
) -> frozenset[str]:
    """The term-shaped probes of a prose value that are words of the prose: not
    its whole text and not taken by a capture rule."""
    return frozenset(
        probe
        for probe, _ in probes
        if probe not in whole
        and probe not in taken
        and ("-" in probe or "_" in probe or "." in probe)
        and _term_shaped(probe)
    )


def _captured_probes(texts: list[str | None]) -> list[tuple[str, bool]]:
    """(probe, whole-word) of every secret the capture rules recognise in
    ``texts``: the secret whole (a plain word or digits as a whole word) and
    its secret-shaped fragments."""
    # Imported here: reference_extraction imports knowledge_ingest, whose
    # credential path imports this module.
    from genesis.memory.reference_extraction import captured_secrets

    probes: list[tuple[str, bool]] = []
    for secret in captured_secrets([t for t in texts if t]):
        whole = [(_normalise(form), not _secret_token(form)) for form in _decoded(secret.strip())]
        # The secret whole is matched as the capture rules decide (a plain word
        # or digits as a whole word), never as ``_value_probes`` would probe it
        # as a value of its own; its fragments as that does.
        probes += whole
        probes += [(p, w) for p, w in _value_probes(secret) if p not in {q for q, _ in whole}]
    # A secret of format characters only normalises to nothing: no probe.
    return [(probe, as_word) for probe, as_word in probes if probe.strip()]


def _login_of(value: str) -> str | None:
    """The login half of a "login / secret" value, normalised, or None."""
    login, sep, secret = value.strip().partition(" / ")
    return _normalise(login.strip().strip(_TOKEN_EDGE)) if sep and secret.strip() else None


def value_masks(
    value: str,
    *,
    prose: bool = False,
    detail: str | None = None,
    also: tuple[tuple, ...] = (),
) -> tuple[ValueMask, ValueMask]:
    """(the strict probe set, the identifier's) of ``value`` and the secrets the
    capture rules find in it and in ``detail``, from ONE probe pass. ``also``:
    (value, description[, prose]) of other held secrets the write must not
    repeat (the value it replaces, the entries its name resolves to, their
    recorded masked spans), probed the same way. Only the identifier exempts the
    login of a "login / secret" value (it names the account), of this value and
    of each ``also`` value alike; the tags are probed strictly.

    The prose exemption (``ValueMask``) is decided per source: a term-shaped
    probe is skipped only when every source it comes from is prose (``prose``
    for ``value``, the third item of an ``also`` entry, recorded as
    ``value_is_prose``), so a known held value or a recorded span is masked even
    in an upload's prose, and an upload's prose words are never masked in a
    delimited write's name."""
    sources = [
        (value, prose),
        *(
            (item[0], len(item) > 2 and bool(item[2]))
            for item in also
            if item[0] and item[0].strip()
        ),
    ]
    # The identifier and the tags read a line break as a space: a value that
    # spans lines is probed in that form too.
    sources += [
        (" ".join(text.splitlines()), p)
        for text, p in sources
        if " ".join(text.splitlines()) != text
    ]
    captured = _captured_probes([value, detail, *(t for item in also for t in item[:2])])
    taken = {probe for probe, _ in captured}
    probes: list[tuple[str, bool]] = []
    words: set[str] = set()
    kept: set[str] = set()
    # The identifier exempts a probe as a login only when EVERY source that
    # produced it produced it as its login: one that requires it as a secret
    # keeps it (a new password that is an old entry's login).
    as_login: set[str] = set()
    as_secret: set[str] = set()
    for text, is_prose in sources:
        own = _value_probes(text)
        probes += own
        login = _login_of(text)
        as_login |= {probe for probe, _ in own if probe == login}
        as_secret |= {probe for probe, _ in own if probe != login}
        skip = (
            _prose_words(own, {_normalise(f) for f in _decoded(text.strip())}, taken)
            if is_prose
            else frozenset()
        )
        words |= skip
        kept |= {probe for probe, _ in own if probe not in skip}
    exempt = frozenset(words - kept)
    logins = as_login - as_secret
    whole = {_normalise(form) for form in _decoded(value.strip())}
    named = [(p, w) for p, w in probes if p not in logins or p in whole]
    secrets = [text for text, _ in sources[1:]]
    return (
        ValueMask(value, probes=probes, captured=captured, exempt=exempt, secrets=secrets),
        ValueMask(value, probes=named, captured=captured, exempt=exempt, secrets=secrets),
    )


def _normalised_with_origins(text: str) -> tuple[str, list[int] | range]:
    """``text`` normalised character by character as ``_normalise`` does, and
    for each normalised character the index of the original one it came from.
    A whitespace run reads as one space, from its first character, so a mask
    over it covers the whole run in the original."""
    if text.isascii() and not _SPACING.search(text):
        # ASCII normalises to its lower case, character for character.
        return text.lower(), range(len(text))
    out: list[str] = []
    origins: list[int] = []
    for i, char in enumerate(text):
        for part in unicodedata.normalize("NFKC", char):
            if unicodedata.category(part) == "Cf":
                continue
            for folded in part.casefold():
                if folded.isspace():
                    if out and out[-1] == " ":
                        continue
                    folded = " "
                out.append(folded)
                origins.append(i)
    return "".join(out), origins


def _merged(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """``spans`` sorted, with every overlapping or touching pair joined."""
    merged: list[list[int]] = []
    for start, stop in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], stop)
        else:
            merged.append([start, stop])
    return [(start, stop) for start, stop in merged]


def _mask_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """``text`` with the (merged) spans replaced by the mask, in one join."""
    pieces, at = [], 0
    for start, stop in _merged(spans):
        pieces += [text[at:start], HELD_MASK]
        at = stop
    return "".join(pieces) + text[at:]


def _rendered(text: str, spans: list[tuple[int, int]]) -> tuple[str, list[int], list[int]]:
    """(``text`` with the merged spans masked, and for each of its characters
    the original offset it starts at and the one it ends at): a mask's
    characters all start at its span's start and end at its stop."""
    pieces: list[str] = []
    ostart: list[int] = []
    oend: list[int] = []
    at = 0
    for start, stop in _merged(spans):
        pieces += [text[at:start], HELD_MASK]
        ostart += [*range(at, start), *[start] * len(HELD_MASK)]
        oend += [*range(at + 1, start + 1), *[stop] * len(HELD_MASK)]
        at = stop
    pieces.append(text[at:])
    ostart += range(at, len(text))
    oend += range(at + 1, len(text) + 1)
    return "".join(pieces), ostart, oend


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


#: A credential's tags are masked as a whole list, and only then capped; the
#: originals are held in the secret row's meta.
MAX_TAGS = 64
MAX_TAG_CHARS = 256


#: Times ``cap_masked`` masks a cut again before it holds the whole field out.
_CAP_PASSES = 8


def _cut(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters, never through a mask: a cut that
    would split one cuts before it."""
    if len(text) <= limit:
        return text
    # The last mask that starts before the cut: when it runs past it, cut before it.
    at = text.rfind(HELD_MASK, 0, limit + len(HELD_MASK) - 1)
    if at != -1 and at < limit < at + len(HELD_MASK):
        limit = at
    return text[:limit]


def cap_masked(text: str, limit: int, mask: ValueMask | None = None) -> str:
    """``text`` (already masked) cut to ``limit`` characters, never through a
    mask. Masking runs on the WHOLE text first, so the cut never leaves part of
    a secret; but a cut can MAKE a match ("tigers" cut to "tiger", a whole-word
    value). Given ``mask``, a cut that carries the value is masked again and cut
    again, up to ``_CAP_PASSES`` times, and is ``HELD_MASK`` if it still
    carries it. Without ``mask`` nothing is checked after the cut."""
    cut = _cut(text, limit)
    if mask is None:
        return cut
    for _ in range(_CAP_PASSES):
        if not mask.carries(cut):
            return cut
        cut = _cut(mask.mask(cut), limit)
    if not mask.carries(cut):
        return cut
    return HELD_MASK if limit >= len(HELD_MASK) else ""


def cap_tag(tag: object, mask: ValueMask | None = None) -> object:
    """A masked tag as a credential row indexes it: at most ``MAX_TAG_CHARS``
    (a non-string one longer than that as its capped JSON text), cut with
    ``cap_masked`` (``mask``: the cut is checked against the value)."""
    if isinstance(tag, str):
        return cap_masked(tag, MAX_TAG_CHARS, mask)
    text = json.dumps(tag, ensure_ascii=False, default=str)
    return tag if len(text) <= MAX_TAG_CHARS else cap_masked(text, MAX_TAG_CHARS, mask)


#: Passes ``_serialised_clean`` makes before it gives up on a tag list.
_SERIALISED_PASSES = 4


def _serialised_clean(mask: ValueMask, tags: list) -> str | None:
    """``tags`` as JSON, checked as it is stored (``clean_tag_runs``), or None
    when the list never comes clean (the row's default tags)."""
    tags = clean_tag_runs(mask, tags, json.dumps, "[", "]")
    return None if tags is None else json.dumps(tags)


def clean_tag_runs(mask: ValueMask, tags: list, render, prefix: str, suffix: str) -> list | None:
    """``tags`` as rendered (``prefix + ", ".join(render(t)) + suffix``: their
    JSON, or the ``Tags:`` line), each run of tags a span of the value touches
    masked as one tag: rendering can complete a value no single tag carries (in
    literal quotes, or across ``", "``). None when it never comes clean."""
    tags = list(tags)
    for _ in range(_SERIALISED_PASSES):
        parts = [render(t) for t in tags]
        spans = mask.mask_spans(prefix + ", ".join(parts) + suffix)
        if not spans:
            return tags
        bounds, at = [], len(prefix)
        for part in parts:
            bounds.append((at, at + len(part)))
            at += len(part) + 2
        runs = []
        for start, stop in spans:
            # A span in a separator or the prefix touches the tags beside it.
            hit = [i for i, (lo, hi) in enumerate(bounds) if lo - 2 < stop and hi + 2 > start]
            if not hit:
                return None
            runs.append((hit[0], hit[-1] + 1))
        merged = _merged(runs)
        if all(tags[lo:hi] == [HELD_MASK] for lo, hi in merged):
            # Only a lone mask, or the rendering's own frame, spells it.
            return None
        for lo, hi in reversed(merged):
            tags[lo:hi] = [HELD_MASK]
    return None
