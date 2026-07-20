#!/usr/bin/env python3
"""Parity check: current proactive hook (subprocess) vs the new server recall
endpoint, on the SAME prompts. Manual dev tool — NOT run in CI.

Routing through the real engine deliberately changes injected results vs the
old fork (reranker, fusion weights, intent-aware budget, the dropped wing
soft-lane). This is NOT a byte-parity check — it quantifies the delta so the
PR2 hook-flip decision is evidence-based: per-prompt injected counts, memory-id
overlap (Jaccard), and latency, emitted as a markdown table to paste into the
PR2 description.

Requires genesis-server running WITH the PR1 endpoint deployed. The old hook is
invoked from ``scripts/proactive_memory_hook.py`` as it exists on disk — run
this on a checkout whose hook is still the pre-flip fork (i.e. before PR2), so
"old" is genuinely the fork.

Usage:
    python scripts/dev/proactive_parity_check.py [--prompts-file FILE] > parity.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "scripts" / "proactive_memory_hook.py"
ENDPOINT = "/api/genesis/hook/recall"

_ID_RE = re.compile(r"id:([0-9a-f]{8})")
_MEM_LINE_RE = re.compile(r"^\[(Memory|KB·|Memory·external)")

_DEFAULT_PROMPTS = [
    "restart the server",
    "what did we decide about the recall reranker",
    "why did the memory endpoint change",
    "what's the status of the voice work",
    "how does the proactive hook budget work",
    "explain the retrieval fusion weights",
    "the dashboard chart looks off",
    "walk me through the graph expansion path",
    "review the proactive engine profile registry",
    "hi",
    "deploy the latest build",
    "check the intent classification code",
    "what changed in memory recall recently",
    "summarize the injection defense gate",
    "look at the store module",
]


def _server_base() -> str:
    host = os.environ.get("GENESIS_DASHBOARD_HOST", "127.0.0.1")
    port = os.environ.get("GENESIS_DASHBOARD_PORT", "5000")
    return f"http://{host}:{port}"


def _ids_from_lines(lines: list[str]) -> set[str]:
    """First 8-hex id per rendered memory/KB line (the injected memory ids)."""
    ids: set[str] = set()
    for ln in lines:
        if _MEM_LINE_RE.match(ln):
            m = _ID_RE.search(ln)
            if m:
                ids.add(m.group(1))
    return ids


def _run_hook(prompt: str, sid: str) -> tuple[set[str], int, float]:
    payload = json.dumps({"prompt": prompt, "session_id": sid}).encode()
    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload,
        capture_output=True,
        timeout=15,
        check=False,
    )
    ms = (time.monotonic() - t0) * 1000
    lines = [ln for ln in proc.stdout.decode("utf-8", "replace").splitlines() if ln.strip()]
    mem_lines = [ln for ln in lines if _MEM_LINE_RE.match(ln)]
    return _ids_from_lines(lines), len(mem_lines), ms


def _run_endpoint(prompt: str, sid: str) -> tuple[set[str], int, float]:
    body = json.dumps({"prompt": prompt, "session_id": sid, "profile": "cc_hook"}).encode()
    req = urllib.request.Request(  # noqa: S310 - loopback dev tool, fixed http scheme
        _server_base() + ENDPOINT,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
        data = json.loads(resp.read())
    ms = (time.monotonic() - t0) * 1000
    lines = data.get("lines", [])
    mem_lines = [ln for ln in lines if _MEM_LINE_RE.match(ln)]
    return _ids_from_lines(lines), len(mem_lines), ms


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompts-file", type=Path, default=None)
    args = ap.parse_args()

    prompts = _DEFAULT_PROMPTS
    if args.prompts_file and args.prompts_file.exists():
        prompts = [ln.strip() for ln in args.prompts_file.read_text().splitlines() if ln.strip()]

    print("# Proactive parity: fork hook vs server endpoint\n")
    print("| prompt | hook n | endpoint n | id overlap (J) | shared | hook ms | endpoint ms |")
    print("|---|---|---|---|---|---|---|")
    jaccards: list[float] = []
    for i, prompt in enumerate(prompts):
        sid = f"parity-{i}"
        try:
            h_ids, h_n, h_ms = _run_hook(prompt, sid)
        except Exception as exc:  # noqa: BLE001
            print(f"| {prompt[:40]} | HOOK ERROR: {exc} |||||| ")
            continue
        try:
            e_ids, e_n, e_ms = _run_endpoint(prompt, sid)
        except Exception as exc:  # noqa: BLE001
            print(f"| {prompt[:40]} | {h_n} | ENDPOINT ERROR: {exc} ||||| ")
            continue
        j = _jaccard(h_ids, e_ids)
        jaccards.append(j)
        shared = len(h_ids & e_ids)
        print(f"| {prompt[:40]} | {h_n} | {e_n} | {j:.2f} | {shared} | {h_ms:.0f} | {e_ms:.0f} |")

    if jaccards:
        mean_j = sum(jaccards) / len(jaccards)
        print(f"\nMean id-overlap (Jaccard) across {len(jaccards)} prompts: **{mean_j:.2f}**")
        print(
            "\n_Note: overlap < 1.0 is expected — the endpoint routes through the "
            "real engine (reranker, fusion weights, intent-aware budget) that the "
            "fork lacks. Read the counts + overlap for material regressions, not "
            "byte-parity._"
        )


if __name__ == "__main__":
    main()
