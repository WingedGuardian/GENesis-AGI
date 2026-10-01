"""Resolve a Claude Code session id to the name ``SendMessage`` addresses it by.

Genesis names a session by its UUID (charters, ledgers, the ``[Concurrent]``
lines). Cross-session messaging names it by a peer NAME, and ``ListAgents``
never prints the UUID. This module is the join, and the ONLY reader in the
repo of the file it joins through: Claude Code's session registry,
``<config dir>/sessions/<pid>.json``. The format is Claude Code's and is
undocumented, so every field it relies on is checked here and nowhere else.

The selection follows Claude Code's own ``findLivePeerBySessionId`` (read from
the 2.1.280 binary). The candidates are the entries whose ``sessionId`` is the
id asked for, plus, for a session that was moved into a background job, the
job's entry (its ``jobId`` equals a ``parkedJobId`` on one of the session's
entries). A candidate is skipped when it is a spare, is parked, or has no
messaging socket. What is left counts as live only when:

- its ``pidDomain`` equals this process's own domain, built exactly as Claude
  Code builds it: ``linux:<machine-id>:<readlink /proc/self/ns/pid>``, with an
  empty string for either part that cannot be read. The machine id, not the
  boot id: a container restart keeps the machine id and changes the PID
  namespace, so stale entries from before it carry a different domain;
- ``/proc/<pid>/stat`` is readable and the process is not a zombie;
- field 22 of that stat line (the start time in clock ticks) equals the
  entry's ``procStart``, which Claude Code stores as a digit STRING. A pid
  recycled by another process always has a later start time.

Two deliberate differences from Claude Code. It treats an entry from another
domain, or one missing ``procStart`` or ``pidDomain``, as present and then
connects to its socket; this module does not open sockets, so it calls such an
entry not reachable (``other-domain`` / ``unverifiable``) rather than guess.
And where two candidates are live it reports ``ambiguous`` instead of taking
the first.

The registry keeps entries for dead processes, and one session id can sit on
several pids (a resumed session), so matching on the first entry, or on a
name, gives wrong answers. Exactly one live candidate is an address.

A field that is PRESENT with the wrong type reports ``registry-format-changed``.
That is loud on purpose: a silent miss would read exactly like a peer that has
gone away. A field Claude Code itself treats as optional (``procStart``,
``pidDomain``, ``sessionId``) may be absent without that alarm.
``claude agents --json`` (Claude Code's supported scripting interface) is the
independent oracle for all of this; ``python -m genesis session-address
--check`` compares the two.

What this proves, and what it does not: a live process of THIS user, in this
pid namespace, holds a registry entry naming that session. The registry is a
directory any process of the same user can write, so an entry is that user's
claim, not an authenticated identity; another process of the same user can
plant one. Treat an address as the same kind of hint ``ListAgents`` gives,
never as proof of who is on the other end.

Stdlib only, because the per-prompt hook imports it.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# Registry entries are <pid>.json. The directory also holds <pid>.<hex>.key
# files carrying messaging credentials; the pattern is what keeps this module
# from ever opening one.
_ENTRY_FILE = re.compile(r"\d+\.json", re.ASCII)
# Entries measured at about 600 bytes. A file far larger is not an entry.
_MAX_ENTRY_BYTES = 65_536

# What may be RENDERED into another session's context. Names and panes are
# written by other sessions, and a name is an address, so a name that fails
# this is omitted whole and never cut: a shortened name is a wrong address.
# Claude Code's normaliser lowercases and turns whitespace into '-', but keeps
# characters such as '|' and ']' that would forge the tag grammar. Every
# pattern here is used with fullmatch and re.ASCII: `$` alone also matches
# before a trailing newline, and `\d` alone matches non-ASCII digits.
_DISPLAY_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,47}", re.ASCII)
_DISPLAY_PANE = re.compile(r"[A-Za-z0-9_.-]{1,32}:@\d{1,6}\.%\d{1,6}", re.ASCII)
_DIGITS = re.compile(r"\d+", re.ASCII)
# A background job id is peer-written too, and it can reach a result's detail.
_DISPLAY_JOB = re.compile(r"[A-Za-z0-9_-]{1,64}", re.ASCII)

OK = "ok"
NOT_REACHABLE = "not-reachable"
AMBIGUOUS = "ambiguous"
FORMAT_CHANGED = "registry-format-changed"
UNNAMED = "unnamed"
NO_REGISTRY = "no-registry"


@dataclass(frozen=True)
class Resolution:
    """The answer for one session id.

    ``name`` and ``pane`` are the RAW registry values, written by the peer:
    only :func:`render` is safe to place in another session's context.
    ``pane`` is the tmux location, which a child ``claude -p`` process shares
    with its parent, so it is a place, not an identity. ``detail`` says why an
    answer is what it is. ``shared_name`` means another live session carries
    the same name, so ``SendMessage`` needs the ``[ref]`` ``ListAgents`` prints.
    """

    session_id: str
    status: str
    name: str | None = None
    pane: str | None = None
    pid: int | None = None
    shared_name: bool = False
    names: tuple[str, ...] = ()
    detail: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        """The answer as data for a tool result or ``--json``.

        Peer-written text leaves this module only in its allowlisted form: a
        name or pane that fails the allowlist is returned as None, with
        ``name_withheld`` set, exactly as :func:`render` omits it. The raw
        values stay on the object for code in this process.
        """
        name = self.name if _display_name(self.name) else None
        return {
            "session_id": self.session_id,
            "status": self.status,
            "name": name,
            "name_withheld": self.name is not None and name is None,
            "pane": self.pane if _display_pane(self.pane) else None,
            "pid": self.pid,
            "shared_name": self.shared_name,
            "names": [n if _display_name(n) else None for n in self.names],
            "detail": list(self.detail),
            "display": render(self),
        }


def _display_name(name: str | None) -> bool:
    return name is not None and _DISPLAY_NAME.fullmatch(name) is not None


def _display_pane(pane: str | None) -> bool:
    return pane is not None and _DISPLAY_PANE.fullmatch(pane) is not None


@dataclass(frozen=True)
class _Entry:
    pid: int
    session_id: str | None
    proc_start: str | None
    pid_domain: str | None
    name: str | None
    pane: str | None
    has_socket: bool
    spare: bool
    parked: bool
    parked_job: str | None
    job_id: str | None


@dataclass
class _Registry:
    entries: list[_Entry]
    # (parsed object, the pid its FILE is named for). A file name is only a
    # claim, so it is used one way only: to show an entry cannot be a live peer.
    malformed: list[tuple[dict, int]]
    unreadable: int  # files that could not be read or parsed
    # (pid the file is named for, its mtime or None) for each unreadable file.
    unreadable_files: list[tuple[int, float | None]] = field(default_factory=list)
    complete: bool = True  # False when a deadline stopped the scan part way


def registry_dir() -> Path:
    """Claude Code's session registry: ``$CLAUDE_CONFIG_DIR`` or ``~/.claude``."""
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(base) / "sessions"


def own_pid_domain(
    proc_root: Path = Path("/proc"),
    machine_id_path: Path = Path("/etc/machine-id"),
) -> str:
    """This process's pid domain, spelled exactly as Claude Code spells it."""
    try:
        machine_id = machine_id_path.read_text(encoding="utf-8").strip()
    except OSError:
        machine_id = ""
    try:
        namespace = os.readlink(proc_root / "self" / "ns" / "pid")
    except OSError:
        namespace = ""
    return f"linux:{machine_id}:{namespace}"


def _proc_start(proc_root: Path, pid: int) -> tuple[str | None, str]:
    """``(start ticks, why)`` for a pid; ticks is None when it is not live."""
    try:
        raw = (proc_root / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except PermissionError:
        return None, "unreadable"
    except OSError:
        return None, "dead"
    # comm (field 2) is parenthesised and may itself contain ") ", so the
    # fields are split after the LAST ')'. Index 0 is then the state (field 3)
    # and index 19 is the start time (field 22).
    close = raw.rfind(")")
    fields = raw[close + 2 :].split() if close >= 0 else []
    if len(fields) < 20:
        return None, "unreadable"
    if fields[0] in ("Z", "X"):
        return None, "dead"
    return fields[19], "live"


def _optional_str(obj: dict, key: str) -> tuple[bool, str | None]:
    """``(ok, value)``: absent or None is fine; present and not a str is not."""
    value = obj.get(key)
    if value is None:
        return True, None
    return isinstance(value, str), value if isinstance(value, str) else None


def _parse(obj: object) -> _Entry | None:
    """An entry, or None when a field this reader uses is present but mistyped."""
    if not isinstance(obj, dict):
        return None
    pid = obj.get("pid")
    if not (isinstance(pid, int) and not isinstance(pid, bool) and pid > 1):
        return None
    # Every field that drives selection or output is type-checked, so a changed
    # shape fails loudly instead of quietly selecting differently.
    spare = obj.get("spare")
    if spare is not None and not isinstance(spare, bool):
        return None
    values = {}
    for key in (
        "sessionId",
        "procStart",
        "pidDomain",
        "name",
        "tmux",
        "jobId",
        "parkedJobId",
        "messagingSocketPath",
    ):
        ok, values[key] = _optional_str(obj, key)
        if not ok:
            return None
    start = values["procStart"]
    if start is not None and not _DIGITS.fullmatch(start):
        return None
    sock = values["messagingSocketPath"]
    parked = values["parkedJobId"]
    return _Entry(
        pid=pid,
        session_id=values["sessionId"],
        proc_start=start,
        pid_domain=values["pidDomain"],
        name=values["name"] or None,
        pane=values["tmux"] or None,
        has_socket=bool(sock),
        spare=spare is True,
        parked=parked is not None,
        parked_job=parked or None,
        job_id=values["jobId"],
    )


def _read_entry(path: Path) -> bytes | None:
    """An entry file's bytes, or None when it is not a plain, small file.

    Opened without blocking and without following a symlink, then checked to be
    a regular file: a FIFO named like an entry would otherwise block the read
    until something wrote to it, hanging every caller (the per-prompt hook
    included). Claude Code writes these files itself, as regular files.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = os.read(fd, _MAX_ENTRY_BYTES + 1)
    finally:
        os.close(fd)
    return None if len(data) > _MAX_ENTRY_BYTES else data


def _load(directory: Path, deadline: float | None = None) -> _Registry | None:
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return None
    reg = _Registry(entries=[], malformed=[], unreadable=0)
    for fname in names:
        if not _ENTRY_FILE.fullmatch(fname):
            continue
        if deadline is not None and time.monotonic() >= deadline:
            reg.complete = False
            break
        try:
            data = _read_entry(directory / fname)
            if data is None:
                raise ValueError("not a plain, small file")
            obj = json.loads(data)
        except (OSError, ValueError):
            # A torn write, a racing delete, or not a regular file. Counted,
            # never silently skipped.
            reg.unreadable += 1
            try:
                mtime: float | None = os.lstat(directory / fname).st_mtime
            except OSError:
                mtime = None
            reg.unreadable_files.append((int(fname.split(".", 1)[0]), mtime))
            continue
        entry = _parse(obj)
        if entry is None:
            file_pid = int(fname.split(".", 1)[0])
            reg.malformed.append((obj if isinstance(obj, dict) else {}, file_pid))
        else:
            reg.entries.append(entry)
    return reg


def _verdict(entry: _Entry, own: str, proc_root: Path) -> str:
    """Why an entry is or is not a live address. Only "live" is one."""
    if entry.spare:
        return "spare"
    if entry.parked:
        return "parked"
    if not entry.has_socket:
        return "no-socket"
    if entry.pid_domain is None or entry.proc_start is None:
        return "unverifiable"
    if entry.pid_domain != own:
        return "other-domain"
    start, why = _proc_start(proc_root, entry.pid)
    if start is None:
        return why
    return "live" if start == entry.proc_start else "recycled"


def _unaddressable(obj: dict, file_pid: int, own: str, proc_root: Path) -> bool:
    """True only when a malformed entry provably cannot be a live peer here.

    Uses the entry's own pid when it has a usable one, else the pid its file is
    named for (Claude Code names each file for its process).
    """
    domain = obj.get("pidDomain")
    if isinstance(domain, str) and domain != own:
        return True
    pid = obj.get("pid")
    if not (isinstance(pid, int) and not isinstance(pid, bool) and pid > 1):
        pid = file_pid
    return _proc_start(proc_root, pid)[1] == "dead"


def _boot_time(proc_root: Path) -> int | None:
    """Kernel boot time (``btime`` in /proc/stat), or None."""
    try:
        for line in (proc_root / "stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _could_hide_a_peer(pid: int, mtime: float | None, proc_root: Path, btime: int | None) -> bool:
    """Whether an unreadable file named for ``pid`` might be a live peer's entry.

    Not when the pid is dead, and not when the file was last written before
    that process started (so it was written for an earlier process with the
    same number). Birth is ``btime + start ticks / CLK_TCK``, the same sum
    Claude Code uses; MEASURED to match ``ps -o lstart`` inside a container.
    """
    ticks, _why = _proc_start(proc_root, pid)
    if ticks is None:
        return _why != "dead"
    if mtime is None or btime is None:
        return True
    born = btime + int(ticks) / os.sysconf("SC_CLK_TCK")
    return mtime >= born - 1.0


def _expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _via_job(entry: _Entry) -> tuple[str, ...]:
    """Detail naming the background job, with its id only in allowlisted form."""
    job = entry.job_id or ""
    if _DISPLAY_JOB.fullmatch(job):
        return (f"moved to background job {job}",)
    return ("moved to a background job",)


def resolve_many(
    session_ids: list[str] | tuple[str, ...],
    *,
    directory: Path | None = None,
    proc_root: Path = Path("/proc"),
    machine_id_path: Path = Path("/etc/machine-id"),
    deadline: float | None = None,
) -> dict[str, Resolution]:
    """Resolve several ids with ONE read of the registry.

    ``deadline`` is a ``time.monotonic()`` value. A scan it cuts short answers
    nothing (``no-registry``) rather than calling a peer it never reached "not
    reachable".
    """
    if not session_ids:
        return {}
    reg = _load(directory if directory is not None else registry_dir(), deadline)
    if reg is None or not reg.complete:
        why = "registry not readable" if reg is None else "registry scan ran out of time"
        return {sid: Resolution(sid, NO_REGISTRY, detail=(why,)) for sid in session_ids}
    own = own_pid_domain(proc_root, machine_id_path)

    timed_out = {
        sid: Resolution(sid, NO_REGISTRY, detail=("registry scan ran out of time",))
        for sid in session_ids
    }
    # Every loop that touches /proc checks the deadline: the hook calls this
    # synchronously, where the outer asyncio timeout cannot interrupt it.
    # Keyed by the entry, not the pid: the file name and the pid field are
    # separate claims, and nothing here depends on them agreeing.
    verdicts: dict[int, str] = {}
    for e in reg.entries:
        if _expired(deadline):
            return timed_out
        verdicts[id(e)] = _verdict(e, own, proc_root)
    live_names = Counter(e.name for e in reg.entries if verdicts[id(e)] == "live" and e.name)
    # Dead entries are never cleaned out of the registry, so one written by an
    # old Claude Code in an older shape would otherwise mark every miss as a
    # format change forever. Only an entry whose process may still be running
    # can be hiding a live address. The same holds for a file that could not be
    # read at all: unless the pid it is named for is dead, it may be a second
    # live candidate, so no answer can be definitive.
    suspect = []
    for m, file_pid in reg.malformed:
        if _expired(deadline):
            return timed_out
        if not _unaddressable(m, file_pid, own, proc_root):
            suspect.append(m)
    blind = []
    btime = _boot_time(proc_root) if reg.unreadable_files else None
    for pid, mtime in reg.unreadable_files:
        if _expired(deadline):
            return timed_out
        if _could_hide_a_peer(pid, mtime, proc_root, btime):
            blind.append(pid)

    out: dict[str, Resolution] = {}
    for sid in session_ids:
        if any(m.get("sessionId") == sid for m in suspect):
            out[sid] = Resolution(
                sid, FORMAT_CHANGED, detail=("a matching entry has a field of the wrong type",)
            )
            continue
        mine = [e for e in reg.entries if e.session_id == sid]
        jobs = {e.parked_job for e in mine if e.parked_job}
        moved = [e for e in reg.entries if e.session_id != sid and e.job_id and e.job_id in jobs]
        candidates = mine + moved
        hits = [e for e in candidates if verdicts[id(e)] == "live"]
        # Zero hits counts too: Claude Code rewrites a session's own file in
        # place, so a torn read most likely hides the very entry asked for.
        if blind and len(hits) <= 1:
            out[sid] = Resolution(
                sid,
                AMBIGUOUS,
                names=(hits[0].name,) if hits and hits[0].name else (),
                detail=tuple(f"pid {p}: registry file unreadable, process running" for p in blind),
            )
            continue
        if len(hits) == 1:
            hit = hits[0]
            via = _via_job(hit) if any(hit is e for e in moved) else ()
            if hit.name is None:
                out[sid] = Resolution(sid, UNNAMED, pid=hit.pid, pane=hit.pane, detail=via)
            else:
                out[sid] = Resolution(
                    sid,
                    OK,
                    name=hit.name,
                    pane=hit.pane,
                    pid=hit.pid,
                    shared_name=live_names[hit.name] > 1,
                    detail=via,
                )
            continue
        if len(hits) > 1:
            out[sid] = Resolution(sid, AMBIGUOUS, names=tuple(sorted(e.name or "?" for e in hits)))
            continue
        detail = [f"pid {e.pid}: {verdicts[id(e)]}" for e in candidates] or ["no registry entry"]
        if suspect:
            # An entry this reader cannot parse might be the one asked for.
            out[sid] = Resolution(
                sid,
                FORMAT_CHANGED,
                detail=(*detail, f"{len(suspect)} live entries have a field of the wrong type"),
            )
            continue
        if reg.unreadable:
            detail.append(f"{reg.unreadable} registry files unreadable")
        out[sid] = Resolution(sid, NOT_REACHABLE, detail=tuple(detail))
    return out


def resolve(session_id: str, **kwargs) -> Resolution:
    """Resolve one id; see :func:`resolve_many`."""
    return resolve_many([session_id], **kwargs)[session_id]


def render(res: Resolution) -> str:
    """The display suffix for a Concurrent line, or "" when there is nothing to say.

    Built only from allowlisted text and fixed phrases, and the phrases avoid
    the tag grammar's own characters ('[', ']', '|'), so a peer-authored name
    or pane can never add a line or a tag field.
    """
    if res.status == NO_REGISTRY:
        return ""
    if res.status == OK and res.name:
        if not _display_name(res.name):
            return "-> (name not shown; see session_address)"
        text = f"-> {res.name}"
        if _display_pane(res.pane):
            text += f" ({res.pane})"
        if res.shared_name:
            text += " (shared name)"
        return text
    return {
        UNNAMED: "-> (no peer name)",
        AMBIGUOUS: "-> (ambiguous)",
        FORMAT_CHANGED: "-> (registry format changed)",
    }.get(res.status, "-> (not reachable)")
