"""Pull a read-only snapshot of the edge ambient.db to a transient local file.

NET-NEW (no reusable helper existed): adapts the SSH idiom from
``observability.ambient_health`` (BatchMode / ConnectTimeout / orphan-kill) but runs a
remote ``sqlite3.backup()`` (the edge has NO sqlite3 CLI -> remote python3) for a
consistent copy, then ``scp``'s it back. Firewall: this transient file is the
sanctioned offline read-only analysis snapshot — the raw transcript text lives ONLY
here, NEVER in genesis.db.
"""
from __future__ import annotations

import asyncio
import logging
import shlex
from datetime import UTC, datetime
from pathlib import Path

from genesis.observability.ambient_health import (
    AmbientRemoteConfig,
    load_ambient_remote_config,
)

logger = logging.getLogger(__name__)

# sqlite3.backup() over SSH streams the whole DB (tens of MB at ~1-5 MB/s on LAN) —
# far slower than ambient_health's `cat`, so a dedicated, generous ceiling.
_SNAPSHOT_SSH_TIMEOUT_S = 60.0
_REMOTE_TMP = "~/.ambient_snap.db"
_EDGE_DB = "~/ambient.db"
_DEFAULT_DEST = "~/.genesis/attention/snapshots"


class SnapshotError(Exception):
    """Snapshot pull failed (edge unreachable / remote backup / scp / timeout)."""


def _ssh_base(cfg: AmbientRemoteConfig) -> list[str]:
    return [
        "ssh", "-i", str(Path(cfg.ssh_key).expanduser()),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10",
        "-o", "BatchMode=yes",
        f"{cfg.host_user}@{cfg.host_ip}",
    ]


def _backup_py(edge_db: str, remote_tmp: str) -> str:
    """Remote one-liner: consistent read-only backup of the edge DB to a temp file."""
    return (
        "import sqlite3,os;"
        f"s=sqlite3.connect('file:'+os.path.expanduser({edge_db!r})+'?mode=ro',uri=True);"
        f"d=sqlite3.connect(os.path.expanduser({remote_tmp!r}));"
        "s.backup(d);d.close();s.close()"
    )


def _remote_backup_arg(edge_db: str, remote_tmp: str) -> str:
    """The remote backup command as ONE shell-safe token. ssh joins trailing args with
    spaces and re-parses them in the REMOTE shell, so the python one-liner's quotes and
    parens MUST be shell-quoted or the remote bash chokes on '('."""
    return f"python3 -c {shlex.quote(_backup_py(edge_db, remote_tmp))}"


def _scp_cmd(cfg: AmbientRemoteConfig, remote_tmp: str, local: Path) -> list[str]:
    return [
        "scp", "-i", str(Path(cfg.ssh_key).expanduser()),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=10",
        "-o", "BatchMode=yes",
        f"{cfg.host_user}@{cfg.host_ip}:{remote_tmp}", str(local),
    ]


async def _run(cmd: list[str], timeout_s: float) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s + 5)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise SnapshotError(f"{cmd[0]} timed out after {timeout_s}s") from None
    if proc.returncode != 0:
        raise SnapshotError(f"{cmd[0]} failed (rc={proc.returncode}): {err.decode().strip()[:300]}")


async def pull_snapshot(
    dest_dir: str | Path = _DEFAULT_DEST,
    *,
    cfg: AmbientRemoteConfig | None = None,
    edge_db: str = _EDGE_DB,
    remote_tmp: str = _REMOTE_TMP,
    timeout_s: float = _SNAPSHOT_SSH_TIMEOUT_S,
) -> tuple[str, Path]:
    """Pull a consistent read-only snapshot of the edge ambient.db.

    Returns ``(snapshot_id, local_path)``. Raises ``SnapshotError`` on any failure.
    """
    cfg = cfg or load_ambient_remote_config()
    if cfg is None:
        raise SnapshotError("ambient_remote.yaml absent or disabled — no edge configured")

    dest = Path(dest_dir).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    snapshot_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    local = dest / f"ambient_{snapshot_id}.db"
    base = _ssh_base(cfg)

    await _run(base + [_remote_backup_arg(edge_db, remote_tmp)], timeout_s)
    await _run(_scp_cmd(cfg, remote_tmp, local), timeout_s)
    try:  # best-effort remote cleanup
        await _run(base + ["rm", "-f", remote_tmp], 15.0)
    except SnapshotError as exc:
        logger.warning("snapshot remote cleanup failed (harmless): %s", exc)

    logger.info("pulled ambient snapshot %s -> %s (%d bytes)", snapshot_id, local,
                local.stat().st_size if local.exists() else -1)
    return snapshot_id, local
