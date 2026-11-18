#!/usr/bin/env python3
"""Stamp the session that opened a PR into its body, right after `gh pr create`.

WHAT IT DOES
============
After a Bash call that created a pull request, this hook adds one line to the PR
body: ``Opened-by-session: <8hex>``, the same leading eight hex digits that
``prepare-commit-msg`` writes into each commit's ``Genesis-Session`` trailer.
``scripts/pr_opener.py <N>`` reads it back. Code writes the line, never the
model, so it does not depend on a session remembering to.

After a Bash call that EDITED a PR body, it checks the line is still there. A
session that rewrites its body from a local file drops it (MEASURED: on 10 of 37
of this repo's own PRs a later rewrite removed a line another writer had
appended). The line is put back with the ORIGINAL opener's id, read from the
PR's edit history (GitHub keeps every body version), never with the id of the
session that made the edit. A PR whose history has no line (one opened before
this hook, or by a tool that runs no hooks) is left alone.

WHAT TRIGGERS IT
================
- Claude Code's own record of the call: ``tool_response.gitOperation.pr`` with
  ``action`` ``created`` or ``edited``. It is undocumented but MEASURED on every
  create seen (206 in five days, and in a hook-seen payload).
- Because it is undocumented, a create is also found the way gh reports it: a
  WHOLE-line ``https://github.com/<owner>/<repo>/pull/<digits>`` in the output of
  a command that mentions ``pr create`` or ``pr new``. The push's own
  ``/pull/new/<branch>`` hint has no digits and never matches. Such a URL counts
  only if GitHub says the PR was CREATED during this call (its ``duration_ms``
  plus slack), so a PR an earlier step merely printed is never touched (the rule
  ``issue_dup_advisory`` uses for issues). PostToolUseFailure is read too: a
  compound command can fail after its create succeeded.
- A create started with ``run_in_background`` has not finished when this runs;
  it gets a note saying to add the line by hand, and no stamp.

WHAT IT WILL AND WILL NOT WRITE
===============================
- Only PRs whose author is the owner of the repository in the URL (an install
  stamping its own repos); a PR into another owner's repository is left alone.
- github.com only; ``GH_HOST`` is pinned there, as in ``issue_dup_advisory``.
- One line, inserted before the first of the configured ``insert_before``
  markers that starts a line (bot sections a later bot rewrites), else
  appended. A line naming ANOTHER session is replaced with this one at create
  time (a reused body file).
- Bots edit a new PR's body within seconds. A read-modify-write in that window
  loses either the line or the bot's text. So when ``settle_markers`` are
  configured, the hook waits (polling every 2 s, at most ``settle_wait_s``)
  until one is in the body or the body changes, then writes, then re-reads once
  a few seconds later and re-applies the line once if a late write dropped it.
  The public default configures no marker; an install names its own bots in
  ``${GENESIS_HOME:-~/.genesis}/config/pr_opener_stamp.local.yaml``.

This hook writes to GitHub on its own: the one hook here that does. The owner
accepted that for this line (2026-11-17), because the PR itself was already
published through the approved create, and the line names a session id prefix
that each commit's trailer already publishes. GitHub's edit history records
every write and its actor.

ADVISORY: never blocks, never asks; ``main`` returns 0 on every path. One
shared 45 s budget (settings timeout 60) bounds the whole call; running out is
reported as "could not stamp", with the exact line to add by hand.

WHAT IT CANNOT SEE
==================
- An edit made inside a script or a here-doc'd program: no hook sees it, and
  Claude Code reports no ``gitOperation`` for it (MEASURED: 24 of 56 recorded
  ``gh pr edit`` calls). The lookup still answers from the edit history.
- A ``gh pr edit`` that Claude Code does not report as an edit (the fallback
  for edits is ``gitOperation`` only).
- PRs opened outside a Claude Code Bash call: the executor's draft PRs, the
  contribution opener, Devin.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hook_input import read_payload, tool_input, tool_response  # noqa: E402
from hook_output import print_json_bounded  # noqa: E402

#: Cheap prefilter: this hook runs on every Bash call.
_GH_WORD = re.compile(r"(?<![\w-])gh(?![\w-])")
_CREATE_WORDS = re.compile(r"\bpr\s+(?:create|new)\b")
_PR_URL = re.compile(
    r"^[ \t]*(?P<url>https://github\.com/(?P<repo>[\w.-]+/[\w.-]+)/pull/(?P<num>\d+))[ \t]*$",
    re.MULTILINE,
)
_SESSION = re.compile(r"^([0-9a-fA-F]{8})")

#: The whole call's wall-clock budget, under the 60 s registration timeout in
#: `.claude/settings.json`: a hook killed at its timeout prints nothing, so this
#: is what turns a slow GitHub into a named "could not stamp".
_DEADLINE_S = 45.0
_POLL_S = 2.0
#: After the write, wait this long before the one re-read that catches a bot
#: writing back a body it read before the stamp.
_RECHECK_S = 4.0
_MAX_PRS = 4
#: "Created during this call": the call's duration plus slack (see issue_dup_advisory).
_PAST_SLACK = timedelta(seconds=30)
_FUTURE_SLACK = timedelta(minutes=2)
_DEFAULT_DURATION = timedelta(minutes=10)
_MAX_DURATION = timedelta(days=1)
_DEFAULTS = {"settle_markers": [], "settle_wait_s": 20, "insert_before": []}
_CONFIG_NAME = "pr_opener_stamp"
_SELF_ROOT = Path(__file__).resolve().parents[2]


class _Unstamped(Exception):
    """The stamp could not be written; the message says why, for the agent."""


def _lookup():
    """scripts/pr_opener.py, the one definition of what counts as the line."""
    path = Path(__file__).resolve().parents[1] / "pr_opener.py"
    spec = importlib.util.spec_from_file_location("_pr_opener_for_stamp", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    """The only process this hook starts. Tests patch it; the suite needs no network."""
    env = dict(os.environ, GH_PROMPT_DISABLED="1", GH_HOST="github.com")
    return subprocess.run(
        argv, env=env, capture_output=True, text=True, timeout=timeout, check=False
    )


def _gh(argv: list[str], deadline: float, what: str) -> str:
    remaining = deadline - time.monotonic()
    if remaining < 1.0:
        raise _Unstamped("the hook ran out of time")
    try:
        res = _run(["gh", *argv], remaining)
    except subprocess.TimeoutExpired:
        raise _Unstamped(f"{what} timed out") from None
    except OSError as exc:
        raise _Unstamped(f"gh could not start ({type(exc).__name__})") from None
    if res.returncode != 0:
        # stderr is not echoed: a rate-limit message carries the account id.
        raise _Unstamped(f"{what} failed (gh exited {res.returncode})")
    return res.stdout


def _config() -> dict:
    """Public defaults, then the repo's config file, then the install overlay."""
    conf = dict(_DEFAULTS)
    home = Path(os.environ.get("GENESIS_HOME") or Path.home() / ".genesis").expanduser()
    for path in (
        _SELF_ROOT / "config" / f"{_CONFIG_NAME}.yaml",
        home / "config" / f"{_CONFIG_NAME}.local.yaml",
    ):
        try:
            import yaml

            loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - absent, unreadable or no yaml: this layer says nothing
            continue
        if not isinstance(loaded, dict):
            continue
        for key in ("settle_markers", "insert_before"):
            if isinstance(loaded.get(key), list):
                conf[key] = [m for m in loaded[key] if isinstance(m, str) and m]
        wait = loaded.get("settle_wait_s")
        if isinstance(wait, (int, float)) and not isinstance(wait, bool) and 0 <= wait <= 30:
            conf["settle_wait_s"] = wait
    return conf


def session_prefix(payload: dict) -> str | None:
    """prepare-commit-msg's rule (first non-empty of GENESIS_SESSION_ID,
    CLAUDE_CODE_SESSION_ID, CLAUDE_SESSION_ID; its leading 8 hex), then the
    hook payload's session_id. Lowercase, as the line requires."""
    raw = next(
        (
            os.environ[k]
            for k in ("GENESIS_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")
            if os.environ.get(k)
        ),
        None,
    )
    if raw is None and isinstance(payload.get("session_id"), str):
        raw = payload["session_id"]
    m = _SESSION.match((raw or "").splitlines()[0] if raw else "")
    return m.group(1).lower() if m else None


def with_line(body: str, prefix: str, insert_before: list[str], lookup) -> str:
    """``body`` carrying exactly this session's line: another id's line is replaced
    in place; otherwise the line goes before the first marker that starts a line,
    else at the end."""
    line = f"Opened-by-session: {prefix}"
    text = (body or "").replace("\r\n", "\n")
    if not text.strip():
        return line + "\n"
    lines = text.split("\n")
    fenced = lookup.fenced_flags(lines)
    for i, raw in enumerate(lines):
        if not fenced[i] and lookup.LINE.match(raw):
            lines[i] = line
            return "\n".join(lines)
    for i, raw in enumerate(lines):
        if not fenced[i] and any(raw.startswith(m) for m in insert_before):
            head = "\n".join(lines[:i]).rstrip("\n")
            return (head + "\n\n" if head else "") + line + "\n\n" + "\n".join(lines[i:])
    if lookup.fenced_flags([*lines, ""])[-1]:  # a fence still open at the end
        return line + "\n\n" + text
    return text.rstrip("\n") + "\n\n" + line + "\n"


def _read(url: str, deadline: float) -> dict:
    out = _gh(
        ["pr", "view", url, "--json", "number,author,createdAt,body"], deadline, "reading the PR"
    )
    try:
        data = json.loads(out)
    except ValueError:
        raise _Unstamped("gh returned output this hook could not read") from None
    if not isinstance(data, dict):
        raise _Unstamped("gh returned output this hook could not read")
    return data


def _local(lookup) -> set[str]:
    """This checkout's github.com repositories; unreadable remotes stop the stamp, said."""
    try:
        return lookup.local_repos()
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        raise _Unstamped("this checkout's git remotes could not be read") from None


def _own(repo: str, pr: dict, local: set[str]) -> bool:
    """A repository this checkout has as a github.com remote, and a PR its owner opened.

    A PR URL can reach the output from fetched text, so a repo merely named there
    is never enough: it must be one of this install's own remotes."""
    login = ((pr.get("author") or {}).get("login") or "").casefold()
    return repo.casefold() in local and bool(login) and login == repo.split("/", 1)[0].casefold()


def _write(url: str, body: str, deadline: float) -> None:
    fd, path = tempfile.mkstemp(prefix="pr-opener-stamp-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        _gh(["pr", "edit", url, "--body-file", path], deadline, "writing the PR body")
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


def _settle(url: str, pr: dict, conf: dict, deadline: float) -> dict:
    """Wait for a configured bot to finish its first edit, or the body to change."""
    markers, first = conf["settle_markers"], pr.get("body") or ""
    if not markers or any(m in first for m in markers):
        return pr
    stop = min(deadline - 15.0, time.monotonic() + float(conf["settle_wait_s"]))
    while time.monotonic() + _POLL_S <= stop:
        time.sleep(_POLL_S)
        pr = _read(url, deadline)
        body = pr.get("body") or ""
        if body != first or any(m in body for m in markers):
            break
    return pr


def _apply(url: str, prefix: str, conf: dict, deadline: float, lookup, pr: dict) -> str:
    """Write the line, then re-read once a few seconds later and re-apply it once
    if a late writer dropped it. Returns "kept" (there at the end), "removed" (a
    later write dropped it twice) or "unchecked: <why>" (written, but the re-read
    could not run). Only the first write failing raises."""
    _write(url, with_line(pr.get("body") or "", prefix, conf["insert_before"], lookup), deadline)
    try:
        for attempt in range(2):
            if deadline - time.monotonic() > _RECHECK_S + 5:
                time.sleep(_RECHECK_S)
            now = _read(url, deadline).get("body") or ""
            if lookup.stamped(now) == prefix:
                return "kept"
            if attempt == 0:
                _write(url, with_line(now, prefix, conf["insert_before"], lookup), deadline)
    except _Unstamped as why:
        return f"unchecked: {why}"
    return "removed"


def _outcome(url: str, prefix: str, result: str, done: str) -> str:
    if result == "removed":
        return _manual(url, prefix, "a later edit removed it twice")
    if result.startswith("unchecked: "):
        return (
            f"{done} It was written, but the re-read that catches a bot's late write "
            f"could not run ({result[len('unchecked: ') :]}); `{_lookup_cmd(url)}` "
            "shows whether it is still there."
        )
    return done


def _call_window(payload: dict) -> timedelta:
    try:
        duration = timedelta(milliseconds=float(payload.get("duration_ms")))
    except (TypeError, ValueError, OverflowError):
        duration = _DEFAULT_DURATION
    if not timedelta(0) <= duration <= _MAX_DURATION:
        duration = _DEFAULT_DURATION
    return duration + _PAST_SLACK


def _created_now(pr: dict, payload: dict, now: datetime) -> bool:
    try:
        created = datetime.fromisoformat(str(pr.get("createdAt")).replace("Z", "+00:00"))
    except ValueError:
        return False
    return (
        created.tzinfo is not None and now - _call_window(payload) <= created <= now + _FUTURE_SLACK
    )


def _manual(url: str | None, prefix: str | None, why: str) -> str:
    line = (
        f"`Opened-by-session: {prefix}`"
        if prefix
        else "an `Opened-by-session: <your session's 8 hex>` line"
    )
    m = _PR_URL.match(url or "")
    target = f"PR #{m['num']} at {url}" if m else (url or "the new PR")
    return (
        f"[pr-opener-stamp] Could not stamp {target} ({why}). Add {line} to its body by hand "
        f"(`gh pr edit {url or '<url>'} --body-file <file>`); `{_lookup_cmd(url)}` reads it."
    )


def _lookup_cmd(url: str | None) -> str:
    """The lookup's command line for a PR URL (it takes a number and --repo)."""
    m = _PR_URL.match(url or "")
    if not m:
        return "scripts/pr_opener.py <N> --repo <owner/name>"
    return f"scripts/pr_opener.py {m['num']} --repo {m['repo']}"


def _stamp(
    url: str,
    repo: str,
    num: int,
    prefix: str,
    confirmed: bool,
    payload: dict,
    now: datetime,
    conf: dict,
    deadline: float,
    lookup,
) -> str | None:
    pr = _read(url, deadline)
    fresh = _created_now(pr, payload, now)
    if not fresh and not confirmed:
        return None  # a PR another step printed, not one this call created
    if not _own(repo, pr, _local(lookup)):
        return None
    current = lookup.stamped(pr.get("body") or "")
    if current == prefix:
        return f"[pr-opener-stamp] PR #{num} already carries `Opened-by-session: {prefix}`."
    if not fresh:
        # Claude Code's record says "created", GitHub says earlier: never overwrite
        # whatever an older PR already carries.
        return _manual(url, prefix, "GitHub says it was created before this call")
    if current and not confirmed:
        # Found only as a URL in the output: another session may have opened it
        # in the same window and stamped it already. Never re-attribute that.
        return None
    pr = _settle(url, pr, conf, deadline)
    result = _apply(url, prefix, conf, deadline, lookup, pr)
    return _outcome(
        url,
        prefix,
        result,
        f"[pr-opener-stamp] Stamped PR #{num} with `Opened-by-session: {prefix}` (the session "
        "that opened it). If you later rewrite its body from a local file, add that line to "
        "the file; a rewrite this hook sees that drops it is repaired.",
    )


def _heal(url: str, repo: str, num: int, conf: dict, deadline: float, lookup) -> str | None:
    pr = _read(url, deadline)
    if not _own(repo, pr, _local(lookup)) or lookup.stamped(pr.get("body") or ""):
        return None
    owner, name = repo.split("/", 1)
    out = _gh(
        [
            "api",
            "graphql",
            "-f",
            f"query={lookup._EDITS_QUERY}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-F",
            f"number={num}",
        ],
        deadline,
        "reading the PR's edit history",
    )
    try:
        nodes = json.loads(out)["data"]["repository"]["pullRequest"]["userContentEdits"]["nodes"]
    except (ValueError, KeyError, TypeError):
        raise _Unstamped("GitHub's edit history could not be read") from None
    original, source = lookup.opener(pr.get("body") or "", nodes, [], pr.get("createdAt"))
    if source != "history" or not original:
        return None  # never stamped: opened before this hook, or outside a session
    result = _apply(url, original, conf, deadline, lookup, pr)
    return _outcome(
        url,
        original,
        result,
        f"[pr-opener-stamp] The edit to PR #{num} dropped its `Opened-by-session: {original}` "
        "line; it is restored with the session that opened the PR. Add the line to your "
        "local body file so the next rewrite keeps it.",
    )


def _output_text(payload: dict, event: str) -> str:
    parts: list[str] = []
    if event == "PostToolUse":
        resp = tool_response(payload)
        parts += [resp[k] for k in ("stdout", "stderr") if isinstance(resp.get(k), str)]
    if isinstance(payload.get("error"), str):
        parts.append(payload["error"])
    return "\n".join(parts)


def _git_pr(payload: dict, event: str) -> tuple[str, str, int, str] | None:
    """(action, repo, number, url) from Claude Code's gitOperation record."""
    if event != "PostToolUse":
        return None
    op = tool_response(payload).get("gitOperation")
    pr = op.get("pr") if isinstance(op, dict) else None
    if not isinstance(pr, dict) or not isinstance(pr.get("url"), str):
        return None
    m = _PR_URL.match(pr["url"])
    if not m or pr.get("action") not in ("created", "edited"):
        return None
    return pr["action"], m["repo"], int(m["num"]), m["url"]


def _process(payload: dict, now: datetime | None = None) -> None:
    event = (
        "PostToolUseFailure"
        if payload.get("hook_event_name") == "PostToolUseFailure"
        else "PostToolUse"
    )
    cmd = (tool_input(payload) or {}).get("command") or ""
    op = _git_pr(payload, event)
    if op is None and not _GH_WORD.search(cmd):
        return
    creates: dict[str, tuple[str, int, bool]] = {}
    edits: dict[str, tuple[str, int]] = {}
    if op and op[0] == "created":
        creates[op[3]] = (op[1], op[2], True)
    elif op:
        edits[op[3]] = (op[1], op[2])
    if _CREATE_WORDS.search(cmd):
        if event == "PostToolUse" and tool_response(payload).get("backgroundTaskId"):
            _emit(
                _manual(
                    None,
                    session_prefix(payload),
                    "it ran in the background, so its result is not in this call",
                ),
                event,
            )
            return
        if not creates:  # Claude Code named this call's PR; nothing else is ours
            for m in _PR_URL.finditer(_output_text(payload, event)):
                creates.setdefault(m["url"], (m["repo"], int(m["num"]), False))
    if not creates and not edits:
        return
    prefix = session_prefix(payload)
    conf = _config()
    try:
        lookup = _lookup()
    except Exception as exc:  # noqa: BLE001 - a broken sibling is said, never silent
        if creates:
            _emit(
                _manual(
                    next(iter(creates)),
                    prefix,
                    f"scripts/pr_opener.py could not be loaded ({type(exc).__name__})",
                ),
                event,
            )
        return
    now = now or datetime.now(UTC)
    deadline = time.monotonic() + _DEADLINE_S
    notes: list[str] = []
    for url, (repo, num, confirmed) in list(creates.items())[:_MAX_PRS]:
        try:
            if prefix is None:
                raise _Unstamped("no session id was found in the environment or the hook payload")
            note = _stamp(url, repo, num, prefix, confirmed, payload, now, conf, deadline, lookup)
        except _Unstamped as why:
            # An unconfirmed URL may be a PR another step printed: say so.
            why_text = str(why) if confirmed else f"{why}; if this command opened it"
            note = _manual(url, prefix, why_text)
        if note:
            notes.append(note)
    for url, (repo, num) in edits.items():
        try:
            note = _heal(url, repo, num, conf, deadline, lookup)
        except _Unstamped as why:
            note = f"[pr-opener-stamp] Could not check PR #{num} kept its opener line ({why})."
        if note:
            notes.append(note)
    if notes:
        _emit("\n\n".join(notes), event)


def _emit(context: str, event: str) -> None:
    print_json_bounded(
        {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}},
        text_keys=("hookSpecificOutput.additionalContext",),
    )


def main() -> int:
    try:
        _process(read_payload())
    except Exception as exc:  # noqa: BLE001 - advisory: never block on our own bug
        print(f"pr_opener_stamp: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
