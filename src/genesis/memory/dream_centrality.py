"""Compute and publish the weekly betweenness cache, including during dry runs."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sys
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from genesis.db.connection import connect_aiosqlite_rw
from genesis.hostmetrics import readings
from genesis.hostmetrics.preflight import load_levers
from genesis.memory.dream_centrality_worker import MAX_RECORD_BYTES, PROTOCOL_VERSION
from genesis.memory.graphstore import GraphUnavailableError
from genesis.util.tmp import big_tmp_dir

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from typing import BinaryIO

    import aiosqlite
    from qdrant_client import QdrantClient

    from genesis.memory.store import MemoryStore
    from genesis.routing.router import Router

logger = logging.getLogger(__name__)

# A hung worker owns the single-run guard. Use the project deadline so
# a lower admitted CPU quota can still complete legitimate scoring.
_WORKER_TIMEOUT_S = 2 * 60 * 60
_RECOMPUTE_LOCK = asyncio.Lock()


class CentralityNotApplicable(GraphUnavailableError):
    """This install has nothing to score, by configuration: the weekly phase is
    skipped, not failed. Every other GraphUnavailableError (a crashed worker, a
    replaced or unreadable database) is a real failure and stays one."""


@dataclass
class CentralityResult:
    snapshot_at: str
    nodes_scored: int
    top: tuple[str, float] | None
    nodes_persisted: int
    rows: Iterable[tuple[str, float]]


def _score_row(row: object) -> tuple[str, float]:
    if (
        not isinstance(row, list)
        or len(row) != 2
        or not isinstance(row[0], str)
        or type(row[1]) not in (int, float)
        or not math.isfinite(row[1])
        or row[1] < 0
    ):
        raise ValueError("invalid centrality score row")
    return row[0], float(row[1])


def _score_rows(output: BinaryIO) -> Iterator[tuple[str, float]]:
    while line := output.readline(MAX_RECORD_BYTES):
        if not line.endswith(b"\n"):
            raise ValueError("invalid centrality result length")
        row = _score_row(json.loads(line))
        if row[1] <= 0:
            raise ValueError("centrality result includes a zero score")
        yield row


def _read_result(output: BinaryIO, run_id: str, byte_limit: int) -> CentralityResult:
    output.seek(0, os.SEEK_END)
    if output.tell() > byte_limit:
        raise ValueError("centrality worker result exceeded its byte limit")
    output.seek(0)
    first_line = output.readline(MAX_RECORD_BYTES)
    if not first_line.endswith(b"\n"):
        raise ValueError("incomplete centrality result header")
    header = json.loads(first_line)
    if not isinstance(header, dict) or header.get("version") != PROTOCOL_VERSION or header.get("run_id") != run_id:
        raise ValueError("centrality result does not match this run")

    snapshot_at = header["snapshot_at"]
    scored, persisted = header["nodes_scored"], header["nodes_persisted"]
    if (
        datetime.fromisoformat(snapshot_at).utcoffset() is None
        or type(scored) is not int
        or type(persisted) is not int
        or not 0 <= persisted <= scored
    ):
        raise ValueError("invalid centrality result metadata")
    top = _score_row(header["top"]) if scored else None
    start = output.tell()
    rows = _score_rows(output)
    first = next(rows, None)
    count = 1 + sum(1 for _ in rows) if first is not None else 0
    if count != persisted or (first is not None and first != top) or (not count and top and top[1] > 0):
        raise ValueError("incomplete centrality worker result")
    output.seek(start)
    return CentralityResult(snapshot_at, scored, top, persisted, _score_rows(output))


async def _finish[T](task: asyncio.Task[T]) -> T:
    """Drain an owned operation before propagating a new cancellation."""
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            while not task.done():
                with suppress(asyncio.CancelledError):
                    await asyncio.shield(task)
            task.result()
        except Exception:
            logger.warning("Centrality cleanup failed during cancellation", exc_info=True)
        raise


async def _stop_worker(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            # The hostmetrics supervisor escalates a second TERM inside the
            # scope it owns. Killing the supervisor would lose that cleanup.
            with suppress(ProcessLookupError):
                process.terminate()
            await process.wait()


async def _stop_launched_worker(launch: asyncio.Task[asyncio.subprocess.Process]) -> None:
    await _stop_worker(await launch)


async def _run_worker(path: Path, run_id: str, output: BinaryIO) -> CentralityResult:
    if os.environ.get("GENESIS_DREAM_CENTRALITY_DISABLED") == "1":
        raise CentralityNotApplicable("centrality worker is disabled")
    memory, cpus, levers = await asyncio.gather(
        asyncio.to_thread(readings.read_memory),
        asyncio.to_thread(readings.cpu_capacity),
        asyncio.to_thread(load_levers),
    )
    if memory is None:
        raise GraphUnavailableError("could not read worker memory capacity")

    ram = memory.total // 20
    # Reserve one fifth of the configured CPU budget for other work.
    # hostmetrics still makes the final decision using live CPU usage.
    cpu_percent = int(min(100, 50 * cpus, 0.8 * levers.threshold_pct * cpus))
    if cpu_percent < 1:
        raise CentralityNotApplicable("CPU capacity is below the minimum enforceable worker quota")
    launch = asyncio.create_task(asyncio.create_subprocess_exec(
            sys.executable, "-m", "genesis.hostmetrics", "run",
            "--name", "dream-centrality", "--ram", str(ram / 2**30),
            "--cpu", str(cpu_percent), "--",
            "timeout", "--kill-after=5s", f"{_WORKER_TIMEOUT_S}s",
            sys.executable, "-m", "genesis.memory.dream_centrality_worker",
            str(path), run_id, str(ram), str(cpu_percent),
            stdin=asyncio.subprocess.DEVNULL, stdout=output,
    ))
    try:
        process = await asyncio.shield(launch)
        code = await asyncio.wait_for(process.wait(), timeout=_WORKER_TIMEOUT_S + 60)
    except (asyncio.CancelledError, TimeoutError):
        try:
            await _finish(asyncio.create_task(_stop_launched_worker(launch)))
        except Exception:
            logger.warning("Error while stopping the centrality worker", exc_info=True)
        raise
    if code:
        raise GraphUnavailableError(f"centrality worker exited {code}; see resource and worker logs")
    parsing = asyncio.create_task(asyncio.to_thread(_read_result, output, run_id, ram // 8))
    try:
        return await asyncio.shield(parsing)
    except asyncio.CancelledError:
        try:
            await _finish(parsing)
        except Exception:
            logger.warning("Centrality result validation failed during cancellation", exc_info=True)
        raise


async def _publish(path: Path, original: os.stat_result, result: CentralityResult) -> None:
    async with connect_aiosqlite_rw(path, existing_only=True, timeout=5) as writer:
        if not os.path.samestat(original, await asyncio.to_thread(path.stat)):
            raise GraphUnavailableError("database was replaced during centrality scoring")
        await writer.execute("BEGIN IMMEDIATE")
        try:
            await writer.execute("DELETE FROM centrality_cache")
            await writer.executemany(
                "INSERT INTO centrality_cache (memory_id, centrality_score, computed_at) VALUES (?, ?, ?)",
                ((memory_id, round(score, 8), result.snapshot_at) for memory_id, score in result.rows),
            )
            if not os.path.samestat(original, await asyncio.to_thread(path.stat)):
                raise GraphUnavailableError("database was replaced during centrality publication")
        except (Exception, asyncio.CancelledError):
            try:
                await _finish(asyncio.create_task(writer.rollback()))
            except Exception:
                logger.warning("Centrality publication rollback failed", exc_info=True)
            raise
        # Once COMMIT is queued, drain it before closing the owned connection.
        # Cancellation before this point rolls back the entire replacement.
        commit = asyncio.create_task(writer.commit())
        try:
            await asyncio.shield(commit)
        except asyncio.CancelledError:
            try:
                await _finish(commit)
            except Exception:
                logger.warning("Centrality commit failed during cancellation", exc_info=True)
            raise


async def run_centrality_recompute(
    *,
    qdrant: QdrantClient,
    db: aiosqlite.Connection,
    router: Router,
    store: MemoryStore,
    run_id: str,
    dry_run: bool,
) -> dict[str, Any]:
    """Compute the full ranking in a worker and publish it in one transaction."""
    report: dict[str, Any] = {
        "nodes_scored": 0,
        "nodes_persisted": 0,
        "top_score": 0.0,
        "computation_ms": 0.0,
    }
    started = time.monotonic()
    try:
        if _RECOMPUTE_LOCK.locked():
            raise GraphUnavailableError("a centrality recompute is already running")
        async with _RECOMPUTE_LOCK:
            cursor = await db.execute("SELECT file FROM pragma_database_list WHERE name = 'main'")
            row = await cursor.fetchone()
            if not row or not row[0]:
                raise CentralityNotApplicable("centrality requires a file-backed database")
            path = Path(row[0])
            before = await asyncio.to_thread(path.stat)
            tmp_dir = await asyncio.to_thread(big_tmp_dir)
            with tempfile.TemporaryFile(dir=tmp_dir) as output:
                result = await _run_worker(path, run_id, output)
                await _publish(path, before, result)
    except CentralityNotApplicable as exc:
        logger.info("Centrality skipped: %s", exc)
        report["not_applicable"] = str(exc)
        report["graph_unavailable"] = True
        return report
    except Exception as exc:
        logger.warning("Centrality recompute failed", exc_info=True)
        report["error"] = str(exc) or type(exc).__name__
        if isinstance(exc, GraphUnavailableError):
            report["graph_unavailable"] = True
        return report

    report["computation_ms"] = round((time.monotonic() - started) * 1000, 1)
    report["nodes_scored"] = result.nodes_scored
    report["nodes_persisted"] = result.nodes_persisted
    report["snapshot_at"] = result.snapshot_at
    if result.top is not None:
        report["top_memory"], score = result.top
        report["top_score"] = round(score, 6)
    logger.info(
        "Centrality: cached %d/%d nonzero scores in %.1fms",
        result.nodes_persisted, result.nodes_scored, report["computation_ms"],
    )
    return report
