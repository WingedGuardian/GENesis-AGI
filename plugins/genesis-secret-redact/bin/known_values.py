#!/usr/bin/env python3
"""The secret values this install must keep out of a Claude Code session, and every spelling of them.

Runs under any ``python3``; callers prefer the install's venv interpreter, where python-dotenv (the parser the
runtime itself loads this file with) is importable and is used. Without it ``_fallback_parse`` reads the file:
a port of python-dotenv's own grammar and ``${VAR}`` interpolation (its regexes, in its order), held to it by a
differential test. It loads ``src/genesis/secrets_location.py`` by path (that module is stdlib-only by
contract), so the file it reads is the one every other reader resolves.

What counts as a secret: a value of at least ``MIN_LEN`` characters whose variable NAME marks it
(``_SECRET_NAME``), or that is a URL carrying credentials (``scheme://user:pass@host``). Selection is by name
because an entropy rule cannot tell a token from a timezone or a model name (measured on a live install: both
score as high as real keys), and redacting every long value would rewrite paths, hosts and model names the
session needs. Shorter secrets are left out: a value-match on a handful of characters would redact ordinary
text.

Every value is emitted in each spelling a tool's output can carry it in: as parsed, as the secrets file spells
it (a quoted value keeps its escapes there), URL-encoded, JSON-escaped, and the stable middle of a base64 or
base64url encoding at each of the three byte alignments (a value inside a larger base64 blob, such as an HTTP
basic-auth header, lands on one of them). Each long line of a multi-line value is a needle too, with a
placeholder of its own (``[secrets.env:VAR#2]``): a line-by-line reader delivers one line at a time.

``--json`` prints ``{path, realpath, home, mtime_ns, size, needles, restore, overwritten}`` on stdout:
``needles`` is ``[{var, text, placeholder}]``, longest text first; ``restore`` maps each placeholder to the text
it stands for in the secrets file itself (what a write to that file must put back); ``overwritten`` names the
variables whose value in the file is placeholder text (a write stored one, and that key is gone). That stdout is
the only place a value ever goes. Exit 0 with no needles when the file is absent; exit 2 when it exists but
cannot be read.
"""

from __future__ import annotations

import argparse
import base64
import codecs
import importlib.util
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

MIN_LEN = 16
_SECRET_NAME = re.compile(
    r"KEY|TOKEN|SECRET|PASS|PWD|CRED|AUTH|PRIVATE|COOKIE|SESSION|WEBHOOK|DSN|SALT|SIGNING|HMAC|BEARER"
    # Short markers only as a whole word of the name: as a substring, PAT would select every *_PATH.
    r"|(?:^|_)(?:PAT|PW|SK|PSK)(?:_|$)",
    re.IGNORECASE,
)
#: A URL with credentials in it: the whole value is a secret whatever the variable is called. Either
#: ``user:password@`` (the user may be empty, as in ``redis://:password@host``) or a long token alone before
#: the ``@``.
_CREDENTIAL_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://(?:[^/@\s:]*:[^/@\s]+|[^/@\s:]{16,})@")
#: The text a value is replaced by. A value that holds one is never a needle: it would turn one variable's
#: placeholder into another's.
_PLACEHOLDER = re.compile(r"\[secrets\.env:[^\]\r\n]*\]")
#: PEM armor is the same in every key file; as a needle it would rewrite unrelated output.
_PEM_ARMOR = re.compile(r"-----(?:BEGIN|END) [A-Z0-9 ]+-----")


def repo_root(explicit: str | None = None) -> Path:
    """The checkout whose resolver decides the path: explicit, then GENESIS_REPO_ROOT, then ~/genesis."""
    for candidate in (explicit, os.environ.get("GENESIS_REPO_ROOT")):
        if candidate and (Path(candidate) / "src" / "genesis" / "secrets_location.py").is_file():
            return Path(candidate)
    return Path.home() / "genesis"


def secrets_path(root: Path) -> Path:
    """The file this process's environment and checkout resolve to (the runtime's own rule)."""
    resolver = root / "src" / "genesis" / "secrets_location.py"
    spec = importlib.util.spec_from_file_location("_redact_secrets_location", resolver)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {resolver}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.resolve(environ=os.environ, repo_root=root)


# python-dotenv's grammar (its parser.py and variables.py, BSD-3-Clause), pattern for pattern and in its order.
_P_BLANK = re.compile(r"\s*")
_P_SPACE = re.compile(r"[^\S\r\n]*")
_P_EXPORT = re.compile(r"(?:export[^\S\r\n]+)?")
_P_QUOTED_KEY = re.compile(r"'([^']+)'")
_P_KEY = re.compile(r"([^=\#\s]+)")
_P_EQUALS = re.compile(r"(=[^\S\r\n]*)")
_P_SINGLE = re.compile(r"'((?:\\'|[^'])*)'")
_P_DOUBLE = re.compile(r'"((?:\\"|[^"])*)"')
_P_BARE = re.compile(r"([^\r\n]*)")
_P_COMMENT = re.compile(r"(?:[^\S\r\n]*#[^\r\n]*)?")
_P_EOL = re.compile(r"[^\S\r\n]*(?:\r\n|\n|\r|$)")
_P_REST = re.compile(r"[^\r\n]*(?:\r|\n|\r\n)?")
_P_DOUBLE_ESCAPE = re.compile(r"\\[\\'\"abfnrtv]")
_P_SINGLE_ESCAPE = re.compile(r"\\[\\']")
_P_VARIABLE = re.compile(r"\$\{([^}:]*)(?::-([^}]*))?\}")


def _unescape(pattern: re.Pattern[str], text: str) -> str:
    return pattern.sub(lambda m: codecs.decode(m[0], "unicode-escape"), text)


def _interpolate(value: str, env: dict[str, str | None]) -> str:
    """``${NAME}`` and ``${NAME:-default}`` in ``value``, as python-dotenv resolves them."""
    return _P_VARIABLE.sub(lambda m: env.get(m[1], m[2] if m[2] is not None else "") or "", value)


def _bindings(text: str) -> list[tuple[str, str | None, str | None]]:
    """``(key, value before interpolation, the value's text as the file spells it)`` per binding, in file order.

    One binding at a time, as python-dotenv's ``parse_binding`` does: a line it cannot read is skipped whole, and
    a key with no ``=`` has no value (both ``None``). The third item is the matched text itself: what stands
    between the quotes, escapes included, or the bare value.
    """
    pos = 0

    def take(pattern: re.Pattern[str]) -> re.Match[str]:
        nonlocal pos
        match = pattern.match(text, pos)
        if match is None:
            raise ValueError
        pos = match.end()
        return match

    found: list[tuple[str, str | None, str | None]] = []
    while True:
        take(_P_BLANK)
        if pos >= len(text):
            return found
        try:
            take(_P_EXPORT)
            key = None
            if text[pos : pos + 1] != "#":
                key = take(_P_QUOTED_KEY if text[pos : pos + 1] == "'" else _P_KEY)[1]
            take(_P_SPACE)
            value = spelled = None
            if text[pos : pos + 1] == "=":
                take(_P_EQUALS)
                first = text[pos : pos + 1]
                if first == "'":
                    spelled = take(_P_SINGLE)[1]
                    value = _unescape(_P_SINGLE_ESCAPE, spelled)
                elif first == '"':
                    spelled = take(_P_DOUBLE)[1]
                    value = _unescape(_P_DOUBLE_ESCAPE, spelled)
                elif first in ("", "\n", "\r"):
                    value = spelled = ""
                else:
                    value = spelled = re.sub(r"\s+#.*", "", take(_P_BARE)[1]).rstrip()
            take(_P_COMMENT)
            take(_P_EOL)
        except ValueError:
            take(_P_REST)
            continue
        if key is not None:
            found.append((key, value, spelled))


def _fallback_parse(text: str) -> dict[str, str]:
    """KEY=VALUE pairs as python-dotenv's ``dotenv_values`` gives them, without python-dotenv: each value is
    interpolated against the process environment overlaid with the keys read so far."""
    resolved: dict[str, str | None] = {}
    for key, value, _ in _bindings(text):
        resolved[key] = None if value is None else _interpolate(value, {**os.environ, **resolved})
    return {k: v for k, v in resolved.items() if v is not None}


def _file_spellings(text: str) -> dict[str, tuple[str, bool]]:
    """Per variable, ``(the value's text as the file spells it, whether the value is interpolated)``.

    The last binding of a name wins, as it does for the value. An interpolated value (``${OTHER}-suffix``) has
    no spelling of its own in the file: its text there is the expression.
    """
    spelled: dict[str, tuple[str, bool]] = {}
    for key, value, text_in_file in _bindings(text):
        if value is None or text_in_file is None:
            spelled.pop(key, None)
        else:
            spelled[key] = (text_in_file, _P_VARIABLE.search(value) is not None)
    return spelled


def parse_dotenv(text: str) -> dict[str, str]:
    """KEY=VALUE pairs as python-dotenv reads them: python-dotenv itself when importable, else the fallback."""
    try:
        from io import StringIO

        from dotenv import dotenv_values
    except ImportError:
        return _fallback_parse(text)
    return {k: v for k, v in dotenv_values(stream=StringIO(text)).items() if v is not None}


def _is_secret(var: str, value: str) -> bool:
    return len(value) >= MIN_LEN and (
        bool(_SECRET_NAME.search(var)) or bool(_CREDENTIAL_URL.match(value))
    )


def spellings(value: str) -> set[str]:
    """Every encoded form of ``value`` a tool's output can carry, each at least MIN_LEN characters."""
    forms = {
        value,
        urllib.parse.quote(value, safe=""),
        urllib.parse.quote_plus(value),
        json.dumps(value)[1:-1],
    }
    data = value.encode()
    for offset in range(3):
        chunk = data[offset:]
        chunk = chunk[: len(chunk) - len(chunk) % 3]
        core = base64.b64encode(chunk).decode()
        forms.update({core, core.replace("+", "-").replace("/", "_")})
    return {f for f in forms if len(f) >= MIN_LEN}


def select(pairs: dict[str, str], source: str = "") -> tuple[list[dict], dict[str, str]]:
    """``(needles, restore)`` for the parsed ``pairs`` of a secrets file whose text is ``source``.

    ``needles``: ``{var, text, placeholder}`` for every spelling of every secret value, longest text first.
    ``restore``: placeholder -> the text it stands for in the file. For a whole value that is the file's own
    spelling of it (so a line a session reads and writes back unchanged comes out byte for byte), or the
    parsed value when ``source`` does not hold the variable.
    """
    spelled = _file_spellings(source)
    found: dict[str, tuple[str, str]] = {}
    restore: dict[str, str] = {}
    for var, value in pairs.items():
        if not _is_secret(var, value) or _PLACEHOLDER.search(value):
            continue
        whole = f"[secrets.env:{var}]"
        in_file, interpolated = spelled.get(var, (value, False))
        restore[whole] = in_file
        forms = spellings(value)
        if not interpolated and len(in_file) >= MIN_LEN:
            forms.add(in_file)
        for text in forms:
            found.setdefault(text, (var, whole))
        # The file's lines first: where both give a line, the file's spelling is the one to restore.
        for origin in (value,) if interpolated else (in_file, value):
            lines = origin.splitlines()
            for number, line in enumerate(lines if len(lines) > 1 else [], 1):
                line = line.strip()
                if len(line) >= MIN_LEN and not _PEM_ARMOR.fullmatch(line):
                    placeholder = f"[secrets.env:{var}#{number}]"
                    found.setdefault(line, (var, placeholder))
                    restore.setdefault(placeholder, line)
    ordered = sorted(found.items(), key=lambda kv: (-len(kv[0]), kv[0]))
    return [{"var": v, "text": t, "placeholder": p} for t, (v, p) in ordered], restore


def needles(pairs: dict[str, str], source: str = "") -> list[dict]:
    """The needles of ``select``: every spelling of every secret value, longest first."""
    return select(pairs, source)[0]


def load(root: Path) -> dict:
    """The needles for this install, with the file identity the caller re-checks to know when to reload."""
    path = secrets_path(root)
    result = {
        "path": str(path),
        "realpath": os.path.realpath(path),
        "home": str(Path.home()),
        "mtime_ns": 0,
        "size": 0,
        "needles": [],
        "restore": {},
        "overwritten": [],
    }
    try:
        st = path.stat()
    except FileNotFoundError:
        return result
    # Values as the runtime reads them (newlines translated); the file's own spellings from the bytes as they
    # are, so a CRLF file's multi-line value is matched and restored with its ``\r\n``.
    with path.open(encoding="utf-8", errors="replace", newline="") as handle:
        raw = handle.read()
    pairs = parse_dotenv(path.read_text(encoding="utf-8", errors="replace"))
    selected, restore = select(pairs, raw)
    result.update(
        mtime_ns=st.st_mtime_ns,
        size=st.st_size,
        needles=selected,
        restore=restore,
        overwritten=sorted(var for var, value in pairs.items() if _PLACEHOLDER.search(value)),
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="print the needles as JSON")
    parser.add_argument("--repo-root", help="checkout whose resolver to use")
    args = parser.parse_args(argv)
    try:
        result = load(repo_root(args.repo_root))
    except (OSError, ImportError) as exc:
        print(f"known_values: cannot read the secrets file: {type(exc).__name__}", file=sys.stderr)
        return 2
    if args.json:
        json.dump(result, sys.stdout)
    else:
        print(f"{result['path']}: {len(result['needles'])} needles")
    return 0


if __name__ == "__main__":
    sys.exit(main())
