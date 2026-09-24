"""Build the FalkorDB projection of the memory graph. Explicit, not scheduled.

    python -m genesis.memory.graphstore_project

Why this exists as its own entrypoint: selecting the falkordb store with no
projection built makes EVERY root absent from the graph, and the seam is
entitled to read an absent root as "no neighbours" rather than as a failure.
`FalkorGraphStore.traverse` refuses that reading (it raises when the graph holds
no nodes at all), but a store nothing can populate would be unusable — so the
lever's documented precondition needs something that can actually satisfy it.

SCHEDULED SINCE F3 SLICE 1, and still runnable by hand. `genesis-graph-project.timer`
invokes this hourly through `scripts/graph_project_runner.sh`, which passes
`--if-armed` so an install with no engine is a clean no-op rather than an hourly
failure. That schedule is the BOUND on staleness, not freshness-on-write:
`FalkorGraphStore.invalidate()` is a no-op by construction, so between two runs
the projection serves removed links and omits new ones without raising. Slice 2
— a DB-side change signal giving write-level freshness (issue #1641) — is not
built; a burst inside the hour can still exceed the nominal bound.

A manual run is still the right thing when you want an answer rather than a
schedule: run it WITHOUT `--if-armed`, which is what makes a missing engine a
loud exit 1 instead of a shrug. Safe to re-run: the projection is built under a staging key and swapped
in atomically, so readers see the previous projection until the new one is
complete, and never a partial graph.

Reads SQLite read-only via `mode=ro` — WAL-aware, so a projection started while
the server is writing still sees committed rows. Never `immutable=1`, which
ignores the -wal and would silently project a stale snapshot.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import signal
import time
from pathlib import Path
from urllib.parse import quote

from genesis.env import falkordb_socket_path, genesis_db_path
from genesis.memory.graphstore import DatabaseUnreachable, GraphUnavailableError
from genesis.memory.graphstore_config import config_is_readable, load_config
from genesis.memory.graphstore_falkor import GRAPH_KEY, FalkorGraphStore, ProjectionInProgress

#: Client module the store imports. Named here rather than imported so the
#: armed check costs nothing on an install that never provisioned the engine —
#: importing it to find out whether it imports is the thing being avoided.
_CLIENT_MODULE = "falkordb"


def armed(socket_path: Path | None = None) -> tuple[bool, str]:
    """Is the graph engine provisioned on THIS install? ``(verdict, reason)``.

    A POSITIVE test, deliberately, and this is the whole reason it exists as a
    separate function rather than as an ``except`` clause. ``build()`` raises
    ``GraphUnavailableError`` both for "there is no engine here" and for "the
    engine is here and the projection failed" — a scheduled caller that treated
    that one exception as "nothing to do" would exit 0 on a REAL failure, which
    is the fail-open this split exists to prevent. Everything below is checked
    BEFORE any projection is attempted; once this returns True, every later
    failure is a failure and is reported as one.

    The three conditions are the three ways an install can legitimately have
    nothing to project, cheapest first:

    * ``enabled: false`` — the master switch in ``config/graphstore.yaml``. An
      operator who pinned reads to NetworkX is not asking for hourly work.
    * no socket — the engine was never provisioned, or its unit is not running.
      A filesystem check, so it costs nothing on the installs where it is
      false, which is most of them.
    * no client library — the store cannot be constructed at all.

    Note what is deliberately NOT a condition: ``mode``. The projection is kept
    current whenever the engine EXISTS, not only once reads have been moved
    onto it, because the cutover's whole value is that flipping the lever is
    instant and lands on a current graph. Gating on ``mode`` would guarantee
    that the flip lands on a projection as stale as the day it was built, which
    is the defect this timer is for.

    A socket file with no listener behind it PASSES here and then fails in
    ``build()``. That is the intended direction: a stale socket is a broken
    install worth a loud hourly failure, not a state to pass over quietly.

    NOT consulted: ``GENESIS_FALKORDB_STORE_DISABLED``, the env kill switch
    ``effective_mode()`` honours. Deliberate, and stated because its absence
    otherwise looks like an oversight — a systemd unit does not inherit the
    operator's shell environment, so reading it here would report "unset" on
    essentially every scheduled run and give a false sense that the switch was
    respected. The switch pins READS to networkx, which it still does; the
    config ``enabled`` key above is the lever that stops the projector, and it
    is the one that survives into a unit.
    """
    # A config we cannot READ is not consent. `load_config()` absorbs a parse
    # failure and returns DEFAULTS, and DEFAULTS["enabled"] is True — so
    # without this check a corrupted overlay is indistinguishable from an
    # operator enabling the backend, and the projector would read a typo as
    # permission. Checked FIRST, before the value itself, because the value is
    # meaningless when its source did not parse.
    if not config_is_readable():
        return False, "graphstore config did not parse — declining to act on defaults"

    try:
        # `is not True`, NOT falsiness — the same reading `graphstore_config`'s
        # `effective_mode()` uses, and for the same reason it spells out: the
        # overlay is hand-edited YAML, and a QUOTED `enabled: "false"` parses as
        # a non-empty string, which is TRUTHY. A falsiness test therefore reads
        # an operator's attempt to DISABLE the backend as permission to run.
        # MEASURED with `enabled: "false"`: `effective_mode()` returned
        # "networkx" (reads off) while a falsiness-based `armed()` returned True
        # (timer on) — the two halves of one config disagreeing.
        #
        # Matching the sibling exactly is the point: this module must not invent
        # a second reading of a value another module already defines.
        if load_config().get("enabled", True) is not True:
            return False, "graphstore is not enabled in config (enabled is not exactly true)"
    except Exception as exc:  # noqa: BLE001 - anything unexpected is not armed
        # NOT the unreadable-config case — `config_is_readable()` above owns
        # that, because `load_config()` never raises for a parse failure. This
        # catches something worse and unforeseen (an unreadable directory, a
        # permission error on the stat). Kept, and no longer claiming to handle
        # a case it cannot reach.
        return False, f"graphstore config could not be read: {type(exc).__name__}"

    sock = socket_path if socket_path is not None else falkordb_socket_path()
    if not sock.exists():
        return False, f"no graph engine socket at {sock}"

    if importlib.util.find_spec(_CLIENT_MODULE) is None:
        return False, f"the {_CLIENT_MODULE} client is not installed"

    return True, f"engine socket present at {sock}"


async def build(graph_key: str = GRAPH_KEY) -> dict[str, int]:
    """Project the live graph. Returns the projection's own counts."""
    import aiosqlite

    db_path = genesis_db_path()
    store = FalkorGraphStore(graph_key=graph_key)
    # Percent-encode the path before it becomes a URI. `?` and `#` are
    # URI-significant, so a raw interpolation lets SQLite read everything after
    # one as a query string or fragment: a database at `.../memory?copy.db`
    # would silently open `.../memory` instead — the wrong file, with no error.
    # `safe="/"` keeps the separators. Same shape as `inbox/writer.py`.
    #
    # The sweep is issue #1872, not this PR: `connect(f"file:{path}?mode=ro")`
    # without quoting is the prevailing pattern here — MEASURED 2026-09-08, 34
    # sites in `src/genesis/` across 24 files, plus 12 in `scripts/`. (An
    # earlier revision of this comment said "~20", read off a truncated grep;
    # the count above is the full one.) This fixes the instance this PR
    # introduced rather than shipping a new member of a known class.
    #
    # The DATABASE side of this command fails as unavailability too, not as a
    # traceback. `main()` catches `GraphUnavailableError` and turns it into one
    # line and an exit code; a missing file, an unreadable one, or a database
    # without the expected tables raises `sqlite3.OperationalError` straight
    # through it, so the operator-facing command answered a routine mistake —
    # an unset or wrong `GENESIS_DB_PATH` — with a stack trace. The engine and
    # the database are both "the projector could not reach what it needs", and
    # this command has exactly one way to say that.
    try:
        db = await aiosqlite.connect(
            f"file:{quote(str(db_path), safe='/')}?mode=ro", uri=True
        )
    except Exception as exc:
        raise DatabaseUnreachable(
            f"the memory database at {db_path} cannot be opened: {exc}"
        ) from exc
    try:
        return await store.project(db)
    except GraphUnavailableError:
        # Includes DatabaseUnreachable raised INSIDE the store while reading
        # SQLite to build the projection — re-raised with its type intact,
        # which is the whole point of the type being shared rather than
        # private to this module.
        raise
    except Exception as exc:
        raise DatabaseUnreachable(
            f"the memory database at {db_path} cannot be read: {exc}"
        ) from exc
    finally:
        await db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--graph-key",
        default=GRAPH_KEY,
        help=f"graph key to project into (default: {GRAPH_KEY})",
    )
    parser.add_argument(
        "--if-armed",
        action="store_true",
        help=(
            "exit 0 without projecting when this install has no graph engine. "
            "For the scheduled runner: the timer is enabled on every install, "
            "so an install that never provisioned the engine must be a clean "
            "no-op rather than an hourly failure. Never use this for a manual "
            "run — the whole value of running it by hand is being told why."
        ),
    )
    args = parser.parse_args()

    # SIGTERM must unwind, or the projector leaks a full copy of the graph.
    #
    # MEASURED with a control arm: under Python's DEFAULT SIGTERM handling the
    # process dies without running `except BaseException` OR `finally`, while
    # SIGINT runs both. The store deletes its staging graph only from an
    # `except BaseException`, so a `TimeoutStartSec` expiry — which systemd
    # delivers as SIGTERM — abandons a full projection under a pid-keyed name
    # that the next tick, being a different process, will never reuse.
    #
    # Routing SIGTERM onto KeyboardInterrupt puts it on the SAME path Ctrl+C
    # already takes, which is the path measured to clean up correctly, rather
    # than inventing a second shutdown route that would need its own proof.
    # `asyncio.run` already unwinds KeyboardInterrupt.
    #
    # This is the handler half only. It cannot cover SIGKILL, an OOM kill or a
    # power loss — no handler can — so the store ALSO sweeps orphaned staging
    # graphs whose pid is gone at the start of every build. That backstop is
    # what actually closes the class; this just makes the common case immediate
    # instead of waiting an hour.
    def _unwind_on_terminate(signum: int, _frame: object) -> None:
        raise KeyboardInterrupt(f"terminated by signal {signum}")

    with contextlib.suppress(ValueError, OSError):
        # ValueError when not on the main thread — the projector always is, but
        # refusing to run because a signal could not be installed would trade a
        # cleanup guarantee for no projection at all.
        signal.signal(signal.SIGTERM, _unwind_on_terminate)

    if args.if_armed:
        ok, reason = armed()
        if not ok:
            print(f"nothing to project: {reason}")
            return 0

    started = time.monotonic()
    try:
        stats = asyncio.run(build(args.graph_key))
    except GraphUnavailableError as exc:
        # Both failures here are operator problems with operator fixes rather
        # than stack traces — but they are DIFFERENT problems, and one message
        # for both sends half the readers to the wrong one. Caught by running
        # this: the client is a NEW core dependency, so on any install that has
        # not reinstalled yet the real failure is a missing library, while the
        # message was asking whether a service was running.
        if isinstance(exc, ProjectionInProgress):
            # BENIGN, and therefore exit 0 — not a failure to report. Another
            # process is projecting the same data; the work is being done.
            # Checked before the print because this is not a "cannot project"
            # at all, and an hourly unit must not go red for a collision with
            # the manual run this module's docstring recommends.
            print(f"skipping: {exc}")
            return 0
        print(f"cannot project: {exc}")
        if isinstance(exc, DatabaseUnreachable):
            # Checked FIRST and by TYPE. Before this branch existed a database
            # failure fell through to the engine message below and told the
            # reader to check a service that was running perfectly.
            print("  the memory database could not be read.")
            print("  check GENESIS_DB_PATH and the file's permissions.")
        elif "not importable" in str(exc):
            print("  the falkordb client is missing from this environment.")
            print("  reinstall dependencies:  ./scripts/bootstrap.sh")
        else:
            print("  the engine is not reachable over its socket.")
            print("  check the service:  systemctl --user status genesis-falkordb")
        return 1
    elapsed = time.monotonic() - started
    print(
        f"projected {stats['nodes']:,} nodes / {stats['edges']:,} edges "
        f"into {args.graph_key!r} in {elapsed:.2f}s "
        f"({stats['hidden']:,} of those nodes are currently hidden by the "
        f"validity predicate, which is applied per-read, not at projection time)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
