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
    # Cheapest gates first — before importing genesis modules. Match the EXACT
    # "1" the other two consumers honor (repo_pulse_worker.py, genesis_session_
    # context.py) and the value the yaml documents (GENESIS_REPO_PULSE_DISABLED=1);
    # a looser truthy set here would let `=true` silence THIS surface while the
    # detached worker kept doing gh/DB work — a partial, misleading kill switch.
    if os.environ.get("GENESIS_REPO_PULSE_DISABLED") == "1":
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
        if not isinstance(prs, list):
            return  # malformed cache — nothing to compute or prune against

        now = datetime.now(UTC)
        # Freshness TTL: never surface (or prune against) a dead worker's stale
        # snapshot. Derive the allowance from the debounce cadence (2x, floored at
        # 1 day) so a large `min_interval_minutes` (>=1440) can't expire the cache
        # BEFORE the worker is even allowed to refresh it — which would otherwise
        # suppress the surface for the whole debounce window (Codex P2).
        ttl_s = max(_MAX_CACHE_AGE_S, pulse_cfg.knob_int(cfg, "min_interval_minutes") * 60 * 2)
        computed = pr_watch._parse_ts(data.get("computed_at"))
        if computed is None or (now - computed).total_seconds() > ttl_s:
            return

        stale_days = pulse_cfg.knob_int(cfg, "open_pr_stale_days")
        resurface = pulse_cfg.knob_int(cfg, "open_pr_resurface_days")
        max_surface = pulse_cfg.knob_int(cfg, "open_pr_max_surface")

        # Namespace the seen-map by the cache's live repo slug so a PR number from a
        # DIFFERENT repo (a re-pointed remote / fork) can't collide with an aged-out
        # entry of the same number (Codex P2).
        repo_slug = str(data.get("repo") or "?")
        stalled = repo_pulse.select_stalled_open_prs(prs, now=now, stale_days=stale_days)

        seen_path = repo_pulse.open_prs_seen_path()
        surfaced, _existed = pr_watch.load_sidecar(seen_path)
        # ALWAYS rebuild the seen-map from the CURRENT stalled set — even when it is
        # empty — so an entry for a PR that has left the stalled set is dropped; keeping
        # its old first_ts would wrongly age it out and it would not resurface when it
        # goes stale again (Codex P2). select_to_surface([]) returns an empty map, which
        # is the correct pruned state.
        lines, new_surfaced = pr_watch.select_to_surface(
            stalled,
            surfaced,
            now,
            resurface,
            max_surface,
            id_getter=lambda p: f"{repo_slug}#{p['number']}",
            renderer=repo_pulse.format_open_pr_clause,
        )
        pr_watch.save_sidecar(seen_path, new_surfaced)

        # A capped fetch (>max_open_prs open PRs) does not cover the whole open set —
        # render the count as a floor (≥N) so it is reported honestly as a lower bound,
        # never a silent exact count (Codex P2).
        text = repo_pulse.format_open_pr_injection(
            lines, stale_days, capped=bool(data.get("limit_hit"))
        )
        if text:
            print(text)
            sys.stdout.flush()
    except Exception:
        return  # Never block session start.


if __name__ == "__main__":
    main()
