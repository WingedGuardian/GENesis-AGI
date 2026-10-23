"""``genesis db query``: run one read-only SQL statement from the shell.

The sanctioned route for ad-hoc reads of a Genesis database. Read-only is
enforced by SQLite (see :mod:`genesis.db.readonly_query`), so a query whose
text mentions a write keyword (``WHERE s = 'DELETE'``) still runs, and a real
write is refused however it is spelled.

Run it with the venv interpreter: ``~/genesis/.venv/bin/python -m genesis db
query "<sql>"`` (a session shell has no ``python`` on PATH, and the system
``python3`` cannot import genesis).

Exit codes: 0 ok; 1 any other error (SQLite error, no statement, deadline
exceeded); 2 usage error (argparse's own code); 3 database quarantined;
4 refused (outside the read-only allowlist).

Output is safe to parse line by line: in TSV, every control character (C0,
DEL, C1) in a cell OR a column name is escaped, so a value or an alias cannot
forge a row or a column. ``--json`` is strict JSON (no ``NaN``/``Infinity``).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import re
import sys
from pathlib import Path

_NO_LIMIT = 0
_EXIT_REFUSED = 4  # never 2: argparse exits 2 on a usage error
_INVOCATION = '~/genesis/.venv/bin/python -m genesis db query "<sql>"'
_NAMED_ESCAPES = {"\\": "\\\\", "\t": "\\t", "\n": "\\n", "\r": "\\r"}
# C0 controls, DEL, and C1 controls, plus the backslash used as the escape.
_TSV_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\\]")
# json.dumps already escapes C0; DEL and C1 pass through it raw.
_JSON_UNSAFE = re.compile(r"[\x7f-\x9f]")


def _escape_tsv(text: str) -> str:
    return _TSV_UNSAFE.sub(
        lambda m: _NAMED_ESCAPES.get(
            m.group(),
            f"\\u{ord(m.group()):04x}" if ord(m.group()) > 255 else f"\\x{ord(m.group()):02x}",
        ),
        text,
    )


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return "x'" + value.hex() + "'"
    return _escape_tsv(str(value))


def _json_value(value):
    if isinstance(value, bytes):
        return "x'" + value.hex() + "'"
    if isinstance(value, float) and not math.isfinite(value):
        # Strict JSON has no NaN/Infinity tokens; keep the value readable.
        return "NaN" if math.isnan(value) else ("Infinity" if value > 0 else "-Infinity")
    return value


def _non_negative_int(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {value}")
    return value


def _non_negative_float(text: str) -> float:
    value = float(text)
    if not value >= 0 or not math.isfinite(value):  # also rejects nan
        raise argparse.ArgumentTypeError(f"must be a finite number >= 0, got {text}")
    return value


def _cmd_query(args: argparse.Namespace) -> int:
    from genesis.db.integrity import DatabaseIntegrityError
    from genesis.db.readonly_query import ReadOnlyViolation, run_readonly_query
    from genesis.env import genesis_db_path

    db = None
    max_rows = None if args.limit == _NO_LIMIT else args.limit
    try:
        if args.db == "":
            raise ValueError("--db must not be empty")
        db = genesis_db_path() if args.db is None else args.db
        result = run_readonly_query(
            db, args.sql, args.param or (), max_rows=max_rows, timeout_s=args.timeout or None
        )
    except ReadOnlyViolation as exc:
        print(str(exc), file=sys.stderr)
        return _EXIT_REFUSED
    except DatabaseIntegrityError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # sqlite3.Error and friends: report, do not trace
        print(f"error: {exc}", file=sys.stderr)
        missing = False
        if db is not None:
            # An optional diagnostic must not mask the original failure.
            with contextlib.suppress(OSError, RuntimeError, ValueError):
                missing = not Path(db).expanduser().exists()
        if missing:
            print(f"  no database at {db} (pass --db PATH)", file=sys.stderr)
        return 1

    if args.json:
        if len(set(result.columns)) != len(result.columns):
            print(
                "error: duplicate JSON column names; use explicit unique aliases", file=sys.stderr
            )
            return 1
        # One mapping/encoder stream at a time, after all query errors are known.
        encoder = json.JSONEncoder(indent=2, ensure_ascii=False, allow_nan=False)
        if result.rows:
            sys.stdout.write("[\n")
            for index, row in enumerate(result.rows):
                if index:
                    sys.stdout.write(",\n")
                values = {
                    col: _json_value(val) for col, val in zip(result.columns, row, strict=True)
                }
                sys.stdout.write("  ")
                for chunk in encoder.iterencode(values):
                    escaped = _JSON_UNSAFE.sub(lambda m: f"\\u{ord(m.group()):04x}", chunk)
                    sys.stdout.write(escaped.replace("\n", "\n  "))
            sys.stdout.write("\n]\n")
        else:
            print("[]")
    else:
        if result.columns:
            print("\t".join(_escape_tsv(c) for c in result.columns))
        for row in result.rows:
            print("\t".join(_cell(v) for v in row))
    if result.truncated:
        print(
            f"TRUNCATED: {len(result.rows)} rows shown, more exist "
            "(raise --limit, or --limit 0 for all)",
            file=sys.stderr,
        )
    return 0


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    from genesis.db.readonly_query import DEFAULT_MAX_ROWS, DEFAULT_TIMEOUT_S, MAX_RESULT_BYTES

    p = subparsers.add_parser("db", help="Database tools (read-only query)")
    # required: a bare `genesis db` is a usage error (exit 2), like argparse's
    # own convention for a missing subcommand.
    sub = p.add_subparsers(dest="db_command", required=True)

    q = sub.add_parser(
        "query",
        help="Run ONE read-only SQL statement (enforced by SQLite, not by the text)",
        description=(
            f"Run one read-only SQL statement: {_INVOCATION}. Writes, ATTACH, VACUUM, "
            "transactions and state-changing pragmas are refused by SQLite itself "
            "(exit 4). rtree tables are not supported. Exit codes: 0 ok, 1 error, "
            f"2 usage, 3 quarantined database, 4 refused. Retained results have a "
            f"{MAX_RESULT_BYTES}-byte accounting budget, including with --limit 0; "
            "this is not a total process memory bound."
        ),
    )
    q.add_argument("sql", help="One SQL statement; use ? placeholders with --param")
    q.add_argument(
        "--param",
        action="append",
        metavar="VALUE",
        help="Bind a ? placeholder (repeatable, in order; values are strings)",
    )
    q.add_argument("--db", default=None, help="Database path (default: the Genesis DB)")
    q.add_argument("--json", action="store_true", help="Print a JSON list of objects")
    q.add_argument(
        "--limit",
        type=_non_negative_int,
        default=DEFAULT_MAX_ROWS,
        help=(
            f"Max rows to print (default {DEFAULT_MAX_ROWS}; 0 = no row limit). "
            "Truncation is reported on stderr."
        ),
    )
    q.add_argument(
        "--timeout",
        type=_non_negative_float,
        default=DEFAULT_TIMEOUT_S,
        metavar="SECONDS",
        help=(
            f"Cooperative SQLite deadline in seconds (default {DEFAULT_TIMEOUT_S:g}; "
            "0 = no deadline). Progress and row checks plus a setup-time busy cap; "
            "native calls and output sinks have no hard process deadline."
        ),
    )
    q.set_defaults(func=_cmd_query)
