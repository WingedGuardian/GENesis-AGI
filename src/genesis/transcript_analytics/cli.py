"""ta — local Claude Code transcript analytics.

    ta ingest [--since DAYS] [--timer]   build/refresh the per-source Parquet store
    ta status                            store size, sources, staleness, row counts
    ta sql "<query>" [--csv]             query the views (tool_calls, turns, hooks, events,
                                         session_meta, agents, sessions; raw_<table> too)
    ta prune --before YYYY-MM-DD         drop sources whose transcript is GONE and is older

``--timer`` honours the off-switch: GENESIS_TRANSCRIPT_ANALYTICS_DISABLED=1 or a
``DISABLED`` file in the data directory makes it exit 0 without work.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import resource
import stat
import sys
from pathlib import Path

DEFAULT_PROJECTS = Path(os.environ.get("TA_PROJECTS", Path.home() / ".claude" / "projects"))
DEFAULT_DATA = Path(
    os.environ.get("TA_DATA", Path.home() / ".genesis" / "analytics" / "transcripts")
)


def _peak_rss_mb() -> int:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)


def _disabled(args) -> bool:
    return (
        os.environ.get("GENESIS_TRANSCRIPT_ANALYTICS_DISABLED") == "1"
        or (args.data / "DISABLED").exists()
    )


def _positive_window(value):
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("--since must be a finite positive number") from exc
    if isinstance(value, bool) or not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("--since must be a finite positive number")
    return parsed


def cmd_ingest(args) -> int:
    from genesis.transcript_analytics import publication as source_publication
    from genesis.transcript_analytics import scrub, store

    args.since = _positive_window(args.since)

    if args.timer and _disabled(args):
        print("transcript-analytics: disabled by lever; nothing done")
        return 0
    try:
        options = {"since_days": args.since}
        if getattr(args, "adopt_projects_root", False):
            options["adopt_projects_root"] = True
        s = store.ingest(args.projects, args.data, **options)
    except store.Busy:
        print("transcript-analytics: another ingest/prune is running; skipped")
        return 75  # the unit's SuccessExitStatus: a skipped tick, not a failure
    except store.ScrubberUnavailable as e:
        # Fail closed AND retry later: writing would store fingerprints that never
        # rebuild, losing the failure text for good (review SF-2).
        print(f"transcript-analytics: {e}", file=sys.stderr)
        return 2
    except source_publication.Failed as exc:
        print(
            json.dumps(
                {
                    "publication_failed": True,
                    "durability_uncertain": isinstance(exc, source_publication.Uncertain),
                    "reason": str(exc),
                    "staged_unpublished": getattr(exc, "staged_unpublished", None),
                    "rebuilt_staged": getattr(exc, "rebuilt_staged", None),
                    "rebuilt_durably_committed": getattr(exc, "rebuilt_durably_committed", None),
                    "last_durable_revision": getattr(exc, "last_durable_revision", None),
                    "visible_revision": getattr(exc, "visible_revision", None),
                }
            ),
            file=sys.stderr,
        )
        return 2
    from genesis.transcript_analytics import derive

    derive_failed = False
    derive_deferred = False
    # Freshness is DERIVED from the inputs (re-audit SF-1), not from what this run did.
    if not args.no_derive:
        try:
            s["derived"] = derive.build(args.data, if_stale=True)
        except store.Busy:
            s["derived"] = "skipped: lock busy"
            derive_deferred = True
        except Exception as e:  # noqa: BLE001 - a derive failure must not lose the ingest summary (SF-3)
            s["derived"] = f"failed: {e!r}"
            derive_failed = True
    s["peak_rss_mb"] = _peak_rss_mb()
    s["scrub_version"] = scrub.version()
    print(json.dumps(s))
    return 1 if s["failed"] or derive_failed else (75 if derive_deferred else 0)


def cmd_derive(args) -> int:
    from genesis.transcript_analytics import derive, store

    if args.timer and _disabled(args):
        print("transcript-analytics: disabled by lever; nothing done")
        return 0
    try:
        print(json.dumps(derive.build(args.data, if_stale=args.if_stale)))
    except store.Busy:
        print("transcript-analytics: another ingest/prune is running; skipped")
        return 75
    return 0


def _file_bytes(path, root):
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            return 0, 0, None
        if path.relative_to(root).parts[0] == "derived":
            return 0, info.st_size, None
        return info.st_size, 0, None
    except OSError as exc:
        return 0, 0, str(exc)


def _parquet_bytes(root):
    """Count regular Parquet files without traversing directory symlinks."""
    source, snapshot = 0, 0
    errors = []
    if root.is_symlink():
        return source, snapshot, ["data directory is a symlink"]
    for directory, dirs, files in os.walk(
        root, followlinks=False, onerror=lambda e: errors.append(str(e))
    ):
        dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
        for name in files:
            if not name.endswith(".parquet"):
                continue
            path = Path(directory) / name
            source_bytes, snapshot_bytes, error = _file_bytes(path, root)
            source += source_bytes
            snapshot += snapshot_bytes
            if error:
                errors.append(error)
    return source, snapshot, errors


def _marker_totals(markers):
    import pyarrow.parquet as pq

    malformed = 0
    marker_errors = []
    for m in markers:
        try:
            if m.is_symlink():
                raise ValueError("marker is a symlink")
            stats = json.loads((pq.read_metadata(m).metadata or {}).get(b"ta.stats", b"{}"))
            if not isinstance(stats, dict):
                raise ValueError("ta.stats is not an object")
            count = stats.get("malformed", 0)
            if type(count) is not int or count < 0:
                raise ValueError("malformed count is not a nonnegative integer")
            malformed += count
        except (OSError, ValueError, TypeError) as exc:
            marker_errors.append({"marker": m.name, "path": str(m), "reason": str(exc)})
    return malformed, marker_errors


def _status(args):
    from genesis.transcript_analytics import catalog, query, store

    selected = catalog.load(args.data)
    sources = store.discover(args.projects)
    stale = 0
    source_errors = []
    for p, rel in sources:
        try:
            st = os.stat(p)
        except FileNotFoundError:  # removed since discovery (review N-6)
            continue
        try:
            if not store.is_current(
                args.data, store.srckey(rel), store.source_fingerprint(p, st), selected, source=rel
            ):
                stale += 1
        except (OSError, ValueError) as exc:
            stale += 1
            source_errors.append({"source": store.source_identity(rel), "reason": str(exc)})
    markers = [
        store._table_path(args.data, store.MARKER, key, selected)
        for key in store.source_keys(args.data, selected)
    ]
    malformed, marker_errors = _marker_totals(markers)
    source_size, snapshot_size, size_errors = _parquet_bytes(args.data)
    out = {
        "data": str(args.data),
        "transcripts_now": len(sources),
        "stale_or_unbuilt": stale,
        "sources_stored": len(markers),
        "store_mb": round((source_size + snapshot_size) / 2**20, 1),
        "source_store_mb": round(source_size / 2**20, 1),
        "snapshot_store_mb": round(snapshot_size / 2**20, 1),
        "marker_errors": marker_errors,
        "source_errors": source_errors,
        "size_errors": size_errors,
        "malformed_lines": malformed,
        "disabled": (args.data / "DISABLED").exists(),
    }
    keys, excluded = store.compatible_sources(args.data, selected)
    out["coverage"] = {"included": len(keys), "excluded": excluded}
    out["inventory"] = store.inventory(args.data, selected)
    out["catalog_revision"] = selected.revision if selected is not None else None
    out["snapshot"] = query.snapshot_manifest(args.data)
    out["snapshot_current"] = query.derived_current(args.data, selected)
    out["snapshot_compatible"] = query.snapshot_compatible(args.data)
    return out


def cmd_status(args) -> int:
    from genesis.transcript_analytics import store
    from genesis.transcript_analytics.locks import publication

    with store._locked(store.DEFAULT_LOCK), publication():
        out = _status(args)
    print(json.dumps(out, indent=2))
    return 0


def _cell(v, width: int) -> str:
    from genesis.transcript_analytics.rendering import terminal_text

    s = terminal_text(v)
    # Display preview only: the stored value is intact; the marker says how much is hidden.
    return s if len(s) <= width else s[:width] + f"…(+{len(s) - width} chars)"


def cmd_sql(args) -> int:
    from genesis.transcript_analytics.query import run_query

    cols, rows = run_query(
        args.data,
        args.query,
        live=args.live,
        manifest_path=args.manifest,
        window=args.window,
        baseline=args.baseline,
    )
    if not cols:
        return 0
    if args.csv:
        import csv

        w = csv.writer(sys.stdout)
        w.writerow(cols)
        w.writerows(rows)
        return 0
    shown = rows[: args.max_rows]
    from genesis.transcript_analytics.rendering import terminal_text

    cols = [terminal_text(c) for c in cols]
    cells = [[_cell(v, args.cell_width) for v in r] for r in shown]
    widths = [max([len(c)] + [len(r[i]) for r in cells]) for i, c in enumerate(cols)]
    print("  ".join(c.ljust(w) for c, w in zip(cols, widths, strict=True)))
    print("  ".join("-" * w for w in widths))
    for r in cells:
        print("  ".join(v.ljust(w) for v, w in zip(r, widths, strict=True)))
    print(
        f"({len(rows)} rows{'' if len(rows) <= args.max_rows else f', first {args.max_rows} shown; use --csv for all'})"
    )
    return 0


def cmd_prune(args) -> int:
    from genesis.transcript_analytics import store

    try:
        n = store.prune(args.data, before=args.before, projects=args.projects)
    except store.Busy:
        print("transcript-analytics: another ingest/prune is running; nothing pruned")
        return 75
    except ValueError as e:
        print(f"transcript-analytics: refusing to prune: {e}", file=sys.stderr)
        return 2
    print(json.dumps({"pruned_sources": n}))
    return 0


def _configure(ap):
    ap.add_argument("--projects", type=Path, default=None)
    ap.add_argument("--data", type=Path, default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest")
    p.add_argument(
        "--since",
        type=_positive_window,
        default=None,
        help="only sources modified in the last N days",
    )
    p.add_argument("--timer", action="store_true", help="honour the disable lever")
    p.add_argument(
        "--no-derive",
        action="store_true",
        help="skip the snapshot rebuild until a later ingest or explicit derive",
    )
    p.add_argument(
        "--adopt-projects-root",
        action="store_true",
        help="explicitly bind a migrated store to this projects root",
    )
    p.set_defaults(handler=cmd_ingest)
    sub.add_parser("status").set_defaults(handler=cmd_status)
    p = sub.add_parser("derive", help="rebuild the materialized view snapshot")
    p.add_argument(
        "--if-stale", action="store_true", help="only when the snapshot no longer matches the store"
    )
    p.add_argument("--timer", action="store_true", help="honour the disable lever")
    p.set_defaults(handler=cmd_derive)
    p = sub.add_parser("sql")
    p.add_argument("query")
    p.add_argument("--csv", action="store_true")
    p.add_argument("--manifest", type=Path, help="write query provenance JSON alongside a report")
    p.add_argument("--window", help="analysis window label; SQL must apply this filter")
    p.add_argument("--baseline", help="deployment commit or baseline identifier")
    p.add_argument(
        "--live", action="store_true", help="query the per-source files, not the snapshot (slow)"
    )
    p.add_argument("--max-rows", type=int, default=100)
    p.add_argument(
        "--cell-width",
        type=int,
        default=120,
        help="display preview width; --csv prints whole values",
    )
    p.set_defaults(handler=cmd_sql)
    p = sub.add_parser("prune")
    p.add_argument("--before", required=True)
    p.set_defaults(handler=cmd_prune)
    sub.add_parser("verify", help="compare all snapshot and live rows").set_defaults(
        handler=cmd_verify
    )
    ap.set_defaults(func=run)


def cmd_verify(args):
    from .verify import verify

    result = verify(args.data)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def run(args, argv=None):
    from . import config, resources

    try:
        cfg = config.load()
    except config.NotInstalled as exc:
        print(json.dumps({"installed": False, "enabled": False, "reason": str(exc)}))
        return 0 if args.cmd == "status" else 69
    except Exception as exc:  # configuration errors must not enable collection
        print(f"transcript analytics configuration unavailable: {exc}", file=sys.stderr)
        return 2
    args.cfg = cfg
    args.projects = args.projects or cfg.projects_dir
    args.data = args.data or cfg.data_dir
    if not cfg.enabled or _disabled(args):
        print(
            json.dumps(
                {
                    "enabled": False,
                    "data": str(args.data),
                    "reason": "disabled by configuration or kill switch",
                }
            )
        )
        return 0 if args.cmd == "status" or getattr(args, "timer", False) else 2
    import importlib.util

    missing = [name for name in ("duckdb", "pyarrow") if importlib.util.find_spec(name) is None]
    if missing:
        print(
            json.dumps(
                {
                    "enabled": True,
                    "unavailable": missing,
                    "action": "run the normal update/bootstrap to install the transcript-analytics extra",
                }
            )
        )
        return 2
    result = resources.ensure_capped(argv or sys.argv[1:], cfg)
    if result is not None:
        return result
    try:
        return args.handler(args)
    except Exception as exc:
        from .store import Busy

        print(f"transcript analytics: {exc}", file=sys.stderr)
        return 75 if isinstance(exc, Busy) else 1


def add_parser(subparsers):
    _configure(
        subparsers.add_parser("transcripts", help="opt-in local Claude Code transcript analytics")
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="genesis transcripts")
    _configure(ap)
    arguments = list(sys.argv[1:] if argv is None else argv)
    return run(ap.parse_args(arguments), ["transcripts", *arguments])


if __name__ == "__main__":
    sys.exit(main())
