#!/usr/bin/env python3
"""Read-only report on the draft lesson backlog in ``procedural_memory``.

For each draft lesson: its nearest EARLIER lesson and the cosine of their
principle embeddings. Over all lessons: topics (connected components of the
"cosine >= 0.85" graph), the drafts each live lesson would absorb (nearest live
lesson at cosine >= 0.85), and the share of drafts with no neighbour above 0.75.

Topics are single-link components, which chain: two unrelated lessons join one
topic through a run of near neighbours. The per-row nearest-neighbour figures
are the reliable ones; the topics are a map, not a verdict.

The 0.85 threshold is the store-time novelty gate's (extractor
NOVELTY_THRESHOLD). The gate refuses a new lesson at or above it against the
same-type lessons stored when it was written (drafts included, held lessons
excluded, the 50 highest-confidence). The report compares against the lessons
live now, which may have been written or promoted later, so a current match
does not show the gate ever saw the pair.
The report splits near-duplicates by whether they share a task type.

Similarity runs in row blocks bounded by a share of the memory this process can
use, capped by its cgroup (one block may
be the whole table when it is small). The database is opened read-only (mode=ro) after the admission fence;
a fenced database is refused. Output: markdown plus a per-draft CSV in
~/.genesis/output/ (or --out). No row is changed.
"""

from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
_HOOKS = str(Path(__file__).resolve().parent / "hooks")
if _HOOKS not in sys.path:
    sys.path.insert(0, _HOOKS)

from genesis.learning.procedural.extractor import (  # noqa: E402
    CROSS_TYPE_PREFILTER,
)
from genesis.learning.procedural.extractor import (  # noqa: E402
    NOVELTY_THRESHOLD as TOPIC_COSINE,
)

ALONE_COSINE = 0.75
TOP_TOPICS = 30
EXAMPLES_PER_TOPIC = 3
# Share of available memory one similarity block may use.
_MEMORY_SHARE = 0.10
_FALLBACK_BLOCK = 256


@dataclass
class Lessons:
    ids: list[str]
    task_types: list[str]
    principles: list[str]
    created: list[str]
    draft: np.ndarray  # bool (N,)
    quarantined: np.ndarray  # bool (N,); never live, as every production reader treats it
    vectors: np.ndarray  # float32 (N, D), L2-normalised
    skipped: int  # rows with no usable embedding


class FencedDatabase(Exception):
    """The admission fence refuses this database (or its state is unknowable)."""


def load(db_path: Path) -> Lessons:
    """Read every non-deprecated lesson. Rows without a usable embedding are
    counted in ``skipped``, never silently dropped. Raises ``FencedDatabase``
    when the admission fence refuses the database."""
    from db_admission_check import database_is_fenced

    from genesis.learning.procedural.embedding import EMBEDDING_DIM

    if database_is_fenced(db_path):
        raise FencedDatabase(str(db_path))
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        rows = conn.execute(
            "SELECT id, task_type, principle, created_at, draft, principle_embedding,"
            " quarantined"
            " FROM procedural_memory WHERE deprecated = 0"
            " ORDER BY julianday(created_at), created_at, id"
        ).fetchall()
    finally:
        conn.close()
    keep, vecs, skipped = [], [], 0
    for row in rows:
        blob = row[5]
        if not isinstance(blob, bytes) or len(blob) != EMBEDDING_DIM * 4:
            skipped += 1
            continue
        vec = np.frombuffer(blob, dtype="<f4")
        if not np.isfinite(vec).all() or not vec.any():
            skipped += 1  # a NaN or zero vector would poison every max and argmax
            continue
        keep.append(row)
        vecs.append(vec)
    matrix = (
        np.vstack(vecs).astype(np.float32) if vecs else np.zeros((0, EMBEDDING_DIM), np.float32)
    )
    # float64: a float32 sum of squares overflows or underflows for extreme components.
    norms = np.linalg.norm(matrix.astype(np.float64), axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return Lessons(
        ids=[r[0] for r in keep],
        task_types=[r[1] for r in keep],
        principles=[r[2] for r in keep],
        created=[r[3] for r in keep],
        draft=np.array([bool(r[4]) for r in keep], dtype=bool),
        quarantined=np.array([bool(r[6]) for r in keep], dtype=bool),
        vectors=(matrix / norms).astype(np.float32),
        skipped=skipped,
    )


def block_rows(n: int) -> int:
    """Rows per similarity block: one block's (rows x n) float32 product, plus
    working copies, within a share of the memory this process can use. That is
    the cgroup-capped figure (``hostmetrics.read_memory``), not procfs
    MemAvailable, which inside a container can be the host's. The x3 charges
    each row for copies that exist once per row, so it over-reserves; that is
    safe."""
    from genesis.hostmetrics.readings import read_memory

    if n == 0:
        return 1
    try:
        memory = read_memory()
    except (OSError, ValueError):
        memory = None
    if memory is None:
        return min(n, _FALLBACK_BLOCK)
    available = memory.available
    per_row = n * 4 * 3
    return max(1, min(n, int(available * _MEMORY_SHARE) // per_row))


class _Components:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def analyse(lessons: Lessons, *, block: int) -> dict:
    """Nearest earlier lesson per draft, nearest live lesson per draft, best
    neighbour per draft, and topic components. Rows are in created_at order, so
    "earlier" is "lower index"."""
    n = len(lessons.ids)
    x = lessons.vectors
    # Live means usable now: neither a draft nor quarantined (the production
    # readers filter quarantined = 0), so a failed procedure absorbs nothing.
    live_mask = ~lessons.draft & ~lessons.quarantined
    comps = _Components(n)
    nearest_earlier = np.full(n, -1)
    nearest_earlier_cos = np.full(n, np.nan)
    nearest_live = np.full(n, -1)
    nearest_live_cos = np.full(n, np.nan)
    best_cos = np.full(n, np.nan)
    for start in range(0, n, block):
        stop = min(n, start + block)
        sims = x[start:stop] @ x.T  # (b, n)
        for k, i in enumerate(range(start, stop)):
            row = sims[k]
            for j in np.nonzero(row[i + 1 :] >= TOPIC_COSINE)[0] + i + 1:
                comps.union(i, int(j))
            if not lessons.draft[i]:
                continue
            others = row.copy()
            others[i] = -np.inf
            if n > 1:
                best_cos[i] = float(others.max())
            if i > 0:
                j = int(np.argmax(row[:i]))
                nearest_earlier[i], nearest_earlier_cos[i] = j, float(row[j])
            live = np.where(live_mask, others, -np.inf)
            if live_mask.any():
                j = int(np.argmax(live))
                if np.isfinite(live[j]):
                    nearest_live[i], nearest_live_cos[i] = j, float(live[j])
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(comps.find(i), []).append(i)
    return {
        "nearest_earlier": nearest_earlier,
        "nearest_earlier_cos": nearest_earlier_cos,
        "nearest_live": nearest_live,
        "nearest_live_cos": nearest_live_cos,
        "best_cos": best_cos,
        "topics": sorted(groups.values(), key=lambda g: (-len(g), g[0])),
    }


def _histogram(values: np.ndarray) -> list[tuple[str, int]]:
    edges = [-1.0, 0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0001]
    labels = [
        "< 0.50",
        "0.50-0.60",
        "0.60-0.70",
        "0.70-0.75",
        "0.75-0.80",
        "0.80-0.85",
        "0.85-0.90",
        "0.90-0.95",
        ">= 0.95",
    ]
    counts, _ = np.histogram(values[~np.isnan(values)], bins=edges)
    out = list(zip(labels, counts.tolist(), strict=True))
    missing = int(np.isnan(values).sum())
    if missing:
        out.append(("no value", missing))
    return out


def _one_line(text: str) -> str:
    return " ".join((text or "").split()).replace("|", "\\|")


def render(lessons: Lessons, result: dict, *, generated_at: str, csv_name: str) -> str:
    n = len(lessons.ids)
    drafts = np.nonzero(lessons.draft)[0]
    nd = len(drafts)
    live = ~lessons.draft & ~lessons.quarantined
    out = [
        "# Lesson backlog report",
        "",
        f"Generated {generated_at}. Read-only; no row was changed.",
        "",
        f"- Lessons read (not deprecated): {n + lessons.skipped}",
        f"- Without a usable embedding, left out: {lessons.skipped}",
        f"- Drafts: {nd}; live (not draft, not quarantined): {int(live.sum())}; "
        f"quarantined non-drafts: {n - nd - int(live.sum())}",
        f"- Per-draft detail: `{csv_name}`",
        "",
    ]
    if nd:
        # NaN (a draft with no other lesson) counts as alone, never as matched.
        alone = int(np.sum(~(result["best_cos"][drafts] >= ALONE_COSINE)))
        near = [i for i in drafts if result["nearest_earlier_cos"][i] >= TOPIC_COSINE]
        same = sum(
            1
            for i in near
            if lessons.task_types[i] == lessons.task_types[int(result["nearest_earlier"][i])]
        )
        out += [
            f"Drafts with no neighbour at or above {ALONE_COSINE}: {alone} of {nd} "
            f"({alone / nd:.1%}).",
            "",
            "## Nearest earlier lesson, per draft (the reliable figure)",
            "",
            "| cosine | drafts |",
            "|---|---|",
            *[
                f"| {label} | {count} |"
                for label, count in _histogram(result["nearest_earlier_cos"][drafts])
            ],
            "",
            f"Of the {len(near)} drafts whose nearest earlier lesson is at or above "
            f"{TOPIC_COSINE}, {same} share its task type and {len(near) - same} do not. "
            "The extractor's store path dedups in three layers: a same-type novelty "
            "check at this threshold, a cross-type LLM duplicate check over candidates "
            f"from cosine {CROSS_TYPE_PREFILTER}, and a context-overlap check against "
            "trusted lessons only. Other write paths, such as the executor's "
            "retrospective, store drafts with none of them. A cross-type pair here may "
            "be one those layers judged distinct, missed, or never saw; the split alone "
            "does not show which.",
            "",
        ]
    topics = result["topics"]
    multi = [t for t in topics if len(t) > 1]
    out += [
        f"## Topics (single-link components at cosine >= {TOPIC_COSINE})",
        "",
        f"{len(topics)} topics; {len(multi)} hold more than one lesson. Single-link "
        "chains, so a large topic can join lessons that are not near each other; "
        "read it with the nearest-neighbour figures above.",
        "",
        f"### The {min(TOP_TOPICS, len(topics))} largest of {len(topics)}",
        "",
    ]
    for rank, topic in enumerate(topics[:TOP_TOPICS], 1):
        d = int(lessons.draft[topic].sum())
        lv = int(live[topic].sum())
        out.append(f"{rank}. **{len(topic)} lessons** ({d} drafts, {lv} live)")
        for i in topic[:EXAMPLES_PER_TOPIC]:
            out.append(f"   - `{lessons.task_types[i]}`: {_one_line(lessons.principles[i])}")
    absorbed: dict[int, list[int]] = {}
    for i in drafts:
        j = int(result["nearest_live"][i])
        if j >= 0 and result["nearest_live_cos"][i] >= TOPIC_COSINE:
            absorbed.setdefault(j, []).append(int(i))
    total = sum(len(v) for v in absorbed.values())
    ranked = sorted(absorbed.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    out += [
        "",
        f"## Drafts each live lesson would absorb (nearest live lesson at cosine >= {TOPIC_COSINE})",
        "",
        "This is the store-time novelty threshold. The gate refuses a new lesson this "
        "close to the same-type lessons stored when it was written (drafts included, "
        "held lessons excluded, the 50 highest-confidence); these live lessons are "
        "today's and may have been written or promoted later, so a match here is "
        "current coverage, not a record of what the gate saw.",
        "",
        f"{total} of {nd} drafts have such a live lesson; {len(ranked)} live lessons "
        f"would absorb at least one. The {min(TOP_TOPICS, len(ranked))} that absorb the most:",
        "",
        "| live lesson | task type | drafts absorbed |",
        "|---|---|---|",
        *[
            f"| `{lessons.ids[j]}` | `{lessons.task_types[j]}` | {len(v)} |"
            for j, v in ranked[:TOP_TOPICS]
        ],
        "",
        "The full mapping is in the CSV (`nearest_live_id` at `nearest_live_cosine`).",
        "",
    ]
    if nd and (n - nd):
        out += [
            "Nearest live lesson, per draft (read a low absorbed count against this):",
            "",
            "| cosine | drafts |",
            "|---|---|",
            *[
                f"| {label} | {count} |"
                for label, count in _histogram(result["nearest_live_cos"][drafts])
            ],
            "",
        ]
    return "\n".join(out)


def write_csv(path: Path, lessons: Lessons, result: dict) -> None:
    def ref(j: int) -> str:
        return lessons.ids[j] if j >= 0 else ""

    def num(v: float) -> str:
        return "" if np.isnan(v) else f"{v:.4f}"

    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "draft_id",
                "task_type",
                "created_at",
                "nearest_earlier_id",
                "nearest_earlier_cosine",
                "nearest_live_id",
                "nearest_live_cosine",
                "best_neighbour_cosine",
                "principle",
            ]
        )
        for i in np.nonzero(lessons.draft)[0]:
            w.writerow(
                [
                    lessons.ids[i],
                    lessons.task_types[i],
                    lessons.created[i],
                    ref(int(result["nearest_earlier"][i])),
                    num(result["nearest_earlier_cos"][i]),
                    ref(int(result["nearest_live"][i])),
                    num(result["nearest_live_cos"][i]),
                    num(result["best_cos"][i]),
                    _one_line(lessons.principles[i]),
                ]
            )


def _aliases_database(target: Path, db_path: Path) -> bool:
    """Whether writing ``target`` would write over the input database or one of
    its SQLite files (the -wal, -shm and -journal siblings), by name, symlink or
    hard link. A read-only report must never write to what it reads."""
    # SQLite names its sidecar files after the file it opened, the resolved path.
    real_db = Path(db_path).resolve()
    for sibling in ("", "-wal", "-shm", "-journal"):
        db_file = Path(str(real_db) + sibling)
        if target.resolve() == db_file.resolve():
            return True
        try:
            if target.exists() and db_file.exists() and os.path.samefile(target, db_file):
                return True
        except OSError:
            return True  # cannot tell, so do not write
    return False


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", default=None, help="genesis.db path (default: the install's)")
    ap.add_argument("--out", default=None, help="output .md path (default: ~/.genesis/output/)")
    ap.add_argument(
        "--block", type=_positive, default=None, help="rows per block (default: from memory)"
    )
    args = ap.parse_args(argv)

    from genesis.env import genesis_db_path, output_dir, user_timezone

    db_path = Path(args.db) if args.db else genesis_db_path()
    if not db_path.exists():
        print(f"lesson_backlog_report: no database at {db_path}", file=sys.stderr)
        return 2
    try:
        lessons = load(db_path)
    except FencedDatabase:
        print(f"lesson_backlog_report: {db_path} is fenced; not reading it", file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else None
    if out is not None and out.with_suffix(".csv") == out:
        print(
            "lesson_backlog_report: --out must not end in .csv (that is the CSV's name)",
            file=sys.stderr,
        )
        return 2
    block = args.block or block_rows(len(lessons.ids))
    result = analyse(lessons, block=block)
    # Shown to the user, so dated in the configured zone (stored and computed in UTC).
    now = datetime.now(UTC).astimezone(ZoneInfo(user_timezone()))
    if out is None:
        out = output_dir() / f"lesson-backlog-report-{now.date()}.md"
    csv_path = out.with_suffix(".csv")
    for target in (out, csv_path):
        if _aliases_database(target, db_path):
            print(
                f"lesson_backlog_report: {target} is the input database or one of its"
                " files; not writing over it",
                file=sys.stderr,
            )
            return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    write_csv(csv_path, lessons, result)
    out.write_text(render(lessons, result, generated_at=now.isoformat(), csv_name=csv_path.name))
    print(f"wrote {out} and {csv_path} ({len(lessons.ids)} lessons, block={block})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
