"""Async PDF text extraction via a crash/hang-isolated process pool.

PyMuPDF is not thread-safe and can natively crash (segfault) or hang on
malformed/adversarial PDFs. Genesis ingests UNTRUSTED PDFs (dashboard uploads,
web-fetched sources via ``knowledge_ingest_source``), so extraction runs in a
dedicated single-worker, ``spawn``-context ``ProcessPoolExecutor``:

- a crash surfaces as ``BrokenProcessPool`` (recoverable) instead of taking
  down the genesis-server event loop;
- a hang is bounded by ``PDF_EXTRACT_TIMEOUT_S`` and the wedged worker is
  SIGKILLed (``Future.cancel()`` is a no-op once a worker is running, so a
  caller-side timeout alone would leak the worker);
- both cases raise ``PDFExtractionError`` and lazily recreate the pool.

With ``max_workers=1`` a crash also fails any PDF concurrently queued behind the
offending one (they share the single worker) — an accepted tradeoff given the
low ingest volume; each failed caller simply retries on a fresh pool.

Temp/IPC note: a ``spawn`` ProcessPoolExecutor on Linux/CPython 3.12 keeps its
IPC in ``/dev/shm`` (POSIX ``sem.mp-*``) plus anonymous fd pipes — it writes
NOTHING under ``$TMPDIR`` (verified empirically). So despite genesis-server's
``TMPDIR=~/.genesis/cc-tmp`` (which the tmp-watchgod sweeps), the pool has no
artifacts there to be reaped. Revisit if a future CPython changes this.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

from genesis.knowledge._pdf_worker import _warmup, extract_pdf_text_sync

logger = logging.getLogger(__name__)

# Bound a pathological/hung parse. Legitimate PDF *text* extraction (not
# rendering) completes in well under a minute even for very large documents;
# 300s gives generous headroom while ensuring a single wedged/adversarial PDF
# cannot jam the max_workers=1 pipeline indefinitely. This is the subprocess
# exception to the "no speculative timeouts" rule (genesis-development timeout
# policy): the pool is the ONLY recovery mechanism for a hung native call, and
# a hang here blocks every subsequent ingest.
PDF_EXTRACT_TIMEOUT_S = 300.0

# Worker spawn should be near-instant; bound it so a broken environment fails
# pool creation loudly instead of hanging.
_START_TIMEOUT_S = 30.0

_pool: ProcessPoolExecutor | None = None
_lock = asyncio.Lock()


class PDFExtractionError(RuntimeError):
    """PDF extraction crashed, hung, or otherwise failed in the worker pool."""


def _make_pool() -> ProcessPoolExecutor:
    """Create a spawn-context single-worker pool and force the worker to start.

    Blocking (spawn) — call via ``asyncio.to_thread``. Forcing the worker to
    spawn now pre-pays the spawn latency and surfaces a broken environment at
    creation time rather than mid-ingest.
    """
    ctx = multiprocessing.get_context("spawn")
    pool = ProcessPoolExecutor(max_workers=1, mp_context=ctx)
    pool.submit(_warmup).result(timeout=_START_TIMEOUT_S)
    return pool


async def _get_pool() -> ProcessPoolExecutor:
    global _pool
    if _pool is not None:
        return _pool
    async with _lock:
        if _pool is None:
            _pool = await asyncio.to_thread(_make_pool)
        return _pool


async def extract_pdf_text(path: str) -> tuple[str, list[str], int]:
    """Extract ``(full_text, sections, page_count)`` from a PDF, off the loop.

    Raises ``PDFExtractionError`` if the worker crashes or the parse exceeds
    ``PDF_EXTRACT_TIMEOUT_S`` (the wedged worker is killed and the pool
    recreated on the next call). Callers treat this as a failed ingest; the
    server is unaffected.
    """
    pool = await _get_pool()
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(pool, extract_pdf_text_sync, path),
            timeout=PDF_EXTRACT_TIMEOUT_S,
        )
    except TimeoutError as exc:
        logger.error(
            "PDF extraction timed out after %.0fs — killing worker: %s",
            PDF_EXTRACT_TIMEOUT_S, path,
        )
        await _kill_and_recreate(pool)
        raise PDFExtractionError(
            f"PDF extraction timed out after {PDF_EXTRACT_TIMEOUT_S:.0f}s"
        ) from exc
    except BrokenProcessPool as exc:
        logger.error("PDF worker crashed while extracting %s", path)
        await _kill_and_recreate(pool)
        raise PDFExtractionError("PDF parser crashed") from exc


async def _kill_and_recreate(broken: ProcessPoolExecutor) -> None:
    """Hard-kill the pool's worker(s) and drop the pool (compare-and-swap).

    ``pool.shutdown()`` alone can block on a wedged native call, so SIGKILL the
    worker processes directly (mirroring guardian/_subprocess.py), then let
    ``_get_pool`` lazily recreate. Only the caller that observed *this* broken
    instance replaces it, so a concurrent failure can't discard a fresh pool.
    """
    global _pool
    async with _lock:
        if _pool is not broken:
            return  # already replaced by another caller
        _kill_workers(broken)
        try:
            broken.shutdown(wait=False, cancel_futures=True)
        except Exception:
            logger.debug("PDF pool shutdown(wait=False) failed", exc_info=True)
        _pool = None


def _kill_workers(pool: ProcessPoolExecutor) -> None:
    """SIGKILL the pool's worker process(es). Guards against signalling pid 0/1."""
    for proc in list(getattr(pool, "_processes", {}).values()):
        pid = getattr(proc, "pid", None)
        if pid and pid > 1:  # never signal pid 0/1 (would hit the process group)
            try:
                proc.kill()
            except Exception:
                logger.debug("Failed to kill PDF worker %s", pid, exc_info=True)


async def shutdown_pdf_pool(timeout: float = 10.0) -> None:
    """Tear down the PDF pool on runtime shutdown (bounded; kills if it stalls)."""
    global _pool
    async with _lock:
        pool, _pool = _pool, None
    if pool is None:
        return

    def _close() -> None:
        try:
            pool.shutdown(wait=True, cancel_futures=True)
        except Exception:
            logger.debug("PDF pool graceful shutdown failed", exc_info=True)

    try:
        await asyncio.wait_for(asyncio.to_thread(_close), timeout=timeout)
    except TimeoutError:
        logger.warning(
            "PDF pool shutdown stalled after %.0fs — killing workers", timeout
        )
        _kill_workers(pool)
