"""Reversible four-column TSV rows; framing alone does not prove a clean census.

Backslash, tab, LF and CR are escaped. Other Unicode and surrogateescaped OS
bytes remain intact. Readers split only literal LF/tab, never splitlines().
"""

_ESCAPES = str.maketrans({"\\": "\\\\", "\t": "\\t", "\n": "\\n", "\r": "\\r"})
_UNESCAPES = {"\\": "\\", "t": "\t", "n": "\n", "r": "\r"}


def encode(rows):
    text = "".join("\t".join(field.translate(_ESCAPES) for field in row) + "\n" for row in rows)
    return text.encode("utf-8", "surrogateescape")


def _unescape(field):
    if "\r" in field:
        raise ValueError("unescaped CR in audit report field")
    result = []
    characters = iter(field)
    for character in characters:
        if character == "\\":
            escaped = next(characters, None)
            if escaped not in _UNESCAPES:
                raise ValueError("invalid audit report escape")
            character = _UNESCAPES[escaped]
        result.append(character)
    return "".join(result)


def decode(payload):
    if not payload:
        return []
    if not payload.endswith(b"\n"):
        raise ValueError("unterminated audit report row")
    rows = []
    for line in payload[:-1].decode("utf-8", "surrogateescape").split("\n"):
        fields = line.split("\t")
        if len(fields) != 4:
            raise ValueError("audit report row must have four fields")
        rows.append(tuple(_unescape(field) for field in fields))
    return rows
