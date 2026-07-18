"""d0004 — purge job_health rows for jobs whose registration was removed.

A retired scheduled job leaves its ``job_health`` row behind forever:
nothing deletes ``job_health`` rows on retirement, and the staleness
annotator (``mcp/health/manifest.py::_annotate_staleness``) measures the
``last_run − last_success`` gap — which stays 0 for a job that STOPPED
FIRING entirely (its ``last_run`` freezes). So the fossil row renders as a
perpetually-healthy job in ``job_health`` / the dashboard.

``schedule_infra_monitor`` is the observed case: its registration and method
were removed in #147 (superseded by ``infra_profile_refresh`` +
``awareness/loop.py::_check_infra_protection_posture``), yet its row (last
run 2026-04) still reports ``stale: false``.

ONLY jobs whose CODE is gone are purged here (verified by grep: zero
registration refs). A dormant-but-registered job (``build_lane_poll``) or a
disabled-module pipeline row (``pipeline:<profile>``, whose module still
exists and re-registers when enabled) is NOT a fossil and keeps its row.
The general "registered job silently stopped firing" detector — which needs
the live APScheduler registry, unavailable to a post-boot migration — is
tracked separately, not built here.

migrate()/verify() are SYNC (framework contract, cf. d0001/d0002); own
connections only — never the runtime's async ``rt._db``. Idempotent: the
DELETE is a no-op once purged and on a fresh install (no such row).
"""

from __future__ import annotations

import sqlite3

from genesis.env import genesis_db_path

requires_operator = False

# Job names whose registration has been REMOVED from the codebase. Extend
# ONLY with jobs whose code is gone (grep-verified: no add_job/registration
# ref) — never a merely-dormant but still-registered job.
_RETIRED_JOBS = (
    "schedule_infra_monitor",  # removed in #147 → infra_profile_refresh + posture check
)


def _placeholders() -> str:
    return ",".join("?" * len(_RETIRED_JOBS))


def migrate() -> dict:
    """Delete job_health rows for every job in _RETIRED_JOBS; return purge count."""
    db = sqlite3.connect(genesis_db_path(), timeout=30.0)
    try:
        cur = db.execute(
            # noqa string interpolates only `?` placeholders; values stay
            # parameterized and _RETIRED_JOBS is a module constant of literals.
            f"DELETE FROM job_health WHERE job_name IN ({_placeholders()})",  # noqa: S608
            _RETIRED_JOBS,
        )
        db.commit()
        return {"purged": cur.rowcount}
    finally:
        db.close()


def verify() -> bool:
    """Complete when no retired job name remains in job_health."""
    db = sqlite3.connect(f"file:{genesis_db_path()}?mode=ro", uri=True)
    try:
        row = db.execute(
            f"SELECT COUNT(*) FROM job_health WHERE job_name IN ({_placeholders()})",  # noqa: S608
            _RETIRED_JOBS,
        ).fetchone()
        return row[0] == 0
    finally:
        db.close()
