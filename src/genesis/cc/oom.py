"""Read-only cgroup correlation, never proof of which process was an OOM victim."""

from __future__ import annotations

import os
import signal
from dataclasses import dataclass
from pathlib import Path

from genesis.cc.exceptions import CCError, CCMemoryPressureError

_MEMBERSHIP = Path("/proc/self/cgroup")


@dataclass(frozen=True)
class OOMSnapshot:
    path: str | None
    identity: tuple[int, int] | None
    oom_kill: int | None


@dataclass(frozen=True)
class OOMEvidence:
    path: str | None
    before_identity: tuple[int, int] | None
    after_identity: tuple[int, int] | None
    before: int | None
    after: int | None
    returncode: int | None
    internal: bool = False

    @property
    def correlated(self) -> bool:
        # -SIGKILL is the observed direct/systemd-run --scope signal result.
        # 137 alone is ambiguous and cannot establish a signal.
        return (
            not self.internal
            and self.returncode == -signal.SIGKILL
            and self.before_identity is not None
            and self.before_identity == self.after_identity
            and self.before is not None
            and self.after is not None
            and 0 <= self.before < self.after
        )


def _sample(path: Path | None) -> OOMSnapshot:
    identity = None
    count = None
    if path is not None:
        try:
            with path.open() as handle:
                stat = os.fstat(handle.fileno())
                identity = (stat.st_dev, stat.st_ino)
                counters = dict(line.split() for line in handle)
                count = int(counters["oom_kill"])
                if count < 0:
                    count = None
        except (OSError, UnicodeError, ValueError, KeyError):
            pass
    return OOMSnapshot(str(path) if path is not None else None, identity, count)


def capture_baseline(
    *, managed: bool, membership: Path | None = None,
    root: Path = Path("/sys/fs/cgroup"), uid: int | None = None,
) -> OOMSnapshot:
    """Pin the inherited cgroup, or the proven enclosing user slice for scopes."""
    path = None
    try:
        lines = (membership or _MEMBERSHIP).read_text().splitlines()
        entries = [line[3:] for line in lines if line.startswith("0::")]
        if len(entries) == 1 and entries[0].startswith("/"):
            parts = Path(entries[0]).parts[1:]
            if ".." not in parts and not entries[0].endswith(" (deleted)"):
                if managed:
                    user_slice = f"user-{os.getuid() if uid is None else uid}.slice"
                    parts = parts[:parts.index(user_slice) + 1]
                path = root.joinpath(*parts, "memory.events")
    except (OSError, UnicodeError, ValueError):
        pass
    return _sample(path)


def capture_finish(
    baseline: OOMSnapshot, returncode: int | None, *, internal: bool = False,
) -> OOMEvidence:
    after = _sample(Path(baseline.path) if baseline.path is not None else None)
    return OOMEvidence(
        baseline.path, baseline.identity, after.identity,
        baseline.oom_kill, after.oom_kill, returncode, internal,
    )


def attribute_error(evidence: OOMEvidence | None, cause: CCError) -> CCError:
    """Keep the classifier diagnosis, but stop replay before provider recovery."""
    if evidence is not None and evidence.correlated:
        return CCMemoryPressureError(evidence, cause=cause)
    return cause
