"""What the inbox sync (``scripts/inbox_sync.sh``) reads from Genesis config.

One source for the script and the server: the script runs
``python -m genesis.inbox.sync_config`` and the staleness check imports the
same functions, so they cannot disagree about the remote, the inbox path or
where the heartbeat lives.

CLI output, one ``key=value`` per line::

    remote=<rclone remote, or empty when the sync is off>
    watch_path=<inbox directory>
    heartbeat=<heartbeat file path>

Exit codes: 0 ok (including "off"), 2 the configured remote is invalid, 1 the
config could not be read. The script refuses to run on anything but 0, so a
broken config stops the sync loudly instead of reading as "off".
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from genesis.env import genesis_home, repo_root

# An rclone remote: a remote name (not starting with "-", so it can never be
# read as an option), a colon, then a path with no control characters.
_REMOTE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*:[^\x00-\x1f\x7f]*$")


def remote_is_valid(remote: str) -> bool:
    """True when ``remote`` has the shape of an rclone ``name:path`` remote."""
    return bool(_REMOTE_RE.fullmatch(remote))


def heartbeat_path() -> Path:
    """The file each configured sync cycle writes its outcome to."""
    return genesis_home() / "state" / "inbox_sync.json"


def load_sync_settings() -> tuple[str, Path]:
    """Return ``(sync_remote, watch_path)`` from the inbox config and overlay.

    Strict: a malformed overlay raises rather than reading as "off", because
    the remote usually lives only in the overlay.
    """
    from genesis.inbox.config import load_inbox_config

    config = load_inbox_config(repo_root() / "config" / "inbox_monitor.yaml", strict=True)
    return config.sync_remote, config.watch_path


def main() -> int:
    try:
        remote, watch_path = load_sync_settings()
    except Exception as exc:  # noqa: BLE001 — reported, and the script refuses to run
        print(f"inbox sync config could not be read: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if remote and not remote_is_valid(remote):
        print(
            f"inbox_monitor.sync_remote is not a valid rclone remote: {remote!r}", file=sys.stderr
        )
        return 2
    print(f"remote={remote}")
    print(f"watch_path={watch_path}")
    print(f"heartbeat={heartbeat_path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
