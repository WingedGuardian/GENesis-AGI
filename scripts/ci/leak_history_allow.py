#!/usr/bin/env python3
"""Drop owner-verified historical false positives from a detect-secrets history scan.

The branch leak scan's history half (``step_detect_secrets`` in
``scripts/ci/leak_scan.sh``) scans every line the branch's commits added, so a
harmless high-entropy string committed without an inline
``# pragma: allowlist secret`` fails every later push of every branch carrying
that commit: force-push is blocked, and a squash merge keeps the original commit
off ``main``. This removes exactly those findings and nothing else.

An entry names one finding by four exact fields: the commit (full sha), the
path, the detector type and ``sha1``, detect-secrets' ``hashed_secret`` of the value. The value itself
is never written down. A finding is dropped only when all four match, so the
same string in another commit or file, or another detector's hit on the same
line, still fails.

The list is read from ``origin/main`` and never from the branch under scan, so
an entry takes effect only after a reviewed merge. A branch that runs the
unmodified scanner cannot silence its own finding by adding an entry in the same
push. (A branch that edits this script is out of scope; branch-leak-scan.yml says
the same of the whole scan.)

Usage: leak_history_allow.py <detect-secrets-results.json>
  Rewrites the results file in place without the allowlisted findings, and
  prints each one it drops. Exit 0 on success. Exit 3 when the list on
  origin/main is malformed or origin/main cannot be read: an unreadable list is
  never an empty one, so the caller fails closed.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys

#: Repository-root path of the list. It sits outside the detect-secrets roots
#: (src/ config/ scripts/ .github/), whose entropy detectors would flag its hex
#: values. gitleaks scans the whole tree, so no field name may carry one of its
#: generic-rule keywords (secret, key, token, ...): the hash field is ``sha1``,
#: and a test runs the pinned gitleaks over this file when it is available.
ALLOW_PATH = ".leak-scan-history-allow.json"
#: The ref the list is read from. The scan itself already requires it.
ALLOW_REF = "refs/remotes/origin/main"

_HEX40 = re.compile(r"[0-9a-f]{40}")
_FIELDS = {"commit", "path", "type", "sha1", "reason"}
#: Only the entropy detectors may be listed. Their hits are long random-looking
#: strings, so the unsalted sha1 in a public file reveals nothing. A keyword,
#: basic-auth, private-key or vendor-token hit is a real credential, or a short
#: human-chosen value whose sha1 is crackable: rotate it, never list it.
_LISTABLE_TYPES = {"Base64 High Entropy String", "Hex High Entropy String"}
#: The reason is printed into the CI log, so it cannot carry a newline, another
#: control character, or a `::` workflow command.
_REASON_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f]|::")
#: The scratch tree names each commit by this many characters
#: (``materialize`` in scripts/ci/leak_scan_added_lines.py).
_COMMIT_PREFIX = 12


class AllowlistError(Exception):
    """The list exists but cannot be trusted; the caller fails closed."""


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def read_allowlist() -> str | None:
    """The list's text on ALLOW_REF, or None when that ref holds no list."""
    if _git("rev-parse", "--verify", "--quiet", f"{ALLOW_REF}^{{commit}}").returncode != 0:
        raise AllowlistError(f"{ALLOW_REF} cannot be resolved")
    listed = _git("ls-tree", "--name-only", ALLOW_REF, "--", ALLOW_PATH)
    if listed.returncode != 0:
        raise AllowlistError(f"cannot list {ALLOW_PATH} on {ALLOW_REF}")
    if listed.stdout.strip() != ALLOW_PATH:
        return None
    shown = _git("show", f"{ALLOW_REF}:{ALLOW_PATH}")
    if shown.returncode != 0:
        raise AllowlistError(f"cannot read {ALLOW_PATH} on {ALLOW_REF}")
    return shown.stdout


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    """json.loads keeps the LAST of a repeated key, so a merge leftover could apply
    a value no reviewer saw; any repeat rejects the whole list."""
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise AllowlistError(f"{ALLOW_PATH} repeats a key in one object")
    return dict(pairs)


def parse_allowlist(text: str) -> list[dict]:
    """Validate every entry; any malformed entry rejects the whole list."""
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise AllowlistError(f"{ALLOW_PATH} is not valid JSON: {exc.msg}") from None
    if (
        not isinstance(data, dict)
        or set(data) != {"entries"}
        or not isinstance(data["entries"], list)
    ):
        raise AllowlistError(f'{ALLOW_PATH} must be an object with one key, "entries", a list')
    entries = []
    for i, entry in enumerate(data["entries"]):
        where = f"{ALLOW_PATH} entry {i}"
        if not isinstance(entry, dict) or set(entry) != _FIELDS:
            raise AllowlistError(f"{where} must have exactly the keys {sorted(_FIELDS)}")
        if not all(isinstance(entry[k], str) and entry[k].strip() for k in _FIELDS):
            raise AllowlistError(f"{where}: every field must be a non-empty string")
        if not _HEX40.fullmatch(entry["commit"]):
            raise AllowlistError(f"{where}: commit must be a full 40-character lowercase sha")
        if not _HEX40.fullmatch(entry["sha1"]):
            raise AllowlistError(f"{where}: sha1 must be 40 lowercase hex characters")
        if entry["type"] not in _LISTABLE_TYPES:
            raise AllowlistError(
                f"{where}: type must be one of {sorted(_LISTABLE_TYPES)} (rotate a real credential, never list it)"
            )
        if _REASON_FORBIDDEN.search(entry["reason"]):
            raise AllowlistError(
                f"{where}: reason must be one line with no control characters or '::'"
            )
        path = entry["path"]
        if path.startswith("/") or ".." in path.split("/"):
            raise AllowlistError(f"{where}: path must be repository-relative")
        entries.append(entry)
    return entries


def _key(entry: dict) -> tuple[str, str, str, str]:
    return (entry["commit"][:_COMMIT_PREFIX], entry["path"], entry["type"], entry["sha1"])


def apply(results: dict, entries: list[dict]) -> list[tuple[str, dict, str]]:
    """Remove allowlisted findings from a detect-secrets results object in place.

    Result filenames are ``<commit12>/<path>``, relative to the scratch tree.
    Returns (filename, finding, reason) for each finding it removed.
    """
    allowed = {_key(e): e for e in entries}
    dropped: list[tuple[str, dict, str]] = []
    found = results.get("results", {})
    for filename in list(found):
        commit, sep, path = filename.partition("/")
        if not sep:
            continue
        kept = []
        for finding in found[filename]:
            key = (commit, path, finding.get("type"), finding.get("hashed_secret"))
            if key in allowed:
                dropped.append((filename, finding, allowed[key]["reason"]))
            else:
                kept.append(finding)
        if kept:
            found[filename] = kept
        else:
            del found[filename]
    return dropped


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.split("\n\n")[0], file=sys.stderr)
        return 2
    try:
        text = read_allowlist()
        entries = parse_allowlist(text) if text is not None else []
    except AllowlistError as exc:
        print(f"::error::history allowlist: {exc}. Failing closed.")
        return 3
    if not entries:
        return 0
    with open(argv[1], encoding="utf-8") as fh:
        results = json.load(fh)
    dropped = apply(results, entries)
    with open(argv[1], "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
    for filename, finding, reason in dropped:
        # The location and detector only; the scanner never reports the value.
        print(
            f"history allowlist: dropped {finding.get('type')} at {filename}"
            f":{finding.get('line_number')} ({reason})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
