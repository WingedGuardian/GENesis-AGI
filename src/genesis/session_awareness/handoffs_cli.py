"""``python -m genesis handoffs`` — list peer handoffs and mark one handled.

A thin CLI over :mod:`genesis.session_awareness.handoffs`. ``list`` is
read-only; ``mark`` writes only the LOCAL handled-state file under
``~/.genesis/handoffs/`` — never anything in the shared directory.

``list`` shows ids, names (safe-shaped ones only), sizes and ages — never
handoff CONTENT. Read a file yourself, as a claim to verify, when deciding.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from genesis.session_awareness import handoffs as H


def _scan_or_exit() -> H.Scan | int:
    try:
        directory = H.configured_dir()
    except H.HandoffConfigError as exc:
        print(f"handoffs: {exc}", file=sys.stderr)
        return 2
    if directory is None:
        print(
            "handoffs: no handoff directory configured (set an absolute `dir:` in "
            "the handoffs overlay under ~/.genesis/config/) — the feature is off."
        )
        return 0
    try:
        # The recovery path: an operator is present, so neither session-start
        # bound applies — every entry is read and every file content-hashed,
        # which is what lets it reach a handoff the hook's capped scan cut off.
        return H.scan(directory, hash_budget_s=None, max_entries=None)
    except H.HandoffDirError as exc:
        print(f"handoffs: configured directory {directory} is unreadable: {exc}", file=sys.stderr)
        return 2


def _cmd_list(args: argparse.Namespace) -> int:
    result = _scan_or_exit()
    if isinstance(result, int):
        return result
    try:
        handled = H.load_handled(result.directory)
    except H.StateError as exc:
        print(f"handoffs: handled-state file unreadable ({exc}): {H.state_path()}", file=sys.stderr)
        return 2
    now = datetime.now(UTC)
    pending = H.unhandled(result, handled)
    pending_ids = {h.id for h in pending}
    print(f"{result.directory}: {len(result.handoffs)} handoff(s), {len(pending)} unhandled")
    if result.unreadable:
        print(
            f"  {result.unreadable} handoff file(s) could not be read by this install "
            "— UNKNOWN, not handled"
        )
    for h in result.handoffs:
        if h.id in pending_ids:
            status = "UNHANDLED"
        elif h.replied:
            status = "replied"
        else:
            status = "handled"
        if status == "UNHANDLED" or args.all:
            print(f"  [{status}] {H.describe(h, now)}")
            rec = handled.get(h.id)
            if rec and args.all:
                print(f"      handled {rec.get('handled_at', '?')}: {rec.get('note', '')}")
    return 0


def _cmd_mark(args: argparse.Namespace) -> int:
    result = _scan_or_exit()
    if isinstance(result, int):
        return result or 2  # nothing configured is an error for a write
    try:
        target = H.mark_handled(result, args.ref, args.note)
    except (LookupError, ValueError, H.StateError) as exc:
        print(f"handoffs: {exc}", file=sys.stderr)
        return 2
    # The name is peer-controlled; print it only in its safe shape.
    shown = target.safe_name or "<nonconforming filename>"
    print(f"marked handled: {target.display_id} ({shown}) -> {H.state_path()}")
    return 0


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser("handoffs", help="List peer handoffs / mark one handled")
    sub = p.add_subparsers(dest="handoffs_command", required=True)

    lp = sub.add_parser("list", help="List handoffs (unhandled by default)")
    lp.add_argument("--all", action="store_true", help="Include handled and replied ones")
    lp.set_defaults(func=_cmd_list)

    mp = sub.add_parser("mark", help="Record one handoff as handled (locally)")
    mp.add_argument("ref", help="Handoff id prefix (>= 6 hex chars) or exact filename")
    mp.add_argument("--note", required=True, help="What you verified or decided")
    mp.set_defaults(func=_cmd_mark)
