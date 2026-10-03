"""Private, durable campaign evidence and conservative decimal accounting."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

CEILING = Decimal("5")


class Incomplete(ValueError):
    """Evidence is insufficient to execute or claim qualification."""


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def load_json(data: str | bytes, *, exact_numbers=False):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Incomplete("duplicate JSON key")
            result[key] = value
        return result

    def nonfinite(value):
        raise Incomplete("nonfinite JSON number")

    return json.loads(
        data,
        object_pairs_hook=pairs,
        parse_constant=nonfinite,
        **({"parse_float": str} if exact_numbers else {}),
    )


def money(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise Incomplete("missing or invalid currency")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise Incomplete("invalid currency") from exc
    if (
        not amount.is_finite()
        or amount < 0
        or abs(amount.as_tuple().exponent) > 1000
        or len(amount.as_tuple().digits) > 1000
    ):
        raise Incomplete("negative or nonfinite currency")
    return amount


def currency_sum(values) -> Decimal:
    """Add exact decimal coefficients without the process Decimal context.

    Default precision rounds tiny reservations away at the $5 boundary.
    Integer scaling preserves every digit, including across restarts.
    """
    amounts = [money(value) for value in values]
    exponent = min((a.as_tuple().exponent for a in amounts), default=0)
    coefficient = 0
    for amount in amounts:
        parts = amount.as_tuple()
        digits = int("".join(map(str, parts.digits)))
        coefficient += digits * 10 ** (parts.exponent - exponent)
    return Decimal((0, tuple(map(int, str(coefficient))), exponent))


def token_charge(rate, count: int) -> Decimal:
    """Exact USD per-million-token multiplication and division."""
    parts = money(rate).as_tuple()
    coefficient = int("".join(map(str, parts.digits))) * count
    return money(Decimal((0, tuple(map(int, str(coefficient))), parts.exponent - 6)))


def sync_directory(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


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


class Journal:
    """One writer, one append-only JSONL file, hash-chained for corruption detection.

    The hash chain detects accidental damage; it is not authentication against
    the owner editing this private file. A torn trailing record is never repaired
    automatically, because it may have reserved money before a dispatch.
    """

    def __init__(self, directory: Path):
        self.directory = directory
        self.events: list[dict] = []
        self.attempts: dict[str, dict] = {}
        self.manifest: dict = {}
        self._lock = None
        self._file = None

    def __enter__(self):
        # The parent must already exist; do not create unsynced ancestor entries.
        self.directory.mkdir(mode=0o700, exist_ok=True)
        sync_directory(self.directory.parent)
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Incomplete("campaign directory must have mode 0700 and not be a symlink")
        self._lock = private_open(self.directory / "writer.lock", os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._file = private_open(
                self.directory / "events.jsonl", os.O_CREAT | os.O_RDWR | os.O_APPEND
            )
            # Persist the directory entries as well as each record.
            sync_directory(self.directory)
            with os.fdopen(os.dup(self._file), "rb") as stream:
                for line in stream:
                    if not line.endswith(b"\n"):
                        raise Incomplete("torn journal record")
                    event = load_json(line)
                    self._accept(event)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *args):
        for attr in ("_file", "_lock"):
            fd = getattr(self, attr)
            if fd is not None:
                os.close(fd)
                setattr(self, attr, None)

    @property
    def committed(self) -> Decimal:
        return currency_sum(a.get("charge", a["reservation"]) for a in self.attempts.values())

    def _accept(self, event: dict):
        if not isinstance(event, dict) or set(event) != {
            "seq",
            "previous",
            "kind",
            "data",
            "hash",
            "at",
        }:
            raise Incomplete("invalid journal envelope")
        payload = {k: v for k, v in event.items() if k != "hash"}
        previous = self.events[-1]["hash"] if self.events else None
        if (
            event["seq"] != len(self.events)
            or event["previous"] != previous
            or event["hash"] != digest(payload)
        ):
            raise Incomplete("journal chain mismatch")
        kind, data = event["kind"], event["data"]
        if not isinstance(event["at"], str) or datetime.fromisoformat(event["at"]).tzinfo is None:
            raise Incomplete("invalid journal timestamp")
        if not isinstance(data, dict):
            raise Incomplete("invalid journal payload")
        if kind == "manifest":
            if self.events or data.get("ceiling_usd") != "5":
                raise Incomplete("conflicting manifest or ceiling")
            from genesis.eval.qualification.manifest import validate_manifest

            validate_manifest(data)
            self.manifest = data
        else:
            if not self.manifest:
                raise Incomplete("missing manifest")
            key = data.get("attempt")
            if kind == "reserve":
                if key in self.attempts or not isinstance(key, str):
                    raise Incomplete("duplicate or invalid reservation")
                if key not in self.manifest["schedule"]:
                    raise Incomplete("attempt outside frozen schedule")
                reservation = money(data.get("reservation"))
                if (
                    reservation != money(self.manifest["schedule"][key]["maximum_usd"])
                    or reservation <= 0
                ):
                    raise Incomplete("unverified reservation")
                if currency_sum((self.committed, reservation)) > CEILING:
                    raise Incomplete("insufficient campaign funds")
                self.attempts[key] = dict(data)
            else:
                attempt = self.attempts.get(key)
                if attempt is None:
                    raise Incomplete("event without reservation")
                self._transition(attempt, kind, data)
        self.events.append(event)

    def _transition(self, attempt: dict, kind: str, data: dict):
        if kind == "dispatch":
            if "dispatch" in attempt:
                raise Incomplete("repeated dispatch")
            attempt["dispatch"] = data
        elif kind == "observation":
            if "dispatch" not in attempt or "observation" in attempt:
                raise Incomplete("invalid observation ordering")
            generation = data.get("generation_id")
            if generation and any(
                a.get("observation", {}).get("generation_id") == generation
                for a in self.attempts.values()
            ):
                raise Incomplete("generation ID reused across attempts")
            attempt["observation"] = data
        elif kind == "failure":
            if "dispatch" not in attempt or "failure" in attempt:
                raise Incomplete("invalid failure ordering")
            attempt["failure"] = data
        elif kind == "billing":
            if "observation" not in attempt or "charge" in attempt:
                raise Incomplete("invalid reconciliation ordering")
            attempt.setdefault("billing_observations", []).append(data["billing"])
        elif kind == "settle":
            if "observation" not in attempt or "charge" in attempt:
                raise Incomplete("invalid settlement ordering")
            from genesis.eval.qualification.transport import validate_billing

            charge = validate_billing(
                self.manifest,
                attempt["observation"],
                data["billing"],
                previous_bills=attempt.get("billing_observations", ()),
            )
            if charge > money(attempt["reservation"]):
                raise Incomplete("charge exceeds reservation")
            attempt["charge"] = str(charge)
            attempt["billing"] = data["billing"]
        elif kind == "score":
            if "charge" not in attempt or "score" in attempt:
                raise Incomplete("invalid scoring ordering")
            if (
                type(data.get("agreement")) is not bool
                or "prediction" not in data
                or "error" not in data
            ):
                raise Incomplete("invalid scoring evidence")
            task = self.manifest["schedule"][data["attempt"]]
            case = self.manifest["cases"][task["case_index"]]
            expected = (
                case["expected_target"]
                if task["contract"] == "procedure_novelty"
                else case["user_passed"]
            )
            if data["agreement"] != (data["prediction"] == expected and not bool(data["error"])):
                raise Incomplete("inconsistent scoring evidence")
            attempt["score"] = data
        else:
            raise Incomplete("unknown journal event")

    def append(self, kind: str, **data):
        event = {
            "seq": len(self.events),
            "previous": self.events[-1]["hash"] if self.events else None,
            "kind": kind,
            "data": data,
            "at": datetime.now(UTC).isoformat(),
        }
        event["hash"] = digest(event)
        # Validate before writing, then reload the state on any write failure.
        self._accept(event)
        record = canonical(event) + b"\n"
        try:
            written = os.write(self._file, record)
            if written != len(record):
                raise OSError("short journal write")
            os.fsync(self._file)
        except BaseException:
            # Never continue with an in-memory state that might differ from disk.
            self.__exit__(None, None, None)
            raise

    def initialize(self, manifest: dict):
        if self.manifest:
            if canonical(self.manifest) != canonical(manifest):
                raise Incomplete("conflicting manifest")
        else:
            self.append("manifest", **manifest)
