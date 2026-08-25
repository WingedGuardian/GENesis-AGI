#!/usr/bin/env python3
"""SessionStart hook: surface age-stale OPEN PRs inline (session-manager PR-4c).

The repo-pulse worker fetches the open-PR set into a home-anchored cache each
non-debounced boundary; this hook reads that cache, computes staleness against a
FRESH ``now``, and surfaces the age-stale ones passively inline as one line:

    [Open PRs] 3 open PRs idle >=7d — #1379 (12d) · #1223 (12d, dependabot) ·
    #1406 (8d, draft). Ask "show PRs" to review.

Worker = fetch-only; this hook owns ALL display logic + the seen-map, so the
surface is always computed against the current clock (never a stale snapshot's).
Its stdout becomes context visible to Claude at session start (same contract as
scripts/surface_pr_updates.py). The whole body is fail-open: any error, missing
cache, stale cache, or disabled config -> print nothing, never block session start.
It NEVER prints CI/review state or "ready to merge" — a visibility nudge only.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime

# The cache is only ever as fresh as the worker's last successful run. A snapshot
# older than this is treated as a dead/degraded worker and NOT surfaced (a stale
# open-PR list is worse than none).
_MAX_CACHE_AGE_S = 86400  # 1 day


def main() -> None:
    # Cheapest gates first — before importing genesis modules.
    if os.environ.get("GENESIS_REPO_PULSE_DISABLED", "").lower() in ("1", "true", "yes"):
        return
    # Genesis-dispatched (background) sessions must not consume the human's
    # surface — leave the seen-map untouched so the next FOREGROUND session sees it.
    if os.environ.get("GENESIS_CC_SESSION") == "1":
        return

    repo_src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    if repo_src not in sys.path:
        sys.path.insert(0, repo_src)

    try:
        from genesis.session_awareness import pr_watch, repo_pulse
        from genesis.session_awareness import repo_pulse_config as pulse_cfg

        cfg = pulse_cfg.load_config()
        if pulse_cfg.effective_mode() == "off" or not cfg.get("open_pr_enabled", True):
            return

        cache_path = repo_pulse.open_prs_cache_path()
        if not cache_path.exists():
            return  # worker hasn't populated it yet (e.g. first boundary)
        try:
            data = json.loads(cache_path.read_text())
        except Exception:
            return
        prs = data.get("prs") if isinstance(data, dict) else None
        if not isinstance(prs, list) or not prs:
            return

        now = datetime.now(UTC)
        # Freshness TTL: never surface a dead worker's stale snapshot.
        computed = pr_watch._parse_ts(data.get("computed_at"))
        if computed is None or (now - computed).total_seconds() > _MAX_CACHE_AGE_S:
            return

        stale_days = pulse_cfg.knob_int(cfg, "open_pr_stale_days")
        resurface = pulse_cfg.knob_int(cfg, "open_pr_resurface_days")
        max_surface = pulse_cfg.knob_int(cfg, "open_pr_max_surface")

        stalled = repo_pulse.select_stalled_open_prs(prs, now=now, stale_days=stale_days)
        if not stalled:
            return

        seen_path = repo_pulse.open_prs_seen_path()
        surfaced, _existed = pr_watch.load_sidecar(seen_path)
        lines, new_surfaced = pr_watch.select_to_surface(
            stalled,
            surfaced,
            now,
            resurface,
            max_surface,
            id_getter=lambda p: str(p["number"]),
            renderer=repo_pulse.format_open_pr_clause,
        )
        # Persist seen-state even when nothing new to show (records baselines).
        pr_watch.save_sidecar(seen_path, new_surfaced)

        text = repo_pulse.format_open_pr_injection(lines, stale_days)
        if text:
            print(text)
            sys.stdout.flush()
    except Exception:
        return  # Never block session start.


if __name__ == "__main__":
    main()
