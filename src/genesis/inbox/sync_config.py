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

# Characters no config value handed to the script may contain: C0 and C1
# controls (a newline would inject a second ``key=`` line into the output the
# script parses), Unicode line/paragraph separators, and bidi controls (they
# make a logged value read differently from what it is).
_UNSAFE_CHARS = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029\u200e\u200f\u202a-\u202e\u2066-\u2069]")

# An rclone remote: a remote name (not starting with "-", so it can never be
# read as an option), a colon, then a path.
_REMOTE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*:.*$", re.DOTALL)


def remote_is_valid(remote: str) -> bool:
    """True when ``remote`` has the shape of an rclone ``name:path`` remote."""
    return bool(_REMOTE_RE.fullmatch(remote)) and not _UNSAFE_CHARS.search(remote)


def mirror_target_problem(watch_path: Path, *, require_inside_home: bool = True) -> str | None:
    """Why ``watch_path`` must not be the sync's mirror target, or None if it may.

    ``rclone sync`` makes the target match the vault, DELETING everything in
    it the vault lacks, every 5 minutes. ``watch_path`` is a setting (the
    settings tool can write it), so the target is confined: strictly inside
    the home directory, and neither inside nor around Genesis's home or the
    repository. Checked on the resolved path, so a symlink cannot point it out.

    ``require_inside_home=False`` keeps only the catastrophic cases (the home
    directory itself, a filesystem root, Genesis's home or the repository and
    anything around them): the settings validator uses it, since a watch path
    outside home can be legitimate for the inbox monitor on another install.
    """
    raw = str(watch_path)
    if _UNSAFE_CHARS.search(raw):
        return "contains control characters"
    try:
        target = watch_path.expanduser().resolve()
        home = Path.home().resolve()
        protected = [genesis_home().expanduser().resolve(), repo_root().resolve()]
    except (OSError, RuntimeError) as exc:
        return f"cannot be resolved ({type(exc).__name__})"
    if target == home or target == Path(target.anchor) or target in home.parents:
        return f"must not be {target} (the home directory, a filesystem root, or above home)"
    if require_inside_home and home not in target.parents:
        return f"must be a folder strictly inside {home}"
    for p in protected:
        if target == p or p in target.parents or target in p.parents:
            return f"must not be, contain or sit inside {p}"
    return None


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
    if remote:
        problem = mirror_target_problem(watch_path)
        if problem:
            print(
                f"inbox_monitor.watch_path {str(watch_path)!r} cannot be the sync target: {problem}",
                file=sys.stderr,
            )
            return 2
    heartbeat = heartbeat_path()
    if _UNSAFE_CHARS.search(str(heartbeat)):
        print("the heartbeat path contains control characters", file=sys.stderr)
        return 2
    print(f"remote={remote}")
    print(f"watch_path={watch_path}")
    print(f"heartbeat={heartbeat}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
