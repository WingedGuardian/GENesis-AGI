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
import errno
import html
import os
import sqlite3
import stat
import sys
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.inbox.url_coverage import _CoverageUrlOccurrence

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
    text: str = ""


@dataclass
class RowInput:
    """One ``inbox_items`` row reduced to what classification needs.

    ``item_urls`` records stored-input membership, including legacy text.
    Missing/corrupt input stays unknown; only ``item_texts`` records retained
    unambiguous logical boundaries. Baselines are never dispatched-input proof.
    """

    status: str | None
    error_message: str | None
    response_path: str | None
    processed_at: str | None
    rowid: int
    item_urls: list[str] | None
    # Association keys may drop tracking params, but coverage retains EVERY
    # original occurrence with its own source-proven presentation.
    item_occurrences: dict[str, list[_CoverageUrlOccurrence]] = field(default_factory=dict)
    input_format: str = "v2"
    item_texts: list[str] = field(default_factory=list)
    baseline_urls: list[str] = field(default_factory=list)
    baseline_text: str = ""


@dataclass
class Report:
    notes: list[dict] = field(default_factory=list)
    no_url_notes: list[str] = field(default_factory=list)
    totals: dict[str, int] = field(default_factory=dict)


def _row_item_urls(
    batch_items: object,
) -> tuple[list[str], dict[str, list[_CoverageUrlOccurrence]]] | None:
    """Stored input URL membership, retaining original occurrence contexts.

    Legacy text proves URL membership but not erased logical item boundaries.
    Corrupt versioned input is rejected whole, never salvaged as legacy text.
    """
    from genesis.db.crud.inbox_items import BATCH_ITEMS_V2_PREFIX, _decode_v2_items
    from genesis.inbox.scanner import strip_tracking_params
    from genesis.inbox.url_coverage import _coverage_url_occurrences

    if not isinstance(batch_items, str) or not batch_items.strip():
        return None
    items = (
        _decode_v2_items(batch_items)
        if batch_items.startswith(BATCH_ITEMS_V2_PREFIX)
        else [batch_items]
    )
    if items is None:
        return None
    urls: list[str] = []
    seen: set[str] = set()
    occurrences_by_norm: dict[str, list[_CoverageUrlOccurrence]] = {}
    for item in items:
        if not isinstance(item, str):
            continue
        for occurrence in _coverage_url_occurrences(item):
            url = strip_tracking_params(occurrence.display)
            occurrences_by_norm.setdefault(url, []).append(occurrence)
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls, occurrences_by_norm


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
        r for r in rows if r.status in statuses and r.item_urls is not None and url in r.item_urls
    ]


def _newest(candidates: list[RowInput]) -> RowInput:
    """``processed_at`` DESC, then ``rowid`` DESC; NULL sorts oldest."""
    return max(candidates, key=lambda r: ((r.processed_at or ""), r.rowid))


def _response_state(
    row: RowInput, read_response: Callable[[str], str | None],
) -> tuple[str, str | None]:
    if row.response_path is None:
        return "none", None
    text = read_response(row.response_path)
    return ("missing" if text is None else "present"), text


def _completed_entry(
    url: str, row: RowInput, read_response: Callable[[str], str | None],
) -> dict:
    from genesis.inbox.url_coverage import _SOURCE_FIELD_RE, _uncovered_occurrences

    response, text = _response_state(row, read_response)
    mentioned = covered = "unknown"
    if text is not None:
        mentioned = "yes" if url in _note_urls(text) else "no"
        occurrences = row.item_occurrences.get(url)
        if not _SOURCE_FIELD_RE.search(text):
            covered = "source_not_recorded"
        elif occurrences:
            covered = "no" if _uncovered_occurrences(text, occurrences) else "yes"
    entry = _entry(url, "evaluated", response, covered)
    entry.update(rowid=row.rowid, mentioned=mentioned)
    return entry


def _classify_url(
    url: str,
    rows: list[RowInput],
    has_legacy_completed: bool,
    read_response: Callable[[str], str | None],
) -> dict:
    """First-match classification for one URL, in spec order."""
    completed = _attributed(rows, url, ("completed",))
    if completed:
        return _completed_entry(url, _newest(completed), read_response)

    if _attributed(rows, url, ("pending", "processing")):
        return _entry(url, "in_flight")

    failed = _attributed(rows, url, ("failed",))
    if failed:
        return _entry(url, "failed", reason=(_newest(failed).error_message or "").split(":", 1)[0])

    if has_legacy_completed or any(
        row.item_urls is None or url in row.item_urls or url in row.baseline_urls for row in rows
    ):
        return _entry(url, "unattributable")
    return _entry(url, "never")


def _item_identity(text: str) -> str:
    from genesis.inbox.scanner import normalize_url_line

    return normalize_url_line(text.strip())


def _input_format(stored: object, decoded: object) -> str:
    from genesis.db.crud.inbox_items import BATCH_ITEMS_V2_PREFIX

    if stored is None or (isinstance(stored, str) and not stored.strip()):
        return "missing"
    if not isinstance(stored, str):
        return "unreadable_input"
    if stored.startswith(BATCH_ITEMS_V2_PREFIX):
        return "v2" if decoded is not None else "corrupt_v2"
    return "legacy"


def _item_views(note: NoteInput, rows: list[RowInput]) -> list[dict]:
    from genesis.inbox.scanner import normalize_url_line, segment_items

    entries = []
    stored = {row.rowid: {_item_identity(text) for text in row.item_texts} for row in rows}
    stored_urls = {row.rowid: set(row.item_urls or []) for row in rows}
    baseline_urls = {url for row in rows for url in row.baseline_urls}
    # The cumulative writer sorts/deduplicates stripped lines. It retains
    # appearance, not order or logical boundaries; never re-segment it.
    baseline_lines = {
        normalize_url_line(line.strip())
        for row in rows
        for line in row.baseline_text.splitlines() if line.strip()
    }
    for item in segment_items(note.text, deduplicate_urls=False):
        identity = _item_identity(item.text)
        exact = [r for r in rows if identity in stored[r.rowid]]
        urls = set(_note_urls(item.text))
        url_only = [r.rowid for r in rows if urls & stored_urls[r.rowid]]
        if exact:
            completed = [r for r in exact if r.status == "completed"]
            chosen = _newest(completed or exact)
            status = {
                "completed": "evaluated",
                "pending": "in_flight",
                "processing": "in_flight",
            }.get(chosen.status, chosen.status or "unattributable")
        else:
            uncertain = (
                url_only or urls & baseline_urls
                or all(normalize_url_line(line.strip()) in baseline_lines
                       for line in item.text.splitlines() if line.strip())
                or any(r.input_format != "v2" for r in rows)
            )
            status = "unattributable" if uncertain else "never"
        directive = item.kind == "note" and all(
            line.strip().startswith("[") and line.strip().endswith("]")
            for line in item.text.splitlines()
            if line.strip()
        )
        entries.append(
            {
                "text": item.text,
                "kind": "directive" if directive else item.kind,
                "class": status,
                "stored_match": bool(exact),
                "rows": [r.rowid for r in exact],
                "url_only": url_only,
            }
        )
    return entries


def build_report(
    notes: list[NoteInput],
    rows_by_file: dict[str, list[RowInput]],
    read_response: Callable[[str], str | None],
) -> Report:
    """Pure: classify every URL of every note into the five classes."""
    report = Report(
        totals={cls: 0 for cls in CLASSES}
        | {"urls": 0, "notes": 0, "all_notes": len(notes), "items": 0}
    )
    for note in notes:
        if not note.urls:
            report.no_url_notes.append(note.rel)
        rows = rows_by_file.get(note.path, [])
        has_legacy_completed = any(
            r.status == "completed" and (r.item_urls is None or r.input_format != "v2")
            for r in rows
        )
        entries = [
            _classify_url(url, rows, has_legacy_completed, read_response) for url in note.urls
        ]
        counts = {cls: 0 for cls in CLASSES}
        for entry in entries:
            counts[entry["class"]] += 1
            report.totals[entry["class"]] += 1
        report.totals["urls"] += len(entries)
        report.totals["notes"] += bool(note.urls)
        items = _item_views(note, rows)
        report.totals["items"] += len(items)
        history = [{"row": row, "response": _response_state(row, read_response)[0]} for row in rows]
        report.notes.append(
            {"rel": note.rel, "counts": counts, "urls": entries, "items": items, "history": history}
        )
    return report


def _cell(value: str) -> str:
    """One Markdown table cell: no newlines, pipes escaped, or the row splits."""
    return " ".join(str(value).split()).replace("|", "\\|")


def _render_history(note: dict) -> list[str]:
    lines = []
    for retained in note["history"]:
        row = retained["row"]
        lines.append(
            f"History row {row.rowid}: status: {_cell(row.status or 'unknown')}; "
            f"input: {row.input_format}; response: {retained['response']}; "
            f"response path: {_cell(row.response_path or 'none')}"
        )
        for entry in note["urls"]:
            url = entry["url"]
            association = (
                "stored_input" if url in (row.item_urls or [])
                else "baseline_only" if url in row.baseline_urls
                else "unassociated"
            )
            if association != "unassociated":
                lines.append(f"- {_cell(url)}: {association}")
    return lines


def _render_items(items: list[dict]) -> list[str]:
    lines = []
    for index, item in enumerate(items, 1):
        lines += [
            "", f"### Item {index}", "",
            f"kind: {item['kind']}; item status: {item['class']}; "
            f"stored match: {'yes' if item['stored_match'] else 'no'}; "
            f"rows: {item['rows']}; url_only: {item['url_only']}", "",
        ]
        # Quote whole untrusted blocks; never execute source directives.
        lines.extend(["<pre>", html.escape(item["text"]), "</pre>"])
    return lines


def _render_note(note: dict) -> list[str]:
    counts = note["counts"]
    evaluated = [entry for entry in note["urls"] if entry["class"] == "evaluated"]
    missing = sum(1 for entry in evaluated if entry["response"] == "missing")
    uncovered = sum(1 for entry in evaluated if entry["covered"] == "no")
    lines = [
        f"## {note['rel']}", "",
        f"{len(note['urls'])} URLs: {counts['evaluated']} evaluated "
        f"({missing} response missing, {uncovered} not covered), "
        f"{counts['in_flight']} in flight, {counts['failed']} failed, "
        f"{counts['unattributable']} unattributable, {counts['never']} never", "",
        "| url | class | response | covered | reason |",
        "| --- | --- | --- | --- | --- |",
    ]
    lines += [
        f"| {_cell(entry['url'])} | {entry['class']} | {entry['response']} | "
        f"{entry['covered']} | {_cell(entry['reason'])} |"
        for entry in note["urls"]
    ]
    lines.append("")
    lines += [
        f"URL {entry['url']} — selected row: {entry.get('rowid', 'unknown')}; "
        f"mentioned: {entry.get('mentioned', 'unknown')}"
        for entry in evaluated
    ]
    return lines + _render_history(note) + _render_items(note["items"]) + [""]


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
        f"- All selected notes: {t['all_notes']}; Logical items: {t['items']}",
        f"- Other-path history rows: {t.get('other_path_rows', 'not loaded')}",
        "- Input association, response presence, Source coverage and incidental mentions are distinct.",
        "- Baseline membership is history only; no field proves acquisition or sound interpretation.",
        "- Never means no retained matching input; unreferenced responses are not inventoried.",
        "",
    ]
    for note in report.notes:
        lines.extend(_render_note(note))
    lines.append(f"Notes with no URLs: {len(report.no_url_notes)}")
    lines.append("")
    return "\n".join(lines)


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Open the DB read-only via ``mode=ro`` — never ``immutable=1`` (WAL).

    ``Path.as_uri`` percent-encodes the path, so a ``--db`` value containing
    ``?``, ``#`` or ``%`` still names the file the caller meant.
    """
    return sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)


def _note_urls(text: str) -> list[str]:
    from genesis.inbox.scanner import strip_tracking_params
    from genesis.inbox.url_coverage import _coverage_url_occurrences

    urls: list[str] = []
    seen: set[str] = set()
    for occurrence in _coverage_url_occurrences(text):
        url = strip_tracking_params(occurrence.display)
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _inbox_files(inbox: Path) -> list[Path]:
    """Every file the monitor may evaluate, of any type, without reading bodies."""
    from genesis.inbox.scanner import RESPONSE_SUFFIX, is_excluded_parent_dir

    def on_error(error: OSError) -> None:
        raise error

    paths: list[Path] = []
    # rglob silently suppresses directory errors, hiding protected identities.
    # Do not follow directory symlinks. Prune the folders a recursive inbox
    # scan never reads below: exact for the default response folder, and for
    # any other name this only keeps more files protected.
    for directory, subdirs, names in os.walk(inbox, onerror=on_error):
        subdirs[:] = [name for name in subdirs if not is_excluded_parent_dir(name)]
        for name in names:
            if name.startswith("."):
                continue
            if name.endswith(RESPONSE_SUFFIX) or ".sync-conflict-" in name:
                continue
            path = Path(directory) / name
            # Path.is_file hides every OSError on Python 3.14; only absence
            # may drop a note, any other metadata error refuses the inventory.
            try:
                info = path.stat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                if exc.errno != errno.ELOOP:  # a symlink loop names no file
                    raise
                continue
            if stat.S_ISREG(info.st_mode):
                paths.append(path)
    return sorted(paths)


def _note_paths(inbox: Path) -> list[Path]:
    """Eligible markdown notes: the files the coverage listing reports on."""
    return [path for path in _inbox_files(inbox) if path.name.endswith(".md")]


def _scan_notes(inbox: Path, file_name: str | None) -> list[NoteInput]:
    notes: list[NoteInput] = []
    for path in _note_paths(inbox):
        if file_name is not None and path.name != file_name:
            continue
        # The monitor's read_content decodes with errors="replace"; a note
        # carrying non-UTF-8 bytes must not abort the whole report.
        text = path.read_text(encoding="utf-8", errors="replace")
        notes.append(
            NoteInput(
                str(path),
                str(path.relative_to(inbox)),
                _note_urls(text),
                text,
            )
        )
    return notes


def _file_identity(path: Path) -> tuple[int, int] | None:
    """Follow links; only expected absence means there is no identity."""
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino


def _output_is_safe(out: Path, inbox: Path, protected: list[Path]) -> bool:
    """One guard, used before DB access and again once responses are known."""
    from genesis.inbox.scanner import RESPONSE_SUFFIX

    try:
        out_resolved = out.resolve()
        protected_targets = {path.resolve() for path in protected}
        protected_ids = {_file_identity(path) for path in protected} - {None}
        refused = (
            out.absolute().is_relative_to(inbox)
            or out_resolved.is_relative_to(inbox.resolve())
            or out_resolved in protected_targets
            or _file_identity(out) in protected_ids
            or (out_resolved.name.endswith(RESPONSE_SUFFIX) and out_resolved.exists())
        )
    except (OSError, RuntimeError) as exc:
        print(f"cannot establish report output safety: {exc}", file=sys.stderr)
        return False
    if refused:
        print(
            f"refusing to write the report over inbox/response file: {out}",
            file=sys.stderr,
        )
        return False

    return True


def _row_input(row: tuple) -> RowInput:
    """Decode the report SELECT's explicit stored-field order."""
    from genesis.db.crud.inbox_items import stored_item_texts

    decoded = _row_item_urls(row[5])
    return RowInput(
        status=row[0], error_message=row[1], response_path=row[2],
        processed_at=row[3], rowid=row[4],
        item_urls=decoded[0] if decoded else None,
        item_occurrences=decoded[1] if decoded else {},
        input_format=_input_format(row[5], decoded),
        item_texts=stored_item_texts(row[5]),
        baseline_urls=_note_urls(row[6]) if isinstance(row[6], str) else [],
        baseline_text=row[6] if isinstance(row[6], str) else "",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inbox", type=Path, default=None)
    parser.add_argument("--db", type=Path, default=REPO_DIR / "data" / "genesis.db")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--file", default=None)
    args = parser.parse_args(argv)

    inbox = args.inbox or Path(os.environ.get("GENESIS_INBOX_PATH", str(Path.home() / "inbox")))
    if not inbox.is_dir():
        print(f"inbox directory not found: {inbox}", file=sys.stderr)
        return 2
    if not args.db.is_file():
        print(f"database not found: {args.db}", file=sys.stderr)
        return 2

    # absolute(), NOT resolve() or abspath(): the monitor scans the
    # configured path verbatim, so inbox_items.file_path carries that
    # spelling. abspath() collapses `..` before symlinks are followed
    # (`/x/link/../inbox` != `/x/../inbox` when link is a symlink), and
    # resolve() rewrites the spelling entirely — either misses the rows.
    inbox = inbox.absolute()
    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    default_name = f"inbox-coverage-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}.md"
    out = args.out or (Path.home() / ".genesis" / "output" / default_name)
    if out.is_dir():
        out = out / default_name

    database = args.db.resolve()
    database_targets = [
        database,
        *(database.with_name(database.name + suffix) for suffix in ("-wal", "-shm", "-journal")),
    ]
    if not _output_is_safe(out, inbox, database_targets):
        return 2

    try:
        notes = _scan_notes(inbox, args.file)
    except (OSError, RuntimeError) as exc:
        print(f"cannot establish inbox inventory: {exc}", file=sys.stderr)
        return 2

    rows_by_file: dict[str, list[RowInput]] = {}
    with closing(connect_readonly(args.db)) as conn:
        conn.execute("BEGIN")
        all_rows = conn.execute("SELECT COUNT(*) FROM inbox_items").fetchone()[0]
        response_targets = [
            Path(row[0])
            for row in conn.execute(
                "SELECT DISTINCT response_path FROM inbox_items WHERE response_path IS NOT NULL"
            )
        ]
        for note in notes:
            rows_by_file[note.path] = [
                _row_input(r)
                for r in conn.execute(
                    "SELECT status, error_message, response_path, processed_at,"
                    " rowid, batch_items, evaluated_content FROM inbox_items WHERE file_path = ?",
                    (note.path,),
                )
            ]

    @cache
    def _read_response(path: str) -> str | None:
        try:
            return Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    report = build_report(notes, rows_by_file, _read_response)
    report.totals["other_path_rows"] = all_rows - sum(len(rows) for rows in rows_by_file.values())

    try:
        protected = [*database_targets, *_inbox_files(inbox), *response_targets]
    except (OSError, RuntimeError) as exc:
        print(f"cannot establish inbox inventory: {exc}", file=sys.stderr)
        return 2
    if not _output_is_safe(out, inbox, protected):
        return 2

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        render_md(report, generated_at=generated_at, inbox=str(inbox), db=str(args.db)),
        encoding="utf-8",
    )
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
