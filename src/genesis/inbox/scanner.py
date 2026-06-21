"""Inbox scanner — stateless filesystem functions for change detection."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

RESPONSE_SUFFIX = ".genesis.md"

# URL deduplication: strip volatile tracking/share params so the same article
# re-pasted with different params (e.g. a LinkedIn share from android vs
# desktop) compares equal. Query params only — URL *paths* are left intact
# (path-level share codes are too risky to strip).
_URL_IN_LINE_RE = re.compile(r"https?://[^\s]+")
_TRACKING_PARAM_PREFIXES = ("utm_", "mc_")
_TRACKING_PARAM_EXACT = frozenset({
    "rcm", "fbclid", "gclid", "igshid", "mkt_tok",
    "_hsenc", "_hsmi", "vero_id", "yclid", "msclkid", "trk", "trkemail",
})
_URL_TRAILING_PUNCT = ".,;:!?)]}'\""


def _strip_tracking_params(url: str) -> str:
    """Remove tracking query params from a single URL; leave path/fragment intact."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not (
            k.lower().startswith(_TRACKING_PARAM_PREFIXES)
            or k.lower() in _TRACKING_PARAM_EXACT
        )
    ]
    return urlunsplit((
        parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment,
    ))


def normalize_url_line(line: str) -> str:
    """Return *line* with tracking query params stripped from any URL it contains.

    Used only for dedup comparison — the original line is preserved for
    evaluation. Non-URL text is returned unchanged; trailing sentence
    punctuation after a URL is preserved.
    """
    def _repl(match: re.Match[str]) -> str:
        raw = match.group(0)
        trail = ""
        while raw and raw[-1] in _URL_TRAILING_PUNCT:
            trail = raw[-1] + trail
            raw = raw[:-1]
        return _strip_tracking_params(raw) + trail

    return _URL_IN_LINE_RE.sub(_repl, line)


def scan_folder(
    watch_path: Path,
    response_dir: str = "_genesis",
    *,
    recursive: bool = False,
) -> list[Path]:
    """List scannable files in the watch path, excluding responses, hidden, and temp."""
    if not watch_path.is_dir():
        return []
    results = []
    entries = sorted(watch_path.rglob("*")) if recursive else sorted(watch_path.iterdir())
    for item in entries:
        if item.name.startswith(".") or item.name.startswith("_") or item.name.startswith("~"):
            continue
        if item.name == response_dir:
            continue
        # In recursive mode, skip files inside excluded directories
        if recursive and any(
            part.startswith(".") or part.startswith("_") or part == response_dir
            for part in item.relative_to(watch_path).parts[:-1]
        ):
            continue
        if item.name.endswith(RESPONSE_SUFFIX):
            continue
        # Skip Dropbox/sync conflict files
        if ".sync-conflict-" in item.name or item.name.endswith(".tmp"):
            continue
        if item.is_file():
            results.append(item)
    return results


def compute_hash(file_path: Path) -> str:
    """Compute SHA-256 of normalized content (whitespace/encoding-invariant).

    Normalises before hashing so that sync-tool artefacts (BOM changes,
    CRLF↔LF conversion, trailing-whitespace cleanup) do not trigger
    spurious re-evaluations.
    """
    raw = file_path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]  # strip UTF-8 BOM
    text = raw.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n")
    normalized = "\n".join(line.rstrip() for line in text.split("\n"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def read_content(file_path: Path, max_bytes: int = 50_000) -> str:
    """Read file content as UTF-8, truncating to max_bytes."""
    raw = file_path.read_bytes()[:max_bytes]
    return raw.decode("utf-8", errors="replace")


def detect_changes(
    watch_path: Path,
    known_items: dict[str, str],
    response_dir: str = "_genesis",
    *,
    recursive: bool = False,
) -> tuple[list[Path], list[Path]]:
    """Detect new and modified files by comparing hashes.

    Args:
        watch_path: Directory to scan.
        known_items: Mapping of file_path (str) → content_hash.
        response_dir: Subdirectory name to exclude.
        recursive: When True, scan subdirectories recursively.

    Returns:
        (new_files, modified_files) — paths of changed items.
        Files that vanish between scan and hash are silently skipped.
    """
    files = scan_folder(watch_path, response_dir, recursive=recursive)
    new: list[Path] = []
    modified: list[Path] = []
    for f in files:
        key = str(f)
        try:
            current_hash = compute_hash(f)
        except (FileNotFoundError, PermissionError):
            # File vanished or became unreadable between scan and hash
            continue
        if key not in known_items:
            new.append(f)
        elif known_items[key] != current_hash:
            modified.append(f)
    return new, modified
