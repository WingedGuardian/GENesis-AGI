"""Private append-only campaign evidence, with a read-only historical reader."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import stat
import threading
from datetime import UTC, datetime
from pathlib import Path

KINDS = {
    "manifest",
    "reservation",
    "dispatch",
    "observation",
    "billing",
    "failure",
    "acknowledgement",
    "answer",
}


class Incomplete(ValueError):
    """Evidence is insufficient to run or to claim qualification."""


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def load_json(data: str | bytes):
    """Reject duplicate keys and nonfinite numbers, including exponent overflow."""

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Incomplete("duplicate JSON key")
            result[key] = value
        return result

    def nonfinite(_value):
        raise Incomplete("nonfinite JSON number")

    def finite_float(value):
        number = float(value)
        if not (-float("inf") < number < float("inf")):
            raise Incomplete("nonfinite JSON number")
        return number

    return json.loads(
        data, object_pairs_hook=pairs, parse_constant=nonfinite, parse_float=finite_float
    )


def private_open(path: Path, flags: int) -> int:
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or info.st_nlink != 1
    ):
        os.close(fd)
        raise Incomplete("evidence file must be private, owned, and unlinked elsewhere")
    return fd


def sync_directory(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def check_directory(directory: Path):
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Incomplete("campaign directory must have mode 0700 and not be a symlink")


class Campaign:
    """Exclusive writer; uncertain tails remain untouched and prevent append.

    ``read`` acquires a shared lock without creating or truncating anything.
    The directory must stay under operator control: intentional local edits or
    unlinking an active lock are outside this accident-prevention boundary.
    """

    def __init__(self, directory: Path):
        self.directory = directory
        self._lines: list[dict] = []
        self.torn_tail = b""
        self._lock = self._file = None
        self._poisoned = False
        self._mutex = threading.RLock()

    @property
    def lines(self):
        with self._mutex:
            return copy.deepcopy(self._lines)

    def __enter__(self):
        with self._mutex:
            return self._enter()

    def _enter(self):
        if self._lock is not None or self._file is not None:
            raise Incomplete("campaign is already in use by this writer")
        self.directory.mkdir(mode=0o700, exist_ok=True)
        check_directory(self.directory)
        try:
            self._lock = private_open(self.directory / "campaign.lock", os.O_CREAT | os.O_RDWR)
            try:
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Incomplete("campaign is in use by another session") from exc
            self._file = private_open(
                self.directory / "answers.jsonl", os.O_CREAT | os.O_RDWR | os.O_APPEND
            )
            os.fsync(self._lock)
            os.fsync(self._file)
            sync_directory(self.directory)
            sync_directory(self.directory.parent)
            self._load()
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_exc):
        with self._mutex:
            for attr in ("_file", "_lock"):
                fd = getattr(self, attr)
                if fd is not None:
                    os.close(fd)
                    setattr(self, attr, None)

    @classmethod
    def read(cls, directory: Path):
        """Read existing private evidence only; missing paths are errors."""
        result = cls(directory)
        check_directory(directory)
        try:
            result._lock = private_open(directory / "campaign.lock", os.O_RDONLY)
            fcntl.flock(result._lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            result._file = private_open(directory / "answers.jsonl", os.O_RDONLY)
            result._load()
        finally:
            result.__exit__()
        return result

    def _load(self):
        os.lseek(self._file, 0, os.SEEK_SET)
        with os.fdopen(os.dup(self._file), "rb") as stream:
            data = stream.read()
        end = data.rfind(b"\n") + 1
        self.torn_tail = data[end:]
        self._lines = []
        for number, raw in enumerate(data[:end].split(b"\n")[:-1], 1):
            try:
                line = load_json(raw)
            except (ValueError, UnicodeError) as exc:
                raise Incomplete(f"invalid evidence line {number}") from exc
            if (
                not isinstance(line, dict)
                or not isinstance(line.get("kind"), str)
                or line["kind"] not in KINDS
            ):
                raise Incomplete(f"invalid evidence line {number}")
            self._lines.append(line)

    def append(self, kind: str, **data) -> dict:
        with self._mutex:
            return self._append(kind, **data)

    def _append(self, kind: str, **data) -> dict:
        if self._file is None or self._poisoned or self.torn_tail:
            raise Incomplete("journal is closed or has an uncertain write")
        if kind not in KINDS or "kind" in data or "at" in data:
            raise Incomplete("invalid evidence kind or reserved field")
        line = load_json(canonical({"kind": kind, **data, "at": datetime.now(UTC).isoformat()}))
        record = canonical(line) + b"\n"
        try:
            written = os.write(self._file, record)
            if written != len(record):
                raise OSError("short evidence write")
            os.fsync(self._file)
            self._lines.append(line)
            return copy.deepcopy(line)
        except BaseException:
            self._poisoned = True
            raise
