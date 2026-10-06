"""Private append-only evidence: every paid request and the answer it bought.

OpenRouter does not keep completions to fetch again, so a paid answer exists
only here. One campaign directory holds ``answers.jsonl``; each line is one
JSON object. ``dispatch`` is written and fsynced BEFORE a request leaves the
process, then ``answer`` or ``failure`` after. Rescoring reads answers and never
pays twice; the dispatch count is what the request cap measures.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

KINDS = ("dispatch", "answer", "failure")


class Incomplete(ValueError):
    """Evidence is insufficient to run or to claim qualification."""


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def load_json(data: str | bytes):
    """Strict JSON: duplicate keys and non-finite numbers are errors."""

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Incomplete("duplicate JSON key")
            result[key] = value
        return result

    def nonfinite(_value):
        raise Incomplete("nonfinite JSON number")

    return json.loads(data, object_pairs_hook=pairs, parse_constant=nonfinite)


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


class Campaign:
    """An exclusive ``flock`` on a 0700 directory plus its answers file.

    A torn final line is discarded on open: its write never completed, so a
    torn ``dispatch`` was never sent, and a torn ``answer`` leaves its case
    unanswered while the intact ``dispatch`` before it still counts.
    """

    def __init__(self, directory: Path):
        self.directory = directory
        self.lines: list[dict] = []
        self.torn_tail_discarded = False
        self._lock = self._file = None

    def __enter__(self):
        self.directory.mkdir(mode=0o700, exist_ok=True)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Incomplete("campaign directory must have mode 0700 and not be a symlink")
        try:
            self._lock = private_open(self.directory / "campaign.lock", os.O_CREAT | os.O_RDWR)
            try:
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Incomplete("campaign is in use by another session") from exc
            self._file = private_open(
                self.directory / "answers.jsonl", os.O_CREAT | os.O_RDWR | os.O_APPEND
            )
            # File fsync alone cannot persist newly created directory entries.
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
        for attr in ("_file", "_lock"):
            fd = getattr(self, attr)
            if fd is not None:
                os.close(fd)
                setattr(self, attr, None)

    def _load(self):
        with os.fdopen(os.dup(self._file), "rb") as stream:
            data = stream.read()
        complete = data[: data.rfind(b"\n") + 1]
        if len(complete) != len(data):
            os.ftruncate(self._file, len(complete))
            os.fsync(self._file)
            self.torn_tail_discarded = True
        for number, raw in enumerate(complete.splitlines(), 1):
            try:
                line = load_json(raw)
            except ValueError as exc:
                raise Incomplete(f"invalid evidence line {number}") from exc
            if not isinstance(line, dict) or line.get("kind") not in KINDS:
                raise Incomplete(f"invalid evidence line {number}")
            self.lines.append(line)

    def append(self, kind: str, **data) -> dict:
        if kind not in KINDS:
            raise ValueError(f"unknown evidence kind {kind!r}")
        line = {"kind": kind, **data, "at": datetime.now(UTC).isoformat()}
        record = canonical(line) + b"\n"
        written = os.write(self._file, record)
        if written != len(record):
            raise OSError("short evidence write")
        os.fsync(self._file)
        self.lines.append(line)
        return line
