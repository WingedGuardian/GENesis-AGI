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
import logging
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

logger = logging.getLogger(__name__)

INTERACTIVE_ENTRYPOINTS = frozenset({"cli"})

# A line is parsed only if it holds one of these. Spacing-tolerant on purpose:
# CC writes compact JSON, other writers may not.
# The files that make a directory a skill, as scripts/generate_skill_catalog.py
# _SELF_MARKERS spells them; used for population, prefilter and Read signals.
SKILL_FILES = ("SKILL.md", "skill.md")
_PREFILTER = (
    b'"Skill"',
    b"Base directory for this skill",
    b"skill_injection_hook",
    *(m.encode() for m in SKILL_FILES),
)
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
    "WHERE metadata LIKE '%skill_tags%' AND started_at >= ? AND started_at <= ?"
)

# Where the nudge hook reads its catalog and the generator writes it
# (scripts/hooks/skill_injection_hook.py and scripts/generate_skill_catalog.py,
# both CATALOG_PATH). Neither honors GENESIS_HOME, so neither does this.
CATALOG_PATH = Path.home() / ".genesis" / "skill_catalog.json"

# Longest path value kept from a transcript (Linux PATH_MAX). A longer one is
# not a real skill path; it is counted in names_invalid and never retained.
_MAX_PATH_CHARS = 4096
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

# Interactive sessions in a window above which "no interactive use of any
# skill" is treated as a recording-format change rather than a quiet fortnight
# (the live 14-day window holds 19 interactive sessions and 11 skills active
# interactively).
_NO_SIGNAL_SESSIONS = 10

# Issues listed in full; the rest are only counted.
_MAX_ISSUES = 50

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
    issues: list[dict] = field(default_factory=list)
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


def issue(issues: list[dict] | None, kind: str, path: object, mode: str, exc=None) -> None:
    """Record why an input could not be read in full. Every input failure the
    report survives lands here, so ``status`` can say ``partial``; modes are
    missing, unreadable, malformed and no_signal."""
    if issues is None:
        return
    # A filesystem name is third-party text: bounded, and made encodable (a
    # surrogate-escaped non-UTF-8 name would fail the MCP response).
    text = str(path).encode("utf-8", "backslashreplace").decode("utf-8")
    entry = {"input": kind, "path": text[:_MAX_PATH_CHARS], "mode": mode}
    if exc is not None:
        entry["detail"] = f"{type(exc).__name__}: {getattr(exc, 'strerror', None) or exc}"
    issues.append(entry)


def _mode(exc: BaseException) -> str:
    return "missing" if isinstance(exc, FileNotFoundError) else "unreadable"


def _json_file(path: Path, issues: list[dict], kind: str, *, required: bool) -> dict:
    """The JSON object in ``path``; an empty dict, with an issue recorded, when
    it is unreadable, malformed or not an object (missing is an issue only for
    a ``required`` file)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        if required or not isinstance(exc, FileNotFoundError):
            issue(issues, kind, path, _mode(exc), exc)
        return {}
    except (ValueError, RecursionError) as exc:
        issue(issues, kind, path, "malformed", exc)
        return {}
    if not isinstance(data, dict):
        issue(issues, kind, path, "malformed")
        return {}
    return data


def _entries(directory: Path, issues: list[dict], *, skills: bool) -> Iterator[os.DirEntry]:
    """Skill directories (holding a skill file) or ``*.md`` files, by name. An
    entry that cannot be stat'ed is skipped and recorded; an unreadable
    directory raises OSError to its caller."""
    for entry in sorted(os.scandir(directory), key=lambda e: e.name):
        try:
            if not skills:
                ok = entry.is_file() and entry.name.endswith(".md")
            elif ok := entry.is_dir():
                ok = False
                for marker in SKILL_FILES:
                    try:
                        info = os.stat(os.path.join(entry.path, marker))
                    except FileNotFoundError:
                        continue
                    if stat.S_ISREG(info.st_mode):  # as the catalog's is_file()
                        ok = True
                        break
        except OSError as exc:
            issue(issues, "skill_dir" if skills else "entry", entry.path, "unreadable", exc)
            continue
        if ok:
            yield entry


def load_population(
    *,
    catalog_path: Path,
    user_skills_dir: Path,
    plugins_manifest: Path,
    commands_dirs: Iterable[Path] = (),
) -> Population:
    """The nudge catalog (nudge-eligible), user-level skills, plugin skills and
    slash commands (the Skill tool loads commands too). Each source reports how
    many NEW names it added, so a name shared by two sources counts once.

    These are files other programs write, so a wrong shape anywhere is skipped,
    and a source that cannot be read at all carries an ``error`` instead of
    aborting the report."""
    pop = Population()

    errors: list[str] = []

    def source(kind: str, path: object, read) -> None:
        # Counted by difference, so names added before a failure still count.
        entry: dict = {"source": kind, "path": str(path)}
        before = len(pop.skills)
        errors.clear()
        try:
            read()
        except OSError as exc:
            errors.append(f"{type(exc).__name__}: {exc.strerror or exc}")
            issue(pop.issues, kind, path, _mode(exc), exc)
        entry["skills"] = len(pop.skills) - before
        if errors:
            entry["error"] = "; ".join(errors)
        pop.sources.append(entry)

    def catalog() -> int:
        found = 0
        data = _json_file(catalog_path, pop.issues, "catalog", required=True)
        skipped = 0
        tiers = [data.get(tier) for tier in ("tier1", "tier2")]
        if data and not any(isinstance(e, list) for e in tiers):
            issue(pop.issues, "catalog", catalog_path, "malformed")  # no tier list at all
        for tier, entries in zip(("tier1", "tier2"), tiers, strict=True):
            if entries is not None and not isinstance(entries, list):
                skipped += 1
            for item in entries if isinstance(entries, list) else []:
                item = _dict(item)
                name, path = item.get("name"), item.get("path")
                if not isinstance(name, str) or not name:
                    skipped += 1
                    continue
                found += pop._add(name, f"catalog-{tier}", nudge_eligible=True)
                if isinstance(path, str) and path.startswith("/"):
                    pop._abs_dirs[path.rstrip("/")] = name
                elif isinstance(path, str) and path:
                    pop._rel_dirs.append((path.rstrip("/"), name))
        if skipped:
            issue(pop.issues, "catalog_entries", catalog_path, "malformed")
        return found

    def user() -> int:
        found = 0
        try:
            exists = stat.S_ISDIR(os.stat(user_skills_dir).st_mode)
        except FileNotFoundError:
            exists = False  # no user skills: legitimately empty
        if exists:
            for entry in _entries(user_skills_dir, pop.issues, skills=True):
                found += pop._add(entry.name, "user", nudge_eligible=False)
                pop._abs_dirs.setdefault(entry.path.rstrip("/"), entry.name)
        return found

    def plugins() -> int:
        found = 0
        manifest = _json_file(plugins_manifest, pop.issues, "plugins", required=False)
        if "plugins" in manifest and not isinstance(manifest["plugins"], dict):
            issue(pop.issues, "plugins", plugins_manifest, "malformed")
        installed = _dict(manifest.get("plugins"))
        for key, installs in installed.items():
            plugin = str(key).split("@", 1)[0]
            for install in installs if isinstance(installs, list) else [installs]:
                root = _dict(install).get("installPath")
                if not isinstance(root, str) or not root:
                    issue(pop.issues, "plugin_install", plugins_manifest, "malformed")
                    continue
                pop._plugin_roots.append((root, plugin))
                # One unreadable install costs that install, not the manifest.
                for sub, kind, skills in (
                    ("skills", "plugin", True),
                    ("commands", "plugin-command", False),
                ):
                    directory = Path(root) / sub
                    try:
                        if not directory.is_dir():
                            continue
                        for entry in _entries(directory, pop.issues, skills=skills):
                            name = entry.name if skills else entry.name[:-3]
                            found += pop._add(f"{plugin}:{name}", kind, nudge_eligible=False)
                    except OSError as exc:
                        errors.append(f"{directory}: {type(exc).__name__}: {exc.strerror or exc}")
                        issue(pop.issues, "plugin_dir", directory, _mode(exc), exc)
        return found

    def process_nudges() -> int:
        # The hook emits these whether or not the plugin manifest lists them, so
        # every one is nudge-eligible and has a row even in a quiet window.
        found = 0
        for name in sorted(PROCESS_NUDGE_TARGETS):
            if name in pop.skills:
                pop.skills[name]["nudge_eligible"] = True
            else:
                found += pop._add(name, "process-nudge", nudge_eligible=True)
        return found

    def commands(directory: Path):
        def read() -> int:
            found = 0
            if directory.is_dir():
                for entry in _entries(directory, pop.issues, skills=False):
                    found += pop._add(entry.name[:-3], "command", nudge_eligible=False)
            return found

        return read

    source("catalog", catalog_path, catalog)
    source("user", user_skills_dir, user)
    source("plugins", plugins_manifest, plugins)
    source("process-nudge", "scripts/hooks/skill_injection_hook.py", process_nudges)
    for commands_dir in commands_dirs:
        source("commands", commands_dir, commands(commands_dir))
    return pop


def claude_root() -> Path:
    """The Claude Code config root that holds ``projects/``, ``skills/`` and
    ``plugins/``: ``CLAUDE_HOME`` first (Genesis's explicit override, the one
    ``genesis.env.claude_home`` reads), else ``CLAUDE_CONFIG_DIR`` (where Claude
    Code itself writes when relocated; ``session_awareness.peer_address`` reads
    it too), else ``~/.claude``."""
    for var in ("CLAUDE_HOME", "CLAUDE_CONFIG_DIR"):
        if value := os.environ.get(var):
            return Path(value).expanduser()
    return Path.home() / ".claude"


def _default_population() -> Population:
    from genesis.env import repo_root

    root = claude_root()
    return load_population(
        catalog_path=CATALOG_PATH,
        user_skills_dir=root / "skills",
        plugins_manifest=root / "plugins" / "installed_plugins.json",
        commands_dirs=(repo_root() / ".claude" / "commands", root / "commands"),
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


def iter_transcript_files(
    projects_root: Path, since: datetime, issues: list[dict] | None = None
) -> Iterator[tuple[Path, bool]]:
    """``(path, is_subagent)`` for transcripts modified at or after ``since``.

    One ``scandir`` per directory, never a recursive walk: top-level
    ``*.jsonl`` and ``<session>/subagents/*.jsonl``. A directory or entry that
    cannot be read is recorded in ``issues``, never silently dropped.
    """
    cutoff = since.timestamp()
    try:
        projects = sorted(os.scandir(projects_root), key=lambda e: e.name)
    except OSError as exc:
        issue(issues, "projects_root", projects_root, _mode(exc), exc)
        return
    for project in projects:
        try:
            if not project.is_dir():
                continue
            entries = sorted(os.scandir(project.path), key=lambda e: e.name)
        except OSError as exc:
            issue(issues, "project_dir", project.path, "unreadable", exc)
            continue
        for entry in entries:
            if entry.name.endswith(".jsonl"):
                try:
                    if entry.is_file() and entry.stat().st_mtime >= cutoff:
                        yield Path(entry.path), False
                except OSError as exc:
                    issue(issues, "transcript", entry.path, _mode(exc), exc)
                continue
            try:
                if not entry.is_dir():
                    continue
                # scandir, not isdir: isdir swallows a permission error.
                agents = sorted(
                    os.scandir(os.path.join(entry.path, "subagents")), key=lambda e: e.name
                )
            except (FileNotFoundError, NotADirectoryError):
                continue  # a session without subagents
            except OSError as exc:
                issue(issues, "transcript_dir", entry.path, "unreadable", exc)
                continue
            for agent in agents:
                try:
                    if (
                        agent.name.endswith(".jsonl")
                        and agent.is_file()
                        and agent.stat().st_mtime >= cutoff
                    ):
                        yield Path(agent.path), True
                except OSError as exc:
                    issue(issues, "transcript", agent.path, _mode(exc), exc)


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
            t
            for b in content
            if isinstance(b, dict)
            and b.get("type") == "text"
            and isinstance(t := b.get("text"), str)
        )
    return ""


def _dict(value: object) -> dict:
    """``value`` if it is a dict, else empty: a schema-drifted record is a
    non-signal, never an exception that aborts the scan."""
    return value if isinstance(value, dict) else {}


@dataclass
class _Session:
    """Per-session aggregates, never raw occurrences, so memory grows with the
    distinct skills a session touched, not with how often it touched them."""

    kind: str | None = None
    # (signal, value) -> [count, latest timestamp]
    uses: dict[tuple[str, str], list] = field(default_factory=dict)
    # (form, value) -> [count, earliest timestamp]
    nudges: dict[tuple[str, str], list] = field(default_factory=dict)
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
        self.names_invalid = 0
        self.files_failed = 0
        self.issues: list[dict] = []

    def scan_file(self, path: Path, is_subagent: bool = False) -> None:
        """Scan one transcript. Known bad shapes are skipped where they are read;
        anything else that raises is counted in ``files_failed`` and logged, so
        one file can cost its own signals but never the whole report."""
        try:
            self._scan_file(path, is_subagent)
        except Exception:
            self.files_failed += 1
            # A systematic bug fails every file: one traceback, then one line each.
            logger.warning("skill usage: scanning %s failed", path, exc_info=self.files_failed == 1)

    def _scan_file(self, path: Path, is_subagent: bool) -> None:
        try:
            # O_NONBLOCK: a FIFO named *.jsonl must not block the scan (and with it
            # the MCP tool's single-flight lock) forever; only regular files are read.
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as exc:
            issue(self.issues, "transcript", path, _mode(exc), exc)
            return
        try:
            info = os.fstat(fd)
        except OSError as exc:
            os.close(fd)
            issue(self.issues, "transcript", path, "unreadable", exc)
            return
        if not stat.S_ISREG(info.st_mode):
            os.close(fd)
            return
        budget = info.st_size  # fixed before reading: appends are left for later
        fh = os.fdopen(fd, "rb")
        self.files_scanned += 1
        self.subagent_files += int(is_subagent)
        # A subagent transcript (<parent>/subagents/*.jsonl) belongs to the
        # session whose directory holds it, as transcript_analytics reads it.
        file_sid: str | None = path.parent.parent.name if is_subagent else None
        consumed = 0
        kind_seen = registered = False
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
                    file_sid = m.group(1).decode(errors="replace")
                if file_sid is not None and not registered:
                    registered = True
                    self.sessions[file_sid]  # register the session even if it holds no signal
                if file_sid is not None and not kind_seen and (m := _ENTRYPOINT_RE.search(line)):
                    kind_seen = True
                    session = self.sessions[file_sid]
                    # The parent transcript decides; a subagent file only fills a gap.
                    if not is_subagent or session.kind is None:
                        session.kind = m.group(1).decode(errors="replace")
                if not any(token in line for token in _PREFILTER):
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, RecursionError):  # RecursionError: deeply nested JSON
                    self.lines_unparsed += 1
                    continue
                if isinstance(record, dict):
                    self._record(record, file_sid or path.stem, owned=is_subagent)
        if file_sid is None:
            self.sessions[path.stem]

    def _keep(self, signal: str, value: str) -> bool:
        """Drop a value that cannot be a skill name or path before it is
        retained, counting it in names_invalid."""
        if signal in ("load", "named", "process"):
            ok = bool(_NAME_RE.fullmatch(value))
        else:
            ok = 0 < len(value) <= _MAX_PATH_CHARS
        self.names_invalid += not ok
        return ok

    def _record(self, record: dict, default_sid: str, *, owned: bool = False) -> None:
        """``owned``: the file decides the session (a subagent transcript belongs
        to the parent session whose directory holds it, whatever its records say)."""
        signals = list(self._signals(record))
        if not signals:
            return
        sid = record.get("sessionId")
        if owned or not isinstance(sid, str) or not sid:
            sid = default_sid
        # An edit excludes that session's reads whenever it happened, so it is
        # kept before the window filter, timestamped or not.
        for signal, value in signals:
            if signal == "edit" and self._keep(signal, value):
                self.sessions[sid].edited.add(value)
        signals = [s for s in signals if s[0] != "edit"]
        if not signals:
            return
        stamp = _parse_ts(record.get("timestamp"))
        if stamp is None:
            self.lines_untimestamped += 1
            return
        if not (self.since <= stamp <= self.until):
            return
        session = self.sessions[sid]
        for signal, value in signals:
            if signal == "nudge":
                if not isinstance(value, str):
                    self.nudges_unparsed += 1
                    continue
                found, unparsed = parse_nudges(value)
                self.nudges_unparsed += unparsed
                for name, form in found:
                    if self._keep(form, name):
                        agg = session.nudges.setdefault((form, name), [0, stamp])
                        agg[0] += 1
                        agg[1] = min(agg[1], stamp)
            elif self._keep(signal, value):
                agg = session.uses.setdefault((signal, value), [0, stamp])
                agg[0] += 1
                agg[1] = max(agg[1], stamp)

    @staticmethod
    def _signals(record: dict) -> Iterator[tuple[str, object]]:
        kind = record.get("type")
        message = _dict(record.get("message"))
        if kind == "assistant":
            content = message.get("content")
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tool, data = block.get("name"), block.get("input") or {}
                if not isinstance(tool, str) or not isinstance(data, dict):
                    continue
                if tool == "Skill" and isinstance(data.get("skill"), str):
                    yield "load", data["skill"]
                    continue
                file_path = data.get("file_path")
                if isinstance(file_path, str) and os.path.basename(file_path) in SKILL_FILES:
                    if tool == "Read":
                        yield "read", os.path.normpath(file_path)
                    elif tool in _EDIT_TOOLS:
                        yield "edit", os.path.normpath(file_path)
        elif kind == "user" and record.get("isMeta") and "sourceToolUseID" not in record:
            text = _text_of(message.get("content"))
            if text.startswith(_BASE_DIR):
                yield "slash", text[len(_BASE_DIR) :].split("\n", 1)[0].strip()
            elif _PRELOAD_MARK in text and _BASE_DIR in text:
                yield "preload", text.split(_BASE_DIR, 1)[1].split("\n", 1)[0].strip()
        elif kind == "attachment":
            attachment = _dict(record.get("attachment"))
            if "skill_injection_hook" in str(attachment.get("command") or ""):
                yield "nudge", attachment.get("stdout") or attachment.get("content") or ""


# --- dispatched injections (cc_sessions) --------------------------------------------


def count_injected(rows: Iterable[tuple[str | None, str | None]]) -> dict:
    """Per-tag counts from ``(metadata, status)`` rows carrying ``skill_tags``."""
    per_skill: dict[str, dict[str, int]] = defaultdict(
        lambda: {"injected": 0, "injected_failed": 0}
    )
    sessions = 0
    for metadata, status in rows:
        try:
            tags = _dict(json.loads(metadata or "")).get("skill_tags")
        except (ValueError, TypeError, RecursionError):
            continue
        if not isinstance(tags, list):  # as loop_closure.skill_funnel checks it
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


def read_injected_sync(db_path: Path, since: datetime, until: datetime) -> dict | None:
    """``count_injected`` over the live DB, read-only; None if it cannot be read."""
    if not Path(db_path).is_file():
        return None
    try:
        # as_uri() percent-encodes the path, so a "?", "#" or "%" in it cannot
        # change the URI's meaning or drop mode=ro.
        con = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            rows = con.execute(_INJECTED_SQL, (since.isoformat(), until.isoformat())).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return count_injected(rows)


async def read_injected(db, since: datetime, until: datetime) -> dict:
    """``count_injected`` over an open aiosqlite connection."""
    cursor = await db.execute(_INJECTED_SQL, (since.isoformat(), until.isoformat()))
    return count_injected(await cursor.fetchall())


# --- report ---------------------------------------------------------------------------


def _triple() -> dict[str, int]:
    # "unknown": a session with no entrypoint record, kept out of both buckets.
    return {"interactive": 0, "headless": 0, "unknown": 0, "total": 0}


def _bump(counter: dict[str, int], kind: str, n: int = 1) -> None:
    counter[kind] += n
    counter["total"] += n


def build_report(
    scanner: Scanner,
    population: Population,
    injected: dict | None,
    *,
    duration_s: float,
    db_source: str | None = None,
) -> dict:
    rows: dict[str, dict] = {}
    # One unit throughout: occurrences dropped (a population name counts once).
    names_invalid = scanner.names_invalid

    def valid(name: str, occurrences: int = 1) -> bool:
        nonlocal names_invalid
        if _NAME_RE.fullmatch(name):
            return True
        names_invalid += occurrences
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
        if valid(name):  # catalog and directory names are third-party text too
            row(name)

    sessions = _triple()
    unknown_entrypoint = 0
    for session in scanner.sessions.values():
        if session.kind is None:
            unknown_entrypoint += 1
        if session.kind is None:
            kind = "unknown"
        else:
            kind = "interactive" if session.kind in INTERACTIVE_ENTRYPOINTS else "headless"
        _bump(sessions, kind)

        last_use: dict[str, datetime] = {}
        for (signal, value), (count, stamp) in session.uses.items():
            if signal == "read" and value in session.edited:
                continue  # editing a skill is not using it
            if signal == "preload":  # configured, not chosen: counted, never a use
                if valid(name := population.resolve_dir(value), count):
                    _bump(row(name)["preloads"], kind, count)
                continue
            if signal == "load":
                name, counter = value, "loads"
            elif signal == "slash":
                name, counter = population.resolve_dir(value), "slash_loads"
            else:
                name, counter = population.resolve_dir(os.path.dirname(value)), "reads"
            if not valid(name, count):
                continue
            r = row(name)
            _bump(r[counter], kind, count)
            if name not in last_use or stamp > last_use[name]:
                last_use[name] = stamp
            if r["_last"] is None or stamp > r["_last"]:
                r["_last"] = stamp

        first_nudge: dict[str, datetime] = {}
        for (form, value), (count, stamp) in session.nudges.items():
            name = population.resolve_dir(value) if form == "path" else value
            if not valid(name, count):
                continue
            _bump(row(name)["nudges"], kind, count)
            if name not in first_nudge or stamp < first_nudge[name]:
                first_nudge[name] = stamp

        for name in last_use:
            _bump(rows[name]["sessions_used"], kind)
        for name, nudged_at in first_nudge.items():
            r = rows[name]
            _bump(r["sessions_nudged"], kind)
            if name in last_use and last_use[name] >= nudged_at:
                _bump(r["_converted"], kind)

    uncatalogued_tags: dict[str, int] = {}
    if injected is not None:
        for tag, counts in injected["per_skill"].items():
            if not valid(tag, counts["injected"]):
                continue
            if tag in population.skills or tag in rows:
                r = row(tag)
                r["injected"] += counts["injected"]
                r["injected_failed"] += counts["injected_failed"]
            else:
                uncatalogued_tags[tag] = counts["injected"]

    def state_of(r: dict, kind: str) -> str:
        if r["sessions_used"][kind]:
            return "active"
        if r["nudges"][kind]:
            return "zombie"
        return "dead" if r["nudge_eligible"] else "not_nudgeable"

    out = []
    for r in rows.values():
        state = state_of(r, "total")
        # Per kind too: headless counts test and SDK runs, so a skill used only
        # there reads active overall and dead or zombie interactively.
        r["state_by_kind"] = {k: state_of(r, k) for k in ("interactive", "headless")}
        converted, nudged = r.pop("_converted"), r["sessions_nudged"]
        r["conversion"] = {
            k: round(converted[k] / nudged[k], 3) if nudged[k] else None for k in nudged
        }
        last = r.pop("_last")
        r["last_fired"] = last.isoformat() if last else None
        r["state"] = state
        out.append(r)
    out.sort(key=lambda r: (_STATE_ORDER[r["state"]], -r["sessions_used"]["total"], r["name"]))

    issues = [*population.issues, *scanner.issues]
    if injected is None:
        issue(issues, "db", db_source or "no database connection", "unavailable")
    if scanner.files_failed:
        issue(issues, "transcript", f"{scanner.files_failed} file(s)", "failed")
    # Canaries for a change in how Claude Code records sessions or skill use: an
    # entrypoint no longer recorded, or many interactive sessions and not one
    # interactive use of any skill.
    if unknown_entrypoint:
        issue(issues, "entrypoint", f"{unknown_entrypoint} session(s)", "unclassified")
    if sessions["interactive"] >= _NO_SIGNAL_SESSIONS and not any(
        r["sessions_used"]["interactive"] for r in out
    ):
        issue(issues, "transcripts", "", "no_signal")
    counts: dict[str, int] = defaultdict(int)
    for entry in issues:
        counts[f"{entry['input']}/{entry['mode']}"] += 1
    return {
        "status": "partial" if issues else "ok",
        # Bounded: the report goes back into a model's context; the counts
        # cover every issue, the list only the first _MAX_ISSUES.
        "input_issues": issues[:_MAX_ISSUES],
        "input_issues_total": len(issues),
        "input_issue_counts": dict(counts),
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
        "files_failed": scanner.files_failed,
        "names_invalid": names_invalid,
        "duration_s": round(duration_s, 2),
        "db_join": "ok" if injected is not None else "unavailable",
        "db_source": db_source,
        "injected_sessions": injected["sessions"] if injected is not None else "unavailable",
        "uncatalogued_tags": uncatalogued_tags,
        "catalog_sources": population.sources,
        "skills": out,
        "note": (
            "status is 'partial' when any input_issues entry exists or a file failed: "
            "then a zero may be a missing input, not a quiet window. "
            "A zero means none found in the scanned sessions, within the window; "
            "files_failed counts transcripts whose signals may be partial or missing. "
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
    db_source: str | None = None,
) -> dict:
    started = time.monotonic()
    scanner = Scanner(since, until)
    for path, is_subagent in iter_transcript_files(projects_root, since, scanner.issues):
        scanner.scan_file(path, is_subagent)
    return build_report(
        scanner,
        population,
        injected,
        duration_s=time.monotonic() - started,
        db_source=db_source,
    )


# --- CLI ------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    from genesis.env import genesis_db_path

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
    db = args.db or genesis_db_path()
    report = run_scan(
        projects_root=args.projects_root or claude_root() / "projects",
        since=since,
        until=until,
        population=_default_population(),
        injected=read_injected_sync(db, since, until),
        db_source=str(db),
    )
    text = json.dumps(report, indent=2)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    else:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
