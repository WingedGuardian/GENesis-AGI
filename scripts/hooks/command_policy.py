"""What a subagent may run of git and gh, judged on one command's argv.

Moved verbatim from ``git_push_guard.py``, whose ``_subagent_refusal(seg)``
now calls :func:`subagent_refusal`, so the guard's behaviour is unchanged. A
second caller, ``scripts/close_check.py``, builds a narrower read-only policy on
top of these predicates: they know which options make git run a program, how gh
swallows a word after an undeclared flag, and when a GraphQL document is a
mutation.

Imports only ``shell_parse``; no guard state.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shell_parse import (  # noqa: E402
    gh_api_request,
    gh_command,
    gh_requests_help,
    git_subcommand_index,
)

#: What a SUBAGENT may run of git and gh: an ALLOWLIST (owner ruling 2026-10-10).
#: The earlier denylist of writing commands lost a write verb to every review
#: round, because the write verbs of git, git-lfs and gh are an open set. Now a
#: segment is allowed only when it is on these lists; anything else, and anything
#: whose command this guard cannot read, is refused.
#:
#: git: every subcommand that works on the local repository, plus the network
#: READS (fetch, pull, clone, ls-remote, fetch-pack). Taken from
#: ``git --list-cmds=main,others`` (git 2.43) MINUS the network writers (push,
#: send-pack, http-push), the server and transport ends (receive-pack,
#: upload-pack, upload-archive, daemon, http-backend, shell, remote-*, instaweb),
#: mail (imap-send), credential access (credential*), commands whose job is to
#: run other commands (for-each-repo, filter-branch, difftool, mergetool, hook)
#: and git's internal helpers. Options that make a listed command run a program
#: are refused per command (``_GIT_RUNS_A_PROGRAM``, ``_GIT_REBASE_EXEC``); config
#: and environment that do the same (``-c core.sshCommand``) are not read. Not listed, so refused: a git alias, an external
#: ``git-<name>``, ``subtree``, ``svn``, ``send-email``, a newer git's command.
_GIT_SUBAGENT_OK = frozenset(
    """
    add am annotate apply archive bisect blame branch bugreport bundle cat-file
    check-attr check-ignore check-mailmap check-ref-format checkout
    checkout-index cherry cherry-pick clean clone column commit commit-graph
    commit-tree config count-objects describe diagnose diff diff-files
    diff-index diff-tree fast-export fast-import fetch fetch-pack fmt-merge-msg
    for-each-ref format-patch fsck fsck-objects gc get-tar-commit-id grep
    hash-object help index-pack init init-db interpret-trailers lfs log ls-files
    ls-remote ls-tree mailinfo mailsplit maintenance merge merge-base merge-file
    merge-index merge-tree mktag mktree multi-pack-index mv name-rev notes
    pack-objects pack-redundant pack-refs patch-id prune prune-packed pull
    quiltimport range-diff read-tree rebase reflog remote repack replace
    request-pull rerere reset restore rev-list rev-parse revert rm shortlog show
    show-branch show-index show-ref sparse-checkout stage stash status
    stripspace submodule switch symbolic-ref tag unpack-file unpack-objects
    update-index update-ref update-server-info var verify-commit verify-pack
    verify-tag version whatchanged worktree write-tree
    """.split()  # noqa: SIM905 - a word list kept readable
)
#: git commands that are families: their first word after the family name must
#: be one of these (none at all lists or prints help). Left out on purpose:
#: ``lfs push``/``lock``/``unlock`` (write the LFS server), ``lfs pre-push``,
#: ``submodule foreach`` and ``bisect run`` (run arbitrary commands).
_GIT_FAMILY_VERBS: dict[str, frozenset[str]] = {
    "lfs": frozenset({
        "checkout", "clone", "env", "fetch", "fsck", "install", "locks",
        "logs", "ls-files", "migrate", "pointer", "prune", "pull", "status",
        "track", "uninstall", "untrack", "update", "version", "help"
    }),
    "submodule": frozenset({
        "status", "summary", "init", "deinit", "update", "sync",
        "absorbgitdirs", "set-branch", "set-url", "add"
    }),
    "remote": frozenset({
        "add", "rename", "remove", "rm", "set-head", "set-branches",
        "get-url", "set-url", "show", "prune", "update"
    }),
    "bisect": frozenset({
        "start", "bad", "good", "new", "old", "terms", "skip", "reset",
        "visualize", "view", "replay", "log", "help"
    }),
}
#: Options that make an allowlisted git command run a program it names:
#: ``(long options, short option letters)``. git accepts any unambiguous prefix
#: of a long option, so a token counts when it is a prefix of one listed here
#: (four characters or more, which over-refuses a few harmless prefixes).
_GIT_RUNS_A_PROGRAM: dict[str, tuple[tuple[str, ...], str]] = {
    "fetch": (("--upload-pack",), ""),
    "pull": (("--upload-pack",), ""),
    "clone": (("--upload-pack", "--config", "--template"), "uc"),
    "ls-remote": (("--upload-pack",), "u"),
    "fetch-pack": (("--upload-pack", "--exec"), ""),
    "archive": (("--exec",), ""),
    "grep": (("--open-files-in-pager",), "O"),
}
#: ``git rebase --exec``/``-x`` runs a command per commit. git accepts any
#: unambiguous prefix of a long option, so ``--ex`` is ``--exec``.
_GIT_REBASE_EXEC = re.compile(r"--ex|-[A-Za-z]*x")

#: gh: the read-only subcommands of each group. Every other subcommand, and every
#: group not listed (``codespace``, ``browse``, ``completion``, an extension or a
#: ``gh alias`` name), is refused. ``auth`` keeps only ``status``: ``login`` can
#: upload an SSH key and ``refresh`` changes token scopes.
_GH_SUBAGENT_READS: dict[str, frozenset[str]] = {
    "pr": frozenset({"list", "ls", "view", "diff", "checks", "status", "checkout", "co"}),
    "issue": frozenset({"list", "ls", "view", "status"}),
    "run": frozenset({"list", "ls", "view", "watch", "download"}),
    "workflow": frozenset({"list", "ls", "view"}),
    "release": frozenset({"list", "ls", "view", "download", "verify", "verify-asset"}),
    "repo": frozenset({"list", "ls", "view", "clone", "gitignore", "license"}),
    "search": frozenset({"code", "commits", "issues", "prs", "repos"}),
    "label": frozenset({"list", "ls"}),
    "gist": frozenset({"list", "ls", "view", "clone"}),
    "cache": frozenset({"list", "ls"}),
    "secret": frozenset({"list", "ls"}),
    "variable": frozenset({"list", "ls", "get"}),
    "project": frozenset({"list", "ls", "view", "field-list", "item-list"}),
    "ruleset": frozenset({"list", "ls", "view", "check"}),
    "discussion": frozenset({"list", "ls", "view"}),
    "org": frozenset({"list", "ls"}),
    "extension": frozenset({"list", "ls", "search"}),
    "alias": frozenset({"list", "ls"}),
    "auth": frozenset({"status"}),
    "config": frozenset({"get", "list"}),
    "ssh-key": frozenset({"list", "ls"}),
    "gpg-key": frozenset({"list", "ls"}),
    "attestation": frozenset({"verify"}),
}
#: gh commands with no subcommands that only print: allowed whatever follows.
_GH_SUBAGENT_LEAF_READS = frozenset({"status", "version", "help", "reference", "licenses"})
#: gh's own aliases (``gh ext``, ``gh rs``; root ``gh co`` is ``gh pr checkout``).
_GH_GROUP_ALIASES = {
    "ext": "extension",
    "extensions": "extension",
    "rs": "ruleset",
    "at": "attestation",
}
_GH_ROOT_READ_ALIASES = frozenset({"co"})

#: A gh command word: what a real group, alias or extension name looks like
#: (extensions and aliases may carry capitals, digits, dots and underscores).
#: A segment whose "group" is anything else (``=`` from a here-doc line
#: ``gh = Fake()``) is text, not a gh call: gh would reject it as an unknown
#: command, so it runs nothing.
_GH_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

#: Before the verb, gh's command lookup treats any flag the group does not
#: declare as taking a value and swallows the next word (MEASURED, gh 2.101:
#: ``gh pr --admin view merge --help`` prints ``pr merge``'s help). Only these
#: are declared at the group level: ``-R``/``--repo`` take a value, help takes
#: none. ``--version`` is NOT one: it belongs to the root command alone, so
#: after a group word gh swallows the next word for it too (MEASURED:
#: ``gh pr --version view comment`` resolves to ``pr comment``).
_GH_PREPATH_VALUE = frozenset({"-R", "--repo"})
_GH_PREPATH_BOOL = frozenset({"-h", "--help"})

_GRAPHQL_READ_START = re.compile(r"\s*(?:(?:query|fragment)\b|\{)")
_GRAPHQL_NAME = re.compile(r"[_A-Za-z][_0-9A-Za-z]*")


def _graphql_without_literals(q: str) -> str | None:
    """``q`` with every GraphQL string, block string and comment replaced by a
    space, so ``mutation`` inside a search string or a comment is not read as an
    operation. Per the GraphQL lexical grammar: ``\"\"\"`` opens a block string
    closed by an unescaped ``\"\"\"`` (``\\\"\"\"`` is its only escape), ``"`` opens a
    string closed by an unescaped ``"`` on the same line, and ``#`` outside a
    string runs to the end of the line. None for an unterminated string: the
    caller then counts the query as a write."""
    out: list[str] = []
    i, n = 0, len(q)
    while i < n:
        if q.startswith('"""', i):
            j = i + 3
            while not q.startswith('"""', j):
                if j >= n:
                    return None
                j += 4 if q.startswith('\\"""', j) else 1
            i = j + 3
            out.append(" ")
        elif q[i] == '"':
            j = i + 1
            while j < n and q[j] != '"':
                if q[j] in "\r\n":
                    return None
                j += 2 if q[j] == "\\" else 1
            if j >= n:
                return None
            i = j + 1
            out.append(" ")
        elif q[i] == "#":
            while i < n and q[i] not in "\r\n":
                i += 1
            out.append(" ")
        else:
            out.append(q[i])
            i += 1
    return "".join(out)


def _graphql_is_query(q: str) -> bool:
    """Whether the GraphQL document ``q`` defines only queries (and fragments).

    An operation's kind is the keyword at the document's TOP LEVEL, outside every
    ``{}`` and ``()``: there ``mutation``/``subscription`` start an operation and
    ``{`` an anonymous query. Inside the brackets the same word is an alias, a
    field or a ``$variable``, and in a string or comment it is text, so neither
    makes a read a write. The document must also start as a query or a fragment
    (``query``, ``fragment`` or ``{``). Anything this cannot read is not a query: a shell expansion, an
    unterminated string, unbalanced brackets."""
    if "$(" in q or "${" in q or "`" in q or not _GRAPHQL_READ_START.match(q):
        return False
    bare = _graphql_without_literals(q)
    if bare is None:
        return False
    depth, top = 0, []
    for ch in bare:
        if ch in "{(":
            depth += 1
            top.append(" ")
        elif ch in "})":
            depth -= 1
            if depth < 0:
                return False
            top.append(" ")
        else:
            top.append(ch if depth == 0 else " ")
    if depth:
        return False
    return not {"mutation", "subscription"} & set(_GRAPHQL_NAME.findall("".join(top)))


def _gh_api_is_read(argv: list[str]) -> bool:
    """Whether a ``gh api`` argv only reads from GitHub.

    REST: the effective method is GET or HEAD (``shell_parse.gh_api_request``
    applies gh's defaulting: a parameter makes it POST unless ``-X GET`` keeps
    it a query string), with no ``--input``, no method-override header and no
    flag the parse does not model (its value could be a method). GraphQL is
    always POST, so it is a read only when every ``query`` field is a literal
    document :func:`_graphql_is_query` accepts and there is no ``--input``. A
    query from a file (``@``), a variable or a substitution is not a read."""
    req = gh_api_request(argv)
    if req is None or req.unmodelled or req.has_input:
        return False
    if any("method-override" in t.lower() for t in argv):
        return False
    if (req.endpoint or "").strip("/").lower() == "graphql":
        queries = [v for k, v in req.fields if k == "query"]
        return bool(queries) and all(_graphql_is_query(q) for q in queries)
    return req.method in ("GET", "HEAD")


def _gh_prepath_is_plain(argv: list[str], path_end: int) -> bool:
    """Whether every flag before the verb is one gh's lookup reads as we do.

    Any other flag there makes gh swallow the next word (see
    ``_GH_PREPATH_VALUE``), so the word this guard reads as the verb may be a
    value and the real verb the word after it: ``gh pr --admin view merge`` runs
    ``pr merge``. Rather than model which words are swallowed, such a command is
    not trusted as a read."""
    i = 1
    while i < path_end and i < len(argv):
        tok = argv[i]
        if tok.startswith("-") and tok != "-":
            name = tok.split("=", 1)[0]
            if name in _GH_PREPATH_VALUE or tok[:2] == "-R":
                i += 1 if ("=" in tok or (tok[:2] == "-R" and len(tok) > 2)) else 2
                continue
            if tok in _GH_PREPATH_BOOL:
                i += 1
                continue
            return False
        i += 1
    return True


def _git_subagent_refusal(argv: list[str]) -> str | None:
    """Why a subagent may not run this git argv, or None when it may."""
    idx = git_subcommand_index(argv)
    if idx is None:
        return None  # `git`, `git --version`, `git -C dir`: no subcommand runs
    sub, rest = argv[idx], argv[idx + 1 :]
    if sub not in _GIT_SUBAGENT_OK:
        return f"git {sub}"
    verbs = _GIT_FAMILY_VERBS.get(sub)
    if verbs is not None:
        word = next((t for t in rest if not t.startswith("-")), None)
        if word is not None and word not in verbs:
            return f"git {sub} {word}"
    if sub == "rebase" and any(_GIT_REBASE_EXEC.match(t) for t in rest):
        return "git rebase --exec"
    runs = _GIT_RUNS_A_PROGRAM.get(sub)
    if runs is not None:
        longs, shorts = runs
        for t in rest:
            name = t.split("=", 1)[0]
            if (name.startswith("--") and len(name) >= 4 and any(o.startswith(name) for o in longs)) or (
                shorts and re.match(rf"-[A-Za-z]*[{shorts}]", t)
            ):
                return f"git {sub} {name} (runs a program)"
    return None


def subagent_refusal(exe: str, argv: list[str], *, verb_unresolved: bool = False) -> str | None:
    """A short label for a git or gh argv a subagent may NOT run, else None.

    ``argv`` is the WHOLE command with the program first (``["git", "push"]``),
    as in a parsed segment. Without it, ``["push", "origin"]`` is read with
    ``push`` as the program, and nothing is refused.

    Allowed: what :data:`_GIT_SUBAGENT_OK` and :data:`_GIT_FAMILY_VERBS` list
    for git; for gh, a subcommand :data:`_GH_SUBAGENT_READS` lists, a leaf in
    :data:`_GH_SUBAGENT_LEAF_READS`, a help request, or a ``gh api`` call
    :func:`_gh_api_is_read` accepts. Everything else is refused, including a
    segment whose verb the parse could not resolve. Other programs are not this
    rule's subject and return None."""
    if exe not in ("git", "gh"):
        return None
    argv = list(argv)
    if verb_unresolved:
        return f"{exe} (its subcommand is built at run time)"
    if exe == "git":
        return _git_subagent_refusal(argv)
    inv = gh_command(argv)
    if inv is None:
        # No command path: bare `gh`, `gh --version`, or flags only.
        extra = [t for t in argv[1:] if t not in ("--help", "--version", "-h", "-v")]
        return "gh (no readable command)" if extra else None
    # A flag gh's root does not declare swallows the NEXT word, so with one
    # before the group the word read as the group is a value and the command
    # runs from the word after it (MEASURED, gh 2.101: `gh --yes o/r repo
    # delete` is `repo delete`). Checked before every early return below.
    if not _gh_prepath_is_plain(argv, inv.path_end):
        return f"gh {inv.group} (a flag before the command hides which command runs)"
    if not _GH_WORD.fullmatch(inv.group):
        return None  # here-doc text such as `gh = Fake()`, not a gh call
    if inv.group in _GH_ROOT_READ_ALIASES and argv[1] == inv.group:
        # gh's default `co` alias (`gh pr checkout`). gh expands an alias only
        # as the first word. A user can redefine `co` with `gh alias set
        # --clobber`; that is not visible here.
        return None if "--" not in argv else "gh co (git options after --)"
    group = _GH_GROUP_ALIASES.get(inv.group, inv.group)
    verb = inv.subcommand or (inv.positionals[0] if inv.positionals else None)
    # Where the command path ends in argv: after the verb when there is one (a
    # group gh_command does not model leaves its verb as the first positional,
    # so find it). With no verb, every word after the group was a flag value,
    # for gh as for the walk, so nothing is hidden: the path ends at the group.
    if inv.subcommand is not None or verb is None or group not in _GH_SUBAGENT_READS:
        lookup_end = inv.path_end
    elif verb in argv[inv.path_end :]:
        lookup_end = argv.index(verb, inv.path_end) + 1
    else:
        lookup_end = len(argv)
    if not _gh_prepath_is_plain(argv, lookup_end):
        return f"gh {group} (a flag before the verb hides which command runs)"
    if gh_requests_help(argv):
        return None  # help only: gh prints it and runs nothing
    if group == "api":
        return None if _gh_api_is_read(argv) else "gh api (not a plain read)"
    if group in _GH_SUBAGENT_LEAF_READS:
        return None
    reads = _GH_SUBAGENT_READS.get(group)
    if reads is None:
        return f"gh {group}"
    if verb is None or verb in reads:
        # `gh repo clone`/`gh pr checkout` pass words after `--` to git, which
        # can name a program to run (`--upload-pack`, `--config`).
        if "--" in argv and verb in ("clone", "checkout", "co"):
            return f"gh {group} {verb} (git options after --)"
        return None
    return f"gh {group} {verb}"
