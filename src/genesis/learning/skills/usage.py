"""Skill-usage telemetry over Claude Code transcripts (issue #3197).

Read-only glue over files Claude Code already writes: no hook on the hot path,
no new store. For every known skill it reports loads, slash loads, SKILL.md
reads, skill-injection nudges, dispatched injections (``cc_sessions``
``skill_tags``), nudge-to-use conversion and a state:

- ``active``: chosen at least once in the window (a tool load, a slash load or
  a SKILL.md read);
- ``zombie``: nudged at least once, never chosen;
- ``dead``: the nudge hook can offer it (it is in the catalog), neither nudged
  nor used;
- ``not_nudgeable``: neither nudged nor used, and the hook cannot nudge it
  (user-level and plugin skills, which Claude Code still lists).

Configured deliveries never decide the state (owner ruling, 2026-10-20): a
dispatched session's ``skill_tags`` injection and an agent-frontmatter preload
are reported as their own ``injected`` and ``preloads`` columns, so a skill the
dispatcher injects but no session ever chooses still reads as a zombie.

Every transcript count is split ``interactive`` (entrypoint ``cli``) vs
``headless`` (``sdk-cli``, ``sdk-py``, …), because headless test and dispatch
runs outnumber interactive sessions many times over.

Signals, as recorded by Claude Code (shapes read from live transcripts,
CC 2.1.218-2.1.301):

- tool load: an assistant ``tool_use`` named ``Skill``, skill in ``input.skill``;
- slash load: a ``user`` record with ``isMeta`` and NO ``sourceToolUseID`` whose
  text starts ``Base directory for this skill: <path>`` (a tool load emits the
  same record WITH ``sourceToolUseID``, so it is not counted twice);
- preload: an ``isMeta`` record carrying ``<skill-format>true</skill-format>``
  and the base-directory line (skills listed in an agent's frontmatter);
- read: a ``Read`` of a ``SKILL.md``, unless the same session edits that file.
  Reads through Bash (``cat``, ``sed``) are not counted;
- nudge: a hook attachment whose command is ``skill_injection_hook.py``.

``invoked_skills`` attachments (post-compaction re-injection) are not signals.
A slash command typed by the user leaves no base-directory record, so a command
used only by typing is not counted.

Run ``python -m genesis.learning.skills.usage --days 14`` for the baseline; the
``skill_usage_status`` health MCP tool wraps the same scan.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import stat
import sys
import time
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

INTERACTIVE_ENTRYPOINTS = frozenset({"cli"})

# A line is parsed only if it holds one of these. Spacing-tolerant on purpose:
# CC writes compact JSON, other writers may not.
_PREFILTER = (b'"Skill"', b"Base directory for this skill", b"skill_injection_hook", b"SKILL.md")
_ENTRYPOINT_RE = re.compile(rb'"entrypoint":\s*"([^"]+)"')
_SESSION_RE = re.compile(rb'"sessionId":\s*"([^"]+)"')

# The nudge forms scripts/hooks/skill_injection_hook.py emits. Its test drives
# the real hook, so a wording change there fails a test here.
_NAMED_NUDGE = re.compile(r"^\[Skill\] The '([^']+)' skill")
_PROCESS_NUDGE = re.compile(r"^\[Process\] .*?\b(superpowers:[\w-]+)")
_PATH_NUDGE = re.compile(r"^\[Skill\] A skill matches this task\. Read (\S+)/SKILL\.md")
_BASE_DIR = "Base directory for this skill: "
_PRELOAD_MARK = "<skill-format>true</skill-format>"
_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

_INJECTED_SQL = (
    "SELECT metadata, status FROM cc_sessions "
    "WHERE metadata LIKE '%skill_tags%' AND started_at >= ?"
)
_STATE_ORDER = {"active": 0, "zombie": 1, "dead": 2, "not_nudgeable": 3}

# A skill name taken from a transcript or a skill_tags row is model- or
# writer-controlled text, and this report goes back into a model's context. Only
# identifier-shaped names become rows (real ones: "genesis-development",
# "superpowers:test-driven-development", "user_evaluate"); anything else is
# counted in names_invalid. Rows are not capped: valid names are bounded by the
# skills that exist, and a cap would hide real data.
_NAME_RE = re.compile(r"[A-Za-z0-9._:-]{1,120}")

# Safety cap on one transcript line. The largest line measured on a live install
# was 2.4 MB (2026-10-20); 64 MiB is 27x that, so it never drops a real record,
# and it keeps a line with no newline from being read whole into memory. Longer
# lines are skipped and counted in lines_oversized.
MAX_LINE_BYTES = 64 * 1024 * 1024

# The skills the hook nudges through its [Process] lines rather than the catalog.
# They are nudge-eligible although they are plugin skills; the real-hook test
# asserts the hook still emits exactly these.
PROCESS_NUDGE_TARGETS = frozenset(
    {"superpowers:brainstorming", "superpowers:test-driven-development"}
)

# Longest accepted window. Ten years is far past any transcript retention, so it
# never limits a real question; it exists to reject nonsense (1e7 overflows).
MAX_DAYS = 3650.0


def validate_days(days: float) -> float:
    """``days`` if it is a usable window length, else ValueError."""
    if (
        isinstance(days, bool)
        or not isinstance(days, int | float)
        or not 0 < days <= MAX_DAYS  # also rejects NaN and inf
    ):
        raise ValueError(f"days must be a number in (0, {MAX_DAYS:g}], got {days!r}")
    return float(days)


# --- skill population ---------------------------------------------------------


@dataclass
class Population:
    """Every skill we know of, and how to name a skill from its directory."""

    skills: dict[str, dict] = field(default_factory=dict)
    sources: list[dict] = field(default_factory=list)
    _abs_dirs: dict[str, str] = field(default_factory=dict)
    _rel_dirs: list[tuple[str, str]] = field(default_factory=list)
    _plugin_roots: list[tuple[str, str]] = field(default_factory=list)

    def _add(self, name: str, source: str, *, nudge_eligible: bool) -> bool:
        """Register a skill; True only when the name is new."""
        if name in self.skills:
            return False
        self.skills[name] = {"source": source, "nudge_eligible": nudge_eligible}
        return True

    def resolve_dir(self, directory: str) -> str:
        """Skill name for a skill directory (absolute, worktree or repo-relative)."""
        d = directory.rstrip("/")
        if d in self._abs_dirs:
            return self._abs_dirs[d]
        for root, plugin in self._plugin_roots:
            prefix = root.rstrip("/") + "/skills/"
            if d.startswith(prefix) and "/" not in d[len(prefix) :]:
                return f"{plugin}:{d[len(prefix) :]}"
        for rel, name in self._rel_dirs:
            if d == rel or d.endswith("/" + rel):
                return name
        return os.path.basename(d)


def load_population(
    *,
    catalog_path: Path,
    user_skills_dir: Path,
    plugins_manifest: Path,
    commands_dirs: Iterable[Path] = (),
) -> Population:
    """The nudge catalog (nudge-eligible), user-level skills, plugin skills and
    slash commands (the Skill tool loads commands too). Each source reports how
    many NEW names it added, so a name shared by two sources counts once."""
    pop = Population()

    found = 0
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        catalog = {}
    for tier in ("tier1", "tier2"):
        for entry in catalog.get(tier) or []:
            name, path = entry.get("name"), str(entry.get("path") or "")
            if not name:
                continue
            found += pop._add(name, f"catalog-{tier}", nudge_eligible=True)
            if path.startswith("/"):
                pop._abs_dirs[path.rstrip("/")] = name
            elif path:
                pop._rel_dirs.append((path.rstrip("/"), name))
    pop.sources.append({"source": "catalog", "path": str(catalog_path), "skills": found})

    found = 0
    if user_skills_dir.is_dir():
        for entry in sorted(os.scandir(user_skills_dir), key=lambda e: e.name):
            if entry.is_dir() and os.path.isfile(os.path.join(entry.path, "SKILL.md")):
                found += pop._add(entry.name, "user", nudge_eligible=False)
                pop._abs_dirs.setdefault(entry.path.rstrip("/"), entry.name)
    pop.sources.append({"source": "user", "path": str(user_skills_dir), "skills": found})

    found = 0
    try:
        plugins = json.loads(plugins_manifest.read_text(encoding="utf-8")).get("plugins") or {}
    except (OSError, ValueError, AttributeError):
        plugins = {}
    for key, installs in plugins.items():
        plugin = str(key).split("@", 1)[0]
        for install in installs if isinstance(installs, list) else [installs]:
            root = isinstance(install, dict) and install.get("installPath")
            if not root:
                continue
            pop._plugin_roots.append((root, plugin))
            skills_dir = Path(root) / "skills"
            if not skills_dir.is_dir():
                continue
            for entry in sorted(os.scandir(skills_dir), key=lambda e: e.name):
                if entry.is_dir() and os.path.isfile(os.path.join(entry.path, "SKILL.md")):
                    found += pop._add(f"{plugin}:{entry.name}", "plugin", nudge_eligible=False)
    pop.sources.append({"source": "plugins", "path": str(plugins_manifest), "skills": found})
    for name in PROCESS_NUDGE_TARGETS & pop.skills.keys():
        pop.skills[name]["nudge_eligible"] = True

    for commands_dir in commands_dirs:
        found = 0
        if commands_dir.is_dir():
            for entry in sorted(os.scandir(commands_dir), key=lambda e: e.name):
                if entry.is_file() and entry.name.endswith(".md"):
                    found += pop._add(entry.name[:-3], "command", nudge_eligible=False)
        pop.sources.append({"source": "commands", "path": str(commands_dir), "skills": found})
    return pop


def _default_population() -> Population:
    from genesis.env import claude_home, genesis_home, repo_root

    return load_population(
        catalog_path=genesis_home() / "skill_catalog.json",
        user_skills_dir=claude_home() / "skills",
        plugins_manifest=claude_home() / "plugins" / "installed_plugins.json",
        commands_dirs=(repo_root() / ".claude" / "commands", claude_home() / "commands"),
    )


# --- nudges -----------------------------------------------------------------------


def parse_nudges(stdout: str) -> tuple[list[tuple[str, str]], int]:
    """``([(name_or_dir, form), …], unparsed)`` from one hook's stdout.

    ``form`` is ``named``, ``process`` or ``path`` (the value is then a skill
    directory to resolve). A line that starts like a nudge but matches no form
    (the hook's unnamed fallback included) is counted as unparsed.
    """
    found: list[tuple[str, str]] = []
    unparsed = 0
    for line in stdout.splitlines():
        line = line.strip()
        if m := _NAMED_NUDGE.match(line):
            found.append((m.group(1), "named"))
        elif m := _PROCESS_NUDGE.match(line):
            found.append((m.group(1), "process"))
        elif m := _PATH_NUDGE.match(line):
            found.append((m.group(1), "path"))
        elif line.startswith(("[Skill]", "[Process]")):
            unparsed += 1
    return found, unparsed


# --- transcript scan ----------------------------------------------------------------


def iter_transcript_files(projects_root: Path, since: datetime) -> Iterator[tuple[Path, bool]]:
    """``(path, is_subagent)`` for transcripts modified at or after ``since``.

    One ``scandir`` per directory, never a recursive walk: top-level
    ``*.jsonl`` and ``<session>/subagents/*.jsonl``.
    """
    cutoff = since.timestamp()
    try:
        projects = sorted(os.scandir(projects_root), key=lambda e: e.name)
    except OSError:
        return
    for project in projects:
        if not project.is_dir():
            continue
        try:
            entries = sorted(os.scandir(project.path), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_file() and entry.name.endswith(".jsonl"):
                    if entry.stat().st_mtime >= cutoff:
                        yield Path(entry.path), False
                elif entry.is_dir():
                    sub = os.path.join(entry.path, "subagents")
                    if not os.path.isdir(sub):
                        continue
                    for agent in sorted(os.scandir(sub), key=lambda e: e.name):
                        try:
                            if (
                                agent.name.endswith(".jsonl")
                                and agent.is_file()
                                and agent.stat().st_mtime >= cutoff
                            ):
                                yield Path(agent.path), True
                        except OSError:
                            continue
            except OSError:
                continue


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


@dataclass
class _Session:
    kind: str | None = None
    kind_from_parent: bool = False
    uses: list[tuple[datetime, str, str]] = field(default_factory=list)  # (ts, signal, value)
    nudges: list[tuple[datetime, str, str]] = field(default_factory=list)  # (ts, form, value)
    edited: set[str] = field(default_factory=set)


class Scanner:
    """Accumulates signals file by file, so a caller can yield between files."""

    def __init__(self, since: datetime, until: datetime) -> None:
        self.since, self.until = since, until
        self.sessions: dict[str, _Session] = defaultdict(_Session)
        self.files_scanned = 0
        self.subagent_files = 0
        self.bytes_scanned = 0
        self.lines_unparsed = 0
        self.lines_untimestamped = 0
        self.nudges_unparsed = 0
        self.lines_oversized = 0

    def scan_file(self, path: Path, is_subagent: bool = False) -> None:
        try:
            # O_NONBLOCK: a FIFO named *.jsonl must not block the scan (and with it
            # the MCP tool's single-flight lock) forever; only regular files are read.
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            return
        try:
            info = os.fstat(fd)
        except OSError:
            os.close(fd)
            return
        if not stat.S_ISREG(info.st_mode):
            os.close(fd)
            return
        budget = info.st_size  # fixed before reading: appends are left for later
        fh = os.fdopen(fd, "rb")
        self.files_scanned += 1
        self.subagent_files += int(is_subagent)
        file_sid: str | None = None
        consumed = 0
        kind_seen = False
        with fh:
            while (remaining := budget - consumed) > 0:
                line = fh.readline(min(remaining, MAX_LINE_BYTES))  # never past the budget
                if not line.endswith(b"\n"):
                    if len(line) < MAX_LINE_BYTES:
                        break  # still being written
                    consumed += len(line)
                    while (rest := budget - consumed) > 0:  # drain the oversized line
                        chunk = fh.readline(min(rest, MAX_LINE_BYTES))
                        consumed += len(chunk)
                        if not chunk or chunk.endswith(b"\n"):
                            break
                    self.lines_oversized += 1
                    continue
                consumed += len(line)
                self.bytes_scanned += len(line)
                if file_sid is None and (m := _SESSION_RE.search(line)):
                    file_sid = m.group(1).decode()
                    self.sessions[file_sid]  # register the session even if it holds no signal
                if file_sid is not None and not kind_seen and (m := _ENTRYPOINT_RE.search(line)):
                    kind_seen = True
                    session = self.sessions[file_sid]
                    # The parent transcript decides; a subagent file only fills a gap.
                    if not is_subagent:
                        session.kind, session.kind_from_parent = m.group(1).decode(), True
                    elif session.kind is None:
                        session.kind = m.group(1).decode()
                if not any(token in line for token in _PREFILTER):
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    self.lines_unparsed += 1
                    continue
                if isinstance(record, dict):
                    self._record(record, file_sid or path.stem)
        if file_sid is None:
            self.sessions[path.stem]

    def _record(self, record: dict, default_sid: str) -> None:
        signals = list(self._signals(record))
        if not signals:
            return
        stamp = _parse_ts(record.get("timestamp"))
        if stamp is None:
            self.lines_untimestamped += 1
            return
        if not (self.since <= stamp <= self.until):
            return
        session = self.sessions[str(record.get("sessionId") or default_sid)]
        for signal, value in signals:
            if signal == "edit":
                session.edited.add(value)
            elif signal == "nudge":
                found, unparsed = parse_nudges(value)
                self.nudges_unparsed += unparsed
                session.nudges.extend((stamp, form, name) for name, form in found)
            else:
                session.uses.append((stamp, signal, value))

    @staticmethod
    def _signals(record: dict) -> Iterator[tuple[str, str]]:
        kind = record.get("type")
        if kind == "assistant":
            content = (record.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tool, data = block.get("name"), block.get("input") or {}
                if not isinstance(data, dict):
                    continue
                if tool == "Skill" and isinstance(data.get("skill"), str):
                    yield "load", data["skill"]
                    continue
                file_path = data.get("file_path")
                if isinstance(file_path, str) and file_path.endswith("/SKILL.md"):
                    if tool == "Read":
                        yield "read", os.path.normpath(file_path)
                    elif tool in _EDIT_TOOLS:
                        yield "edit", os.path.normpath(file_path)
        elif kind == "user" and record.get("isMeta") and "sourceToolUseID" not in record:
            text = _text_of((record.get("message") or {}).get("content"))
            if text.startswith(_BASE_DIR):
                yield "slash", text[len(_BASE_DIR) :].split("\n", 1)[0].strip()
            elif _PRELOAD_MARK in text and _BASE_DIR in text:
                yield "preload", text.split(_BASE_DIR, 1)[1].split("\n", 1)[0].strip()
        elif kind == "attachment":
            attachment = record.get("attachment") or {}
            if "skill_injection_hook" in str(attachment.get("command") or ""):
                yield "nudge", str(attachment.get("stdout") or attachment.get("content") or "")


# --- dispatched injections (cc_sessions) --------------------------------------------


def count_injected(rows: Iterable[tuple[str | None, str | None]]) -> dict:
    """Per-tag counts from ``(metadata, status)`` rows carrying ``skill_tags``."""
    per_skill: dict[str, dict[str, int]] = defaultdict(
        lambda: {"injected": 0, "injected_failed": 0}
    )
    sessions = 0
    for metadata, status in rows:
        try:
            tags = json.loads(metadata or "").get("skill_tags") or []
        except (ValueError, AttributeError):
            continue
        tags = [t for t in tags if isinstance(t, str) and t]
        if not tags:
            continue
        sessions += 1
        for tag in set(tags):
            per_skill[tag]["injected"] += 1
            if status == "failed":
                per_skill[tag]["injected_failed"] += 1
    return {"per_skill": dict(per_skill), "sessions": sessions}


def read_injected_sync(db_path: Path, since: datetime) -> dict | None:
    """``count_injected`` over the live DB, read-only; None if it cannot be read."""
    if not Path(db_path).is_file():
        return None
    try:
        # as_uri() percent-encodes the path, so a "?", "#" or "%" in it cannot
        # change the URI's meaning or drop mode=ro.
        con = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            rows = con.execute(_INJECTED_SQL, (since.isoformat(),)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return count_injected(rows)


async def read_injected(db, since: datetime) -> dict:
    """``count_injected`` over an open aiosqlite connection."""
    cursor = await db.execute(_INJECTED_SQL, (since.isoformat(),))
    return count_injected(await cursor.fetchall())


# --- report ---------------------------------------------------------------------------


def _triple() -> dict[str, int]:
    return {"interactive": 0, "headless": 0, "total": 0}


def _bump(counter: dict[str, int], kind: str, n: int = 1) -> None:
    counter[kind] += n
    counter["total"] += n


def build_report(
    scanner: Scanner,
    population: Population,
    injected: dict | None,
    *,
    duration_s: float,
) -> dict:
    rows: dict[str, dict] = {}
    names_invalid = 0

    def valid(name: str) -> bool:
        nonlocal names_invalid
        if _NAME_RE.fullmatch(name):
            return True
        names_invalid += 1
        return False

    def row(name: str) -> dict:
        if name not in rows:
            info = population.skills.get(name)
            rows[name] = {
                "name": name,
                "source": info["source"] if info else None,
                "uncatalogued": info is None,
                "nudge_eligible": bool(info and info["nudge_eligible"]),
                "loads": _triple(),
                "slash_loads": _triple(),
                "reads": _triple(),
                "preloads": _triple(),
                "nudges": _triple(),
                "sessions_nudged": _triple(),
                "sessions_used": _triple(),
                "injected": 0,
                "injected_failed": 0,
                "_converted": _triple(),
                "_last": None,
            }
        return rows[name]

    for name in population.skills:
        row(name)

    sessions = _triple()
    unknown_entrypoint = 0
    for session in scanner.sessions.values():
        if session.kind is None:
            unknown_entrypoint += 1
        kind = "interactive" if session.kind in INTERACTIVE_ENTRYPOINTS else "headless"
        _bump(sessions, kind)

        used_at: dict[str, list[datetime]] = defaultdict(list)
        for stamp, signal, value in session.uses:
            if signal == "read" and value in session.edited:
                continue  # editing a skill is not using it
            if signal == "preload":  # configured, not chosen: counted, never a use
                if valid(name := population.resolve_dir(value)):
                    _bump(row(name)["preloads"], kind)
                continue
            if signal == "load":
                name, counter = value, "loads"
            elif signal == "slash":
                name, counter = population.resolve_dir(value), "slash_loads"
            else:
                name, counter = population.resolve_dir(os.path.dirname(value)), "reads"
            if not valid(name):
                continue
            r = row(name)
            _bump(r[counter], kind)
            used_at[name].append(stamp)
            if r["_last"] is None or stamp > r["_last"]:
                r["_last"] = stamp

        first_nudge: dict[str, datetime] = {}
        for stamp, form, value in session.nudges:
            name = population.resolve_dir(value) if form == "path" else value
            if not valid(name):
                continue
            _bump(row(name)["nudges"], kind)
            if name not in first_nudge or stamp < first_nudge[name]:
                first_nudge[name] = stamp

        for name in used_at:
            _bump(rows[name]["sessions_used"], kind)
        for name, nudged_at in first_nudge.items():
            r = rows[name]
            _bump(r["sessions_nudged"], kind)
            if any(t >= nudged_at for t in used_at.get(name, ())):
                _bump(r["_converted"], kind)

    uncatalogued_tags: dict[str, int] = {}
    if injected is not None:
        for tag, counts in injected["per_skill"].items():
            if not valid(tag):
                continue
            if tag in population.skills or tag in rows:
                r = row(tag)
                r["injected"] += counts["injected"]
                r["injected_failed"] += counts["injected_failed"]
            else:
                uncatalogued_tags[tag] = counts["injected"]

    out = []
    for r in rows.values():
        if r["sessions_used"]["total"]:
            state = "active"
        elif r["nudges"]["total"]:
            state = "zombie"
        elif r["nudge_eligible"]:
            state = "dead"
        else:
            state = "not_nudgeable"
        converted, nudged = r.pop("_converted"), r["sessions_nudged"]
        r["conversion"] = {
            k: round(converted[k] / nudged[k], 3) if nudged[k] else None for k in nudged
        }
        last = r.pop("_last")
        r["last_fired"] = last.isoformat() if last else None
        r["state"] = state
        out.append(r)
    out.sort(key=lambda r: (_STATE_ORDER[r["state"]], -r["sessions_used"]["total"], r["name"]))

    return {
        "status": "ok",
        "window": {
            "start": scanner.since.isoformat(),
            "end": scanner.until.isoformat(),
            "days": round((scanner.until - scanner.since) / timedelta(days=1), 2),
        },
        "sessions_scanned": sessions,
        "sessions_unknown_entrypoint": unknown_entrypoint,
        "files_scanned": scanner.files_scanned,
        "subagent_files": scanner.subagent_files,
        "bytes_scanned": scanner.bytes_scanned,
        "lines_unparsed": scanner.lines_unparsed,
        "lines_untimestamped": scanner.lines_untimestamped,
        "nudges_unparsed": scanner.nudges_unparsed,
        "lines_oversized": scanner.lines_oversized,
        "names_invalid": names_invalid,
        "duration_s": round(duration_s, 2),
        "db_join": "ok" if injected is not None else "unavailable",
        "injected_sessions": injected["sessions"] if injected is not None else "unavailable",
        "uncatalogued_tags": uncatalogued_tags,
        "catalog_sources": population.sources,
        "skills": out,
        "note": (
            "A zero means none found in the scanned sessions, within the window. "
            "interactive = entrypoint 'cli'; headless = dispatched, SDK and test runs. "
            "The nudge hook nudges a skill at most once per session, so nudges ~= "
            "sessions_nudged. State counts only chosen uses (tool loads, slash loads, "
            "SKILL.md reads); injected (cc_sessions skill_tags, put in a dispatched "
            "session's system prompt) and preloads (agent frontmatter) are shown but "
            "never decide it. Not counted: SKILL.md reads through Bash, and slash "
            "commands typed by the user (they leave no record of which command ran)."
        ),
    }


def run_scan(
    *,
    projects_root: Path,
    since: datetime,
    until: datetime,
    population: Population,
    injected: dict | None,
) -> dict:
    started = time.monotonic()
    scanner = Scanner(since, until)
    for path, is_subagent in iter_transcript_files(projects_root, since):
        scanner.scan_file(path, is_subagent)
    return build_report(scanner, population, injected, duration_s=time.monotonic() - started)


# --- CLI ------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    from genesis.env import claude_home, genesis_db_path

    parser = argparse.ArgumentParser(
        prog="python -m genesis.learning.skills.usage", description=__doc__.split("\n", 1)[0]
    )
    parser.add_argument("--days", type=float, default=14.0, help="window length (default 14)")
    parser.add_argument("--projects-root", type=Path, default=None, help="Claude Code projects dir")
    parser.add_argument(
        "--db", type=Path, default=None, help="genesis.db for skill_tags (read-only)"
    )
    parser.add_argument("--out", type=Path, default=None, help="write JSON here instead of stdout")
    args = parser.parse_args(argv)
    try:
        validate_days(args.days)
    except ValueError as exc:
        parser.error(str(exc))

    until = datetime.now(UTC)
    since = until - timedelta(days=args.days)
    report = run_scan(
        projects_root=args.projects_root or claude_home() / "projects",
        since=since,
        until=until,
        population=_default_population(),
        injected=read_injected_sync(args.db or genesis_db_path(), since),
    )
    text = json.dumps(report, indent=2)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    else:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
