"""Deploy-staleness snapshot — is what's MERGED actually DEPLOYED here?

Genesis installs pull merged PRs two ways: a full ``scripts/update.sh`` run
(bootstrap + migrations + systemd sync + guardian redeploy + pin healing) or a
bare ``git merge`` between updates. The bare merge deploys tier-1 (code loads
on restart, schema migrations self-apply at boot) but silently skips tier-2 —
systemd unit installation, guardian host redeploy, CC/Node pins. Observed
live 2026-07-13: six days of manual merges left a shipped timer uninstalled
and the host guardian 67 files behind, with zero signal anywhere.

This snapshot makes that drift visible:

- ``last_update``  — most recent successful ``update_history`` row + age
- ``git``          — commits behind upstream, fetch age (local refs only —
  NEVER fetches; a health probe must not do network I/O)
- ``units``        — systemd unit files missing vs ``scripts/systemd/*.template``
- ``tier2_pending`` — update.sh-only paths changed since the last successful
  update (the predictive "you need to run update.sh" signal)
- ``host_gateway`` — guardian host deployed_commit drift vs HEAD, read from
  the state file the nightly cc-align timer / update.sh write (no SSH here)

The awareness tick's ``_check_deploy_staleness`` consumes the same collectors
to raise a dashboard/morning-report observation. Everything is best-effort:
collectors degrade to ``None``/empty and never raise into the caller.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

# Same-package probe helper: never raises, rc -1 = timeout, -2 = exec failure.
from genesis.observability.git_health import _CHEAP_TIMEOUT_S, _run_git

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)

# Paths whose changes only ACTIVATE via scripts/update.sh (tier-2). A bare
# git-merge that touches these leaves the install running stale plumbing until
# update.sh runs. File-level granularity is deliberate — a docs-only edit to
# bootstrap.sh costs one advisory observation, not a missed deploy.
TIER2_PATHS = (
    "scripts/systemd",
    "scripts/bootstrap.sh",
    "scripts/update.sh",
    "scripts/lib/cc_version.sh",
    "scripts/hooks",
    "pyproject.toml",
)

# Guardian-relevant paths — keep in LOCKSTEP with update.sh GUARDIAN_PATHS
# (the redeploy trigger). If update.sh's list changes, change this one.
GUARDIAN_HOST_PATHS = (
    "src/genesis/guardian",
    "src/genesis/util",
    "src/genesis/env.py",
    "src/genesis/observability",
    "src/genesis/db",
    "config/guardian-claude.md",
    "config/genesis-guardian.service",
    "config/genesis-guardian.timer",
    "config/genesis-guardian-watchman.service",
    "config/genesis-guardian-watchman.timer",
    "pyproject.toml",
    "scripts/install_guardian.sh",
    "scripts/guardian-gateway.sh",
)

_HOST_STATE_FILE = "host_gateway_state.json"


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ── Collectors (sync, injectable paths, never raise) ────────────────


def collect_git_facts(repo: Path, now: datetime | None = None) -> dict:
    """Local-refs-only git staleness facts. No network, ever."""
    now = now or _utcnow()
    facts: dict = {
        "head": None,
        "commits_behind_upstream": None,
        "fetch_age_hours": None,
    }
    try:
        rc, out, _ = _run_git(repo, "rev-parse", "--short", "HEAD", timeout=_CHEAP_TIMEOUT_S)
        if rc == 0:
            facts["head"] = out.strip()
        # Behind-count against the current branch's upstream (origin/main on a
        # standard install). Counts against the LAST FETCHED state — pair with
        # fetch_age_hours to judge how trustworthy the number is.
        rc, out, _ = _run_git(
            repo, "rev-list", "--count", "HEAD..@{upstream}", timeout=_CHEAP_TIMEOUT_S
        )
        if rc == 0:
            facts["commits_behind_upstream"] = int(out.strip())
        rc, gcd, _ = _run_git(repo, "rev-parse", "--git-common-dir", timeout=_CHEAP_TIMEOUT_S)
        if rc == 0 and gcd.strip():
            git_dir = Path(gcd.strip())
            if not git_dir.is_absolute():
                git_dir = repo / git_dir
            fetch_head = git_dir / "FETCH_HEAD"
            if fetch_head.exists():
                age_s = now.timestamp() - fetch_head.stat().st_mtime
                facts["fetch_age_hours"] = round(age_s / 3600, 1)
    except Exception:
        logger.debug("deploy_health: git facts collection failed", exc_info=True)
    return facts


def collect_missing_units(template_dir: Path, unit_dir: Path) -> list[str] | None:
    """Systemd unit files expected from templates but absent from the user
    unit dir. Bootstrap syncs EVERY ``*.template`` (opt-in applies to timer
    ENABLEMENT, not file presence), so any missing file means bootstrap has
    not run since that template landed. ``None`` = could not determine."""
    try:
        if not template_dir.is_dir():
            return None
        expected = sorted(t.name.removesuffix(".template") for t in template_dir.glob("*.template"))
        if not expected:
            return None
        return [name for name in expected if not (unit_dir / name).exists()]
    except Exception:
        logger.debug("deploy_health: unit comparison failed", exc_info=True)
        return None


def collect_tier2_pending(repo: Path, since_commit: str | None) -> list[str] | None:
    """Tier-2 files changed since the last successful update.sh commit.

    Non-empty means "a bare merge brought update.sh-only changes" — the
    predictive signal. ``None`` = no baseline (no successful update recorded,
    or its commit no longer resolves after a rebase/gc)."""
    if not since_commit:
        return None
    try:
        rc, _, _ = _run_git(
            repo, "cat-file", "-e", f"{since_commit}^{{commit}}", timeout=_CHEAP_TIMEOUT_S
        )
        if rc != 0:
            return None
        rc, out, _ = _run_git(
            repo,
            "diff",
            "--name-only",
            f"{since_commit}..HEAD",
            "--",
            *TIER2_PATHS,
            timeout=_CHEAP_TIMEOUT_S,
        )
        if rc != 0:
            return None
        return [line for line in out.splitlines() if line.strip()]
    except Exception:
        logger.debug("deploy_health: tier2 diff failed", exc_info=True)
        return None


def collect_host_gateway(repo: Path, state_path: Path, now: datetime | None = None) -> dict:
    """Guardian host deploy drift, from the state file cc_align_host_sync
    writes on every gateway ``version`` probe (update.sh + nightly timer).

    ``status`` values: ``no_data`` (guardian-less install or probe never ran),
    ``ok`` (host at HEAD or no guardian-path delta), ``drift`` (guardian paths
    changed since the host's deployed commit), ``unknown_commit`` (host commit
    doesn't resolve locally — converge via update.sh)."""
    now = now or _utcnow()
    try:
        if not state_path.exists():
            return {"status": "no_data"}
        data = json.loads(state_path.read_text())
        version = data.get("version") or {}
        deployed = (version.get("deployed_commit") or "").strip()
        checked_at = data.get("checked_at")
        age_hours = None
        if checked_at:
            try:
                checked_dt = datetime.fromisoformat(checked_at)
                if checked_dt.tzinfo is None:
                    checked_dt = checked_dt.replace(tzinfo=UTC)
                age_hours = round((now - checked_dt).total_seconds() / 3600, 1)
            except ValueError:
                pass
        out: dict = {
            "deployed_commit": deployed or None,
            "checked_at": checked_at,
            "age_hours": age_hours,
        }
        if not deployed or deployed == "unknown":
            out["status"] = "unknown_commit"
            return out
        rc, _, _ = _run_git(
            repo, "cat-file", "-e", f"{deployed}^{{commit}}", timeout=_CHEAP_TIMEOUT_S
        )
        if rc != 0:
            out["status"] = "unknown_commit"
            return out
        rc, diff_out, _ = _run_git(
            repo,
            "diff",
            "--name-only",
            f"{deployed}..HEAD",
            "--",
            *GUARDIAN_HOST_PATHS,
            timeout=_CHEAP_TIMEOUT_S,
        )
        if rc != 0:
            out["status"] = "unknown_commit"
            return out
        drift_files = [line for line in diff_out.splitlines() if line.strip()]
        out["drift_files"] = len(drift_files)
        out["status"] = "drift" if drift_files else "ok"
        return out
    except Exception:
        logger.debug("deploy_health: host gateway check failed", exc_info=True)
        return {"status": "no_data"}


async def last_success_update(db: aiosqlite.Connection | None) -> dict:
    """Most recent successful update_history row (age computed by caller UIs).

    ``None`` fields = table missing/empty (pre-first-update install)."""
    if db is None:
        return {"completed_at": None, "new_commit": None, "age_days": None}
    try:
        cursor = await db.execute(
            "SELECT completed_at, new_commit FROM update_history "
            "WHERE status='success' ORDER BY completed_at DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        if not row or not row[0]:
            return {"completed_at": None, "new_commit": None, "age_days": None}
        completed_at, new_commit = row[0], row[1]
        age_days = None
        try:
            completed_dt = datetime.fromisoformat(completed_at)
            if completed_dt.tzinfo is None:
                completed_dt = completed_dt.replace(tzinfo=UTC)
            age_days = round((_utcnow() - completed_dt).total_seconds() / 86400, 1)
        except ValueError:
            pass
        return {
            "completed_at": completed_at,
            "new_commit": new_commit,
            "age_days": age_days,
        }
    except Exception:
        logger.debug("deploy_health: update_history query failed", exc_info=True)
        return {"completed_at": None, "new_commit": None, "age_days": None}


# Sustained-staleness thresholds (the awareness check's paging axis). A
# finding-CLASS boundary, so they live here beside derive_findings — the
# single producer of finding keys — not in the awareness layer: an alert
# formula written against facts the findings gate can filter out first is
# exactly the bug this placement prevents.
STALE_UPDATE_DAYS = 7.0
STALE_UPDATE_COMMITS = 20


def derive_findings(
    *,
    missing_units: list[str] | None,
    tier2_pending: list[str] | None,
    host_gateway: dict,
    commits_behind: int | None,
    update_age_days: float | None = None,
    behind_threshold: int = 50,
) -> list[str]:
    """Stable, order-deterministic finding keys — the alert/dedup contract.

    Keys (not prose) so the awareness check can hash them for observation
    dedup and tests can assert exactly. Two behind-related classes with
    DIFFERENT thresholds: ``stale_update`` (≥STALE_UPDATE_DAYS old AND
    ≥STALE_UPDATE_COMMITS behind — the sustained condition the awareness
    check pages on) and ``behind_upstream`` (> behind_threshold regardless
    of update age — a plain volume signal)."""
    findings: list[str] = []
    if missing_units:
        findings.append("missing_units:" + ",".join(sorted(missing_units)))
    if tier2_pending:
        findings.append(f"tier2_pending:{len(tier2_pending)}")
    if host_gateway.get("status") == "drift":
        findings.append("host_guardian_drift")
    elif host_gateway.get("status") == "unknown_commit":
        findings.append("host_guardian_unknown_commit")
    if (
        update_age_days is not None
        and commits_behind is not None
        and update_age_days >= STALE_UPDATE_DAYS
        and commits_behind >= STALE_UPDATE_COMMITS
    ):
        findings.append(f"stale_update:{round(update_age_days, 1)}d,{commits_behind}behind")
    if commits_behind is not None and commits_behind > behind_threshold:
        findings.append(f"behind_upstream:{commits_behind}")
    return findings


# ── Snapshot entry point (HealthDataService) ────────────────────────


def _collect_sync(repo: Path, genesis_home_dir: Path) -> dict:
    """All filesystem/git collectors in one worker-thread hop."""
    git_facts = collect_git_facts(repo)
    missing_units = collect_missing_units(
        repo / "scripts" / "systemd",
        Path.home() / ".config" / "systemd" / "user",
    )
    host_gateway = collect_host_gateway(repo, genesis_home_dir / _HOST_STATE_FILE)
    return {
        "git": git_facts,
        "missing_units": missing_units,
        "host_gateway": host_gateway,
    }


async def deploy_health(db: aiosqlite.Connection | None) -> dict:
    """Snapshot section for HealthDataService — see module docstring."""
    try:
        from genesis.env import genesis_home, repo_root

        repo = repo_root()
        collected = await asyncio.to_thread(_collect_sync, repo, genesis_home())
        update = await last_success_update(db)
        tier2 = await asyncio.to_thread(collect_tier2_pending, repo, update.get("new_commit"))
        findings = derive_findings(
            missing_units=collected["missing_units"],
            tier2_pending=tier2,
            host_gateway=collected["host_gateway"],
            commits_behind=collected["git"].get("commits_behind_upstream"),
            update_age_days=update.get("age_days"),
        )
        return {
            "status": "attention" if findings else "healthy",
            "findings": findings,
            "last_update": update,
            "git": collected["git"],
            "missing_units": collected["missing_units"],
            "tier2_pending": tier2,
            "host_gateway": collected["host_gateway"],
        }
    except Exception:
        logger.error("deploy_health snapshot failed", exc_info=True)
        return {"status": "error"}
