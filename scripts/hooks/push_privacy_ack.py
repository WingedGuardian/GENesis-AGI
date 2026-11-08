"""One-use foreground acknowledgement and qualified expiry pruning."""

import errno
import fcntl
import json
import math
import os
import re
import stat
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from pathlib import Path

if __name__ == "__main__":
    # Hygiene executes the shipped source helper, not a custom installed path.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from genesis.util.atomic import atomic_write_text  # noqa: E402

_TOKEN = re.compile(r"[0-9a-f]{64}\Z")
_TEMP = re.compile(r"tmp[a-z0-9_]{8}\.tmp\Z")
_TTL = 86400
_RETENTION = 7 * _TTL
_LIMIT = 4096


def _directory(common: Path, *, create: bool) -> Path:
    path = Path(common).resolve(strict=True)
    for name in ("genesis", "privacy-ack"):
        path /= name
        if create:
            with suppress(FileExistsError):
                path.mkdir(mode=0o700)
        info = path.lstat()
        forbidden = 0o077 if name == "privacy-ack" else 0o022
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & forbidden:
            raise ValueError("invalid namespace")
    return path


def _open(path: Path, flags: int) -> int:
    try:
        fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENXIO}:
            raise ValueError("invalid state file") from None
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("invalid state file")
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextmanager
def _locked(directory: Path):
    fd = _open(directory / ".lock", os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)  # Keep the inode: unlinking it lets contenders lock different files.


def _timestamp(value: object) -> float:
    if type(value) not in {int, float}:
        raise ValueError("invalid time")
    try:
        result = float(value)
    except OverflowError:
        raise ValueError("invalid time") from None
    if not math.isfinite(result) or result < 0:
        raise ValueError("invalid time")
    return result


def _read(path: Path, token: str) -> dict:
    fd = _open(path, os.O_RDONLY)
    try:
        raw = os.read(fd, _LIMIT + 1)
    finally:
        os.close(fd)
    if len(raw) > _LIMIT:
        raise ValueError("oversized state")
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {"version", "identity", "created_at"}:
        raise ValueError("invalid state schema")
    if (type(value["version"]) is not int or value["version"] != 1
            or value["identity"] != token):
        raise ValueError("invalid state values")
    value["created_at"] = _timestamp(value["created_at"])
    return value


def acknowledge(common: Path, token: str, *, now: float | None = None) -> str:
    """Return repeat only after consuming a valid marker; errors never allow."""
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        return "invalid"
    try:
        now = _timestamp(time.time() if now is None else now)
        directory = _directory(common, create=True)
        with _locked(directory):
            path = directory / (token + ".json")
            try:
                age = now - _read(path, token)["created_at"]
            except FileNotFoundError:
                age = None
            if age is not None:
                if age < 0:
                    return "invalid"
                path.unlink()
                if age <= _TTL:
                    return "repeat"
            atomic_write_text(path, json.dumps({"version": 1, "identity": token, "created_at": now}),
                              encoding="utf-8", overwrite=False)
            return "first"
    except BlockingIOError:
        return "busy"
    except (ValueError, UnicodeError):
        return "invalid"
    except OSError:
        return "unavailable"


def prune(common: Path, *, now: float | None = None) -> int | None:
    """Retire only qualified owned state; None means skip/failure, never success."""
    try:
        now = _timestamp(time.time() if now is None else now)
        directory = _directory(common, create=False)
    except FileNotFoundError:
        return 0
    except (OSError, ValueError):
        return None
    removed = 0
    try:
        with _locked(directory):
            for path in directory.iterdir():
                token = path.name.removesuffix(".json") if path.name.endswith(".json") else ""
                marker = bool(_TOKEN.fullmatch(token))
                if not marker and not _TEMP.fullmatch(path.name):
                    continue
                try:
                    fd = _open(path, os.O_RDONLY)
                    try:
                        info = os.fstat(fd)
                    finally:
                        os.close(fd)
                    if now - info.st_mtime <= _RETENTION:
                        continue
                    if marker and now - _read(path, token)["created_at"] <= _RETENTION:
                        continue
                    path.unlink()
                    removed += 1
                except (OSError, ValueError, UnicodeError):
                    continue  # Preserve unknown content and inaccessible objects.
    except (OSError, ValueError):
        return None
    return removed


def main() -> int:
    """Prune only the named hygiene repository, independent of ambient Git cwd."""
    try:
        if len(sys.argv) != 3 or sys.argv[1] != "--prune":
            raise ValueError("invalid invocation")
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("GIT_") or key.startswith("GIT_CONFIG")}
        result = subprocess.run(
            ["git", "-C", sys.argv[2], "rev-parse", "--path-format=absolute", "--git-common-dir"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, timeout=5,
        )
        common = Path(os.fsdecode(result.stdout).removesuffix("\n"))
        if not common.is_absolute():
            raise ValueError("invalid common directory")
        count = prune(common)
        if count is None:
            raise ValueError("unavailable state")
        print(f"Pruned {count} expired privacy acknowledgement files.")
        return 0
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Privacy acknowledgement pruning could not be qualified.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
