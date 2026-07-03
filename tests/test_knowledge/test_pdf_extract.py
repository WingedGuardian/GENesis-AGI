"""Tests for the crash/hang-isolated PDF extraction process pool (pdf_extract).

These tests spin up a REAL spawn-context ProcessPoolExecutor (not mocked) so
they exercise the actual crash-recovery, shutdown, and IPC-temp behaviour.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

import pytest


def _make_pdf(tmp_path: Path, text: str = "Hello from PDF", name: str = "doc.pdf") -> Path:
    import pymupdf

    p = tmp_path / name
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(str(p))
    doc.close()
    return p


def _sleep_worker(path: str) -> tuple[str, list[str], int]:
    """Top-level (picklable) worker that hangs — used to test hang isolation.

    Importable by the spawn pool worker via its qualified name.
    """
    import time as _t

    _t.sleep(30)
    return ("", [], 0)


@pytest.fixture(autouse=True)
async def _reset_pool():
    """Start each test with a torn-down pool and clean up after."""
    import genesis.knowledge.pdf_extract as pe

    await pe.shutdown_pdf_pool()
    yield
    await pe.shutdown_pdf_pool()


async def test_extract_returns_text(tmp_path):
    import genesis.knowledge.pdf_extract as pe

    pdf = _make_pdf(tmp_path)
    full_text, sections, page_count = await pe.extract_pdf_text(str(pdf))

    assert "Hello from PDF" in full_text
    assert page_count == 1  # count of NON-blank pages (parity with old inline impl)
    assert sections == [full_text]  # single non-blank page → its text


async def test_concurrent_extractions_all_succeed(tmp_path):
    """Concurrent callers share the single-worker pool (serialized) and all succeed."""
    import asyncio

    import genesis.knowledge.pdf_extract as pe

    pdfs = [_make_pdf(tmp_path, f"Doc number {i}", name=f"d{i}.pdf") for i in range(4)]

    results = await asyncio.gather(*(pe.extract_pdf_text(str(p)) for p in pdfs))
    assert len(results) == 4
    for i, (text, _sections, page_count) in enumerate(results):
        assert f"Doc number {i}" in text
        assert page_count == 1


async def test_crash_recovery(tmp_path):
    """A killed worker → PDFExtractionError on the in-flight call, then self-heal."""
    import genesis.knowledge.pdf_extract as pe

    pdf = _make_pdf(tmp_path)
    text, _, _ = await pe.extract_pdf_text(str(pdf))  # prime the pool
    assert "Hello" in text

    # Kill the worker process out from under the pool.
    procs = list(pe._pool._processes.values())
    assert procs, "expected a live worker process"
    for p in procs:
        assert p.pid and p.pid > 1  # never signal pid 0/1 (would hit the process group)
        os.kill(p.pid, signal.SIGKILL)
    # Let the OS reap it so the next submit sees a broken pool.
    for _ in range(50):
        if any(not p.is_alive() for p in procs):
            break
        time.sleep(0.05)

    with pytest.raises(pe.PDFExtractionError):
        await pe.extract_pdf_text(str(pdf))

    # Pool must recreate itself on the next call.
    text2, _, _ = await pe.extract_pdf_text(str(pdf))
    assert "Hello" in text2


async def test_hang_is_killed_and_pool_dropped(tmp_path, monkeypatch):
    """A worker that hangs past the timeout is killed; the pool is dropped."""
    import genesis.knowledge.pdf_extract as pe

    monkeypatch.setattr(pe, "extract_pdf_text_sync", _sleep_worker)
    monkeypatch.setattr(pe, "PDF_EXTRACT_TIMEOUT_S", 1.0)

    pdf = _make_pdf(tmp_path)
    t0 = time.monotonic()
    with pytest.raises(pe.PDFExtractionError):
        await pe.extract_pdf_text(str(pdf))
    elapsed = time.monotonic() - t0

    assert elapsed < 15, "should time out at ~1s, not wait the full 30s hang"
    assert pe._pool is None, "wedged pool must be dropped for recreation"


async def test_shutdown_then_reextract(tmp_path):
    """shutdown_pdf_pool tears the pool down; a later call lazily recreates it."""
    import genesis.knowledge.pdf_extract as pe

    pdf = _make_pdf(tmp_path)
    await pe.extract_pdf_text(str(pdf))
    assert pe._pool is not None

    await pe.shutdown_pdf_pool()
    assert pe._pool is None

    text, _, _ = await pe.extract_pdf_text(str(pdf))
    assert "Hello" in text
