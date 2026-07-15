"""Guardian-side offline-bundle watch — archive + freshness (F.4).

The container publishes a verified ``git bundle`` of the main repo to the shared
mount (``guardian/repo_bundle.py``). This host-side module, run each guardian
tick, does two things the container cannot:

1. **Archive** the newest bundle + its stamp to a host-only directory
   (``<state_path>/repo-archive/``) the container cannot reach — so a destroyed
   container still leaves a ``git clone``-able snapshot on the host. Versioned,
   pruned to ``keep`` copies, NEVER below one.
2. **Freshness alert** — WARN (damped) when the newest archived bundle's
   ``last_verified_at`` is older than ``stale_days``. Keying on
   ``last_verified_at`` (rewritten every healthy publish check, even when HEAD is
   unchanged) rather than the bundle's mtime means a quiet-commit period never
   false-alarms, while a dead awareness loop OR a repo that has been unhealthy for
   ``stale_days`` (publish is health-gated) both surface — the latter complements
   ``git_watch``'s direct corruption alert.

Pure host-side file operations — no incus exec, no container dependency — so it
runs on every enabled tick, independent of the container's state (mirrors
``cred_watch._check_mirror_and_archive``). Never raises into the tick.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from genesis.guardian.alert.base import Alert, AlertSeverity

# Reuse the identical atomic-copy discipline as the container-side publish /
# credential mirror (same-dir tmp + os.replace, preserves mtime, forces 0600).
from genesis.guardian.credential_bridge import _atomic_copy, _needs_copy
from genesis.guardian.repo_bundle import is_valid_bundle_name

logger = logging.getLogger(__name__)

_STATE_FILE = "bundle_alert_state.json"
_ARCHIVE_SUBDIR = "repo-archive"
_SHARED_BUNDLE_SUBDIR = ("guardian", "repo-bundle")  # under <state_path>/shared/
_STAMP_NAME = "BUNDLE_STAMP"
_BUNDLE_GLOB = "genesis-*.bundle"


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _read_stamp(path: Path) -> dict | None:
    try:
        obj = json.loads(path.read_text())
        return obj if isinstance(obj, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


async def check_repo_bundle_and_alert(config, dispatcher) -> None:
    """Guardian tick entry: archive the newest bundle, then freshness-alert.

    Never raises into the tick. Both halves are best-effort and independent."""
    cfg = config.repo_bundle
    if not cfg.enabled:
        return
    source_dir = config.shared_path.joinpath(*_SHARED_BUNDLE_SUBDIR)
    archive_dir = config.state_path / _ARCHIVE_SUBDIR

    try:
        _archive_bundles(cfg, source_dir, archive_dir)
    except Exception:
        logger.warning("bundle archive hop failed", exc_info=True)

    try:
        await _freshness_alert(config, dispatcher, archive_dir)
    except Exception:
        logger.warning("bundle freshness check failed", exc_info=True)


def _archive_bundles(cfg, source_dir: Path, archive_dir: Path) -> None:
    """Copy the newest published bundle + stamp → a host-only archive the
    container cannot reach, then prune to ``keep`` copies (never below one).

    REFUSE a stamp-less or empty source (a zeroed/half-written shared mount must
    never even start an archive round — ``cred_watch._archive_mirror`` semantics).
    The stamp names the current bundle; only that file is copied (older heads were
    already pruned on the shared side)."""
    stamp_src = source_dir / _STAMP_NAME
    stamp = _read_stamp(stamp_src)
    if not stamp:
        logger.debug("bundle archive: no stamp at %s (incomplete/absent) — skipping", stamp_src)
        return
    bundle_name = stamp.get("bundle")
    # The stamp is read from the CONTAINER-writable shared mount. Validate the
    # bundle name is a plain basename (no ``..``/absolute) BEFORE building any path
    # from it — otherwise a malformed/compromised stamp could make the host
    # guardian read/write outside source_dir/archive_dir, defeating the host-only
    # archive boundary (Codex P1). Absolute/traversal names → skip.
    if not bundle_name or not is_valid_bundle_name(str(bundle_name)):
        logger.debug("bundle archive: invalid/absent stamp bundle name %r — skipping", bundle_name)
        return
    bundle_src = source_dir / str(bundle_name)
    if not bundle_src.is_file():
        logger.debug("bundle archive: stamped bundle %s missing — skipping", bundle_name)
        return

    archive_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(archive_dir, 0o700)

    bundle_dest = archive_dir / str(bundle_name)
    if _needs_copy(bundle_src, bundle_dest):
        _atomic_copy(bundle_src, bundle_dest)
    # Stamp is tiny and rewritten every healthy tick (last_verified_at) — always
    # refresh it so the freshness reader sees the latest verification time.
    _atomic_copy(stamp_src, archive_dir / _STAMP_NAME)

    _prune_archive(archive_dir, keep=max(1, int(cfg.keep)))


def _prune_archive(archive_dir: Path, keep: int) -> None:
    """Keep the ``keep`` newest ``genesis-*.bundle`` files by mtime; delete older.

    ``_atomic_copy`` preserves the source mtime, so mtime ordering is the
    chronological publish order. Never deletes below one bundle by construction
    (``keep`` >= 1 and the current bundle was just (re)copied)."""
    bundles = [f for f in archive_dir.glob(_BUNDLE_GLOB) if f.is_file()]
    if len(bundles) <= keep:
        return
    bundles.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    for stale in bundles[keep:]:
        try:
            stale.unlink()
        except OSError:
            logger.debug("bundle archive: could not prune %s", stale, exc_info=True)


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        obj = json.loads(path.read_text())
        return obj if isinstance(obj, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))
    except OSError:
        logger.warning("failed to persist bundle alert state", exc_info=True)


async def _freshness_alert(config, dispatcher, archive_dir: Path) -> None:
    """WARN (damped) when the newest archived bundle is stale.

    Feature-active gate: if no stamp has ever been archived, this is a
    never-configured install (or the first tick before the first publish) — no
    alert, just clear any lingering warn state (mirrors ``cred_watch``'s
    escrow-proxy gate)."""
    cfg = config.repo_bundle
    now = datetime.now(UTC)
    state_file = config.state_path / _STATE_FILE
    stamp = _read_stamp(archive_dir / _STAMP_NAME)

    if not stamp:
        if _load_state(state_file).get("warned_at"):
            _save_state(state_file, {})
        return

    verified = _parse(stamp.get("last_verified_at")) or _parse(stamp.get("created_at"))
    if verified is None:
        age_days = None
        stale = True
    else:
        age_days = (now - verified).total_seconds() / 86400
        stale = age_days > cfg.stale_days

    state = _load_state(state_file)
    if not stale:
        if state.get("warned_at"):
            _save_state(state_file, {})
            await _send(
                dispatcher,
                AlertSeverity.INFO,
                "Offline repo bundle fresh again",
                "The host-side offline git bundle is updating normally again.",
            )
        return

    last_alert = _parse(state.get("last_alert_at"))
    if last_alert and (now - last_alert).total_seconds() < cfg.realert_hours * 3600:
        return
    detail = (
        f"newest verified bundle is {age_days:.1f} days old"
        if age_days is not None
        else "the archived bundle stamp has no verification time"
    )
    _save_state(
        state_file,
        {
            "warned_at": state.get("warned_at") or now.isoformat(),
            "last_alert_at": now.isoformat(),
        },
    )
    await _send(
        dispatcher,
        AlertSeverity.WARNING,
        "Offline repo bundle stale",
        f"The host-side offline git bundle is stale: {detail} "
        f"(threshold {cfg.stale_days:.0f} days). Either the awareness loop "
        "isn't publishing or the container's git has been unhealthy that "
        "long — the offline re-clone lifeline may be out of date. Check the "
        "awareness loop and scripts/git_repair.py.",
    )


def bundle_archive_status(config) -> dict:
    """Read-only snapshot of the host-only bundle archive (backs the
    ``bundle-status`` gateway verb). Lists archived bundles + the newest stamp so
    the container can confirm the offline re-clone lifeline exists and how fresh
    it is. No mutation, no secrets."""
    archive_dir = config.state_path / _ARCHIVE_SUBDIR
    stamp = _read_stamp(archive_dir / _STAMP_NAME)
    bundles: list[dict] = []
    if archive_dir.is_dir():
        for f in sorted(archive_dir.glob(_BUNDLE_GLOB)):
            try:
                st = f.stat()
            except OSError:
                continue
            bundles.append({"name": f.name, "size": st.st_size, "mtime": st.st_mtime})
    return {
        "ok": True,
        "action": "bundle-status",
        "archive_dir": str(archive_dir),
        "count": len(bundles),
        "bundles": bundles,
        "stamp": stamp,
    }


async def _send(dispatcher, severity: AlertSeverity, title: str, body: str) -> None:
    try:
        await dispatcher.send(Alert(severity=severity, title=title, body=body))
    except Exception:
        logger.warning("failed to send bundle freshness alert", exc_info=True)
