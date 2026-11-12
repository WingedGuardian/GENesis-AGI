#!/usr/bin/env python3
"""The secret values this install must keep out of a Claude Code session, and every spelling of them.

Runs under any ``python3``; callers prefer the install's venv interpreter, where python-dotenv (the parser the
runtime itself loads this file with) is importable and is used. Without it a fallback parser covers the shapes
a secrets file holds, with ``${VAR}`` expansion left out. It loads ``src/genesis/secrets_location.py`` by path
(that module is stdlib-only by contract), so the file it reads is the one every other reader resolves.

What counts as a secret: a value of at least ``MIN_LEN`` characters whose variable NAME marks it
(``_SECRET_NAME``), or that is a URL carrying credentials (``scheme://user:pass@host``). Selection is by name
because an entropy rule cannot tell a token from a timezone or a model name (measured on a live install: both
score as high as real keys), and redacting every long value would rewrite paths, hosts and model names the
session needs. Shorter secrets are left out: a value-match on a handful of characters would redact ordinary
text.

Every value is emitted in each spelling a tool's output can carry it in: as written, URL-encoded,
JSON-escaped, the stable middle of a base64 or base64url encoding at each of the three byte alignments (a value
inside a larger base64 blob, such as an HTTP basic-auth header, lands on one of them), and, for a multi-line
value, each of its long lines (a line-by-line reader or a split write delivers one line at a time).

``--json`` prints ``{path, mtime_ns, size, needles: [{var, text, raw}]}`` on stdout, longest needle first.
That stdout is the only place a value ever goes. Exit 0 with no needles when the file is absent; exit 2 when
it exists but cannot be read.
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

MIN_LEN = 16
_SECRET_NAME = re.compile(
    r"KEY|TOKEN|SECRET|PASS|PWD|CREDENTIAL|AUTH|PRIVATE|COOKIE|SESSION|WEBHOOK|DSN|SALT|SIGNING",
    re.IGNORECASE,
)
#: A URL with a password in it: the whole value is a credential whatever the variable is called.
_CREDENTIAL_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/@\s:]+:[^/@\s]+@")
_ASSIGN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*(.*)$")


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


_DOUBLE_ESCAPES = {"n": "\n", "r": "\r", "t": "\t"}


def _fallback_parse(text: str) -> dict[str, str]:
    """KEY=VALUE pairs without python-dotenv: quoted values (which may span lines and be followed by a comment),
    ``export``, comments. ``${VAR}`` expansion is not done. Tests check parity on these shapes."""
    pairs: dict[str, str] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _ASSIGN.match(line)
        if not match:
            continue
        key, rest = match[1], match[2].strip()
        quote = rest[:1]
        if quote in ("'", '"'):
            body = rest[1:]
            while True:  # find the closing quote, joining lines until it appears
                end = body.find(quote) if quote == "'" else _closing_double(body)
                if end >= 0 or i >= len(lines):
                    break
                body += "\n" + lines[i]
                i += 1
            value = body[: end if end >= 0 else len(body)]
            if quote == '"':
                value = re.sub(r"\\([\\\"nrt])", lambda m: _DOUBLE_ESCAPES.get(m[1], m[1]), value)
            pairs[key] = value
        else:
            pairs[key] = re.split(r"\s+#", rest, maxsplit=1)[0].strip()
    return pairs


def _closing_double(body: str) -> int:
    """Index of the first unescaped double quote in ``body``, or -1."""
    escaped = False
    for idx, ch in enumerate(body):
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == '"':
            return idx
    return -1


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
    """Every form of ``value`` a tool's output can carry, each at least MIN_LEN characters."""
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
    forms.update(line.strip() for line in value.splitlines())
    return {f for f in forms if len(f) >= MIN_LEN}


def needles(pairs: dict[str, str]) -> list[dict]:
    """Secret-named values of at least MIN_LEN characters in every spelling, longest first.

    ``raw`` marks the value as written in the file: the only spelling a placeholder may be turned back into.
    """
    out: dict[str, tuple[str, bool]] = {}
    for var, value in pairs.items():
        if _is_secret(var, value):
            for text in spellings(value):
                if text not in out or text == value:
                    out[text] = (var, text == value)
    return [
        {"var": var, "text": text, "raw": raw}
        for text, (var, raw) in sorted(out.items(), key=lambda kv: (-len(kv[0]), kv[0]))
    ]


def load(root: Path) -> dict:
    """The needles for this install, with the file identity the caller re-checks to know when to reload."""
    path = secrets_path(root)
    try:
        st = path.stat()
    except FileNotFoundError:
        return {
            "path": str(path),
            "realpath": os.path.realpath(path),
            "mtime_ns": 0,
            "size": 0,
            "needles": [],
        }
    text = path.read_text(encoding="utf-8", errors="replace")
    return {
        "path": str(path),
        "realpath": os.path.realpath(path),
        "mtime_ns": st.st_mtime_ns,
        "size": st.st_size,
        "needles": needles(parse_dotenv(text)),
    }


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
