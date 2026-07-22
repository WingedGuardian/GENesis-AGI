#!/usr/bin/env python3
"""SessionStart hook: surface unseen upstream-PR-steward notifications inline.

The upstream-pr-steward campaign already Telegram-pings the owner when a tracked
external PR changes, and logs each ping to outreach_history. Those pings are
easy to miss on Telegram, so this hook mirrors the unseen ones into the CC
session as a one-line nudge:

    [PRs] 1 external-PR update you may not have seen — PR steward: … (Jul 9).
    Ask "show PRs" to review.

Its stdout becomes context visible to Claude at session start (same contract as
scripts/check_stale_pending.py). The whole body is fail-open: any error, missing
table, or disabled config -> print nothing, never block session start.
"""

from __future__ import annotations

import os
import sys
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
        max_surface = pr_watch_config.knob_int(cfg, "max_surface")

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
    except Exception:
        return  # Never block session start.


if __name__ == "__main__":
    main()
