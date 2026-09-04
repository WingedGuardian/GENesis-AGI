#!/usr/bin/env python3
"""Size retention for the hook audit stores (merge overrides, discard snapshots).

Each store is a directory of small JSONL files, one per hook flush. This deletes the
OLDEST whole files until a store fits its byte bound, so the stores stay bounded on a
smaller disk. Invoked by ``scripts/disk_hygiene.sh`` (the genesis-disk-hygiene.timer);
also runnable by hand.

Retention lives HERE rather than in the writer deliberately. The previous design
pruned in-hook, on the merge and discard paths, and that retention engine was the
source of most of the writer's defects — a guard about to return a security verdict
was also rewriting a file. A per-file store is what makes the daily-timer shape work:
an age prune cannot bound an append-forever file, but deleting whole old files can.

Best-effort — a failure here must not skip other hygiene steps, and a store that does
not exist yet (no override or discard has ever happened) is a clean no-op.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_DIR / "scripts" / "hooks"
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))


def _default_dirs() -> list[str]:
    """The live stores, resolved by the WRITERS' own function.

    It used to re-derive them here, and the copy was subtly different: it accepted
    a RELATIVE override that both writers explicitly refuse. The pruner then
    trimmed some unrelated directory under its working directory while the real
    store — which the writers were still filling at the default path — grew with
    no retention at all, for as long as that env var stayed set (Codex P2,
    PR #1609). Asking the shared resolver makes the two impossible to disagree.
    """
    from audit_jsonl import STORES, resolve_store_dir

    return [resolve_store_dir(var) for var in STORES]


def _trim(directory: str, max_bytes: int) -> int:
    from audit_jsonl import trim_dir_by_size

    return trim_dir_by_size(directory, max_bytes)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "dirs",
        nargs="*",
        help="store directories to trim (default: the merge-override and discard stores)",
    )
    ap.add_argument(
        "--max-bytes",
        type=int,
        default=5_000_000,
        help="per-store byte bound; oldest whole files are deleted until it fits",
    )
    args = ap.parse_args()
    for directory in args.dirs or _default_dirs():
        try:
            if not os.path.isdir(directory):
                print(f"hook audit trim: {directory}: absent, nothing to do")
                continue
            freed = _trim(directory, args.max_bytes)
            print(f"hook audit trim: {directory}: removed {freed} byte(s) (bound {args.max_bytes})")
        except Exception as exc:  # noqa: BLE001 — never abort the groom
            print(f"hook audit trim error: {directory}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
