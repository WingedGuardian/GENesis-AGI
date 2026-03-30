"""Inbox scanner — stateless filesystem functions for change detection."""

from __future__ import annotations

import hashlib
from pathlib import Path

RESPONSE_SUFFIX = ".genesis.md"


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
    """Compute SHA-256 hex digest of file content."""
    h = hashlib.sha256()
    h.update(file_path.read_bytes())
    return h.hexdigest()


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
