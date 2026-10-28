#!/usr/bin/env python3
"""Read-only inbox URL coverage report (part of #2803).

For every URL a user dropped into an inbox note, answers three things the
inbox itself never reports: was it evaluated, is its ``*.genesis.md``
response still there, and did that response actually cover it? The verdict
is given per URL with explicit denominators, never a truncated table.

Read-only end to end: the SQLite database is opened through a
``file:...?mode=ro`` URI and no inbox or response file is created, modified,
moved or deleted. Exit 2 (one-line stderr) if the inbox dir or the DB is
missing — neither is ever created.

Report building is pure: ``build_report`` and ``render_md`` touch neither
the filesystem nor the database; only ``main`` performs I/O.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

CLASSES = ("evaluated", "in_flight", "failed", "unattributable", "never")


@dataclass
class NoteInput:
    """One source note: its absolute path (the ``file_path`` key) and URLs."""

    path: str
    rel: str
    urls: list[str]


@dataclass
class RowInput:
    """One ``inbox_items`` row reduced to what classification needs.

    ``item_urls`` is ``None`` for a legacy row — NULL/legacy/decode-failed
    ``batch_items`` carries no per-item boundary, so the row cannot be
    attributed to a URL at all.
    """

    status: str | None
    error_message: str | None
    response_path: str | None
    processed_at: str | None
    rowid: int
    item_urls: list[str] | None
    # normalized URL -> original URL as stored, so the coverage check runs on
    # the same token the live gate sees (matching uses the normalized form).
    item_raw: dict[str, str] = field(default_factory=dict)


@dataclass
class Report:
    notes: list[dict] = field(default_factory=list)
    no_url_notes: list[str] = field(default_factory=list)
    totals: dict[str, int] = field(default_factory=dict)


def _row_item_urls(batch_items: object) -> tuple[list[str], dict[str, str]] | None:
    """A row's item URLs, or ``None`` when the row is legacy.

    Only a ``BATCH_ITEMS_V2_PREFIX`` payload decodes into item texts; anything
    else (NULL, legacy plain text, a decode error, a non-list payload) marks
    the row legacy — production decoders treat the same cases as unusable.
    """
    from genesis.db.crud.inbox_items import BATCH_ITEMS_V2_PREFIX
    from genesis.inbox.scanner import extract_urls, strip_tracking_params

    if not isinstance(batch_items, str) or not batch_items.startswith(
        BATCH_ITEMS_V2_PREFIX
    ):
        return None
    try:
        items = json.loads(batch_items.removeprefix(BATCH_ITEMS_V2_PREFIX))
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(items, list):
        return None
    urls: list[str] = []
    seen: set[str] = set()
    raw_by_norm: dict[str, str] = {}
    for item in items:
        if not isinstance(item, str):
            continue
        for raw in extract_urls(item):
            url = strip_tracking_params(raw)
            if url not in seen:
                seen.add(url)
                urls.append(url)
                raw_by_norm[url] = raw
    return urls, raw_by_norm


def _entry(url: str, cls: str, response="", covered="", reason="") -> dict:
    return {
        "url": url,
        "class": cls,
        "response": response,
        "covered": covered,
        "reason": reason,
    }


def _attributed(rows: list[RowInput], url: str, statuses: tuple[str, ...]) -> list[RowInput]:
    """Rows in *statuses* whose decoded item URLs contain *url*."""
    return [
        r
        for r in rows
        if r.status in statuses and r.item_urls is not None and url in r.item_urls
    ]


def _newest(candidates: list[RowInput]) -> RowInput:
    """``processed_at`` DESC, then ``rowid`` DESC; NULL sorts oldest."""
    return max(candidates, key=lambda r: ((r.processed_at or ""), r.rowid))


def _classify_url(
    url: str,
    rows: list[RowInput],
    has_legacy_completed: bool,
    read_response: Callable[[str], str | None],
) -> dict:
    """First-match classification for one URL, in spec order."""
    from genesis.inbox.url_coverage import _uncovered_urls

    completed = _attributed(rows, url, ("completed",))
    if completed:
        row = _newest(completed)
        if row.response_path is None:
            response, covered = "none", "unknown"
        else:
            text = read_response(row.response_path)
            if text is None:
                response, covered = "missing", "unknown"
            else:
                response = "present"
                # The live gate judges coverage on the ORIGINAL item URL —
                # tracking params stripped for matching would wrongly convict
                # a response that cited exactly what the item carried.
                cited = row.item_raw.get(url, url)
                covered = "yes" if _uncovered_urls(text, cited) == [] else "no"
        return _entry(url, "evaluated", response, covered)

    if _attributed(rows, url, ("pending", "processing")):
        return _entry(url, "in_flight")

    failed = _attributed(rows, url, ("failed",))
    if failed:
        return _entry(
            url, "failed", reason=(_newest(failed).error_message or "").split(":", 1)[0]
        )

    if has_legacy_completed:
        return _entry(url, "unattributable")
    return _entry(url, "never")


def build_report(
    notes: list[NoteInput],
    rows_by_file: dict[str, list[RowInput]],
    read_response: Callable[[str], str | None],
) -> Report:
    """Pure: classify every URL of every note into the five classes."""
    report = Report(totals={cls: 0 for cls in CLASSES} | {"urls": 0, "notes": 0})
    for note in notes:
        if not note.urls:
            report.no_url_notes.append(note.rel)
            continue
        rows = rows_by_file.get(note.path, [])
        has_legacy_completed = any(
            r.status == "completed" and r.item_urls is None for r in rows
        )
        entries = [
            _classify_url(url, rows, has_legacy_completed, read_response)
            for url in note.urls
        ]
        counts = {cls: 0 for cls in CLASSES}
        for entry in entries:
            counts[entry["class"]] += 1
            report.totals[entry["class"]] += 1
        report.totals["urls"] += len(entries)
        report.totals["notes"] += 1
        report.notes.append({"rel": note.rel, "counts": counts, "urls": entries})
    return report


def render_md(report: Report, *, generated_at: str, inbox: str, db: str) -> str:
    """Pure: render the report as markdown with every denominator shown."""
    t = report.totals
    lines = [
        "# Inbox URL coverage report",
        "",
        f"- Generated: {generated_at}",
        f"- Inbox: {inbox}",
        f"- Database: {db}",
        f"- Totals: {t['urls']} URLs across {t['notes']} notes — "
        f"{t['evaluated']} evaluated, {t['in_flight']} in flight, "
        f"{t['failed']} failed, {t['unattributable']} unattributable, "
        f"{t['never']} never",
        "",
    ]
    for note in report.notes:
        c = note["counts"]
        ev = [e for e in note["urls"] if e["class"] == "evaluated"]
        missing = sum(1 for e in ev if e["response"] == "missing")
        uncovered = sum(1 for e in ev if e["covered"] == "no")
        lines += [
            f"## {note['rel']}",
            "",
            f"{len(note['urls'])} URLs: {c['evaluated']} evaluated "
            f"({missing} response missing, {uncovered} not covered), "
            f"{c['in_flight']} in flight, {c['failed']} failed, "
            f"{c['unattributable']} unattributable, {c['never']} never",
            "",
            "| url | class | response | covered | reason |",
            "| --- | --- | --- | --- | --- |",
        ]
        lines += [
            f"| {e['url']} | {e['class']} | {e['response']} | "
            f"{e['covered']} | {e['reason']} |"
            for e in note["urls"]
        ]
        lines.append("")
    lines.append(f"Notes with no URLs: {len(report.no_url_notes)}")
    lines.append("")
    return "\n".join(lines)


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Open the DB read-only via ``mode=ro`` — never ``immutable=1`` (WAL)."""
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _note_urls(text: str) -> list[str]:
    from genesis.inbox.scanner import extract_urls, strip_tracking_params

    urls: list[str] = []
    seen: set[str] = set()
    for raw in extract_urls(text):
        url = strip_tracking_params(raw)
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _scan_notes(inbox: Path, file_name: str | None) -> list[NoteInput]:
    from genesis.inbox.scanner import RESPONSE_SUFFIX

    notes: list[NoteInput] = []
    for path in sorted(inbox.rglob("*")):
        if not path.is_file() or not path.name.endswith(".md"):
            continue
        if path.name.endswith(RESPONSE_SUFFIX):
            continue
        # Sync-conflict and temp files are artifacts, never notes.
        if ".sync-conflict-" in path.name or path.name.endswith(".tmp"):
            continue
        rel = path.relative_to(inbox)
        if file_name is not None and path.name != file_name:
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue
        notes.append(
            NoteInput(str(path), str(rel), _note_urls(path.read_text()))
        )
    return notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inbox", type=Path, default=None)
    parser.add_argument("--db", type=Path, default=REPO_DIR / "data" / "genesis.db")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--file", default=None)
    args = parser.parse_args(argv)

    inbox = args.inbox or Path(
        os.environ.get("GENESIS_INBOX_PATH", str(Path.home() / "inbox"))
    )
    if not inbox.is_dir():
        print(f"inbox directory not found: {inbox}", file=sys.stderr)
        return 2
    if not args.db.is_file():
        print(f"database not found: {args.db}", file=sys.stderr)
        return 2

    notes = _scan_notes(inbox.resolve(), args.file)

    rows_by_file: dict[str, list[RowInput]] = {}
    with closing(connect_readonly(args.db)) as conn:
        for note in notes:
            rows_by_file[note.path] = [
                RowInput(
                    status=r[0],
                    error_message=r[1],
                    response_path=r[2],
                    processed_at=r[3],
                    rowid=r[4],
                    item_urls=decoded[0] if decoded else None,
                    item_raw=decoded[1] if decoded else {},
                )
                for r in conn.execute(
                    "SELECT status, error_message, response_path, processed_at,"
                    " rowid, batch_items FROM inbox_items WHERE file_path = ?",
                    (note.path,),
                )
                for decoded in [_row_item_urls(r[5])]
            ]

    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    out = args.out or (
        Path.home()
        / ".genesis"
        / "output"
        / f"inbox-coverage-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.md"
    )

    def _read_response(path: str) -> str | None:
        try:
            return Path(path).read_text()
        except (OSError, UnicodeDecodeError):
            return None

    report = build_report(notes, rows_by_file, _read_response)

    # The output is the report's ONLY write, so it must never land on the
    # read-only corpus: an inbox note or a *.genesis.md response named by
    # --out would be truncated. Resolve first so symlinks cannot smuggle a
    # target inside the inbox.
    out_resolved = out.resolve()
    from genesis.inbox.scanner import RESPONSE_SUFFIX

    if out_resolved.is_relative_to(inbox.resolve()) or (
        out_resolved.name.endswith(RESPONSE_SUFFIX) and out_resolved.exists()
    ):
        print(
            f"refusing to write the report over inbox/response file: {out}",
            file=sys.stderr,
        )
        return 2

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        render_md(report, generated_at=generated_at, inbox=str(inbox), db=str(args.db))
    )
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
