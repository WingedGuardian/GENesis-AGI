#!/usr/bin/env python3
"""PostToolUse advisory: an edit changed one member of a set, siblings remain.

Non-blocking by design. The point is LATENCY — saying this at the moment of the
edit, when reopening the question is cheap, rather than at commit time when all
the work is done, or at review round three when it costs a whole cycle.

Deliberately NOT wrapped in ``run_guard``: that wrapper fails CLOSED and is
reserved for irreversible-action guards. An advisory that blocks an edit because
its own AST parse hiccuped would be far worse than the problem it reports.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from class_scope_scan import (  # noqa: E402
    find_orphaned_literals,
    find_unrevisited_uses,
)
from hook_input import field, read_payload, session_id  # noqa: E402

_MAX_SURVIVORS_SHOWN = 4


def _repo_root(path: Path) -> Path | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path.parent, capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
    return Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None


def _committed_source(repo: Path, rel: str) -> str:
    try:
        out = subprocess.run(
            ["git", "show", f"HEAD:{rel}"],
            cwd=repo, capture_output=True, text=True, timeout=5,
        )
    except (subprocess.SubprocessError, OSError, ValueError):
        return ""
    return out.stdout if out.returncode == 0 else ""


# NOT tempfile.gettempdir(): TMPDIR points at Claude Code's working temp, which
# the tmp-watchgod service policies and which KILLS CC sessions when it fills.
# Project convention puts hook scratch under ~/tmp, which disk-hygiene prunes.
_SENTINEL_DIR = Path.home() / "tmp" / "genesis-class-scope"
_SENTINEL_TTL_SECONDS = 24 * 60 * 60
# Fallback dedup when the sentinel directory is unusable.
_IN_PROCESS_REPORTED: set[str] = set()


def _already_reported(key: str) -> bool:
    """One report per (session, file, finding). Iterating on a file must not nag.

    Sentinels are swept on write rather than accumulating: this fires on every
    Python edit, so an unbounded per-finding file would grow without limit.
    """
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    sentinel = _SENTINEL_DIR / digest
    try:
        _SENTINEL_DIR.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - _SENTINEL_TTL_SECONDS
        for stale in _SENTINEL_DIR.iterdir():
            with contextlib.suppress(OSError):
                if stale.stat().st_mtime < cutoff:
                    stale.unlink()
        # O_CREAT|O_EXCL claims the sentinel atomically. A separate
        # exists()-then-touch has a window in which two concurrent hooks both
        # see it missing and both report the same finding.
        os.close(os.open(sentinel, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        return False
    except FileExistsError:
        return True
    except OSError:
        # Cannot write the sentinel at all (read-only or full disk). Report
        # ONCE-per-process rather than on every edit: an advisory that cannot
        # remember what it said must not become a nag.
        if key in _IN_PROCESS_REPORTED:
            return True
        _IN_PROCESS_REPORTED.add(key)
        return False


def main() -> int:
    payload = read_payload()
    raw_path = field(payload, "file_path")
    if not raw_path or not raw_path.endswith(".py"):
        return 0

    edited = Path(raw_path)
    if not edited.exists():
        return 0
    repo = _repo_root(edited)
    if repo is None:
        return 0
    try:
        rel = edited.resolve().relative_to(repo.resolve()).as_posix()
        new_source = edited.read_text(encoding="utf-8")
    except (OSError, ValueError, UnicodeDecodeError):
        return 0

    old_source = _committed_source(repo, rel)
    if not old_source:
        return 0  # new file — nothing to have diverged from

    sid = session_id(payload)
    lines: list[str] = []

    for finding in find_orphaned_literals(edited, old_source, new_source, repo):
        lit = finding["literal"]
        if _already_reported(f"{sid}|lit|{rel}|{lit}"):
            continue
        survivors = [
            str(p.relative_to(repo)) for p in finding["survivors"][:_MAX_SURVIVORS_SHOWN]
        ]
        lines.append(
            f"  · you changed {lit.strip()[:60]!r} here, but the SAME text "
            f"still exists in: {', '.join(survivors)}"
        )

    for finding in find_unrevisited_uses(old_source, new_source):
        var, fn = finding["variable"], finding["function"]
        if _already_reported(f"{sid}|prov|{rel}|{fn}|{var}"):
            continue
        lines.append(
            f"  · `{var}` in {fn}() now comes from {finding['now']}() instead of "
            f"{finding['was']}() — it means something different. Uses NOT touched "
            f"by this edit: lines {finding['unrevisited']}"
        )

    if lines:
        print(
            "[class-scope] This edit may have changed one member of a set:\n"
            + "\n".join(lines)
            + "\n  Enumerate the rest before moving on — fixing the named instance "
            "and leaving siblings is what turns one review finding into three "
            "rounds.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    if os.environ.get("GENESIS_CLASS_SCOPE_ADVISORY", "").lower() in {"0", "off", "false"}:
        sys.exit(0)
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 — advisory must never break the edit
        sys.exit(0)
