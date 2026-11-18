"""edge_transcripts MCP tool — read transcripts where they sit on the edge device (#4856).

One call answers "what was said on day X": it lists that day's transcript files, or
greps them for a phrase, over the edge connection in ``~/.genesis/ambient_remote.yaml``.
Nothing is copied or stored by this tool. The lines it returns do land in the calling
session's transcript (and so in its backups), as a manual read of the same files would.

The directory is the config's ``transcripts_dir`` (no default: an install that keeps
none is "not configured"). Files are named by their UTC start time,
``YYYYMMDDTHHMMSS….md``; a day is the user's local day, which can span two UTC dates.

Every remote command is a fixed string: the date is validated by full match, the
directory and the query are shell-quoted, the query is a fixed string after ``-e``
and ``--``, file names must match the name pattern, and output is capped on both ends.
Reflection sessions do not get this tool (it is not on their read allowlist).
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)

_NAME = re.compile(r"^(\d{8}T\d{6})[A-Za-z0-9_.-]*\.md$")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# Output caps: a day's transcripts measured at under 60 KB each on a live edge; a
# search result past these is cut, and the result says so.
_REMOTE_CAP_BYTES = 262_144
_MAX_LINES_CEILING = 500
_SSH_TIMEOUT_S = 30


def _config():
    from genesis.observability.ambient_health import load_ambient_remote_config

    return load_ambient_remote_config()


def _ssh(cfg) -> list[str]:
    from genesis.attention.snapshot import _ssh_base

    return _ssh_base(cfg)


async def _remote(cfg, command: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *_ssh(cfg),
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_SSH_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, ""
    if proc.returncode not in (0, 1):  # grep exits 1 for "no match"
        logger.warning(
            "edge_transcripts remote command failed: %s", err.decode(errors="replace")[-300:]
        )
    return proc.returncode or 0, out.decode(errors="replace")


def _local_day(day: str | None) -> date:
    from genesis.env import user_timezone

    tz = ZoneInfo(user_timezone())

    if day is None:
        return datetime.now(tz).date()
    if not _DATE.fullmatch(day):
        raise ValueError("date must be YYYY-MM-DD")
    return date.fromisoformat(day)


def _in_day(name: str, day: date) -> bool:
    from genesis.env import user_timezone

    tz = ZoneInfo(user_timezone())

    match = _NAME.match(name)
    if not match:
        return False
    started = datetime.strptime(match[1], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    start = datetime.combine(day, time.min, tzinfo=tz)
    return start <= started.astimezone(tz) < start + timedelta(days=1)


_DIR_MISSING = 3  # the remote guard's exit code: the directory is not there


def _remote_path(directory: str) -> str:
    """Shell-quoted, with a leading ``~/`` kept expandable (quoting it disables it).
    Only an absolute or home-relative path is accepted, so no value reaches a
    command as an option."""
    if directory == "~":
        return '"$HOME"'
    if directory.startswith("~/"):
        return '"$HOME"/' + shlex.quote(directory[2:])
    if directory.startswith("/"):
        return shlex.quote(directory)
    raise ValueError("transcripts_dir must be an absolute or ~/ path")


def _utc_prefixes(day: date) -> list[str]:
    """The UTC dates a local day touches, as file-name prefixes (one or two)."""
    from genesis.env import user_timezone

    tz = ZoneInfo(user_timezone())
    start = datetime.combine(day, time.min, tzinfo=tz).astimezone(UTC)
    end = (datetime.combine(day, time.min, tzinfo=tz) + timedelta(days=1)).astimezone(UTC)
    last = (end - timedelta(seconds=1)).date()
    days = {start.date(), last}
    return sorted(d.strftime("%Y%m%d") for d in days)


def _find(directory: str, prefixes: list[str] | None) -> str:
    """One remote pass over regular files only (``-H`` follows a symlinked
    directory itself, never a symlinked file), guarded so a missing directory is
    an error rather than an empty day."""
    path = _remote_path(directory)
    names = " -o ".join(f"-name {shlex.quote(p + '*.md')}" for p in prefixes) if prefixes else "-name '*.md'"
    return f"test -d {path} || exit {_DIR_MISSING}; find -H {path}/ -maxdepth 1 -type f \\( {names} \\)"


async def _run(cfg, command: str) -> tuple[str | None, str]:
    """Run ``command`` capped; return (error, output)."""
    rc, out = await _remote(cfg, f"{command} | head -c {_REMOTE_CAP_BYTES}")
    if rc == 124:
        return "edge did not answer", ""
    if rc == _DIR_MISSING:
        return "transcripts_dir is not a directory on the edge", ""
    if rc not in (0, 1):  # 1 is grep's "no match"; ssh's own failure is 255
        return f"edge command failed (exit {rc})", ""
    return None, out


async def _impl(action: str, date_: str | None, query: str | None, max_lines: int) -> dict:
    try:
        cfg = _config()
    except Exception:  # a present but malformed config; the reason stays in the log
        logger.warning("edge config is invalid", exc_info=True)
        return {"error": "edge config is invalid"}
    directory = getattr(cfg, "transcripts_dir", None) if cfg else None
    if not cfg or not directory:
        return {"status": "not configured", "detail": "no transcripts_dir in ambient_remote.yaml"}
    try:
        day = _local_day(date_)
        _remote_path(directory)
    except ValueError as exc:
        return {"error": str(exc)}
    if action == "status":
        err, out = await _run(cfg, _find(directory, None) + " -printf '%f\\n'")
        if err:
            return {"error": err}
        names = sorted(n for n in out.splitlines() if _NAME.match(n))
        return {"day": day.isoformat(), "files_that_day": sum(_in_day(n, day) for n in names),
                "latest": names[-1] if names else None, "total_files": len(names)}
    if action not in ("list", "search"):
        return {"error": "action must be status, list or search"}
    prefixes = _utc_prefixes(day)
    if action == "list":
        err, out = await _run(cfg, _find(directory, prefixes) + " -printf '%f\\n'")
        if err:
            return {"error": err}
        return {"day": day.isoformat(),
                "files": sorted(n for n in out.splitlines() if _NAME.match(n) and _in_day(n, day))}
    if not query or not query.strip():
        return {"error": "search needs a query"}
    cap = max(1, min(int(max_lines), _MAX_LINES_CEILING))
    grep = f"grep -F -i -n -H -C 2 -e {shlex.quote(query)} --"
    err, out = await _run(cfg, _find(directory, prefixes) + f" -exec {grep} {{}} +")
    if err:
        return {"error": err}
    lines = [ln for ln in out.splitlines() if _match_in_day(ln, day)]
    result = {"day": day.isoformat(), "matches": lines[:cap]}
    if len(lines) > cap or len(out.encode()) >= _REMOTE_CAP_BYTES:
        result["truncated"] = f"kept {min(len(lines), cap)} lines; narrow the query or the day"
    return result


_GREP_LINE = re.compile(r"(\d{8}T\d{6}[A-Za-z0-9_.]*\.md)[:-]\d+[:-]")


def _match_in_day(line: str, day: date) -> bool:
    """A grep -H line (``path:N:text``, or ``path-N-text`` for context) from a file in
    the day; the name is found by its own shape, never by splitting the text."""
    match = _GREP_LINE.search(line)
    return bool(match) and _in_day(match[1], day)


@mcp.tool()
async def edge_transcripts(
    action: str = "status",
    date: str | None = None,
    query: str | None = None,
    max_lines: int = 200,
) -> dict:
    """Read transcripts on the edge device in one call; nothing is stored.

    Args:
        action: "status" (file counts and the latest file), "list" (one day's
            files) or "search" (lines matching ``query`` in one day's files,
            with 2 lines of context).
        date: the user's local day, YYYY-MM-DD; default today.
        query: fixed text to find (case-insensitive), for "search".
        max_lines: result line cap for "search" (at most 500).
    """
    return await _impl(action, date, query, max_lines)
