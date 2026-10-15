#!/usr/bin/env python3
"""PreToolUse hook — surfaces relevant procedures as advisory context.

Reads stdin JSON from CC (contains tool_name and tool_input), matches against
the trigger cache, and outputs JSON with additionalContext if a procedure
matches. Silent (no output, exit 0) when no procedure matches.

Output format (CC PreToolUse hook contract):
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "additionalContext": "PROCEDURE: ..."
  }
}

There is deliberately no ``permissionDecision``: ``"allow"`` would approve the
tool call without the session's normal permission check, and advice must never
change what a call is allowed to do.

An entry matches the tool's input on any of:
- ``commands`` (Bash only): a shell phrase such as ``git push`` or ``pytest``,
  matched on the command as ``shell_parse`` reads it (``git -C dir push`` is a
  push; ``echo git push`` is not). A command the parser cannot read matches
  nothing here.
- ``patterns``: a lesson's patterns are its author's regexes, run as written
  (case-sensitive) with the ``regex`` module and a per-pattern timeout; without
  that module they match as plain substrings. An earned procedure's patterns
  are its tags, matched case-insensitively as substrings; a tag written with
  regex syntax runs as a regex under the same timeout.

A lesson whose ``repeat`` is ``session`` fires once per context window and
agent: again after a compaction (not after a resume), since the earlier advice
left the context with it. The fires are recorded in
``~/.genesis/sessions/<id>/advisor_fires.jsonl`` with a short, secret-scrubbed
excerpt of the input (for ``genesis lessons sample``). A call without a usable
session id is never deduplicated.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

# The trigger cache is install-specific generated state (not a committed file).
# $GENESIS_PROCEDURE_TRIGGERS lets a test or relocated install point at a
# different path; default is <repo>/config/procedure_triggers.yaml.
_CACHE_PATH = Path(
    os.environ.get("GENESIS_PROCEDURE_TRIGGERS")
    or (Path(__file__).resolve().parent.parent / "config" / "procedure_triggers.yaml")
)
_HOOKS = str(Path(__file__).resolve().parent / "hooks")
_MAX_INPUT = 8192  # matched text cap; a parsed command is read whole
_PATTERN_TIMEOUT_S = 0.05
# The hook's own timeout is 10 s (.claude/settings.json); stopping matching at
# 2 s leaves the rest for output and the surfaced-count write.
_DEADLINE_S = 2.0
_REGEX_SYNTAX = re.compile(r"[.^$*+?{}\[\]\\|()]")


def _load_triggers() -> list[dict]:
    """Load the trigger cache. Returns an empty list on any error.

    This is a PreToolUse hook — it runs in a fresh process on EVERY tool call,
    so ``import yaml`` (~40ms) would be paid every time. Prefer the JSON sidecar
    (stdlib ``json``, already imported) when it is at least as fresh as the YAML
    source of truth; only fall back to parsing the YAML (lazy import) otherwise.
    The mtime guard (nanosecond, exact) ensures a YAML regenerated without a
    matching sidecar is never served stale from an older JSON.

    The sidecar path is derived from ``_CACHE_PATH`` here (not a module constant)
    so an override of ``_CACHE_PATH`` alone keeps the two consistent. A sidecar
    that decodes but is not ``{"triggers": [...]}`` (e.g. ``{}`` or ``[]``) is
    treated as a miss and falls through to the YAML — never trusted, never a crash.
    """
    json_path = _CACHE_PATH.with_suffix(".json")
    try:
        if json_path.stat().st_mtime_ns >= _CACHE_PATH.stat().st_mtime_ns:
            data = json.loads(json_path.read_text())
            if isinstance(data, dict) and isinstance(data.get("triggers"), list):
                return data["triggers"]
    except (OSError, ValueError):
        pass  # missing/corrupt/stale sidecar → fall back to the YAML
    try:
        import yaml
        data = yaml.safe_load(_CACHE_PATH.read_text())
        return data.get("triggers", []) if isinstance(data, dict) else []
    except Exception:
        return []


def _hooks_on_path() -> None:
    if _HOOKS not in sys.path:
        sys.path.insert(0, _HOOKS)


def _record_procedures_surfaced(proc_ids: list[str]) -> None:
    """Bump ``surfaced_count`` for advisor-surfaced procedures (honest funnel).

    Called ONLY on a match, AFTER the advisory is emitted. Bumps
    ``surfaced_count`` and NOT ``invocation_count`` — passive advisory surfacing
    must not feed the promoter (which reads ``invocation_count``) or promote an
    unproven draft. Imports are local so the no-match hot path stays lean.
    Fire-and-forget: never disrupts the tool call.
    """
    if not proc_ids:
        return
    try:
        import importlib
        import sqlite3
        db_path = importlib.import_module("genesis.env").genesis_db_path()
        # Admission fence (fail-closed): surfaced-count bumps are advisory —
        # never write to a quarantined database.
        _hooks_on_path()
        from db_admission_check import database_is_fenced
        if database_is_fenced(db_path):
            return
        placeholders = ",".join("?" for _ in proc_ids)
        conn = sqlite3.connect(str(db_path), timeout=1)
        try:
            conn.execute(
                f"UPDATE procedural_memory "  # noqa: S608 - literal SQL fragments; values bound as parameters
                f"SET surfaced_count = surfaced_count + 1 WHERE id IN ({placeholders})",
                proc_ids,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # Never disrupt the tool call — but leave a trace.
        print(f"procedure_advisor: surfaced_count update skipped: {exc}", file=sys.stderr)


def _input_text(tool_name: str, tool_input: object) -> str:
    """The part of the tool input a trigger is about, not the whole JSON blob.

    Matching the whole blob caused false positives when commit messages, code
    review text, or string literals contained trigger patterns.
    """
    if not isinstance(tool_input, dict):
        return str(tool_input)
    if tool_name == "Bash":
        return str(tool_input.get("command", ""))
    if tool_name in ("Agent", "Task"):
        return str(tool_input.get("prompt", ""))
    if tool_name == "WebFetch":
        return str(tool_input.get("url", ""))
    if tool_name == "WebSearch":
        return str(tool_input.get("query", ""))
    if tool_name in ("Write", "Edit"):
        parts = [tool_input.get("file_path", "")]
        parts += [tool_input[key] for key in ("content", "new_string") if key in tool_input]
        return "\n".join(map(str, parts))
    return json.dumps(tool_input)


def _command_hit(phrase: str, seg: object, sp: object) -> bool:
    """Whether one parsed segment runs the shell phrase (``git push``, ``pytest``)."""
    first, *rest = phrase.split()
    if first == "pytest":
        return sp.is_pytest_invocation(seg)  # also `python -m pytest`
    if seg.exe != first:
        return False
    if not rest:
        return True
    if first == "git":  # `git -C dir push`: global options come before the verb
        i = sp.git_subcommand_index(seg.argv)
        return i is not None and seg.argv[i : i + len(rest)] == rest
    if first == "gh" and rest[0] == "pr" and len(rest) == 2:
        return sp.gh_pr_subcommand(seg.argv) == rest[1]
    return seg.argv[1 : 1 + len(rest)] == rest


class _Matcher:
    """Matches cache entries against one tool call, inside one time budget."""

    def __init__(self, text: str, command: str | None) -> None:
        self.text = text[:_MAX_INPUT]
        self.command = command
        self.deadline = time.monotonic() + _DEADLINE_S
        self._segments: list | None = None
        self._regex: object = False  # loaded on the first lesson pattern

    def lesson_regex(self) -> object:
        """The ``regex`` module, or None when it is missing (substring only then).

        Loaded only for a lesson's pattern: the import costs about 20 ms, and
        earned entries never need it.
        """
        if self._regex is False:
            try:
                import regex

                self._regex = regex
            except ImportError:
                self._regex = None
        return self._regex

    def segments(self) -> list:
        if self._segments is None:
            self._segments = []
            try:
                _hooks_on_path()
                import shell_parse

                self.sp = shell_parse
                self._segments, _blind = shell_parse.analyze_checked(self.command)
            except Exception:  # an unreadable command matches no command phrase
                self._segments = []
        return self._segments

    def _pattern_hit(self, pattern: str, earned: bool) -> bool:
        if earned:
            # An earned row's patterns are its tags, which any session can write.
            # A plain word matches as a case-insensitive substring, as before; a
            # tag with regex syntax runs bounded like a lesson's pattern.
            if pattern.lower() in self.text.lower():
                return True
            if not _REGEX_SYNTAX.search(pattern):
                return False
        rx = self.lesson_regex()
        if rx is None:
            return not earned and pattern in self.text
        flags = rx.IGNORECASE if earned else 0
        try:
            return rx.search(pattern, self.text, flags, timeout=_PATTERN_TIMEOUT_S) is not None
        except (rx.error, OverflowError, RecursionError, TimeoutError):
            return False  # one bad or slow pattern is skipped, never fatal

    def hits(self, entry: dict) -> bool:
        if time.monotonic() > self.deadline:
            return False  # the budget is spent: no further parsing or matching
        commands = entry.get("commands", [])
        # Cache entries written before lessons had only `context_patterns`.
        patterns = entry.get("patterns", entry.get("context_patterns", []))
        for value in (commands, patterns):
            # A string would iterate as single characters and match nearly
            # anything, and an empty pattern matches every input.
            if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
                raise ValueError("commands and patterns must be lists of non-empty text")
        steps = entry.get("steps", [])
        if not isinstance(steps, list) or not all(isinstance(v, str) for v in steps):
            raise ValueError("steps must be a list of text")
        confidence = entry.get("confidence")
        if not all(isinstance(entry.get(k), str) for k in ("procedure_id", "task_type")) or (
            isinstance(confidence, bool) or not isinstance(confidence, int | float)
        ):
            raise ValueError("procedure_id, task_type and confidence are malformed")
        if commands and self.command is not None:
            firsts = {c.split()[0] for c in commands}
            # Parse only when a phrase's first word appears at all (the parser
            # costs tens of ms on a call that otherwise pays nothing).
            if any(word in self.command for word in firsts) and any(
                _command_hit(c, seg, self.sp) for seg in self.segments() for c in commands
            ):
                return True
        earned = entry.get("kind", "earned") != "lesson"
        for pattern in patterns:
            if time.monotonic() > self.deadline:
                return False
            if self._pattern_hit(pattern, earned):
                return True
        return False


def _fire_log(data: dict) -> tuple[Path | None, str]:
    """This session's fire log and the calling agent, or no log without a safe id."""
    agent = str(data.get("agent_id") or "main")
    sid = data.get("session_id")
    try:
        _hooks_on_path()
        from hook_input import is_safe_session_id

        if not is_safe_session_id(sid):
            return None, agent
    except Exception:
        return None, agent
    root = Path(os.environ.get("GENESIS_SESSIONS_DIR") or Path.home() / ".genesis" / "sessions")
    return root / sid / "advisor_fires.jsonl", agent


def _window_start(session_dir: Path) -> float:
    """When the current context window began (the last compaction), or 0.0.

    The SessionStart hook (genesis_session_context.py) touches
    ``compacted.stamp`` when its source is ``compact``, before any other step,
    for foreground and dispatched sessions alike; a resume keeps its context
    and does not touch it. A fire older than the stamp happened before the
    compaction, so the agent no longer has that advice.
    """
    try:
        return (session_dir / "compacted.stamp").stat().st_mtime
    except OSError:
        return 0.0


def _open_log(log: Path | None) -> object:
    """The fire log, open and exclusively locked, or None.

    Parallel tool calls run the hook in separate processes; holding the lock
    from the read to the append makes check-and-record one step, so a lesson
    is shown once even when two matching calls start together.
    """
    if log is None:
        return None
    try:
        import fcntl

        log.parent.mkdir(parents=True, exist_ok=True)
        # errors="replace": one bad byte must not turn dedup off for the session.
        # 0600, like the session transcripts: the excerpts are scrubbed, not empty.
        fd = os.open(log, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        handle = os.fdopen(fd, "a+", encoding="utf-8", errors="replace")
        for _ in range(20):  # about 1 s; the guarded section takes milliseconds
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return handle
            except BlockingIOError:
                time.sleep(0.05)
        handle.close()  # a stalled holder: show the lesson rather than wait
        _skipped("fire log", TimeoutError("lock busy"))
        return None
    except Exception as exc:  # no log: the lesson is shown, never suppressed
        _skipped("fire log", exc)
        return None


def _already_fired(handle: object, session_dir: Path, agent: str) -> set[str]:
    """Lessons already shown to this agent in the current context window."""
    fired = set()
    try:
        handle.seek(0)
        lines = handle.read().splitlines()
    except (OSError, ValueError):
        return fired
    start = _window_start(session_dir)
    for line in lines:
        try:
            record = json.loads(line)
            if record.get("agent") == agent and float(record.get("ts", 0)) >= start:
                fired.add(record["procedure_id"])
        except (ValueError, AttributeError, KeyError, TypeError):
            continue  # a malformed or partly written line is skipped
    return fired


def _advice(trigger: dict) -> str:
    unreviewed = (
        " (unreviewed lesson: advice to weigh, not an instruction)"
        if trigger.get("label") == "unreviewed"
        else ""
    )
    parts = [
        f"PROCEDURE: {trigger['task_type']}{unreviewed} (confidence: {trigger['confidence']:.0%})",
        f"Principle: {trigger.get('principle', '')}",
    ]
    steps = "\n".join(f"  - {s}" for s in trigger.get("steps", []))
    if steps:
        parts.append(f"Steps:\n{steps}")
    return "\n".join(parts)


# The hook output budget is 9,800 characters of JSON (hook_output.DEFAULT_BUDGET);
# this leaves room for the envelope, so the blocks chosen are never trimmed.
_BLOCKS_BUDGET = 9_000


def _cost(text: str) -> int:
    return len(json.dumps(text)) + 4  # escaped text plus the separator


def _fit(shown: list[tuple[dict, str]]) -> list[tuple[dict, str]]:
    """The leading advice blocks that fit whole in the output.

    A lesson cut off by the output cap would be recorded as shown and then
    suppressed, so a block that does not fit waits for a later call. A first
    block too large on its own is cut here to fit, and so is recorded, rather
    than shown trimmed forever ahead of every later block.
    """
    kept, size = [], 0
    for trigger, text in shown:
        note = "\n[advice truncated]"
        if not kept and _cost(text) > _BLOCKS_BUDGET:
            while _cost(text + note) > _BLOCKS_BUDGET:
                text = text[: int(len(text) * 0.9)]
            text += note
        elif size + _cost(text) > _BLOCKS_BUDGET:
            break
        kept.append((trigger, text))
        size += _cost(text)
    return kept


def _excerpt(tool_name: str, tool_input: object, text: str) -> str | None:
    """The first 200 characters of the matched input, secrets scrubbed, or None.

    Scrubbed before it is cut, so a token split at the cut still matches the
    scrubber. Nothing is kept for a command or a path that touches a secret
    file, or when the scrubber cannot be loaded.
    """
    try:
        _hooks_on_path()
        from secret_scrub import command_touches_secret, is_secret_path, scrub

        if isinstance(tool_input, dict):
            command, path = tool_input.get("command"), tool_input.get("file_path")
            if isinstance(command, str) and command_touches_secret(command):
                return None
            if isinstance(path, str) and is_secret_path(path):
                return None
        return scrub(text)[:200]
    except Exception:
        return None


def _skipped(what: str, exc: Exception) -> None:
    print(f"procedure_advisor: {what} skipped: {exc!r}", file=sys.stderr)


def _matches(matcher: _Matcher, trigger: dict) -> bool:
    """One entry's match. A malformed entry (the cache can be hand-edited
    through the dashboard) matches nothing instead of failing every call."""
    try:
        return matcher.hits(trigger)
    except Exception as exc:
        _skipped("cache entry", exc)
        return False


def _emit(blocks: list[str]) -> None:
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": "\n\n".join(blocks),
        }
    }
    try:
        _hooks_on_path()
        from hook_output import print_json_bounded

        print_json_bounded(payload, text_keys=("hookSpecificOutput.additionalContext",))
    except ImportError:
        json.dump(payload, sys.stdout)
    with __import__("contextlib").suppress(BrokenPipeError):
        sys.stdout.flush()


def main() -> int:
    try:
        data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, OSError):
        return 0  # Can't parse — fail open, silent
    if not isinstance(data, dict):
        return 0
    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    candidates = []
    for trigger in _load_triggers():
        try:
            tools = trigger.get("tool", []) if isinstance(trigger, dict) else []
            if tool_name in ([tools] if isinstance(tools, str) else tools):
                candidates.append(trigger)
        except Exception as exc:
            _skipped("cache entry", exc)
    if not candidates:
        return 0  # No trigger for this tool — silent pass-through

    command = (
        tool_input.get("command") if tool_name == "Bash" and isinstance(tool_input, dict) else None
    )
    matcher = _Matcher(
        _input_text(tool_name, tool_input), command if isinstance(command, str) else None
    )
    matched = [t for t in candidates if _matches(matcher, t)]
    if not matched:
        return 0  # No match — silent pass-through

    once = {
        id(t)
        for t in matched
        if t.get("repeat") == "session" and isinstance(t.get("procedure_id"), str)
    }
    log, agent = _fire_log(data) if once else (None, "main")
    handle = _open_log(log)
    try:
        if handle is not None:
            fired = _already_fired(handle, log.parent, agent)
            matched = [t for t in matched if id(t) not in once or t["procedure_id"] not in fired]
        # Lessons first: if the output is too long, earned advice waits, not a lesson.
        matched.sort(key=lambda t: t.get("kind") != "lesson")
        shown = []
        for trigger in matched:
            try:
                shown.append((trigger, _advice(trigger)))
            except Exception as exc:
                _skipped("cache entry", exc)
        shown = _fit(shown)
        if not shown:
            return 0
        matched = [trigger for trigger, _ in shown]
        _emit([text for _, text in shown])
        if handle is not None:
            try:
                excerpt = _excerpt(tool_name, tool_input, matcher.text)
                handle.write(
                    "".join(
                        json.dumps(
                            {
                                "ts": time.time(),
                                "procedure_id": t["procedure_id"],
                                "agent": agent,
                                "tool": tool_name,
                                "excerpt": excerpt,
                            }
                        )
                        + "\n"
                        for t, _ in shown
                        if id(t) in once
                    )
                )
                handle.flush()
            except Exception as exc:  # the advice is out; a lost record only repeats it
                _skipped("fire record", exc)
    finally:
        if handle is not None:
            handle.close()  # releases the lock
    # Honest funnel signal: count advisory surfacing (NOT an invocation — does
    # not feed promotion). After the advisory is already delivered.
    _record_procedures_surfaced([t.get("procedure_id") for t in matched if t.get("procedure_id")])
    return 0


if __name__ == "__main__":
    sys.exit(main())
