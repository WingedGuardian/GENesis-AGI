#!/usr/bin/env python3
"""Latency benchmark: current proactive hook (subprocess) vs the new server
recall endpoint. Manual dev tool — NOT run in CI.

Runs a set of representative prompts through both paths N times and reports
p50/p95 for each, so the PR2 hook-flip decision is grounded in measured
per-prompt latency against the 2.0s budget (the ceiling the whole rework
must respect). Requires genesis-server running WITH the PR1 endpoint deployed
(``/api/genesis/hook/recall``) — run it after PR1 merges + the server restarts.

Usage:
    python scripts/dev/bench_proactive_hook.py [--runs 3] [--prompts-file FILE]

The server host is resolved from ``GENESIS_DASHBOARD_HOST`` (default
127.0.0.1) — no install-specific values baked in.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "scripts" / "proactive_memory_hook.py"
ENDPOINT = "/api/genesis/hook/recall"

# Representative taxonomy — command / decision-question / general / chatter /
# file-context. Synthetic + generic (no install-specific content).
_DEFAULT_PROMPTS = [
    "restart the server",
    "deploy the latest build",
    "commit the staged changes",
    "what did we decide about the recall reranker",
    "why did the memory endpoint change",
    "what's the status of the voice work",
    "how does the proactive hook budget work",
    "the dashboard chart looks off",
    "explain the retrieval fusion weights",
    "walk me through the graph expansion path",
    "hi",
    "thanks",
    "look at the memory retrieval store module",
    "check the intent classification code",
    "review the proactive engine profile registry",
]


def _server_base() -> str:
    host = os.environ.get("GENESIS_DASHBOARD_HOST", "127.0.0.1")
    port = os.environ.get("GENESIS_DASHBOARD_PORT", "5000")
    return f"http://{host}:{port}"


def _time_hook(prompt: str, session_id: str) -> float | None:
    """Wall-clock ms for one current-hook subprocess invocation."""
    payload = json.dumps({"prompt": prompt, "session_id": session_id}).encode()
    t0 = time.monotonic()
    try:
        subprocess.run(
            [sys.executable, str(HOOK)],
            input=payload,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except Exception:
        return None
    return (time.monotonic() - t0) * 1000


def _time_endpoint(prompt: str, session_id: str) -> float | None:
    """Wall-clock ms for one endpoint call (includes HTTP)."""
    body = json.dumps({"prompt": prompt, "session_id": session_id, "profile": "cc_hook"}).encode()
    req = urllib.request.Request(  # noqa: S310 - loopback dev tool, fixed http scheme
        _server_base() + ENDPOINT,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            resp.read()
    except Exception as exc:  # noqa: BLE001
        print(f"  endpoint error: {exc}", file=sys.stderr)
        return None
    return (time.monotonic() - t0) * 1000


def _pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    values = sorted(values)
    k = (len(values) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def _summ(name: str, samples: list[float]) -> str:
    ok = [s for s in samples if s is not None]
    if not ok:
        return f"{name:10} no successful samples"
    return (
        f"{name:10} n={len(ok):3}  p50={_pct(ok, 0.5):7.1f}ms  "
        f"p95={_pct(ok, 0.95):7.1f}ms  max={max(ok):7.1f}ms  "
        f"mean={statistics.mean(ok):7.1f}ms"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3, help="repeats per prompt")
    ap.add_argument("--prompts-file", type=Path, default=None)
    args = ap.parse_args()

    prompts = _DEFAULT_PROMPTS
    if args.prompts_file and args.prompts_file.exists():
        prompts = [ln.strip() for ln in args.prompts_file.read_text().splitlines() if ln.strip()]

    hook_samples: list[float] = []
    endpoint_samples: list[float] = []
    print(f"Benchmarking {len(prompts)} prompts × {args.runs} runs\n")
    for i, prompt in enumerate(prompts):
        sid = f"bench-{i}"
        for _ in range(args.runs):
            h = _time_hook(prompt, sid)
            e = _time_endpoint(prompt, sid)
            if h is not None:
                hook_samples.append(h)
            if e is not None:
                endpoint_samples.append(e)
        print(f"  [{i + 1}/{len(prompts)}] {prompt[:50]}")

    print("\n=== Latency (lower is better; budget ceiling 2000ms) ===")
    print(_summ("hook", hook_samples))
    print(_summ("endpoint", endpoint_samples))


if __name__ == "__main__":
    main()
