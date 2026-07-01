"""Agentic model gauntlet — earn roster membership, don't assert it.

Validates a roster model (native Claude, or a non-Anthropic peer like GLM 5.2
reached via its native Anthropic-compatible endpoint) by having it drive a REAL
Claude Code session through a multi-turn coding fix-loop, then scoring the result
objectively: did the model fix a broken Python project so its pytest suite goes
green, WITHOUT mutating the protected (tests/config) surface?

Design (see plan Phase 6):
- Spawns through the REAL ``CCInvoker`` (validates the actual selection chokepoint
  + arg/env prod uses). A fresh ``CCInvoker()`` with no callbacks writes no
  cc_sessions rows and fires no live event-bus handlers — side-effect-free.
- Model selection mirrors the tested DirectSession idiom: pre-stamp
  ``roster.overrides_for(model)`` and set ``roster_eligible=bool(overrides)`` so
  ``apply_active`` never reroutes a native-Claude run to the global default. The
  ACTUAL route is verified via ``CCOutput.via_proxy`` (True=peer, False=native).
- Infra failures (any ``CCError``: rate-limit, quota, timeout, MCP, session,
  process) are SKIPPED — excluded from the pass/fail verdict, never a regression
  signal (a peer being out of balance is not a quality regression). Only a
  returned ``CCOutput`` with tests-still-red or a mutated protected surface is a
  genuine FAIL.
- Throwaway sessions are isolated: no MCP (``profile="none"``), a dedicated Bash
  sandbox tmpdir (so genesis-tmp-watchgod can't SIGKILL a live session), and a
  per-model file lock so a manual run and the scheduled job can't interleave.

Results are recorded via the shared ``eval/db.py`` (dataset ``"gauntlet"``,
task_category AGENTIC). Gating is advisory (see ``gauntlet_regression``): a
regression alerts + files a human-gated proposal but NEVER auto-removes a model
from selection/failover.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import logging
import re
import shutil
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from genesis.cc import roster
from genesis.cc.exceptions import CCError
from genesis.cc.invoker import CCInvoker
from genesis.cc.session_config import SessionConfigBuilder
from genesis.cc.types import CCInvocation, CCModel, EffortLevel
from genesis.eval.types import (
    EvalRunSummary,
    EvalTrigger,
    ScoredOutput,
    ScorerType,
    TaskCategory,
)

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)

_FIXTURES_DIR = Path(__file__).resolve().parent / "gauntlet_fixtures"
# Throwaway workdirs + the isolated CC Bash sandbox live under ~/tmp (NOT /tmp,
# NOT ~/.genesis/cc-tmp — see CLAUDE.md temp rules).
_GAUNTLET_ROOT = Path.home() / "tmp" / "gauntlet"

# The PROTECTED surface: files the model must not touch. Editing tests or the
# pytest config to fake a green run is a cheat → FAIL. Any add/modify/delete of
# these is caught by hashing them before and after the run.
_PROTECTED_DIR_TOP = "tests"
_PROTECTED_FILENAMES = {
    "conftest.py",
    "pytest.ini",
    "pyproject.toml",
    "setup.cfg",
    "tox.ini",
    "setup.py",
}
# Fixed tier/effort for reproducibility. For a routed peer the tier only feeds
# the --effort clamp (the peer's model id comes from ANTHROPIC_MODEL, not
# --model); for native Claude it selects the Sonnet tier. The load-bearing
# validation target is peers, where the tier is moot.
_GAUNTLET_MODEL = CCModel.SONNET
_GAUNTLET_EFFORT = EffortLevel.MEDIUM
# Bound the pytest scoring subprocess (not the model) — a fixture suite runs in
# well under a second; 300s is generous headroom for a pathological hang.
_PYTEST_TIMEOUT_S = 300


class GauntletBusyError(RuntimeError):
    """Another gauntlet run for this model is already in progress."""


@dataclass(frozen=True)
class GauntletFixture:
    name: str
    path: Path
    task: str
    timeout_s: int
    difficulty: str


def load_gauntlet_fixtures() -> list[GauntletFixture]:
    """Load committed gauntlet fixtures (each dir with a meta.json)."""
    out: list[GauntletFixture] = []
    for meta_path in sorted(_FIXTURES_DIR.glob("*/meta.json")):
        try:
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            logger.warning("skipping unreadable gauntlet fixture %s", meta_path, exc_info=True)
            continue
        out.append(
            GauntletFixture(
                name=str(meta.get("name") or meta_path.parent.name),
                path=meta_path.parent,
                task=str(meta["task"]),
                timeout_s=int(meta.get("timeout_s", 1200)),
                difficulty=str(meta.get("difficulty", "")),
            )
        )
    return out


def _is_generated(rel: Path) -> bool:
    """True for build/run artifacts that are not part of the protected surface.

    Running pytest (which the model does to see the failures) creates
    ``tests/__pycache__/*.pyc`` and ``.pytest_cache/`` — generated bytecode/
    cache, NOT authored test/config source. Excluding them prevents a false
    "protected files modified" cheat verdict on an honest run.
    """
    return (
        "__pycache__" in rel.parts
        or ".pytest_cache" in rel.parts
        or rel.suffix == ".pyc"
    )


def _is_protected(rel: Path) -> bool:
    if _is_generated(rel):
        return False
    return rel.parts[0] == _PROTECTED_DIR_TOP or rel.name in _PROTECTED_FILENAMES


def _protected_hash(root: Path) -> str:
    """Hash the protected surface (tests/ + pytest config, any depth).

    Walks the whole tree so an ADDED protected file (e.g. a new conftest.py that
    monkeypatches the answer, or a pyproject that suppresses collection) changes
    the hash just as a modification or deletion would. Generated artifacts
    (__pycache__, .pytest_cache, *.pyc) are excluded — see ``_is_generated``.
    """
    h = hashlib.md5()  # noqa: S324 — integrity check, not security
    rels = sorted(
        p.relative_to(root)
        for p in root.rglob("*")
        if p.is_file() and _is_protected(p.relative_to(root))
    )
    for rel in rels:
        h.update(str(rel).encode())
        h.update(b"\0")
        h.update((root / rel).read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def _acquire_lock(model_name: str):
    """Non-blocking per-model advisory lock (CLI vs scheduled mutual exclusion)."""
    lock_path = Path.home() / "tmp" / f".gauntlet-{_safe(model_name)}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")  # noqa: SIM115 — held for the run, closed in _release_lock
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as e:
        fh.close()
        raise GauntletBusyError(
            f"another gauntlet run for {model_name!r} is already in progress"
        ) from e
    return fh


def _release_lock(fh) -> None:
    with contextlib.suppress(Exception):
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


async def _run_pytest(workdir: Path) -> tuple[int, str]:
    """Run the fixture's pytest suite in ``workdir``. Returns (exit_code, output)."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
        cwd=str(workdir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=_PYTEST_TIMEOUT_S)
    except TimeoutError:  # asyncio.TimeoutError is an alias of TimeoutError on 3.11+
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return -1, "pytest scoring timed out"
    return proc.returncode, out.decode(errors="replace")


def _skip(fx: GauntletFixture, reason: str) -> ScoredOutput:
    return ScoredOutput(
        case_id=fx.name,
        passed=False,
        score=0.0,
        actual_output=f"skipped: {reason}",
        scorer_type=ScorerType.AGENTIC_PYTEST,
        scorer_detail=f"SKIPPED ({reason})"[:2000],
        skipped=True,
    )


async def _run_one_fixture(
    invoker: CCInvoker,
    overrides: dict,
    mcp_config: str | None,
    fx: GauntletFixture,
    tmp_root: Path,
) -> ScoredOutput:
    workroot = Path(tempfile.mkdtemp(prefix=f"{_safe(fx.name)}-", dir=str(tmp_root)))
    workdir = workroot / fx.name
    # meta.json is gauntlet metadata, not part of the project the model sees.
    shutil.copytree(
        fx.path,
        workdir,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "meta.json"),
    )
    try:
        before = _protected_hash(workdir)
        inv = CCInvocation(
            prompt=fx.task,
            model=_GAUNTLET_MODEL,
            effort=_GAUNTLET_EFFORT,
            working_dir=str(workdir),
            timeout_s=fx.timeout_s,
            skip_permissions=True,
            mcp_config=mcp_config,
            claude_code_tmpdir=str(tmp_root / "cc-sandbox"),
            roster_eligible=bool(overrides),  # native (empty) → no chokepoint reroute
            **overrides,
        )
        try:
            output = await invoker.run(inv)
        except CCError as e:
            # Infra failure (rate-limit / quota / timeout / mcp / session /
            # process). NOT a model-quality signal → SKIP, never a regression.
            return _skip(fx, f"infra: {type(e).__name__}: {e}")

        # Verify the run actually went where we intended (peer vs native).
        expect_proxy = bool(overrides)
        if output.via_proxy != expect_proxy:
            return _skip(
                fx,
                f"routing mismatch: via_proxy={output.via_proxy} expected {expect_proxy}",
            )

        after = _protected_hash(workdir)
        if after != before:
            # Gamed the protected surface (edited/added/deleted tests or config).
            return ScoredOutput(
                case_id=fx.name,
                passed=False,
                score=0.0,
                actual_output="cheat: protected surface mutated",
                scorer_type=ScorerType.AGENTIC_PYTEST,
                scorer_detail="FAIL: protected files (tests/ or pytest config) were modified",
                latency_ms=float(output.duration_ms),
                cost_usd=output.cost_usd,
                input_tokens=output.input_tokens,
                output_tokens=output.output_tokens,
            )

        pyrc, pytest_out = await _run_pytest(workdir)
        if "No module named pytest" in pytest_out:
            return _skip(fx, "pytest unavailable in gauntlet environment")
        passed = pyrc == 0
        tail = ""
        lines = [ln for ln in pytest_out.strip().splitlines() if ln.strip()]
        if lines:
            tail = lines[-1][:200]
        detail = (
            f"{'PASS' if passed else 'FAIL'} pytest_exit={pyrc} "
            f"via_proxy={output.via_proxy} cost={output.cost_usd:.4f} "
            f"dur={output.duration_ms}ms | {tail}"
        )
        return ScoredOutput(
            case_id=fx.name,
            passed=passed,
            score=1.0 if passed else 0.0,
            actual_output=f"pytest:exit={pyrc}",
            scorer_type=ScorerType.AGENTIC_PYTEST,
            scorer_detail=detail[:2000],
            latency_ms=float(output.duration_ms),
            cost_usd=output.cost_usd,
            input_tokens=output.input_tokens,
            output_tokens=output.output_tokens,
        )
    finally:
        shutil.rmtree(workroot, ignore_errors=True)


async def run_gauntlet(
    model_name: str,
    *,
    db: aiosqlite.Connection | None = None,
    fixtures: list[GauntletFixture] | None = None,
    trigger: EvalTrigger = EvalTrigger.MANUAL,
) -> EvalRunSummary:
    """Run the agentic gauntlet for ``model_name`` and record the results.

    Raises :class:`genesis.cc.roster.RosterError` if ``model_name`` is unknown,
    misconfigured, or its auth token is absent (fail loud — never silently score
    a model you can't reach). Raises :class:`GauntletBusyError` if another run
    for the same model is already in progress. Callers (CLI / scheduled) decide
    how to surface these.
    """
    fixtures = fixtures if fixtures is not None else load_gauntlet_fixtures()
    if not fixtures:
        raise ValueError("no gauntlet fixtures found")

    # Resolve endpoint once (fail loud on unknown/keyless). {} = native Claude.
    overrides = roster.overrides_for(model_name)
    mcp_config = SessionConfigBuilder().build_mcp_config(profile="none")

    run_id = uuid.uuid4().hex
    tmp_root = _GAUNTLET_ROOT / run_id
    tmp_root.mkdir(parents=True, exist_ok=True)
    invoker = CCInvoker()  # fresh, no callbacks → side-effect-free

    lock = _acquire_lock(model_name)
    results: list[ScoredOutput] = []
    start = time.monotonic()
    try:
        for fx in fixtures:
            logger.info("gauntlet[%s]: running fixture %s", model_name, fx.name)
            results.append(await _run_one_fixture(invoker, overrides, mcp_config, fx, tmp_root))
    finally:
        _release_lock(lock)
        shutil.rmtree(tmp_root, ignore_errors=True)

    duration_s = time.monotonic() - start
    passed = sum(1 for r in results if r.passed and not r.skipped)
    skipped = sum(1 for r in results if r.skipped)
    failed = sum(1 for r in results if not r.passed and not r.skipped)
    attempted = passed + failed
    aggregate = passed / attempted if attempted else 0.0

    summary = EvalRunSummary(
        run_id=run_id,
        model_id=model_name,
        model_profile=model_name,
        dataset="gauntlet",
        trigger=trigger,
        task_category=TaskCategory.AGENTIC,
        total_cases=len(fixtures),
        passed_cases=passed,
        failed_cases=failed,
        skipped_cases=skipped,
        aggregate_score=aggregate,
        duration_s=duration_s,
        results=results,
        metadata={"via_proxy": bool(overrides)},
    )
    logger.info(
        "gauntlet[%s] complete: %d/%d passed (%.0f%%), %d skipped, %.1fs",
        model_name, passed, attempted, aggregate * 100, skipped, duration_s,
    )
    if db is not None:
        from genesis.eval.db import insert_run

        await insert_run(db, summary)
    return summary
