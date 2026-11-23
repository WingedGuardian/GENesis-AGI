"""Credential scrub for procedure text, applied where every procedure is written (#4859).

Procedures are recalled into any session whose work matches them, so procedure
text is kept free of credential values. Struggle extraction also asks the model
not to embed secrets (``judge.py``); this is the code-level layer under that.

Two layers:
* The repo's recall-first scrubber (``scripts/hooks/secret_scrub.py``, reached
  through the transcript-analytics adapter): key shapes and ``NAME=value`` /
  ``label: value`` forms. It redacts the VALUE only, so the rest of a command
  survives.
* Procedure-specific forms: the literal value an ``sshpass`` command gives its
  ``-p`` option (read as the shell and getopt read it), and ``password <value>`` in
  prose, bare or quoted. A ``$VAR``, ``${...}`` or ``$(...)`` names a secret and is kept,
  except inside single quotes, where the shell reads it as literal text.

The scrub runs on parsed values (strings inside lists and dicts), never on JSON
text. When the shared scrubber cannot run, ``ScrubFailed`` tells the caller to
withhold that field rather than store it raw.
"""

from __future__ import annotations

import re
from typing import Any


class ScrubFailed(Exception):
    """The shared scrubber could not run; the caller withholds the text."""


_NOT_A_VALUE = r"(?![$`(])"  # a shell variable or substitution names a secret
# sshpass -p takes one argument and it is always the password. The scrub reads
# sshpass's own options the way the shell and getopt do (option string
# +f:d:p:P:heVv): words are split and their quotes removed first, so a clustered
# -vp, a quoted option letter, an option argument that starts with a hyphen and a
# value on the next line are all found. Options end at the first word that is not
# one, the command sshpass runs, so a later -p (ssh's port) is not sshpass's. Each
# -p value is redacted as one shell word: a backslash continues it, and a quote
# may span lines when it closes at the end of a word; an unclosed quote stops at
# the line end. An option letter not known to be a flag is read as taking an
# argument, so an unknown option can only over-redact. A value starting with a
# variable or substitution ($NAME, ${...}, $(...), `...`), unquoted or double-quoted,
# names a secret and is kept; single quotes, a backslash, $'...' and $"..." make
# it literal text, which is redacted.
_WORD_END = r"(?=[\s;|&<>()]|\Z)"
_SHELL_WORD = re.compile(
    r"(?:'[^'\n]*+'|'[^']*+'" + _WORD_END + r"|'[^'\n]*+"
    r"|\"(?:[^\"\\\n]|\\.)*+\"|\"(?:[^\"\\]|\\.)*+\"" + _WORD_END + r"|\"(?:[^\"\\\n]|\\.)*+"
    r"|\\.|[^\s'\"\\;|&<>()])++",
    re.DOTALL,
)
_SSHPASS = re.compile(r"(?i)\bsshpass\b")
_GAP_RUN = re.compile(r"(?:[^\S\n]|\\\r?\n)*+")
# An option that ends its line takes its argument from the next line when that
# line is not blank (wrapped prose); a blank line means it was given none here.
_NEXT_LINE = re.compile(r"\r?\n[^\S\n]*+(?=\S)")
_SSHPASS_FLAGS = frozenset("heVv")
_DOUBLE_QUOTE_ESCAPES = frozenset('$`"\\\n')


def _unquoted(word: str, base: int) -> list[tuple[str, int, bool]]:
    """A shell word's characters after quote removal, each with its offset in the
    text and whether quoting made it literal (single quotes or a backslash)."""
    chars: list[tuple[str, int, bool]] = []
    i, n = 0, len(word)
    while i < n:
        c = word[i]
        if c == "'":
            close = word.find("'", i + 1)
            close = n if close < 0 else close
            chars += [(word[k], base + k, True) for k in range(i + 1, close)]
            i = close + 1
        elif c == '"':
            i += 1
            while i < n and word[i] != '"':
                if word[i] == "\\" and i + 1 < n and word[i + 1] in _DOUBLE_QUOTE_ESCAPES:
                    if word[i + 1] != "\n":
                        chars.append((word[i + 1], base + i + 1, True))
                    i += 2
                else:
                    chars.append((word[i], base + i, False))
                    i += 1
            i += 1
        elif c == "\\":
            if i + 1 < n and word[i + 1] not in "\r\n":
                chars.append((word[i + 1], base + i + 1, True))
            i += 2
        else:
            chars.append((c, base + i, False))
            i += 1
    return chars


_EXPANDS = re.compile(r"\$[A-Za-z_0-9{(@*#?$!-]|`")


def _names_a_secret(text: str, chars: list[tuple[str, int, bool]]) -> bool:
    """A value that starts with an expanding $ or ` is a variable or substitution.
    $'...', $"..." and a $ before any other character are literal text in bash."""
    return bool(chars) and not chars[0][2] and _EXPANDS.match(text, chars[0][1]) is not None


def _password_spans(text: str, pos: int) -> tuple[list[tuple[int, int]], int]:
    """The spans of the literal values sshpass's own options (from pos) give -p, and
    where its options end: the start of the command it runs."""
    spans: list[tuple[int, int]] = []
    wanted = ""  # the option letter waiting for its argument
    while True:
        pos = _GAP_RUN.match(text, pos).end()
        if wanted and text.startswith(("\n", "\r\n"), pos):
            following = _NEXT_LINE.match(text, pos)
            pos = following.end() if following else pos
        word = _SHELL_WORD.match(text, pos)
        if word is None:
            return spans, pos
        chars = _unquoted(word.group(), word.start())
        if wanted:
            # An empty argument ("") is the password itself: nothing to redact.
            if wanted == "p" and chars and not _names_a_secret(text, chars):
                spans.append(word.span())
            wanted = ""
            pos = word.end()
            continue
        option = "".join(c for c, _, _ in chars)
        if not option.startswith("-") or option in ("-", "--"):
            return spans, word.start() if option != "--" else word.end()
        pos = word.end()
        for index in range(1, len(chars)):
            letter = chars[index][0]
            if letter in _SSHPASS_FLAGS:
                continue
            rest = chars[index + 1 :]
            if not rest:
                wanted = letter
            elif letter == "p" and not _names_a_secret(text, rest):
                spans.append((chars[index][1] + 1, word.end()))
            break


def _redact_sshpass(text: str) -> tuple[str, int]:
    """Redact every literal value an sshpass command gives its -p option."""
    out: list[str] = []
    pos = search = count = 0
    while (word := _SSHPASS.search(text, search)) is not None:
        spans, search = _password_spans(text, word.end())
        for start, end in spans:
            out.append(text[pos:start] + "[redacted]")
            count += 1
            pos = end
    out.append(text[pos:])
    return "".join(out), count


_LABEL = r"\b(?:with\s+)?(?:password|passwd|pwd|passphrase)\b(?:\s+is|\s*[:=])?"
# Quoted: any 3+ characters between matching quotes. Each body stops only at its
# own quote (in double quotes and backticks, not one escaped with a backslash), so
# a value holding the other kind or an escaped quote is redacted whole. The closing
# quote is required: a prompt such as -P "Enter password:" ends in a quote that
# opens nothing.


def _quoted(quote: str) -> str:
    """One quoted value. A backslash escapes the quote inside double quotes and
    backticks, so that arm is tried first; the plain body follows, so a value
    ending in a backslash (C:\\keys\\) is still caught. Single quotes have no
    escapes and no expansion in the shell, so their body is plain and a $ in it is
    literal text, not a variable."""
    plain = r"[^" + quote + r"\n]{3,}" + quote
    if quote == "'":
        return quote + plain
    escaped = r"(?:[^" + quote + r"\\\n]|\\.){3,}" + quote
    return quote + _NOT_A_VALUE + r"(?:" + escaped + "|" + plain + ")"


_PASSWORD_QUOTED = re.compile(
    r"(?i)(" + _LABEL + r"\s*)(?:" + _quoted("'") + "|" + _quoted('"') + "|" + _quoted("`") + ")"
)
# Bare: 6+ characters with a digit or symbol (an identifier like reference_lookup is
# not one), not a path or flag. A leading $ names a variable and is kept; a later $
# counts as a symbol. "password reset" and "password manager" never match;
# "password 2fa-enabled" does, which costs one redacted phrase.
_PASSWORD_BARE = re.compile(
    r"(?i)("
    + _LABEL
    + r"\s+)"
    + _NOT_A_VALUE
    + r"(?![-/~<{*])(?=[^\s'\"`,;)]*[\d!@#$%^&*+=?~])[^\s'\"`,;)]{6,}"
)
# What a string the scrub could not process is stored as.
WITHHELD = "[withheld: credential scrub could not run]"


def _shared_scrub(text: str) -> str:
    from genesis.transcript_analytics import scrub as shared

    if not shared.available():
        raise ScrubFailed("shared scrubber unavailable")
    result, failed = shared.scrub_text(text)
    if failed or result is None:
        raise ScrubFailed("shared scrubber withheld the text")
    return result


def credential_key(key: str) -> bool:
    """True when the shared policy withholds a member's value by its key alone."""
    from genesis.transcript_analytics import scrub as shared

    verdict = shared.credential_key(key)
    if verdict is None:
        raise ScrubFailed("shared scrubber unavailable")
    return verdict


def _credential_key_or_withhold(key: str, found: list[str]) -> bool:
    """credential_key; when the policy cannot answer, the value is withheld too
    (True), so it is never stored unchecked."""
    try:
        return credential_key(key)
    except ScrubFailed:
        found.append("withheld")
        return True


def scrub_text(text: str) -> tuple[str, list[str]]:
    """Redact credential spans; return the text and the names of what matched.
    Raises ScrubFailed when the shared scrubber cannot run."""
    found: list[str] = []
    text, count = _redact_sshpass(text)
    if count:
        found.append("sshpass")
    for name, pattern in (
        ("password", _PASSWORD_QUOTED),
        ("password", _PASSWORD_BARE),
    ):
        text, count = pattern.subn(r"\1[redacted]", text)
        if count:
            found.append(name)
    shared = _shared_scrub(text)
    if shared != text:
        found.append("shared")
    return shared, found


# The field names procedure documents are written with: a failure mode
# (operations.record_failure) and a lesson (lesson_shape._LESSON_KEYS, pinned by a
# test). They name fields, never carry a value, so they are kept as they are and a
# value under one is scrubbed as a value; that keeps a document's shape when the
# shared scrubber cannot run (times_hit stays an int).
_SCHEMA_KEYS = frozenset(
    {
        "description",
        "conditions",
        "times_hit",
        "transient",
        "authored_via",
        "authored_in",
        "origin",
        "reviewed",
        "authored_at",
    }
)


def _scrub_key(key: Any, found: list[str], index: int) -> Any:
    """A dict key, scrubbed. A known field name (``_SCHEMA_KEYS``) is kept; when
    the scrub cannot run, any other key is withheld by position."""
    if not isinstance(key, str):
        return scrub_value(key, found)
    if key in _SCHEMA_KEYS:
        return key
    try:
        result, names = scrub_text(key)
    except ScrubFailed:
        found.append("withheld")
        return f"[withheld key {index + 1}]"
    found += names
    return result


def scrub_value(value: Any, found: list[str]) -> Any:
    """Scrub every string inside ``value`` (str, list, tuple, dict); others pass.

    A dict member under a credential-named key (``password``, ``token``,
    ``api_key``: the shared structured policy) is always redacted; keys are
    scrubbed too (``_scrub_key``). A string the scrub cannot process becomes
    ``WITHHELD`` on its own, so its siblings and the shape around it survive;
    ``found`` then names "withheld". Keys that scrub to the same text are both kept,
    the later one numbered."""
    if isinstance(value, str):
        try:
            result, names = scrub_text(value)
        except ScrubFailed:
            found.append("withheld")
            return WITHHELD
        found += names
        return result
    if isinstance(value, list):
        return [scrub_value(v, found) for v in value]
    if isinstance(value, tuple):
        return tuple(scrub_value(v, found) for v in value)
    if isinstance(value, dict):
        out: dict = {}
        for k, v in value.items():
            key = _scrub_key(k, found, len(out))
            while key in out:  # two keys scrubbed to the same text: keep both
                key = f"{key} ({len(out) + 1})"
            if (
                isinstance(k, str)
                and v is not None
                and k not in _SCHEMA_KEYS
                and _credential_key_or_withhold(k, found)
            ):
                out[key] = "[redacted]"
                found.append("key")
            else:
                out[key] = scrub_value(v, found)
        return out
    return value
