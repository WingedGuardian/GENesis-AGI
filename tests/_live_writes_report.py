"""Reversible four-column TSV rows; framing alone does not prove a clean census.

Backslash, tab, LF and CR are escaped. Other Unicode and surrogateescaped OS
bytes remain intact. Readers split only literal LF/tab, never splitlines().
"""

import hashlib
import json
from pathlib import Path

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


def observation_state(state):
    return {"run_id": state.census_id, "installed": state.installed,
            "disarmed": state.disarmed, "faults": state.faults, "exitstatus": state.exitstatus}


def census_state(state):
    return {**observation_state(state), "unreported": list(state.unreported),
            "expected_workers": sorted(state.expected_workers),
            "merged_workers": sorted(state.merged_workers), "workers": dict.copy(state.worker_status)}


def publish(report, hits, metadata):
    """Publish data then an opted-in proof. Failed/partial pairs are not clean.

    Fresh CI directories and unique run IDs are required: this is not an atomic
    transaction for an arbitrarily reused same-ID directory.
    """
    report = Path(report)
    status = Path(str(report) + ".status.json")
    payload = encode(hits)
    if metadata["run_id"]:
        status.unlink(missing_ok=True)
    report.write_bytes(payload)
    if metadata["run_id"]:
        receipt = {**metadata, "schema": 1, "format": "escaped-tsv-v1", "row_count": len(hits),
                   "sha256": hashlib.sha256(payload).hexdigest()}
        status.write_text(json.dumps(receipt, ensure_ascii=True, sort_keys=True) + "\n", encoding="utf-8")
