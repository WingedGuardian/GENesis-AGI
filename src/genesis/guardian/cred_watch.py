"""Guardian-side credential-integrity watch — the escalation-ladder backstop.

The container self-heals first (its awareness tick). The guardian validates the
same files independently via read-only ``incus exec``, WARNs on first sight
(deferring to the container), and STEPS IN — running the restore inside the
container — only if the corruption survives ``grace_minutes``. This covers the
one window the container can't: a degraded/dead genesis-server whose awareness
loop isn't running.

Both the check and the restore run the SAME validator bytes as the container:
the guardian pipes ``cred_integrity.py``'s source into the container's *system*
python3 (survives a broken ``.venv``); only a JSON verdict crosses back, and the
passphrase is resolved container-side, so the guardian never handles a secret.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from genesis.guardian import cred_integrity
from genesis.guardian.alert.base import Alert, AlertSeverity
from genesis.guardian.cred_integrity import RESTORABLE_STATUSES, allowed_restore

# Shared atomic-copy primitive lives in the credential_bridge module — imported
# here so the host-only archive uses the identical copy discipline as the
# container-side mirror. Import is side-effect-free.
from genesis.guardian.credential_bridge import _atomic_copy

logger = logging.getLogger(__name__)

_STATE_FILE = "cred_alert_state.json"

# G.4 host-side credential mirror. The container's awareness tick mirrors the
# encrypted backup bundle to the shared mount at <state>/shared/guardian/
# creds-mirror/; the guardian warns if it goes stale and keeps a host-only
# archive copy (<state>/creds-archive/) the container cannot reach.
_MIRROR_STATE_FILE = "mirror_alert_state.json"
_SHARED_GUARDIAN = ("shared", "guardian")   # under state_path → host view of the mount
_MIRROR_SUBDIR = "creds-mirror"
_MIRROR_STAMP = "MIRROR_STAMP"
_ARCHIVE_SUBDIR = "creds-archive"
_ESCROW_FILE = "backup_passphrase.env"


@dataclass(frozen=True)
class EpisodeDecision:
    action: str   # none | warn | step_in | realert | resolved
    reason: str


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def decide(name: str, is_corrupt: bool, episode: dict | None, now: datetime, cfg) -> EpisodeDecision:
    """Pure escalation decision for one target (fully unit-tested)."""
    if not is_corrupt:
        if episode and episode.get("warned_at"):
            return EpisodeDecision("resolved", "corruption cleared")
        return EpisodeDecision("none", "healthy")

    if episode is None:
        return EpisodeDecision("warn", "first detection — defer to container self-heal")

    first_seen = _parse(episode.get("first_seen")) or now
    elapsed = (now - first_seen).total_seconds()
    grace = cfg.grace_minutes * 60
    stepped = bool(episode.get("stepped_in_at"))

    if not stepped:
        if elapsed < grace:
            return EpisodeDecision("none", "in grace, deferring to container self-heal")
        return EpisodeDecision("step_in", "grace expired — container did not self-heal")

    # Already stepped in but still corrupt → re-alert on cadence only.
    last_alert = _parse(episode.get("last_alert_at"))
    if last_alert and (now - last_alert).total_seconds() < cfg.realert_hours * 3600:
        return EpisodeDecision("none", "already stepped in, within re-alert window")
    return EpisodeDecision("realert", "still corrupt after guardian step-in")


async def _incus_exec_stdin(
    container: str, cmd_str: str, stdin_data: bytes, timeout: float,
) -> tuple[int, str]:
    """Run ``su - ubuntu -c <cmd_str>`` in the container, piping stdin_data.

    Mirrors the heartbeat writer (check.py) — collector._incus_exec has no stdin
    support, so this is a local variant. Returns (rc, stdout)."""
    proc = await asyncio.create_subprocess_exec(
        "incus", "exec", container, "--",
        "su", "-", "ubuntu", "-c", cmd_str,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(input=stdin_data), timeout=timeout,
        )
    except TimeoutError:
        # wait_for cancels communicate() but leaves the incus exec child running.
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise
    if proc.returncode != 0:
        logger.debug(
            "cred_watch incus exec rc=%s: %s",
            proc.returncode, err.decode("utf-8", "replace")[:200],
        )
    return proc.returncode or 0, out.decode("utf-8", "replace")


def _extract_json(stdout: str) -> dict | None:
    """Parse the JSON verdict, tolerant of any login-shell preamble on stdout."""
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    return None


def _build_cmd(subcmd_args: str, cfg) -> str:
    cmd = f"python3 - {subcmd_args}"
    if cfg.container_home:
        cmd += f" --home {shlex.quote(cfg.container_home)}"
    if cfg.backup_dir:
        cmd += f" --backup-dir {shlex.quote(cfg.backup_dir)}"
    return cmd


async def run_container_check(config) -> dict | None:
    """Pipe the validator into the container and return its JSON verdict, or None
    on any exec/parse failure (treated as 'no signal' — never a false alert)."""
    cfg = config.cred_integrity
    src = Path(cred_integrity.__file__).read_bytes()
    cmd = _build_cmd("check --json", cfg)
    try:
        rc, out = await _incus_exec_stdin(
            config.container_name, cmd, src, cfg.check_timeout_s,
        )
    except (TimeoutError, OSError):
        logger.warning("cred_watch check exec failed", exc_info=True)
        return None
    if rc != 0:
        return None
    return _extract_json(out)


async def run_container_restore(config, target_name: str) -> dict | None:
    cfg = config.cred_integrity
    # target_name originates from the container's own verdict / episode keys, but
    # validate it against the known target set before it enters an incus `su -c`
    # string — defense in depth, matching the shlex-quoting of the other args.
    if target_name not in {t.name for t in cred_integrity.DEFAULT_TARGETS}:
        logger.warning("cred_watch: refusing restore of unknown target %r", target_name)
        return None
    src = Path(cred_integrity.__file__).read_bytes()
    cmd = _build_cmd(f"restore --target {shlex.quote(target_name)} --json", cfg)
    try:
        rc, out = await _incus_exec_stdin(
            config.container_name, cmd, src, cfg.restore_timeout_s,
        )
    except (TimeoutError, OSError):
        logger.warning("cred_watch restore exec failed", exc_info=True)
        return None
    if rc != 0:
        return None
    return _extract_json(out)


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text()).get("episodes", {})
    except (json.JSONDecodeError, OSError):
        return {}


def _save_state(path: Path, episodes: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, "episodes": episodes}))
    except OSError:
        logger.warning("failed to persist cred alert state", exc_info=True)


async def check_credential_integrity_and_alert(config, dispatcher) -> None:
    """Guardian tick: validate container credential files, escalate per the ladder.

    Never raises into the tick. Alerts go through the host dispatcher (survives a
    dead container — exactly the window the guardian is here for)."""
    cfg = config.cred_integrity
    if not cfg.enabled:
        return

    # G.4 mirror freshness + host-only archive hop. Host-side file ops only, so
    # they run every enabled tick — independent of (and before) the container
    # verdict below, which may be 'no signal' when genesis-server is degraded.
    try:
        await _check_mirror_and_archive(config, dispatcher)
    except Exception:
        logger.warning("cred mirror/archive check failed", exc_info=True)

    report = await run_container_check(config)
    if report is None:
        logger.debug("cred_watch: no verdict this tick (container unreachable?)")
        return

    results = report.get("results", {})
    now = datetime.now(UTC)
    now_iso = now.isoformat()
    state_file = config.state_path / _STATE_FILE
    episodes = _load_state(state_file)

    corrupt: dict[str, str] = {
        name: f"{r.get('status')}: {r.get('detail', '')}".strip(": ")
        for name, r in results.items()
        if not r.get("ok", True) and r.get("status") != "absent"
    }
    # Raw status per corrupt target — the guardian only auto-restores RESTORABLE
    # statuses. "unreadable" (permission/IO) is ambiguous, not proven corruption,
    # so it alerts but is never stepped-in on (mirrors the container self-heal).
    corrupt_status: dict[str, str] = {
        name: r.get("status", "")
        for name, r in results.items()
        if not r.get("ok", True) and r.get("status") != "absent"
    }

    for name in set(list(corrupt) + list(episodes)):
        is_corrupt = name in corrupt
        episode = episodes.get(name)
        decision = decide(name, is_corrupt, episode, now, cfg)

        if decision.action == "none":
            if is_corrupt and episode is not None:
                episode["last_status"] = corrupt[name]
            continue

        if decision.action == "resolved":
            episodes.pop(name, None)
            await _send(dispatcher, AlertSeverity.INFO,
                        "Credential file recovered",
                        f"{name} is valid again — corruption cleared.")
            continue

        if decision.action == "warn":
            episodes[name] = {
                "first_seen": now_iso, "last_status": corrupt[name],
                "warned_at": now_iso, "last_alert_at": now_iso,
                "restore_attempts": [], "stepped_in_at": None,
            }
            await _send(dispatcher, AlertSeverity.WARNING,
                        f"Credential corruption: {name}",
                        f"{name} is {corrupt[name]}. Deferring to container "
                        f"self-heal for {cfg.grace_minutes} min before the "
                        "guardian steps in.")
            continue

        if decision.action == "realert":
            episode["last_alert_at"] = now_iso
            episode["last_status"] = corrupt[name]
            await _send(dispatcher, AlertSeverity.CRITICAL,
                        f"Credential still corrupt: {name}",
                        f"{name} remains {corrupt[name]} after a guardian restore "
                        "attempt — manual intervention needed.")
            continue

        if decision.action == "step_in":
            attempts = list(episode.get("restore_attempts", []))
            episode["last_status"] = corrupt[name]
            episode["stepped_in_at"] = now_iso
            episode["last_alert_at"] = now_iso
            # Only auto-restore proven-corrupt (restorable) statuses. An
            # "unreadable" verdict is ambiguous — alert for manual action, never
            # overwrite a file that was not proven corrupt.
            if corrupt_status.get(name) not in RESTORABLE_STATUSES:
                await _send(dispatcher, AlertSeverity.CRITICAL,
                            f"Credential unreadable: {name}",
                            f"{name} is {corrupt[name]} and the container did not "
                            f"resolve it in {cfg.grace_minutes} min. This is not a "
                            "proven-corrupt state (permission/IO?), so the guardian "
                            "will NOT auto-restore — manual intervention needed.")
                continue
            if not allowed_restore(attempts, now, cfg.max_restores_per_day):
                episode["restore_attempts"] = attempts
                await _send(dispatcher, AlertSeverity.CRITICAL,
                            f"Credential restore rate-capped: {name}",
                            f"{name} still {corrupt[name]} and the "
                            f"{cfg.max_restores_per_day}/day restore cap is reached "
                            "— the backup copy may itself be bad. Manual action needed.")
                continue

            attempts.append(now_iso)
            episode["restore_attempts"] = attempts
            result = await run_container_restore(config, name)
            recheck = await run_container_check(config)
            # A restore that reported ok is trusted unless a REACHABLE recheck
            # contradicts it. If the recheck itself is unreachable (None), treat it
            # as inconclusive — don't flip a real success into a false "FAILED".
            recheck_ok = (
                True if recheck is None
                else bool(recheck.get("results", {}).get(name, {}).get("ok"))
            )
            if result and result.get("ok") and recheck_ok:
                await _send(dispatcher, AlertSeverity.CRITICAL,
                            f"Guardian restored credential: {name}",
                            f"Container did not self-heal in {cfg.grace_minutes} min; "
                            f"the guardian restored {name} from backup "
                            f"(dated {result.get('backup_mtime', '?')}). "
                            "Rotate it if it changed since the backup.")
            else:
                action = (result or {}).get("action", "exec_failed")
                detail = (result or {}).get("detail", "")
                await _send(dispatcher, AlertSeverity.CRITICAL,
                            f"Guardian restore FAILED: {name}",
                            f"{name} still corrupt — restore {action}: {detail}. "
                            "Manual intervention needed.")

    _save_state(state_file, episodes)


async def _check_mirror_and_archive(config, dispatcher) -> None:
    """G.4: warn if the host-side credential mirror is stale, then keep a
    host-only archive copy (container-unreachable) of the mirror + escrow.

    Pure host-side file operations — no incus exec, no container dependency."""
    shared_guardian = config.state_path.joinpath(*_SHARED_GUARDIAN)
    mirror_dir = shared_guardian / _MIRROR_SUBDIR
    escrow = shared_guardian / _ESCROW_FILE

    # Freshness alerting only makes sense once backups are configured; the escrow
    # file (written by the same bridge tick) is the proxy for "backups on".
    if escrow.exists():
        await _mirror_freshness_alert(config, dispatcher, mirror_dir)
    else:
        # Backups not configured (or turned off): drop any lingering stale-warn
        # state so a later re-enable starts clean. No alert — absence is expected.
        state_file = config.state_path / _MIRROR_STATE_FILE
        if _load_mirror_state(state_file).get("warned_at"):
            _save_mirror_state(state_file, {})

    _archive_mirror(config, mirror_dir, escrow)


def _mirror_newest_mtime(mirror_dir: Path) -> datetime | None:
    """Newest ``*.gpg`` mtime under the mirror, or None if the mirror is
    incomplete. The MIRROR_STAMP completeness marker must be present — a
    half-written mirror (files but no stamp) counts as 'no complete mirror'."""
    if not (mirror_dir / _MIRROR_STAMP).exists():
        return None
    newest = 0.0
    for f in mirror_dir.rglob("*.gpg"):
        try:
            newest = max(newest, f.stat().st_mtime)
        except OSError:
            continue
    if newest <= 0:
        return None
    return datetime.fromtimestamp(newest, tz=UTC)


async def _mirror_freshness_alert(config, dispatcher, mirror_dir: Path) -> None:
    cfg = config.cred_integrity
    now = datetime.now(UTC)
    state_file = config.state_path / _MIRROR_STATE_FILE
    state = _load_mirror_state(state_file)

    newest = _mirror_newest_mtime(mirror_dir)
    if newest is not None:
        age_h = (now - newest).total_seconds() / 3600
        stale = age_h > cfg.mirror_stale_hours
    else:
        age_h = None
        stale = True

    if not stale:
        if state.get("warned_at"):
            _save_mirror_state(state_file, {})
            await _send(dispatcher, AlertSeverity.INFO,
                        "Credential mirror fresh again",
                        "The host-side encrypted credential mirror is updating "
                        "normally again.")
        return

    last_alert = _parse(state.get("last_alert_at"))
    if last_alert and (now - last_alert).total_seconds() < cfg.realert_hours * 3600:
        return
    detail = (
        f"newest backed-up credential is {age_h:.1f}h old" if age_h is not None
        else "no complete mirror found (missing MIRROR_STAMP)"
    )
    _save_mirror_state(state_file, {
        "warned_at": state.get("warned_at") or now.isoformat(),
        "last_alert_at": now.isoformat(),
    })
    await _send(dispatcher, AlertSeverity.WARNING,
                "Credential backup mirror stale",
                f"The host-side credential mirror is stale: {detail} "
                f"(threshold {cfg.mirror_stale_hours:.0f}h). Encrypted backups may "
                "not be landing — a container loss right now could not be recovered "
                "from the host. Check the backup timer and the awareness loop.")


def _archive_mirror(config, mirror_dir: Path, escrow: Path) -> None:
    """Copy the mirror tree + escrow → a host-only archive the container cannot
    reach. Refuse an empty or stamp-less mirror so a zeroed shared mount never
    even starts an archive round.

    The archive is deliberately GROW-ONLY (it never prunes). It is the last line
    of defence, so it must never delete a credential based on a single —
    possibly transient or partial — view of the mirror (e.g. a backup.sh rewrite
    window that briefly drops a .gpg, then the container's own mirror prunes it,
    then this tick sees the shrunken set). Stale extra encrypted blobs are
    harmless on restore; losing the only surviving copy is not. Same-named creds
    are still refreshed in place, so rotations propagate."""
    stamp = mirror_dir / _MIRROR_STAMP
    if not stamp.exists():
        logger.debug("cred archive: mirror has no STAMP (incomplete/absent) — skipping")
        return
    gpg_files = [f for f in mirror_dir.rglob("*.gpg") if f.is_file()]
    if not gpg_files:
        logger.debug("cred archive: mirror has no encrypted files — skipping")
        return

    archive = config.state_path / _ARCHIVE_SUBDIR
    try:
        archive.mkdir(parents=True, exist_ok=True)
        os.chmod(archive, 0o700)
        for f in gpg_files:
            _atomic_copy(f, archive / f.relative_to(mirror_dir))
        _atomic_copy(stamp, archive / _MIRROR_STAMP)
        if escrow.exists():
            _atomic_copy(escrow, archive / _ESCROW_FILE)
    except OSError:
        logger.warning("cred archive hop failed", exc_info=True)


def _load_mirror_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        obj = json.loads(path.read_text())
        return obj if isinstance(obj, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_mirror_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))
    except OSError:
        logger.warning("failed to persist cred mirror alert state", exc_info=True)


async def _send(dispatcher, severity: AlertSeverity, title: str, body: str) -> None:
    try:
        await dispatcher.send(Alert(severity=severity, title=title, body=body))
    except Exception:
        logger.warning("failed to send cred integrity alert", exc_info=True)
