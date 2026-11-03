#!/usr/bin/env python3
"""PreToolUse (Bash): `gh pr create` needs an independent review of the branch first.

The commit gate's marker proves a session wrote an evidence file, not that anyone
else read the change (``review_state.py``: "NOT tamper-proof"). This reads what Claude
Code itself records instead: the session's subagent transcripts. A PR opens when a
reviewer subagent of this session finished a run tied to this branch, or when the
owner said yes in chat and the session added ``# pr-review-waived``.

GUARD AXIOMS
  VERDICT: block, with a logged override. An owner ruling, not a measured harm: the
    audit behind it found 5-18% of 496 PRs unreviewed and no significant difference
    in later findings.
  AUDIENCE: the agent. The message says what to run.
  BACKGROUND: a normal dispatch runs outside any repo, so repo hooks never load
    (``cc/invoker.py``, ``cc_span_settings_path``) and this does not fire. A dispatch
    inside a worktree does load it: it can run a reviewer, and the override is refused
    because nobody is there to give the yes.

WHY NOTHING IS STORED. Everything needed already exists when the question is asked, so
this adds no marker, no receipt and nothing to retire. The threat model is autopilot,
not an adversary: hooks run as the session's user, so a session could forge a file.

SCOPE. Only where ``genesis.yaml`` declares a public repo and the create targets it (an
explicit ``--repo`` or ``GH_REPO``, else any remote of the checkout). No declared repo
means out of scope, as for ``_no_pr_block_applies`` and for its reason: a clone that
declares nothing must not be refused. An unreadable ``genesis.yaml`` looks the same, so it
turns this off too, and the session is told. The wiring ships to every clone; this
predicate is what confines it. OUT OF SCOPE IS A POSITIVE ANSWER: another repo named,
every remote naming another repo, or git saying this directory is no repository or has no
remote. A target that could not be established is "could not verify", never out of scope.

FAIL DIRECTION. Evidence that cannot be read blocks and says "could not verify", since
failing open would retire the gate silently on a Claude Code format change. The repair
path is the override. A module that cannot load blocks through ``degraded_exit``.

NOT COVERED: Codex (it does not run these hooks), the server-side openers (they run
``gh`` as a subprocess), spellings the parser cannot resolve to ``gh`` (a program held
in a variable, an alias, a shell function, a script, a command piped to ``bash``, process
substitution, ``ssh``, ``gh api``, ``hub``), and a script earlier in the command that
changes the branch. A command the parser cannot read at all (over its length bound, a
subcommand built by expansion, ``eval``) is not judged here: the push guard's blind-spot
net refuses it, and a test pins that.

A REVIEW COUNTS FOR THE BRANCH, not for a commit: review then fix is the normal flow,
so later commits do not void it. A review of the branch's first commit therefore
still passes after fifty more. What the reviewer concluded is not read, and neither is
what it was asked: a finished reviewer tied to the branch counts, even one asked a design
question, and a branch with no commits passes the same way. The tie is loose on purpose: a
record's ``gitBranch`` is the SESSION's branch when the reviewer started, not the reviewer's
own (equal in 940 of 943 reviewers), so a session on a branch ties any reviewer it runs.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from hook_input import degraded_exit, field, read_payload, run_guard  # noqa: E402
except Exception:  # noqa: BLE001 — exit 1 is NON-blocking; see degraded_exit.
    if __name__ != "__main__":
        raise
    # hook_input is what would report this, so report it by hand. os._exit so a broken
    # diagnostic stream cannot replace the exit 2.
    try:
        print(
            "GUARD DEGRADED (pr_open_review_gate): shared hook_input is incompatible; "
            "BLOCKING until the hook tree is repaired.",
            file=sys.stderr,
        )
    finally:
        os._exit(2)

# Literal, and above the import it recovers from. Over-blocking here is one repair away.
_DEGRADED_GATED = r"\bgh\b[^\n]*\bpr\b[^\n]*\b(?:create|new)\b"

try:
    from hook_output import print_json_bounded  # noqa: E402
    from shell_parse import (  # noqa: E402
        _GH_FLAG_TABLE,
        _gh_option,
        analyze_checked,
        gh_command,
        gh_pr_subcommand,
        gh_requests_help,
        git_subcommand,
        git_subcommand_index,
        has_trailing_override,
        mentions,
    )
except Exception as _exc:  # noqa: BLE001 — exit 1 is NON-blocking; see degraded_exit.
    if __name__ != "__main__":
        raise
    degraded_exit("pr_open_review_gate", gated=_DEGRADED_GATED, exc=_exc)

SIGIL = "pr-review-waived"

#: The whole word pair, read through `mentions` as `git_push_guard` reads it: a word split
#: by quotes or a line break is still named, in the text the shell assembles.
_MENTION_GH = re.compile(r"\bgh\b")
_MENTION_CREATE = re.compile(r"\b(?:create|new)\b")

#: Reviewers, a closed list: a builder that happens to read code is not a review.
_REVIEWER_TYPES = frozenset(
    {
        "genesis-architect",
        "genesis-security-reviewer",
        "feature-dev:code-reviewer",
        "pr-review-toolkit:code-reviewer",
        "pr-review-toolkit:silent-failure-hunter",
        "pr-review-toolkit:pr-test-analyzer",
        "pr-review-toolkit:type-design-analyzer",
        "pr-review-toolkit:comment-analyzer",
    }
)
#: The built-in /code-review leaves no reviewer type: it runs as `general-purpose` with
#: this recipe header as its prompt (measured on Claude Code 2.1.299 at low and high). If a
#: later version rewords it, the gate stops and names /deep-review, which still counts.
_CODE_REVIEW_RECIPE = re.compile(r"`\w+ effort → ")

#: Earlier steps of a command that leave the branch where it was. Only git's own
#: subcommands are judged this way: the parser also reads heredoc body lines as steps, so
#: "anything unknown moved the branch" would refuse most real creates (219 of 388 had an
#: earlier step; measured over 400 sessions). `remote` is absent: it can create the target.
_STILL_ON_BRANCH_GIT = frozenset(
    {
        "add",
        "commit",
        "diff",
        "fetch",
        "grep",
        "log",
        "ls-files",
        "merge-base",
        "pull",
        "push",
        "rev-list",
        "rev-parse",
        "show",
        "status",
    }
)
#: Earlier steps that may change which repository the directory is, or points at.
_RETARGETING_GIT = frozenset({"clone", "config", "init", "remote", "worktree"})
#: Quoted text, blanked before looking for an assignment: a PR title may mention one.
_QUOTED = re.compile("'[^']*'|\"[^\"]*\"")
_SETS_GH_REPO = re.compile(
    r"\bGH_REPO=|\bunset\b[^;&|\n]*\bGH_REPO\b|\benv\b[^;&|\n]*-u\s*GH_REPO\b"
)
_QUOTED_GH_REPO = re.compile("['\"]GH_REPO=")
#: Steps that change directory, followed by ``_effective_cwd`` only in the plain `cd` form.
_DIRECTORY_STEPS = frozenset({"cd", "pushd", "popd"})
_ENV_CHDIR = re.compile(r"\benv\b[^;&|\n]*\s(?:-C|--chdir)")

#: Each create re-reads the whole command, so a command with hundreds of them could outrun
#: the budget, and a killed hook lets it run. 387 of 388 real commands held one create and
#: the other held two (measured over 400 sessions).
_MAX_CREATES = 20

#: The whole run, git and transcripts together, against the 30 s registration in
#: settings.json: the harness killing a hook lets the command RUN. Measured on a live
#: install: 0.4 s at worst across 150 real creates.
_BUDGET_S = 20.0
_GIT_TIMEOUT_S = 5.0
_deadline: float | None = None
#: A first line carries the whole prompt, which can be long; past this it is not read.
_FIRST_LINE_MAX = 2 * 1024 * 1024
#: The final assistant line is near the end. A run that ends in a larger tool output
#: than this reads as unfinished, which asks for one more review, not a wrong pass.
_TAIL_MAX = 4 * 1024 * 1024
#: A meta file is a few short fields.
_META_MAX = 64 * 1024


class _Unverifiable(Exception):
    """The evidence could not be read: distinct from "there is none"."""

    remedy = (
        "A review alone does not clear this. Where you can, run one plain `gh pr create` on "
        "its own, with literal --repo and --head values, and retry."
    )


class _RecordsUnreadable(_Unverifiable):
    """The session's subagent records could not be read. A readable finished reviewer settles it."""

    remedy = (
        "Run /deep-review or a code-reviewer agent, wait for it to finish, then retry. If this "
        "still says so, Claude Code's record format has changed."
    )


def _time_left() -> float:
    """Seconds left of the one budget this run has; raises when it is spent."""
    if _deadline is None:
        return _GIT_TIMEOUT_S
    left = _deadline - time.monotonic()
    if left <= 0:
        raise _Unverifiable("the check ran past the hook's time budget")
    return left


def _git_run(cwd: str | None, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *(["-C", cwd] if cwd else []), *args],
            capture_output=True,
            text=True,
            timeout=min(_GIT_TIMEOUT_S, _time_left()),
            env={**os.environ, "LC_ALL": "C"},  # its message is read below
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _Unverifiable(f"git could not be read ({type(exc).__name__})") from exc


def _git(cwd: str | None, *args: str) -> str | None:
    """A git read in ``cwd``: None if git answered no, raises if it did not answer."""
    done = _git_run(cwd, *args)
    return done.stdout.strip() if done.returncode == 0 else None


def _remotes(cwd: str | None) -> str:
    """This directory's remotes; empty only when git says it is no repository or has none.

    A nonzero exit is not that answer: a directory an earlier step has yet to create, a
    corrupt config and refused ownership all exit 128 too.
    """
    done = _git_run(cwd, "remote", "-v")
    if done.returncode == 0:
        return done.stdout
    if "not a git repository" in done.stderr:
        return ""
    raise _Unverifiable("git could not read this directory's remotes")


def _names(text: str, name: str, *, path_tail_ok: bool = False) -> bool:
    """Whether ``text`` names ``name`` as a whole token, not inside a longer word.

    A branch is not named by ``feat/x/other``. A worktree is named by ``<wt>/a.py``.
    """
    after = r"[\w-]" if path_tail_ok else r"[\w/-]"
    return re.search(rf"(?<![\w-]){re.escape(name)}(?!{after})", text) is not None


def _branch_is_distinctive(head: str) -> bool:
    """A branch called `fix` or `dev` is a word in nearly any prompt, not a tie."""
    return "/" in head or "-" in head


def _worktree_named(prompt: str, worktree: str) -> bool:
    if "-" in worktree:
        return _names(prompt, worktree, path_tail_ok=True)
    # A directory called `review` is not named by "please review this": only by a path.
    return re.search(rf"/{re.escape(worktree)}(?![\w-])", prompt) is not None


def _require_regular(path: Path) -> None:
    """Refuse a FIFO or device: opening one blocks past any budget, and a killed
    hook lets the command run."""
    if not stat.S_ISREG(os.stat(path).st_mode):
        raise OSError(f"not a regular file: {path.name}")


def _first_line(path: Path) -> dict:
    _require_regular(path)
    with open(path, errors="replace") as f:
        line = f.readline(_FIRST_LINE_MAX)
    return json.loads(line)


def _prompt_text(line: dict) -> str:
    content = (line.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    return " ".join(str(b.get("text") or "") for b in content or [] if isinstance(b, dict))


def _final_message(path: Path) -> dict | None:
    """The message of the run's last assistant line, if there is one."""
    _require_regular(path)
    size = path.stat().st_size
    with open(path, "rb") as f:
        f.seek(max(0, size - _TAIL_MAX))
        chunk = f.read().decode(errors="replace")
    lines = chunk.split("\n")
    if size > _TAIL_MAX:
        lines = lines[1:]  # the cut landed mid-line
    for raw in reversed(lines):
        _time_left()
        if not raw.strip():
            continue
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        if line.get("type") != "assistant":
            continue
        return line.get("message") or {}
    return None


def _finished(message: dict | None) -> bool:
    """Whether a run's final assistant turn ended it normally.

    Not "the last line is text": a running reviewer's lines are often text-only, and one
    stopped by a usage limit ends in text on a synthetic model line (both measured over
    1,183 reviewer transcripts).
    """
    if message is None:
        return False
    return message.get("stop_reason") == "end_turn" and message.get("model") != "<synthetic>"


def _is_inside(path: object, checkout: str) -> bool:
    return isinstance(path, str) and (path == checkout or path.startswith(f"{checkout}/"))


def _is_reviewer(agent_type: str, prompt: str) -> bool:
    if agent_type in _REVIEWER_TYPES:
        return True
    return agent_type == "general-purpose" and _CODE_REVIEW_RECIPE.match(prompt) is not None


def has_reviewer_evidence(
    transcript_path: str, head: str, checkout: str | None, worktree: str | None
) -> bool:
    """Whether a reviewer of this session finished a run tied to ``head``.

    Raises :class:`_Unverifiable` when the session's records cannot be read, or none of
    them carries a field this reads, which is what a Claude Code format change looks like:
    "no review" would send the session to run a reviewer that could never clear it. A
    session with no subagents simply has none: the directory is created lazily.
    """
    if not transcript_path or not transcript_path.endswith(".jsonl"):
        raise _RecordsUnreadable("the hook payload names no session transcript")
    if not Path(transcript_path).exists():
        raise _RecordsUnreadable("the session transcript is not on disk")
    agents = Path(transcript_path[: -len(".jsonl")]) / "subagents"
    if not agents.is_dir():
        return False
    try:
        metas = sorted(
            agents.glob("agent-*.meta.json"), key=lambda p: p.stat().st_mtime, reverse=True
        )
    except OSError as exc:
        raise _RecordsUnreadable(
            f"the subagent records could not be listed ({type(exc).__name__})"
        ) from exc
    typed = candidates = readable = ended = keyed = 0
    for meta in metas:
        _time_left()
        try:
            _require_regular(meta)
            with open(meta) as f:
                agent_type = json.loads(f.read(_META_MAX)).get("agentType")
            if not isinstance(agent_type, str):
                continue
            typed += 1
            if agent_type not in _REVIEWER_TYPES and agent_type != "general-purpose":
                continue
            candidates += 1
            transcript = meta.with_name(meta.name[: -len(".meta.json")] + ".jsonl")
            first = _first_line(transcript)
            if first.get("gitBranch") or first.get("cwd"):
                readable += 1
            prompt = _prompt_text(first)
            if not _is_reviewer(agent_type, prompt):
                continue
            tied = (
                first.get("gitBranch") == head
                or (checkout and _is_inside(first.get("cwd"), checkout))
                or (_branch_is_distinctive(head) and _names(prompt, head))
                or (worktree is not None and _worktree_named(prompt, worktree))
            )
            if not tied:
                continue
            message = _final_message(transcript)
            if message is not None:
                ended += 1
                keyed += "stop_reason" in message
            if _finished(message):
                return True
        except (OSError, ValueError, AttributeError, TypeError):
            continue  # one unreadable record is not evidence either way; the rest still count
    if metas and not typed:
        raise _RecordsUnreadable("no subagent record carries a readable agent type")
    if candidates and not readable:
        raise _RecordsUnreadable("no subagent record carries a readable branch or directory")
    if ended and not keyed:
        raise _RecordsUnreadable("no subagent record says how its run ended")
    return False


class _CreateFlags(NamedTuple):
    repo: str | None
    head: str | None
    dry_run: bool
    unknown: bool  # a dashed token no table knows: it may have taken the next word


def _option_parts(tok: str, values: frozenset[str]) -> list[tuple[str, str | None]]:
    """``(name, glued value)`` for each option in a token, pflag's way.

    ``-dRo/r`` is ``-d`` then ``-R o/r``: gh accepts a cluster (measured), and a repo in one
    must not read as no repo. A short gh does not know is kept, so the rest is still read.
    """
    if tok.startswith("--") or len(tok) <= 2:
        return [_gh_option(tok, values)]
    parts: list[tuple[str, str | None]] = []
    for pos in range(1, len(tok)):
        short, rest = f"-{tok[pos]}", tok[pos + 1 :]
        if short in values:
            parts.append((short, rest.removeprefix("=") if rest else None))
            return parts
        if rest.startswith("="):
            parts.append((short, rest[1:]))
            return parts
        parts.append((short, None))
    return parts


def _create_flags(argv: list[str]) -> _CreateFlags:
    """The options of a create, read as gh reads them: a flag's VALUE is not a flag.

    ``--title "-Ra/b"`` names no repo and ``--body --dry-run`` is a body, so these come
    from the table ``gh_requests_help`` walks, in one pass, last value winning. Before the
    command path gh accepts only ``-R``/``--repo`` here; after it, the ``pr create`` row.
    """
    inv = gh_command(argv)
    row_values, row_bools = _GH_FLAG_TABLE[("pr", "create")]
    before_path = frozenset({"-R", "--repo"})
    repo = head = None
    dry_run = unknown = False
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            break
        if not tok.startswith("-") or tok == "-":
            i += 1
            continue
        pre = inv is not None and i < inv.path_end
        values = before_path if pre else row_values
        step = 1
        for name, glued in _option_parts(tok, values):
            if name in values:
                value = glued
                if value is None:
                    value = argv[i + 1] if i + 1 < len(argv) else ""
                    step = 2
                if name in ("-R", "--repo"):
                    repo = value
                elif name in ("-H", "--head"):
                    head = value
            elif not pre and name in row_bools:
                # pflag: `--dry-run=false` is a real create, and only a true value skips it.
                if name == "--dry-run":
                    dry_run = glued is None or glued.casefold() in ("true", "t", "1")
            else:
                unknown = True
        i += step
    return _CreateFlags(repo, head, dry_run, unknown)


def _create_segments(segs: list) -> list:
    return [
        s
        for s in segs
        if gh_pr_subcommand(s.argv) in ("create", "new")
        and not gh_requests_help(s.argv)
        and not _create_flags(s.argv).dry_run
    ]


def _identity_is_public(identity: tuple[str, str], canonical: str) -> bool:
    """`www.github.com` and `ssh.github.com` reach the same repo through gh (measured)."""
    host, repo = identity
    return (host == "github.com" or host.endswith(".github.com")) and repo == canonical.casefold()


def _repo_is_public(guard, value: str, canonical: str) -> bool:
    """Whether a repo selector names the public repo, in any spelling gh accepts."""
    # gh reads `<url>?x=1` and `<url>#f` as the same repo (measured); the identity below does
    # not. An expansion or glob is not yet the value the shell will pass.
    if not value.strip() or re.search(r"[$`?#{}*\[~]", value):
        raise _Unverifiable("the repository selector is not a plain repository name")
    identity = guard._repo_identity_from_url(value)  # a URL, scp form or host-qualified path
    if identity is not None:
        return _identity_is_public(identity, canonical)
    normalized = guard._normalize_repo(value)
    if normalized is None:
        raise _Unverifiable("the repository selector could not be read")
    return normalized.casefold() == canonical.casefold()


def _targets_public_repo(
    guard, flags: _CreateFlags, cmd: str, cwd, canonical: str, *, retargeted: bool
) -> bool:
    if flags.repo is not None:
        return _repo_is_public(guard, flags.repo, canonical)
    if _SETS_GH_REPO.search(_QUOTED.sub("", cmd)) or _QUOTED_GH_REPO.search(cmd):
        raise _Unverifiable("GH_REPO is changed in the command")
    if os.environ.get("GH_REPO"):
        return _repo_is_public(guard, os.environ["GH_REPO"], canonical)
    if cwd is guard._CWD_UNKNOWN or cwd is None:
        raise _Unverifiable("the directory this command runs in could not be resolved")
    # By URL: a remote configured with none (say in global config) lists its name alone.
    urls = [w[1] for w in map(str.split, _remotes(cwd).splitlines()) if len(w) >= 2]
    identities = [i for i in map(guard._repo_identity_from_url, urls) if i is not None]
    if any(_identity_is_public(i, canonical) for i in identities):
        return True
    # gh follows an ssh alias from ~/.ssh/config to github.com (measured), so a remote with
    # this repo's name on a host it does not recognise is unproven either way.
    if any(repo == canonical.casefold() for _, repo in identities):
        raise _Unverifiable("a remote has this repository's name on a host that may be an alias")
    if retargeted:  # read before the step ran, so "another repo" may no longer hold
        raise _Unverifiable("an earlier step may change which repository this is")
    return False


def _git_step_only_reads(argv: list[str]) -> bool:
    """`git remote -v`, `git branch --show-current` and `git config <key>` change nothing."""
    i = git_subcommand_index(argv)
    if i is None:
        return False
    rest = argv[i + 1 :]
    if argv[i] == "remote":
        return all(w in ("-v", "--verbose") for w in rest)
    if argv[i] == "branch":
        return rest in ([], ["--show-current"], ["--list"])
    if argv[i] == "config":
        return len(rest) == 1 and not rest[0].startswith("-") or rest[:1] in (["--get"], ["--list"])
    return False


def _directory_untracked(guard, segs: list, seg) -> bool:
    """Whether a step changes directory in a way ``_effective_cwd`` does not follow.

    `if cd x`, `command cd x`, `pushd x`: the parser sees them, but only a segment that is
    exactly `cd <path>` moves the directory it reports, and `env -C` runs this one elsewhere.
    """
    if _ENV_CHDIR.search(_QUOTED.sub("", seg.raw)):
        return True
    for earlier in segs:
        if earlier is seg:
            return False
        if earlier.exe in _DIRECTORY_STEPS and guard._cd_target(earlier.raw) is None:
            return True
    return False


def _retargeted_before(segs: list, seg) -> bool:
    """Whether an earlier step of this command may change which repository a create reaches."""
    for earlier in segs:
        if earlier is seg:
            return False
        if _git_step_only_reads(earlier.argv):
            continue
        if earlier.exe == "git" and git_subcommand(earlier.argv) in _RETARGETING_GIT:
            return True
        if earlier.exe == "gh" and earlier.argv[1:2] == ["repo"]:
            return True
    return False


def _moved_before(segs: list, seg) -> bool:
    """Whether an earlier step of this command may change the branch or directory.

    The branch is read before the command runs, so after one of these it is stale. `cd`
    is not here because ``_effective_cwd`` follows it. A script cannot be told from a
    heredoc line, so it is not here either: see NOT COVERED.
    """
    for earlier in segs:
        if earlier is seg:
            return False
        if _git_step_only_reads(earlier.argv):
            continue
        if earlier.exe == "git" and git_subcommand(earlier.argv) not in _STILL_ON_BRANCH_GIT:
            return True
        if gh_pr_subcommand(earlier.argv) == "checkout":
            return True
        if earlier.exe == "gh" and earlier.argv[1:3] == ["repo", "clone"]:
            return True
    return False


def _linked_worktree_holding(cwd, head: str) -> str | None:
    """The linked worktree that has ``head`` checked out. The first entry is the main one."""
    listing = _git(cwd, "worktree", "list", "--porcelain") or ""
    for entry in listing.split("\n\n")[1:]:
        lines = entry.splitlines()
        if lines and f"branch refs/heads/{head}" in lines:
            return lines[0].removeprefix("worktree ")
    return None


def _branch_context(flags: _CreateFlags, cwd) -> tuple[str, str | None, str | None]:
    """``(head branch, directory that identifies it, linked worktree name)``; raises when unknowable."""
    raw_head = flags.head
    if raw_head is not None and ("$" in raw_head or "`" in raw_head or not raw_head):
        raise _Unverifiable("the --head value is not a literal branch")
    current = _git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD")
    head = raw_head.rsplit(":", 1)[-1] if raw_head else current
    if not head:
        raise _Unverifiable("the branch could not be read")
    checkout = _git(cwd, "rev-parse", "--show-toplevel")
    worktree = None
    if checkout:
        git_dir = _git(cwd, "rev-parse", "--absolute-git-dir")
        common = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
        # A linked worktree's directory identifies this work. The main checkout's does
        # not: "genesis" is in nearly every prompt, and every session starts there.
        if git_dir and common and git_dir != common:
            worktree = os.path.basename(checkout)
    # A directory, or its name, says which branch a reviewer read only when it holds that
    # branch: a review of this worktree says nothing about an unrelated --head.
    if head != current:
        held = _linked_worktree_holding(cwd, head)
        return head, held, os.path.basename(held) if held else None
    return head, checkout if worktree else None, worktree


def _refusal(head: str, reason: str | None) -> str:
    if reason:
        first = f"BLOCKED: could not verify a review of '{head}' before opening its PR: {reason}"
    else:
        first = (
            f"BLOCKED: no independent review of '{head}' found in this session, so its PR does "
            "not open.\nRun /deep-review, or a code-reviewer agent or /code-review from this "
            "branch's checkout, wait for it to finish, then retry. A reviewer started from "
            "elsewhere must name this branch or worktree in its prompt. A self-review does not "
            "count. If this command only writes text that mentions a create, use the Write tool: "
            "a heredoc body reads as commands here."
        )
    return (
        f"{first}\n"
        f"If the owner approves opening it unreviewed, ask in chat; on their yes append "
        f"`# {SIGIL}` to the create command (logged). A dispatched session cannot use it."
    )


def _judge(
    guard, segs: list, seg, cmd: str, payload: dict, canonical: str
) -> tuple[bool, str, str | None]:
    """``(needs a yes, head to name, why it could not be verified)`` for one create."""
    flags = _create_flags(seg.argv)
    head = (flags.head or "").rsplit(":", 1)[-1] or "this branch"
    try:
        cwd = guard._effective_cwd(cmd, payload, seg)
        _time_left()
        untracked = _directory_untracked(guard, segs, seg)
        if flags.repo is None and untracked:
            raise _Unverifiable("a step changes directory in a way this cannot follow")
        retargeted = _retargeted_before(segs, seg)
        if not _targets_public_repo(guard, flags, cmd, cwd, canonical, retargeted=retargeted):
            return False, head, None
        if flags.unknown:
            raise _Unverifiable("the command has an option this check does not know")
        if flags.head is None and (untracked or _moved_before(segs, seg)):
            raise _Unverifiable("an earlier step may change the branch; name it with --head")
        if sum(s.raw == seg.raw for s in segs) > 1:  # `_effective_cwd` stops at the first
            raise _Unverifiable("the same create appears twice; run them separately")
        if cwd is guard._CWD_UNKNOWN or cwd is None:
            raise _Unverifiable("the directory this command runs in could not be resolved")
        head, tied_dir, worktree = _branch_context(flags, cwd)
        transcript = payload.get("transcript_path") if isinstance(payload, dict) else None
        if has_reviewer_evidence(str(transcript or ""), head, tied_dir, worktree):
            return False, head, None
    except _Unverifiable as exc:
        return True, head, f"{exc}.\n{exc.remedy}"
    return True, head, None


def _head_sha(guard, cmd: str, payload: dict, seg) -> str | None:
    """The commit a waiver covers, for its log row. Never decides anything.

    None for an explicit ``--head``: this directory's HEAD may be another branch's.
    """
    if _create_flags(seg.argv).head is not None:
        return None
    try:
        cwd = guard._effective_cwd(cmd, payload, seg)
        return _git(cwd, "rev-parse", "HEAD") if isinstance(cwd, str) else None
    except _Unverifiable:
        return None


def _note_gate_is_off() -> None:
    """Tell the session when a config that exists declares no public repo.

    A typo there turns this off silently otherwise. A clone with no config stays quiet.
    """
    if not os.path.exists(os.path.expanduser("~/.genesis/config/genesis.yaml")):
        return
    print_json_bounded(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": (
                    "NOTE: genesis.yaml exists but declares no readable github.user and "
                    "github.public_repo, so the check that a PR has had a review is OFF "
                    "for this create. If this install has a public repo, fix that file."
                ),
            }
        },
        text_keys=("additionalContext",),
    )


def main() -> int:
    global _deadline
    _deadline = time.monotonic() + _BUDGET_S
    payload = read_payload()
    cmd = field(payload, "command")
    if not cmd or not mentions(cmd, _MENTION_GH, _MENTION_CREATE):
        return 0
    segs, blind = analyze_checked(cmd)
    if blind is not None and blind.bounds_induced:
        return 0  # no segments to judge, and the push guard's blind-spot net refuses it
    creates = _create_segments(segs)
    if not creates:
        return 0
    # The guard is 17k lines (0.45 s to import), so only a create pays for it.
    try:
        import git_push_guard as guard  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 — a load failure must not exit 1
        degraded_exit("pr_open_review_gate", gated=_DEGRADED_GATED, exc=exc)
    canonical = guard._canonical_public_repo()
    if not canonical:
        _note_gate_is_off()
        return 0  # no declared public repo: see SCOPE in the module docstring
    if len(creates) > _MAX_CREATES:
        print(
            f"BLOCKED: could not verify this command: it holds more than {_MAX_CREATES} PR "
            "creates, too many to judge in time. Run them separately.",
            file=sys.stderr,
        )
        return 2
    waived: list[tuple[str, str | None]] = []
    for seg in creates:
        needed, head, reason = _judge(guard, segs, seg, cmd, payload, canonical)
        if not needed:
            continue
        if not has_trailing_override(seg.raw, SIGIL):
            print(_refusal(head, reason), file=sys.stderr)
            return 2
        if guard._is_dispatched():
            print(
                f"BLOCKED: `# {SIGIL}` records the owner's yes in chat, and a dispatched "
                "session has nobody present to give it. Run a reviewer agent instead.",
                file=sys.stderr,
            )
            return 2
        waived.append((head, _head_sha(guard, cmd, payload, seg)))
    # Logged only now: a later create in the same command can still refuse the whole of it.
    for head, sha in waived:
        guard._note_override(SIGIL, waived="pr-open-review", head=sha)
        print(f"NOTE: opening '{head}' unreviewed on the owner's yes (logged).", file=sys.stderr)
    if waived:
        guard._flush_overrides("allowed")
    return 0


if __name__ == "__main__":
    run_guard(main, "pr_open_review_gate")
