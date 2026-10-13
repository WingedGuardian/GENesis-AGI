#!/usr/bin/env python3
"""SessionStart hook: tell a foreground session that peer handoffs are waiting.

Another install can deliver a written handoff into a shared directory. This hook
is the read side: at session start it counts the handoffs that have no local
"handled" record and no ``-REPLY.md`` sibling, and injects a short block naming
them — as UNTRUSTED claims to verify, never as instructions, and never with
their content. Logic lives in ``genesis.session_awareness.handoffs``; the mark
command is ``python -m genesis handoffs mark <id> --note ...``.

It is a sibling of ``surface_open_prs.py`` / ``surface_pr_updates.py`` rather
than a watcher: the session boundary is already where this install surfaces
state someone believes it owes, so no daemon, inotify loop or timer exists.

Read-only. It writes nothing anywhere — not the shared directory, not the local
state file — so running it can never mark anything seen.

Off by default: with no ``dir`` configured (the shipped config) it returns
before touching the filesystem. ``GENESIS_HANDOFFS_DISABLED=1`` is the kill
switch. Genesis-dispatched (background) sessions get nothing: nobody is there to
decide, and the surface is not consumed, so the next foreground session sees it.

Fail-open, loudly: a misconfigured or unreadable directory, a scan that does not
finish inside ``handoffs.SCAN_TIMEOUT_S`` (it runs in a worker process, so a hung
mount cannot hold this one past the hook timeout), a corrupt state file, or an
unexpected error prints ONE fixed-shape line, so "no handoffs" is never what a
failure looks like. It never blocks session start.

Output routes through ``scripts/hooks/hook_output.py`` (BoundedStdout); the
listed entries are additionally clamped in code (``handoffs.MAX_LISTED``).
"""

from __future__ import annotations

import os
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
from hook_output import BoundedStdout  # noqa: E402


def main() -> None:
    if os.environ.get("GENESIS_HANDOFFS_DISABLED") == "1":
        return
    if os.environ.get("GENESIS_CC_SESSION") == "1":
        return

    repo_src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    if repo_src not in sys.path:
        sys.path.insert(0, repo_src)

    out = BoundedStdout(label="handoffs")
    try:
        from genesis.session_awareness import handoffs as H

        try:
            directory = H.configured_dir()
        except H.HandoffConfigError as exc:
            out.emit(
                f"[Handoffs] handoff directory is misconfigured ({str(exc)[:160]}) — "
                "read 'no handoffs' as UNKNOWN this session, not none."
            )
            return
        if directory is None:
            return
        try:
            # Bounded as ONE unit (listing + stat + open + read) in a worker
            # process: this process never touches the peer-controlled directory,
            # so a hung mount cannot outlive the hook's timeout and silence it.
            result = H.scan_within(directory)
        except H.ScanTimeout:
            out.emit(
                f"[Handoffs] scan of {directory} did not finish within "
                f"{H.SCAN_TIMEOUT_S:g}s — read 'no handoffs' as UNKNOWN this session, "
                f"not none. `{H.LIST_COMMAND}` scans it without the session-start bound."
            )
            return
        except H.HandoffDirError as exc:
            out.emit(
                f"[Handoffs] configured handoff directory {directory} could not be read "
                f"({str(exc)[:160]}) — read 'no handoffs' as UNKNOWN this session, not none."
            )
            return
        try:
            handled = H.load_handled(result.directory)
        except H.StateError as exc:
            out.emit(
                f"[Handoffs] local handled-state file {H.state_path()} is unreadable "
                f"({str(exc)[:160]}); handoff surfacing is suspended rather than "
                "re-listing everything. Repair or remove that file."
            )
            return
        pending = H.unhandled(result, handled)
        text = H.render_session_block(result, pending, datetime.now(UTC))
        if text:
            out.emit_or_degrade(
                text,
                block="handoffs",
                notice=(
                    "\n[handoffs: {kept} chars kept — run `python -m genesis handoffs "
                    "list` for the rest]"
                ),
            )
    except Exception as exc:
        out.emit(
            f"[Handoffs] surfacing hook FAILED ({type(exc).__name__[:40]}) -- read "
            "'no handoffs' as UNKNOWN this session, not as none. Trace in the hook debug log."
        )
        traceback.print_exc(file=sys.stderr)


if __name__ == "__main__":
    main()
