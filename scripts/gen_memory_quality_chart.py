#!/usr/bin/env python3
"""Render the memory-quality chart in README.md from the weekly eval snapshots.

Replaces a hand-made memory-GROWTH chart. Growth was a vanity metric: it showed
the store getting bigger, which proves accumulation rather than value. The
question a reader actually has is whether retrieval still works as the store
grows, and `eval_snapshots` has answered that weekly since 2026-05.

Plots `hit_rate` and `MRR` (LLM-judged, weekly) against a reconstructed pool
size. Deliberately NOT plotted: `precision_at_3`. It drifts downward for a
structural reason rather than a quality one — proactive recall selects a fixed
COUNT with no relevance floor, so precision@k is capped whenever fewer than k
memories are genuinely relevant. That is tracked as a design question in the
public issue tracker; plotting it here would read as a quality signal it is not.

Read-only (``mode=ro`` — NOT ``immutable=1``, which ignores the WAL and would
silently miss a snapshot written since the last checkpoint). Stdlib only: no
plotting dependency, and the SVG output is text, so it diffs.

Usage:  python3 scripts/gen_memory_quality_chart.py [--out PATH] [--db PATH]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

W, H = 1200, 520
PAD_L, PAD_R, PAD_T, PAD_B = 78, 92, 74, 92

BG_TOP, BG_BOT = "#0d1117", "#161b22"
INK, MUTED, FAINT = "#e6edf3", "#8b949e", "#30363d"
HIT, MRR, POOL = "#3fb950", "#58a6ff", "#533483"


def _db_path(explicit: str | None) -> Path:
    """Resolve the live DB, checking each candidate EXISTS before accepting it.

    `genesis_db_path()` resolves against the repo root, so from a git worktree it
    returns `<worktree>/data/genesis.db` — a path that does not exist. Trusting the
    resolver's return value alone therefore fails inside exactly the workflow this
    script is used in (generate the chart on a docs branch). Probe, then fall back.
    """
    if explicit:
        chosen = Path(explicit).expanduser()
        if not chosen.exists():
            raise SystemExit(f"--db does not exist: {chosen}")
        return chosen

    candidates: list[Path] = []
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
        from genesis.env import genesis_db_path

        candidates.append(Path(genesis_db_path()))
    except Exception as exc:  # noqa: BLE001 - standalone run without the package is fine
        # Say so. Falling through in silence means a chart can be regenerated from
        # a different database and committed as evidence with nothing to notice.
        print(f"note: project db resolver unavailable ({exc!r}); trying the default path")
    candidates.append(Path.home() / "genesis" / "data" / "genesis.db")

    for cand in candidates:
        if cand.exists():
            return cand
    tried = ", ".join(str(c) for c in candidates)
    raise SystemExit(f"no Genesis database found (tried: {tried}); pass --db")


def load(db: Path) -> list[dict]:
    """Weekly memory-dimension snapshots, oldest first, with pool reconstruction."""
    # quote() the path: in a `file:` URI an unescaped `?` or `#` in the path
    # STARTS the query/fragment, so `--db /tmp/genesis?backup.db` silently opens
    # a different (empty, auto-created) database rather than failing. Read-only
    # mode would be dropped with it. `/` is safe to leave unescaped.
    con = sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT period_end, metrics_json, sample_count FROM eval_snapshots "
            "WHERE dimension='memory' ORDER BY period_end"
        ).fetchall()
        out = []
        for period_end, metrics_json, n in rows:
            m = json.loads(metrics_json or "{}")
            pool = con.execute(
                "SELECT COUNT(*) FROM memory_metadata WHERE created_at <= ?", (period_end,)
            ).fetchone()[0]
            out.append(
                {
                    "date": (period_end or "")[:10],
                    "hit": m.get("hit_rate"),
                    "mrr": m.get("mrr"),
                    "n": n or 0,
                    "pool": pool,
                }
            )
        return out
    finally:
        con.close()


def _segments(pts: list[tuple[float, float] | None]) -> list[list[tuple[float, float]]]:
    """Split on None so a week with no measurement renders as a GAP, not a zero."""
    segs, cur = [], []
    for p in pts:
        if p is None:
            if len(cur) > 1:
                segs.append(cur)
            cur = []
        else:
            cur.append(p)
    if len(cur) > 1:
        segs.append(cur)
    return segs


def render(rows: list[dict]) -> str:
    measured = [r for r in rows if r["n"] > 0 and r["hit"] is not None]
    if len(measured) < 2:
        raise SystemExit("not enough measured weeks to plot")

    max_pool = max(r["pool"] for r in rows) or 1
    # Growth spans the JUDGED window at BOTH ends, and the legend below reports
    # that same window. The headline claims quality held WHILE the store grew, so
    # a ratio reaching outside the weeks where quality was observed would claim
    # coverage the data lacks — and a legend on a different span would let the
    # image contradict its own headline, which is precisely what it did.
    # A zero denominator is refused rather than papered over with `or 1`, which
    # would turn an empty first week into a spectacular fictional multiplier.
    first_pool, last_pool = measured[0]["pool"], measured[-1]["pool"]
    if not first_pool:
        raise SystemExit(
            "cannot state growth: the first judged week reconstructs to an empty "
            "store, which would make any ratio meaningless"
        )
    growth = last_pool / first_pool
    plot_w, plot_h = W - PAD_L - PAD_R, H - PAD_T - PAD_B

    def x(i: int) -> float:
        return PAD_L + (plot_w * i / max(1, len(rows) - 1))

    def y(v: float) -> float:  # metrics are 0..1
        return PAD_T + plot_h * (1.0 - v)

    def y_pool(v: int) -> float:
        return PAD_T + plot_h * (1.0 - v / max_pool)

    def pts(key: str) -> list[tuple[float, float] | None]:
        return [
            (x(i), y(r[key])) if (r["n"] > 0 and r[key] is not None) else None
            for i, r in enumerate(rows)
        ]

    def path(seg: list[tuple[float, float]]) -> str:
        return "M " + " L ".join(f"{px:.1f},{py:.1f}" for px, py in seg)

    o: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        "font-family=\"'Segoe UI', system-ui, -apple-system, sans-serif\" "
        'role="img" aria-label="Memory retrieval quality against store size, weekly">',
        "<defs>",
        '<linearGradient id="bg" x1="0%" y1="0%" x2="0%" y2="100%">',
        f'<stop offset="0%" style="stop-color:{BG_TOP}"/>',
        f'<stop offset="100%" style="stop-color:{BG_BOT}"/>',
        "</linearGradient>",
        f'<linearGradient id="poolFill" x1="0%" y1="0%" x2="0%" y2="100%">'
        f'<stop offset="0%" style="stop-color:{POOL};stop-opacity:0.42"/>'
        f'<stop offset="100%" style="stop-color:{POOL};stop-opacity:0.04"/></linearGradient>',
        "</defs>",
        f'<rect width="{W}" height="{H}" fill="url(#bg)"/>',
        f'<text x="{PAD_L}" y="34" fill="{INK}" font-size="19" font-weight="600">'
        f"Retrieval quality held while the store grew {growth:.1f}&#215;</text>",
        f'<text x="{PAD_L}" y="56" fill="{MUTED}" font-size="13">'
        "Weekly LLM-judged recall quality. Bars show how many recalls were judged that "
        "week &#8212; wider sample, more trustworthy point.</text>",
    ]

    # gridlines
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        gy = y(frac)
        o.append(
            f'<line x1="{PAD_L}" y1="{gy:.1f}" x2="{W - PAD_R}" y2="{gy:.1f}" '
            f'stroke="{FAINT}" stroke-width="1"/>'
        )
        o.append(
            f'<text x="{PAD_L - 12}" y="{gy + 4:.1f}" fill="{MUTED}" font-size="12" '
            f'text-anchor="end">{frac:.2f}</text>'
        )

    # pool area (context, deliberately de-emphasised)
    area = " L ".join(f"{x(i):.1f},{y_pool(r['pool']):.1f}" for i, r in enumerate(rows))
    o.append(
        f'<path d="M {PAD_L},{PAD_T + plot_h:.1f} L {area} L {W - PAD_R},'
        f'{PAD_T + plot_h:.1f} Z" fill="url(#poolFill)"/>'
    )

    # sample-count bars — the honesty layer. Judged-sample size varies by an order
    # of magnitude between weeks, so a high point on a thin bar is noise; drawing
    # the counts is what lets a reader see which points to trust. Scaled to the
    # run's own maximum rather than a fixed figure, so it does not go stale.
    max_n = max((r["n"] for r in rows), default=1) or 1
    for i, r in enumerate(rows):
        if not r["n"]:
            continue
        bh = 26 * (r["n"] / max_n)
        o.append(
            f'<rect x="{x(i) - 3:.1f}" y="{H - PAD_B + 30 - bh:.1f}" width="6" '
            f'height="{bh:.1f}" fill="{MUTED}" opacity="0.55"/>'
        )

    for key, colour in (("hit", HIT), ("mrr", MRR)):
        for seg in _segments(pts(key)):
            o.append(
                f'<path d="{path(seg)}" fill="none" stroke="{colour}" stroke-width="2.5" '
                'stroke-linejoin="round" stroke-linecap="round"/>'
            )
        for i, r in enumerate(rows):
            if r["n"] > 0 and r[key] is not None:
                o.append(f'<circle cx="{x(i):.1f}" cy="{y(r[key]):.1f}" r="3.2" fill="{colour}"/>')

    # x labels: first, last, and every 4th between
    for i, r in enumerate(rows):
        if i in (0, len(rows) - 1) or i % 4 == 0:
            o.append(
                f'<text x="{x(i):.1f}" y="{H - PAD_B + 54}" fill="{MUTED}" font-size="11" '
                f'text-anchor="middle">{r["date"][5:]}</text>'
            )

    first, last = measured[0], measured[-1]
    # Each series gets its own measured set. `measured` is defined on `hit`, so
    # formatting mrr off it raises TypeError the first time a judged week carries
    # one metric and not the other — which the plotting path already allows for.
    mrr_seen = [r for r in rows if r["n"] > 0 and r["mrr"] is not None]
    legend = [(HIT, f"hit rate  {first['hit']:.2f} → {last['hit']:.2f}")]
    if mrr_seen:
        legend.append(
            (MRR, f"MRR  {mrr_seen[0]['mrr']:.2f} → {mrr_seen[-1]['mrr']:.2f}")
        )
    legend.append(
        (POOL, f"store size  {first_pool:,} → {last_pool:,} over the judged window (reconstructed)")
    )
    lx = PAD_L
    for colour, label in legend:
        o.append(f'<rect x="{lx}" y="{H - 34}" width="11" height="11" rx="2" fill="{colour}"/>')
        o.append(f'<text x="{lx + 18}" y="{H - 24}" fill="{MUTED}" font-size="12">{label}</text>')
        lx += 20 + int(len(label) * 6.9)

    o.append(
        f'<text x="{W - PAD_R}" y="{H - 24}" fill="{MUTED}" font-size="11" '
        f'text-anchor="end">{rows[0]["date"]} – {rows[-1]["date"]} '
        f'(judged from {first["date"]})</text>'
    )
    o.append("</svg>")
    return "\n".join(o) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="docs/images/memory-quality-chart.svg")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    rows = load(_db_path(args.db))
    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render(rows))

    # Same predicate render() uses, so the summary cannot report more points than
    # the chart draws.
    measured = [r for r in rows if r["n"] > 0 and r["hit"] is not None]
    print(
        f"wrote {dest} — {len(rows)} weeks ({len(measured)} measured), "
        f"{rows[0]['date']}..{rows[-1]['date']}, "
        f"pool {rows[0]['pool']:,}->{rows[-1]['pool']:,}, "
        f"generated {datetime.now(UTC).date()}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
