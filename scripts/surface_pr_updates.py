#!/usr/bin/env python3
"""SessionStart hook: surface unseen upstream-PR-steward notifications inline.

The upstream-pr-steward campaign already Telegram-pings the owner when a tracked
external PR changes, and logs each ping to outreach_history. Those pings are
easy to miss on Telegram, so this hook mirrors the unseen ones into the CC
session as a one-line nudge:

    [PRs] 1 external-PR update you may not have seen — PR steward: … (Jul 9).
    Ask "show PRs" to review.

Its stdout becomes context visible to Claude at session start (same contract as
scripts/check_stale_pending.py). Fail-open: a missing table, an unreadable or
locked DB, or disabled config -> print nothing and never block session start.
An UNEXPECTED error (in practice a genesis/src import skew) prints ONE
fixed-format line first, so a broken surface is not read as "nothing to
report", then returns. It still never blocks session start.
"""

from __future__ import annotations

import os
import sys
import traceback
from datetime import UTC, datetime


def main() -> None:
    # Cheapest gates first — before importing genesis modules.
    if os.environ.get("GENESIS_PR_WATCH_DISABLED", "").lower() in ("1", "true", "yes"):
        return
    # Genesis-dispatched (background) sessions must not consume the human's
    # unseen pings — leave the sidecar untouched so the next FOREGROUND session
    # still surfaces them.
    if os.environ.get("GENESIS_CC_SESSION") == "1":
        return

    # Make src/ importable when run outside an editable install (mirrors other
    # hooks; the genesis-hook launcher already selects the right venv).
    repo_src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    if repo_src not in sys.path:
        sys.path.insert(0, repo_src)

    try:
        from genesis.session_awareness import pr_watch, pr_watch_config

        cfg = pr_watch_config.load_config()
        if not pr_watch_config.is_enabled(cfg):
            return

        now = datetime.now(UTC)
        lookback = pr_watch_config.knob_int(cfg, "lookback_days")
        resurface = pr_watch_config.knob_int(cfg, "resurface_days")
        # Clamped in CODE, not left to the config default: pr_watch_config.knob_int
        # has no upper bound and load_config merges a .local.yaml overlay, so the "5"
        # in DEFAULTS is a default, not a structural bound. An exemption may only
        # cite a bound configuration cannot change.
        #
        # MAX_SURFACE_CAP is the SHARED constant, which stops this clamp and the
        # settings validator drifting apart. It is NOT a promise that an over-cap
        # value never reaches this line, and an earlier revision of this comment
        # said it was. The file has two doors: settings_update runs the validator
        # and REFUSES an over-cap write, but a hand-edited .local.yaml reaches
        # load_config with no validation at all. MEASURED: an overlay carrying
        # max_surface: 5000 arrives here as 5000 and this line silently reduces it
        # to 20. So the clamp is load-bearing, not belt-and-braces --
        # pr_watch.select_to_surface applies whatever it is handed, which makes
        # this line the ONLY enforcement of the ceiling.
        max_surface = min(
            pr_watch_config.knob_int(cfg, "max_surface"),
            pr_watch_config.MAX_SURFACE_CAP,
        )

        notifs = pr_watch.read_steward_notifications(pr_watch.db_path(), lookback, now)
        if not notifs:
            return

        side_path = pr_watch.sidecar_path()
        surfaced, _existed = pr_watch.load_sidecar(side_path)
        lines, new_surfaced = pr_watch.select_to_surface(
            notifs, surfaced, now, resurface, max_surface
        )
        # Persist seen-state even if nothing new to show (records baselines).
        pr_watch.save_sidecar(side_path, new_surfaced)

        text = pr_watch.format_injection(lines)
        if text:
            print(text)
            sys.stdout.flush()
    except Exception as exc:
        # Fail open -- never block session start. But say so IN BAND.
        #
        # An earlier version wrote only the traceback to stderr, reasoning that
        # stderr "is not model-facing and so costs the session nothing". That is
        # exactly why it also ACHIEVES nothing: Claude Code discards an exit-0
        # hook's stderr (READ: scripts/genesis_session_context.py, which routes
        # its own mis-wire alert in-band for this reason, and
        # scripts/hooks/git_discard_guard.py). This block sits downstream of
        # module attribute reads, so a genesis/src version skew turns the whole
        # surface into a silent no-op INDISTINGUISHABLE FROM "nothing to
        # report" -- the precise outage the diagnostic existed to expose, filed
        # where nothing reads it.
        #
        # ONE fixed-format line, so this cannot approach the hook-output cap:
        # the only variable part is an exception CLASS NAME, sliced. The
        # traceback still goes to stderr for the debug log.
        print(
            f"[PRs] surfacing hook FAILED ({type(exc).__name__[:40]}) -- read "
            "'no updates' as UNKNOWN this session, not as none. "
            "Trace in the hook debug log."
        )
        traceback.print_exc(file=sys.stderr)
        return


if __name__ == "__main__":
    main()
