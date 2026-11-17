"""Masking a held-out credential value in the fields that are indexed (#2426).

The probes of a value for masking (``_value_probes`` here: the secret parts of
the value besides its fragments; ``reference_ops._value_probes`` stays the
refusal check's) feed ``ValueMask`` and its span matcher, the probe sets a
write builds (``value_masks``), and the caps that cut a masked field never
through a mask. Every indexed field of a
credential (its identifier and tags) is masked with these before it is stored;
the original is held in ``reference_secrets``.
"""

from __future__ import annotations

import json
import re
import unicodedata

from genesis.db.crud.knowledge import DETAIL_PLACEHOLDER, SECRET_PLACEHOLDER
from genesis.memory.reference_ops import (
    _FRAGMENT_SPLIT,
    _HOSTNAME,
    _MIN_TOKEN,
    _SCHEME,
    _SPACING,
    _TOKEN_EDGE,
    _decoded,
    _normalise,
    _secret_token,
)

#: What a masked span of a held-out value reads as in an indexed field. Short,
#: fixed, and no credential keyword, so it is no secret-scanner hit.
HELD_MASK = "[held]"

#: A URL scheme, as it ends just before ``://``.
_SCHEME_BEFORE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]{0,31}$")
#: A well-formed URL authority (``userinfo@host:port``), up to its path,
#: query or fragment.
_AUTHORITY = re.compile(r"[^\s/?#]*")
#: How far past ``://`` a userinfo with an unencoded ``/ ? #`` or space in its
#: password is looked for (it ends at the ``@`` before the host).
_USERINFO_MAX = 256
#: A host as it follows a userinfo's ``@``, then its port, ending the authority.
_USERINFO_HOST = re.compile(
    r"(?:[a-z0-9-]+(?:\.[a-z0-9-]+)*|\[[0-9a-f:.]+\])(?::\d{1,5})?(?=[/?#\s]|$)",
    re.IGNORECASE,
)
#: What a host and port read as when the text before an ``@`` is a path, not
#: a userinfo (``host:8080/path@other``): a port, then a path, query or space.
_PORT_LEAD = re.compile(r"\d{1,5}(?:[/?#\s]|$)")


def _url_passwords(form: str):
    """The password of every URL userinfo (the part after the user name and its
    colon, up to the ``@`` before the host) in
    ``form``. A well-formed authority (up to the first ``/ ? #`` or space) that
    holds an ``@`` ends its userinfo at its LAST ``@``. One that holds none may
    still carry a password with an unencoded ``/ ? #`` or space in it
    (``_loose_userinfo``). Found from each ``://``, so a text without one
    costs one scan."""
    at = form.find("://")
    while at != -1:
        start = at + 3
        if _SCHEME_BEFORE.search(form, max(0, at - 32), at):
            authority = form[start:_AUTHORITY.match(form, start).end()]
            userinfo = authority.rpartition("@")[0] if "@" in authority else _loose_userinfo(form, start)
            _user, colon, password = (userinfo or "").partition(":")
            if colon and password:
                yield password
        at = form.find("://", start)


def _loose_userinfo(form: str, start: int) -> str | None:
    """The userinfo after the ``://`` that ends at ``start``, when its
    password holds an unencoded ``/ ? #`` or space: it runs to the ``@`` a host
    follows, the last one before the first space or, when there is none before
    it, the first one after it, looking no further than ``_USERINFO_MAX``
    characters and never past a line break or another ``://``. None when the
    text before that ``@`` is no userinfo: a login (before the first ``:``)
    holding ``/ ? #``, a space or ``@``, or a port then a path
    (``host:8080/path@x``), so an ``@`` in a URL's path is never read as one."""
    end = min(len(form), start + _USERINFO_MAX)
    for stop_at in ("\n", "://"):
        cut = form.find(stop_at, start, end)
        if cut != -1:
            end = cut
    window = form[start:end]
    space = next((i for i, c in enumerate(window) if c.isspace()), len(window))
    before = [i for i in range(window.rfind("@", 0, space), -1, -1) if window[i] == "@"]
    after = [i for i in range(space, len(window)) if window[i] == "@"]
    for i in [*before, *after]:
        if _USERINFO_HOST.match(window, i + 1):
            user, colon, password = window[:i].partition(":")
            if not colon or any(c in user for c in "/?#@") or any(c.isspace() for c in user):
                return None
            return None if _PORT_LEAD.match(password) else window[:i]
    return None


def _secret_parts(value: str) -> list[tuple[str, bool]]:
    """(probe, whole-word only) of the parts of ``value`` that are a secret as
    written: the secret half of "login / secret" and the password of a URL's
    userinfo (``_url_passwords``). Each is probed whole, as written (normalised
    and percent-decoded as every probe is): no edge trimming, and no host-name,
    scheme or digit skip, which are for the fragments of a value. A plain word
    or an all-digit one is matched as a whole word, any other unbounded."""
    whole = value.strip()
    parts = []
    _login, sep, secret = whole.partition(" / ")
    if sep and secret.strip():
        parts.append(secret.strip())
    for form in _decoded(whole) if whole else ():
        for password in _url_passwords(unicodedata.normalize("NFKC", form)):
            # A plain-word URL password stays a stated limit, matched only
            # as a fragment (owner ruling 2026-11-17).
            if not password.isalpha():
                parts.append(password)
    probes: dict[str, bool] = {}
    for part in parts:
        for form in _decoded(part):
            probe = _normalise(form)
            if probe.strip():
                probes[probe] = probes.get(probe, True) and not _secret_token(form)
    return list(probes.items())


#: A fragment (``_FRAGMENT_SPLIT``) that is a run of at least four digits, with
#: any edge punctuation that is no separator (``.`` and ``!``), right after a
#: ``:``.
_DIGIT_AFTER_COLON = re.compile(
    r"(?<=:)[.!]*(\d{4,})[.!]*(?![^\s:/@?&=#;,|\\<>\"'`(){}\[\]])"
)
#: The host a port follows: a host name, ``localhost``, an IPv4 address, an
#: IPv6 one's closing bracket, or any label right after ``@`` or ``://``.
_PORT_HOST = re.compile(
    r"(?:(?:^|(?<=[\s:/@?&=#;,|\\<>\"'`(){}\[\]]))"
    r"(?:(?:[a-z0-9-]+\.)+[a-z]{2,}|localhost|\d{1,3}(?:\.\d{1,3}){3})"
    r"|(?:(?<=@)|(?<=://))[a-z0-9-]+|\])$",
    re.IGNORECASE,
)


def _ports_only(form: str, fragments: set[str]) -> set[str]:
    """The digit ``fragments`` of ``form`` that occur in it ONLY as a port:
    directly after a host and ``:`` (``db.example.org:5432``)."""
    ports: dict[str, int] = {}
    for match in _DIGIT_AFTER_COLON.finditer(form):
        if match.group(1) in fragments and _PORT_HOST.search(
            form, max(0, match.start() - 256), match.start() - 1,
        ):
            ports[match.group(1)] = ports.get(match.group(1), 0) + 1
    if not ports:
        return set()
    # Every whole-word occurrence of each digit run, counted in ONE pass (a
    # pass per run was quadratic in the number of distinct ports).
    words: dict[str, int] = {}
    for match in _WHOLE_DIGITS.finditer(form):
        if match.group(1) in ports:
            words[match.group(1)] = words.get(match.group(1), 0) + 1
    return {digits for digits, count in ports.items() if words.get(digits) == count}


#: A digit run as a whole word, as ``_ports_only`` counts it.
_WHOLE_DIGITS = re.compile(
    r"(?<![^\s:/@?&=#;,|\\<>\"'`(){}\[\]])[.!]*(\d+)[.!]*(?![^\s:/@?&=#;,|\\<>\"'`(){}\[\]])"
)


def _value_probes(value: str, *, login_exempt: bool = False) -> list[tuple[str, bool]]:
    """(probe, whole-word only) for the masks: the whole value, its secret
    parts (``_secret_parts``), and every secret-shaped fragment of it,
    normalised. The value is percent-decoded (up to three rounds),
    NFKC-normalised and split on whitespace and
    ``: / @ ? & = # ; , | \\ < > " ' ` ( ) { } [ ]``, so
    the password, token or key inside a connection URL, a DSN or a
    ``key=value`` line is a fragment of its own; a fragment that is a host name
    or a URL scheme is not probed. A fragment is secret-shaped as written or
    with its edge punctuation trimmed (both forms are probed then). A run of at
    least four digits is probed as a whole word, except where it is a port
    (directly after a host and ``:``). A fragment that reads as a plain word
    (all letters, or under four characters) is not secret-shaped: outside a
    secret part it is matched only as the whole value, and only as a whole
    word: an ordinary word inside another is no match. That is a stated limit.
    A probe given both unbounded and as a whole word is unbounded."""
    whole = value.strip()
    probes: dict[str, bool] = {}

    def add(probe: str, as_word: bool) -> None:
        probes[probe] = probes.get(probe, True) and as_word

    for form in _decoded(whole) if whole else ():
        add(_normalise(form), form.isalpha() or len(form) < _MIN_TOKEN)
    for probe, as_word in _secret_parts(whole):
        add(probe, as_word)
    # "login / secret" is the shape the capture writers format: with
    # ``login_exempt`` (the identifier only, which names the account) the
    # exact login half is not probed as a fragment. A secret part equal to it
    # stays a probe (above). Every other field is probed with the tokens of
    # both halves.
    login, sep, secret = whole.partition(" / ")
    exempt = login.strip().strip(_TOKEN_EDGE) if login_exempt and sep and secret.strip() else None
    for form in _decoded(whole):
        schemes = {m.group(1).casefold() for m in _SCHEME.finditer(form)}
        form = unicodedata.normalize("NFKC", form)
        # Each distinct fragment once (the same output; a long value repeats).
        fragments = dict.fromkeys(_FRAGMENT_SPLIT.split(form))
        digits = {f.strip(_TOKEN_EDGE) for f in fragments if f.strip(_TOKEN_EDGE).isdigit()}
        ports = _ports_only(form, digits) if digits and ":" in form else set()
        for raw in fragments:
            fragment = raw.strip(_TOKEN_EDGE)
            folded = fragment.casefold()
            if not fragment or fragment == exempt or folded in schemes or _HOSTNAME.match(folded):
                continue
            if fragment.isdigit():
                if len(fragment) < _MIN_TOKEN or fragment in ports:
                    continue
                add(_normalise(fragment), True)
            for shaped in dict.fromkeys((raw, fragment)):
                if _secret_token(shaped):
                    add(_normalise(shaped), False)
    probes.pop("", None)
    return list(probes.items())


#: Probes up to this long are looked up by every substring length present;
#: longer ones by their first ``_SHORT`` characters, then verified.
_SHORT = 32
#: A run of the held secrets a mask may grow over is at most this long, and a
#: probe keeps at most ``_RUNS_PER_PROBE`` of them (``_RunIndex``): a probe with
#: more, or a longer one, masks its whole token instead.
_RUN_MAX = 256
_RUNS_PER_PROBE = 64
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
    ``also`` values and the captured secrets): a match grows over runs of
    their text too, not only of ``value``'s (``_RunIndex``, bounded).

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
                _prose_words(
                    probes,
                    whole,
                    {probe for probe, _ in [*captured, *_secret_parts(value)]},
                )
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
        self._index = _RunIndex(self._whole)
        self.window = max((len(p.split()) for p in self._whole), default=1)
        # A value that spells the mask text: the mask is then not only a
        # separator (``carries``). Likewise a body placeholder: it is then not
        # only a placeholder.
        self._spells_mask = any(HELD_MASK in probe for probe, _ in kept)
        self._spells_placeholder = any(
            mark in probe for probe, _ in kept for mark in _PLACEHOLDER_PROBES
        )

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
        A value that itself spells the mask text, or a placeholder, is matched
        in the text as it stands too, so a literal copy of it never escapes as
        "a mask" or "a placeholder"."""
        if not text:
            return False
        held_out = text.replace(SECRET_PLACEHOLDER, "").replace(DETAIL_PLACEHOLDER, "")
        candidates = [held_out.replace(HELD_MASK, "\x00")]
        if self._spells_mask and HELD_MASK in held_out:
            candidates.append(held_out)
        if self._spells_placeholder and held_out != text:
            candidates.append(text.replace(HELD_MASK, "\x00"))
            if self._spells_mask and HELD_MASK in text:
                candidates.append(text)
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
        token = (0, 0)
        for start, stop, probe in self._matches(normalised):
            # The match grows left, then right, as far as it stays part of a
            # run of a secret, short of whitespace. Being part of a run is
            # monotone in the span, so the farthest edge is a binary search.
            # A probe with too many runs, or too long a one, to keep is masked
            # with its whole token instead (never a part of a secret left).
            runs = self._index.runs(probe)
            if runs is None:
                if not (token[0] <= start and stop <= token[1]):
                    token = _token_of(normalised, start, stop)
                    spans.append((origins[token[0]], origins[token[1] - 1] + 1))
                continue
            if runs:
                start, stop = _grown(normalised, start, stop, runs)
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


#: The body placeholders as a probe spells them (``carries``).
_PLACEHOLDER_PROBES = (_normalise(SECRET_PLACEHOLDER), _normalise(DETAIL_PLACEHOLDER))


class _RunIndex:
    """The whitespace-delimited runs of the held secrets a match may grow over
    (``ValueMask.mask_spans``), BOUNDED: for each probe, at most
    ``_RUNS_PER_PROBE`` distinct runs, each at most ``_RUN_MAX`` long, joined
    into one text so whether a grown span is part of a run is one ``in`` test.
    A probe with more runs than that, or one of them longer, has None: the
    match is then masked with its whole whitespace token, so the cost stays
    bounded however large the value is and no part of a secret stays visible.
    A run repeated in the value is kept once, so a value of repeated tokens
    costs one pass."""

    def __init__(self, whole: set[str]):
        self._whole = whole
        self._tokens: str | None = None
        self._runs: dict[str, str | None] = {}

    def _corpus(self, probe: str):
        # A probe with a space (a multi-word value) is looked up in the forms
        # themselves; any other in their distinct tokens.
        if " " in probe:
            return self._whole
        if self._tokens is None:
            self._tokens = " ".join(
                dict.fromkeys(t for form in self._whole for t in form.split(" ") if t)
            )
        return (self._tokens,)

    def runs(self, probe: str) -> str | None:
        """Every distinct run holding ``probe`` (other than the probe itself,
        which grows nothing), joined by a NUL; None when there are more than
        ``_RUNS_PER_PROBE`` or one is longer than ``_RUN_MAX``."""
        if probe not in self._runs:
            self._runs[probe] = self._found(probe)
        return self._runs[probe]

    def _found(self, probe: str) -> str | None:
        found: dict[str, None] = {}
        for form in self._corpus(probe):
            at = form.find(probe)
            while at != -1:
                end = at + len(probe)
                low = max(0, at - _RUN_MAX - 1)
                start = form.rfind(" ", low, at) + 1
                stop = form.find(" ", end, end + _RUN_MAX + 1)
                if stop == -1 and len(form) <= end + _RUN_MAX:
                    stop = len(form)
                if (start, stop) != (at, end):
                    if (start == 0 and low > 0) or stop == -1 or stop - start > _RUN_MAX:
                        return None
                    if form[start:stop] not in found:
                        if len(found) == _RUNS_PER_PROBE:
                            return None
                        found[form[start:stop]] = None
                at = form.find(probe, max(stop, end))
        return "\x00".join(found)


def _grown(text: str, start: int, stop: int, runs: str) -> tuple[int, int]:
    """(start, stop) of the match ``text[start:stop]`` grown left, then right,
    as far as it stays part of a run in ``runs`` (``_RunIndex.runs``), short of
    whitespace. Being part of a run is monotone in the span (a part of a part
    is a part), so each edge is a binary search: a few ``in`` tests. A run is
    at most ``_RUN_MAX`` long, so neither edge is looked for further away."""
    edge = text.rfind(" ", max(0, stop - _RUN_MAX), start)
    low, high = edge + 1 if edge != -1 else max(0, stop - _RUN_MAX), start
    while low < high:
        mid = (low + high) // 2
        if text[mid:stop] in runs:
            high = mid
        else:
            low = mid + 1
    start = high
    end = text.find(" ", stop, start + _RUN_MAX)
    low, high = stop, end if end != -1 else min(len(text), start + _RUN_MAX)
    while low < high:
        mid = (low + high + 1) // 2
        if text[start:mid] in runs:
            low = mid
        else:
            high = mid - 1
    return start, low


def _token_of(text: str, start: int, stop: int) -> tuple[int, int]:
    """(start, stop) of the whitespace-delimited token(s) of ``text`` holding
    ``text[start:stop]``."""
    end = text.find(" ", stop)
    return text.rfind(" ", 0, start) + 1, len(text) if end == -1 else end


def _prose_words(
    probes: list[tuple[str, bool]], whole: set[str], taken: set[str]
) -> frozenset[str]:
    """The probes of a prose value that are words of the prose: not its whole
    text and not taken (by a capture rule, or a secret part of the value), and
    term-shaped, a run of digits, or a plain word with its sentence
    punctuation ("4821", "Hello!")."""
    return frozenset(
        probe
        for probe, _ in probes
        if probe not in whole
        and probe not in taken
        and (
            (("-" in probe or "_" in probe or "." in probe) and _term_shaped(probe))
            or probe.isdigit()
            or (probe != probe.strip(_TOKEN_EDGE) and not _secret_token(probe.strip(_TOKEN_EDGE)))
        )
    )


def _captured_probes(texts: list[str | None]) -> tuple[list[tuple[str, bool]], list[str]]:
    """((probe, whole-word) of every secret the capture rules recognise in
    ``texts``: the secret whole (a plain word or digits as a whole word) and
    its secret-shaped fragments, and the secrets themselves)."""
    # Imported here, not at load: reference_extraction imports knowledge_ingest,
    # whose credential path is where this module is meant to be called from.
    from genesis.memory.reference_extraction import captured_secrets

    probes: list[tuple[str, bool]] = []
    secrets = sorted(captured_secrets([t for t in texts if t]))
    for secret in secrets:
        whole = [(_normalise(form), not _secret_token(form)) for form in _decoded(secret.strip())]
        # The secret whole is matched as the capture rules decide (a plain word
        # or digits as a whole word), never as ``_value_probes`` would probe it
        # as a value of its own; its fragments as that does.
        probes += whole
        probes += [(p, w) for p, w in _value_probes(secret) if p not in {q for q, _ in whole}]
    # A secret of format characters only normalises to nothing: no probe.
    return [(probe, as_word) for probe, as_word in probes if probe.strip()], secrets


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
    captured, captured_texts = _captured_probes(
        [value, detail, *(t for item in also for t in item[:2])]
    )
    taken = {probe for probe, _ in captured}
    probes: list[tuple[str, bool]] = []
    words: set[str] = set()
    kept: set[str] = set()
    # The identifier exempts a probe as a login only when EVERY source that
    # produced it produced it as its login: one that requires it as a secret
    # keeps it (a new password that is an old entry's login), and so does a
    # secret part of the source itself (a secret half equal to its login).
    as_login: set[str] = set()
    as_secret: set[str] = set()
    for text, is_prose in sources:
        own = _value_probes(text)
        probes += own
        login = _login_of(text)
        parts = {probe for probe, _ in _secret_parts(text)}
        as_login |= {probe for probe, _ in own if probe == login and probe not in parts}
        as_secret |= {probe for probe, _ in own if probe != login or probe in parts}
        skip = (
            _prose_words(own, {_normalise(f) for f in _decoded(text.strip())}, taken | parts)
            if is_prose
            else frozenset()
        )
        words |= skip
        kept |= {probe for probe, _ in own if probe not in skip}
    exempt = frozenset(words - kept)
    logins = as_login - as_secret
    whole = {_normalise(form) for form in _decoded(value.strip())}
    named = [(p, w) for p, w in probes if p not in logins or p in whole]
    secrets = [text for text, _ in sources[1:]] + captured_texts
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
