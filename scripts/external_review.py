#!/usr/bin/env python3
"""CLI for the external review runner — the timer's entry point, and a manual one.

Thin wrapper; the policy and the dispatch live in
``genesis.session_awareness.external_review`` (the ``repo_pulse_worker`` shape). Read
that module's docstring for the scope this runner may not exceed and why.

    external_review.py --scan              # enumerate open PRs, dispatch up to budget
    external_review.py --pr 1932           # consider one PR
    external_review.py --scan --dry-run    # decide and record, spawn nothing
    external_review.py --store-dir         # print the audit store path, for shell callers

Exit status is 0 whenever the runner ran to completion, INCLUDING when it decided to
dispatch nothing: "nothing was eligible" is a successful scan, and a timer that
treats it as failure would fill the journal with red for the ordinary case. A
non-zero exit means the runner itself could not run.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--scan", action="store_true", help="consider every open PR")
    group.add_argument("--pr", type=int, metavar="N", help="consider one PR by number")
    group.add_argument(
        "--store-dir",
        action="store_true",
        help="print the audit store directory and exit (for disk_hygiene.sh)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="decide and record, but spawn nothing (overrides config mode)",
    )
    ap.add_argument("--repo", metavar="OWNER/REPO", help="override the live slug lookup")
    ap.add_argument("--verbose", action="store_true", help="log decisions at INFO")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    from genesis.session_awareness import external_review as runner
    from genesis.session_awareness import external_review_config as config

    if args.store_dir:
        print(runner.store_dir())
        return 0

    cfg = config.load_config()
    # An explicit --dry-run may only ever REMOVE authority. It overrides `live`, and
    # it must not resurrect a runner the operator switched off: `off` stays off, or
    # the flag would be a route around the kill switch.
    if args.dry_run and config.effective_mode(cfg) != "off":
        cfg = {**cfg, "mode": "dry_run"}

    if args.pr is not None:
        summary = runner.review_one(args.pr, repo=args.repo, cfg=cfg)
    else:
        summary = runner.scan(repo=args.repo, cfg=cfg)

    mode = summary.get("mode")
    detail = summary.get("detail")
    print(
        f"external-review: mode={mode} considered={summary.get('considered', 0)} "
        f"dispatched={summary.get('dispatched', 0)}" + (f" — {detail}" if detail else "")
    )
    for number, decision, reason in summary.get("decisions", []):
        print(f"  PR #{number}: {decision} — {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
