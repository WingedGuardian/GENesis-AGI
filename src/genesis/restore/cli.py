"""`genesis restore` CLI — thin wrapper around scripts/restore.sh.

Rationale: the restoration logic (cloning the backup repo, decrypting
GPG payloads, uploading Qdrant snapshots via multipart POST, running
`sqlite3 .read`) is all shell-native, and lives next to `backup.sh`
so operators can invoke either directly. The Python wrapper exists so
the restore command is discoverable via `python -m genesis restore` —
the same entry point users are already familiar with for
`genesis serve`, `genesis contribute`, and `genesis eval`.
"""
from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

# Repo root resolution — `cli.py` lives at
# src/genesis/restore/cli.py, so the repo root is three parents up.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_RESTORE_SCRIPT = _REPO_ROOT / "scripts" / "restore.sh"


def run(args: argparse.Namespace) -> int:
    """Execute scripts/restore.sh with forwarded flags.

    Returns the script's exit code so `python -m genesis restore`
    exits non-zero when the restore reports failures.
    """
    if not _RESTORE_SCRIPT.exists():
        print(f"error: restore script not found at {_RESTORE_SCRIPT}")
        return 2

    cmd: list[str] = ["bash", str(_RESTORE_SCRIPT)]
    if args.from_:
        cmd.extend(["--from", args.from_])
    if args.dry_run:
        cmd.append("--dry-run")
    if args.force:
        cmd.append("--force")

    # Inherit environment so GENESIS_BACKUP_PASSPHRASE and friends flow through.
    proc = subprocess.run(cmd, env=os.environ.copy(), check=False)
    return proc.returncode


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register `genesis restore` on an existing subparser group.

    Called from `src/genesis/__main__.py`.
    """
    p = subparsers.add_parser(
        "restore",
        help="Restore Genesis state from your backups repo",
        description=(
            "Rehydrate SQLite, Qdrant, transcripts, auto-memory, local "
            "config overlays, and secrets from your private genesis-backups "
            "repo. Counterpart to scripts/backup.sh."
        ),
    )
    p.add_argument(
        "--from", dest="from_", metavar="URL-OR-PATH",
        help="Backup repo git URL or local path (default: ~/backups/genesis-backups)",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be restored without touching the filesystem",
    )
    p.add_argument(
        "--force", action="store_true",
        help="Overwrite destinations newer than the backup (skip confirmations)",
    )
    p.set_defaults(func=run)
