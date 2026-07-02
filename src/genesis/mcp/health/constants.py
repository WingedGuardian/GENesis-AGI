"""Leaf constants for the health MCP package.

Kept dependency-free (imports nothing from ``genesis.mcp.health.*``) so both
``errors.py`` and ``manifest.py`` can share values without an import cycle —
the package ``__init__`` imports both of those modules. Same pattern as
``genesis/cc/constants.py`` (introduced to break the cc↔awareness cycle).
"""

from __future__ import annotations

# A scheduled job whose ``last_run`` is more than this many days ahead of its
# ``last_success`` has been running-but-not-succeeding long enough to be
# "silently stale" — surfaced as a ``job_stale:`` health alert and as the
# ``stale`` flag on the job_health MCP output. Keyed on the last_run−last_success
# gap (which ``clear_stale_job_failures`` never touches), so it survives the
# per-restart failure-counter reset that otherwise hides these failures.
# >~1 weekly cadence: catches a job that has missed a weekly run, ignores a
# single transient daily failure.
JOB_STALE_GAP_DAYS = 6.0
