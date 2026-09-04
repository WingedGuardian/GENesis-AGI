"""Attribute an abnormal process death to the kernel's OOM killer, from cgroup v2.

A process reaped under memory pressure reaches its owner as a bare kill: a negative
return code, usually empty stderr, and no cause. The owning session cannot tell a
reap from a crash from a timeout, so it re-arms the identical work and loses it
again — invisibility does not merely lose the result, it induces the retry.

**Why the journal is not the answer.** The obvious source is the kernel log, and it
is unreliable here. MEASURED on this install, cgroup ``user.slice`` reported
``oom_kill 35`` against 14 matching lines in the user journal for the same boot —
most reaps leave no log line a session can find, and reading the journal also needs
permissions a subprocess may not have.

The cgroup counter has neither problem. ``memory.events`` is readable by the owning
user, it counts every reap in a cgroup and its descendants, and it never misses. So
attribution here is a DELTA: sample the counter before the work starts, read it again
once the process is dead, and compare.

**What this can and cannot prove.** A delta says an OOM kill happened in this cgroup
while the process was running — NOT that this process was the victim. Another process
in the same cgroup could have been the one reaped. That is why nothing here reports a
certainty: :func:`describe_oom_death` requires BOTH a delta AND death by SIGKILL, and
still says "probable". Two facts make the inference strong on this deployment
specifically, and both are recorded rather than assumed:

* ``cc.invoker.set_oom_score_adj`` deliberately sets ``oom_score_adj=+500`` on CC
  subprocesses so the kernel prefers them over genesis-server and qdrant. The
  dispatched work is the INTENDED victim, so when a reap occurs in its cgroup while
  it dies by SIGKILL, it is very likely the one that was chosen. That write can
  FAIL (denied procfs, read-only, or the process exited first), so it is not assumed
  either: ``set_oom_score_adj`` returns whether it landed and the caller passes that
  through as :meth:`OomProbe.describe`'s ``score_adjusted``, which omits the
  preference sentence when the preference was never established.
* A SIGKILL that Genesis itself sent (a timeout, a reaper) is distinguishable by the
  caller, which knows it sent one — see :meth:`OomProbe.describe` and its ``self_killed``
  argument. Attribution is skipped in that case rather than guessed at.
"""

from __future__ import annotations

import logging
import signal
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_CGROUP_ROOT = Path("/sys/fs/cgroup")
_PROC_SELF_CGROUP = Path("/proc/self/cgroup")

#: The field in ``memory.events`` counting processes reaped by the OOM killer in this
#: cgroup and its descendants. Deliberately NOT ``oom``: that counts times the cgroup
#: hit its limit, which includes pressure that killed nothing.
_OOM_KILL_FIELD = "oom_kill"


def _own_cgroup_relpath() -> str | None:
    """This process's cgroup v2 path, relative to the hierarchy root.

    ``/proc/self/cgroup`` on a v2-only host is a single ``0::<path>`` line. A v1 or
    hybrid host has other lines, which this deliberately ignores rather than guessing
    a v1 layout — an unsupported layout must degrade to "unknown", never to a wrong
    attribution.
    """
    try:
        for line in _PROC_SELF_CGROUP.read_text().splitlines():
            hier, _, path = line.partition("::")
            if hier == "0" and path.startswith("/"):
                return path
    except OSError:
        return None
    return None


#: The cgroup level to watch by default: the whole per-user subtree.
#:
#: NOT the caller's own cgroup, and that distinction is the difference between this
#: working and silently never firing. Genesis spawns CC through ``systemd-run
#: --scope --user``, which puts the child in its OWN transient scope under
#: ``user@<uid>.service`` — a SIBLING of the caller's cgroup, not a descendant. A
#: counter read on the caller's own cgroup can therefore never see the child's reap,
#: and the delta would be zero forever while dispatched work died invisibly.
#:
#: MEASURED on this install, which is what exposed it: all 35 reaps were counted at
#: ``user-1000.slice`` and ``user@1000.service``, while every ``session-*.scope``
#: read 0. Watching the user slice covers the caller, the systemd user units, and
#: every transient scope, while still excluding ``system.slice`` — so an unrelated
#: system daemon being reaped cannot be mistaken for our own.
_USER_SLICE_RE = "user-"


def read_oom_kills(cgroup_relpath: str | None = None) -> tuple[int, str] | None:
    """``(count, cgroup_path)`` of OOM reaps in a cgroup subtree, or None if unknown.

    Resolves to the enclosing per-user slice (``user-<uid>.slice``) when this process
    sits under one — see :data:`_USER_SLICE_RE` for why the caller's OWN cgroup is
    the wrong answer. Falls back to walking upward to the first readable
    ``memory.events`` on a layout with no user slice (a container running as pid 1,
    a v1 host), which is a narrower watch but still correct for a child that
    inherits the cgroup.

    The caller is told WHICH cgroup answered, because a delta from a broad cgroup is
    weaker evidence than one from a narrow cgroup and the annotation says so.

    Returns None — never 0 — when the counter cannot be read at all. Those are
    different facts, and collapsing them would let "no OOM here" be reported for a
    host where the question was never asked.
    """
    rel = cgroup_relpath if cgroup_relpath is not None else _own_cgroup_relpath()
    if rel is None:
        return None
    node = _CGROUP_ROOT / rel.lstrip("/")

    if cgroup_relpath is None:
        # Prefer the per-user slice over anything nearer, so a sibling scope's reap
        # is still counted. Explicit paths are honoured as given — a caller that
        # names a cgroup means that one.
        for ancestor in (node, *node.parents):
            if ancestor.name.startswith(_USER_SLICE_RE) and ancestor.name.endswith(".slice"):
                node = ancestor
                break

    while True:
        try:
            for line in (node / "memory.events").read_text().splitlines():
                field, _, value = line.partition(" ")
                if field == _OOM_KILL_FIELD:
                    return int(value), str(node)
        except (OSError, ValueError):
            pass
        if node == _CGROUP_ROOT or _CGROUP_ROOT not in node.parents:
            return None
        node = node.parent


@dataclass(frozen=True)
class OomProbe:
    """A before-reading of the OOM counter, to be compared after a process dies.

    Construct with :meth:`sample` before spawning the work; call :meth:`describe`
    once the process is dead. Never raises: an unreadable counter yields a probe that
    simply declines to attribute anything, because a monitoring helper that can break
    the thing it monitors is worse than no monitoring.
    """

    baseline: int | None
    cgroup: str | None

    @classmethod
    def sample(cls, cgroup_relpath: str | None = None) -> OomProbe:
        reading = read_oom_kills(cgroup_relpath)
        if reading is None:
            return cls(baseline=None, cgroup=None)
        count, path = reading
        return cls(baseline=count, cgroup=path)

    @property
    def available(self) -> bool:
        """Whether this host answered the question at all."""
        return self.baseline is not None

    def kills_since(self) -> int | None:
        """Reaps in this cgroup since :meth:`sample`, or None if unknown.

        Clamped at zero: a counter that appears to go backwards means the cgroup was
        recreated under us (a restarted unit), which invalidates the comparison
        rather than proving negative kills.
        """
        if self.baseline is None or self.cgroup is None:
            return None
        try:
            for line in (Path(self.cgroup) / "memory.events").read_text().splitlines():
                field, _, value = line.partition(" ")
                if field == _OOM_KILL_FIELD:
                    return max(0, int(value) - self.baseline)
        except (OSError, ValueError):
            return None
        return None

    def describe(
        self,
        returncode: int | None,
        *,
        self_killed: bool = False,
        score_adjusted: bool = False,
    ) -> str | None:
        """A cause annotation for an abnormal exit, or None to stay silent.

        Returns None — deliberately, and in every ambiguous case — unless all of:

        * the process died by SIGKILL (``returncode == -9``). A non-SIGKILL exit was
          not the OOM killer, whatever the counter says.
        * ``self_killed`` is False. A timeout or reaper SIGKILL from Genesis itself
          is a known cause already, and dressing it as a probable OOM would replace
          one true statement with a plausible wrong one.
        * the counter is readable AND increased. An unreadable counter is "unknown",
          which this reports as nothing rather than as absence.

        ``score_adjusted`` is the caller's OBSERVED result of writing
        ``oom_score_adj`` for this process (``cc.invoker.set_oom_score_adj`` returns
        it), not an assumption that the write landed. The "the kernel prefers this
        process" sentence strengthens the inference, so it is printed only when that
        preference was actually established: on a host where procfs denied the write,
        is read-only, or the process exited first, the sentence would present an
        unverified premise as evidence — overstating what the counter and the SIGKILL
        together prove. The default is False so a caller that does not know stays
        silent about it rather than claiming it.

        Silence is the right default because this annotation exists to be trusted:
        a cause line that is sometimes invented is worse than a bare kill, which at
        least does not mislead.
        """
        if self_killed or returncode != -signal.SIGKILL:
            return None
        delta = self.kills_since()
        if not delta:
            return None
        victim = (
            " Dispatched work carries oom_score_adj=+500 so the kernel prefers it "
            "over the server, which makes it the likely victim."
            if score_adjusted
            else ""
        )
        return (
            f"probable cause: out-of-memory reap — the kernel OOM-killed {delta} "
            f"process(es) in {self.cgroup} while this ran, and this process died by "
            f"SIGKILL.{victim} Reduce the working set, or run fewer sessions "
            "concurrently."
        )
