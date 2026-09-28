"""Which git commands are pointed at a repository by something other than cwd.

Shared by ``git_push_guard`` and ``pre_push_privacy_review``. Both resolve the
repository a push or merge acts on from where the command RUNS — the payload
cwd, the last ``cd``, a ``git -C``. git can also be pointed at a different
repository with the ``--git-dir`` / ``--work-tree`` global options or the
``GIT_DIR`` / ``GIT_WORK_TREE`` / ``GIT_COMMON_DIR`` environment variables, and a
command using them must not be classified against the checkout it ran in.

This module DETECTS those forms; it does not resolve the repository they select.
Reproducing git's discovery rules (relative ``GIT_DIR``, ``core.worktree``,
gitfiles) is exactly the half-model that produced the hole. Callers treat a
detection as an unresolvable working directory, which they already fail closed
on.

Every decision reads the segments the shared parser (``shell_parse.analyze_checked``)
resolved — the executed command with its wrappers peeled — rather than a second
tokenizer or a scan of raw text. Three questions, each scoped to what bash
actually does:

* **Global options** — only git's GLOBAL-option region, before the subcommand.
  ``git push -o --git-dir=x`` passes that text to the server; it selects nothing.
* **Command-scoped assignments** — ``GIT_DIR=x git push`` (also behind ``env`` /
  ``sudo``) sets the variable for that one command and nothing after it.
* **Persistent assignments** — ``export GIT_DIR=x``, a bare ``GIT_DIR=x``
  statement, ``read`` / ``printf -v`` / ``declare`` / ``eval`` naming the
  variable. These stay in force for every later command in the same shell, so a
  later ``cd`` does not recover a known repository.

Known limit, unchanged from before these checks existed: a ``source``-d file can
set the variables and is not read. ``source`` is far too common (virtualenv
activation) to treat as a redirect.

Stdlib + ``shell_parse`` only; no subprocesses, so it is safe on a hook's clock.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shell_parse import _argv, analyze_checked, git_subcommand_index  # noqa: E402

#: Environment variables that choose git's repository or work tree.
#: (``GIT_WORK_TREE`` alone does not change which repository git reads —
#: MEASURED, ``HEAD`` is unchanged — it is included as a conservative
#: over-approximation.)
REPO_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")
#: git GLOBAL options that choose the repository or work tree. Each accepts
#: ``--opt=value`` and ``--opt value``.
REPO_FLAGS = ("--git-dir", "--work-tree")

_VARS_ALT = "|".join(REPO_VARS)
#: A word that ASSIGNS one of the variables (``NAME=`` / ``NAME+=``).
_ASSIGN_RE = re.compile(rf"^(?:{_VARS_ALT})\+?=")
#: A word that NAMES one of them, as ``export NAME`` or ``read NAME`` do.
_NAME_RE = re.compile(rf"^(?:{_VARS_ALT})$")
_MENTION_RE = re.compile(rf"\b(?:{_VARS_ALT})\b")
#: ``${GIT_DIR:=x}`` / ``${GIT_DIR=x}`` — a parameter expansion that ASSIGNS.
_EXPANSION_ASSIGN_RE = re.compile(rf"\$\{{(?:{_VARS_ALT}):?=")
#: Any mention of a variable or a flag, for text the parser could not read.
_ANY_MENTION_RE = re.compile(rf"\b(?:{_VARS_ALT})\b|--git-dir\b|--work-tree\b")

#: Builtins whose arguments are ``NAME`` or ``NAME=value`` and which leave the
#: variable set in the CURRENT shell.
_DECLARERS = frozenset({"export", "declare", "typeset", "readonly", "local"})
#: Builtins that assign to a variable NAMED by an argument.
_NAME_ASSIGNERS = frozenset({"read", "mapfile", "readarray", "getopts"})
#: POSIX special builtins. In POSIX mode an assignment prefixed to one of them
#: persists after it returns, so a command-scoped assignment there is treated
#: as persistent (conservatively — bash's default mode does not persist it).
_SPECIAL_BUILTINS = frozenset(
    {
        ":",
        ".",
        "source",
        "eval",
        "exec",
        "export",
        "readonly",
        "set",
        "shift",
        "trap",
        "unset",
        "return",
        "break",
        "continue",
        "times",
        "declare",
        "typeset",
        "local",
    }
)


def _prefix_words(seg) -> list[str]:
    """The words bash read BEFORE the command this segment executes.

    ``analyze`` strips leading assignments and wrappers (``env``, ``sudo``, …) to
    find the executed command; those stripped words are where a command-scoped
    ``GIT_DIR=`` lives. The resolved argv is a suffix of the segment's own
    tokens, so the prefix is what precedes it. If the two ever misalign, the
    prefix only grows — toward treating more words as assignments, the closed
    direction.
    """
    toks = _argv(getattr(seg, "raw", "") or "")
    argv = getattr(seg, "argv", None) or []
    k = max(0, len(toks) - len(argv))
    return [t.lstrip("(") for t in toks[:k]]


def _git_global_region(argv: list[str]) -> list[str]:
    """argv between ``git`` and its subcommand; everything after it if unknown."""
    idx = git_subcommand_index(argv)
    return argv[1:idx] if idx is not None else argv[1:]


def seg_redirects_repo(seg) -> bool:
    """Whether THIS command is pointed at a repository other than its cwd's.

    True for a ``--git-dir`` / ``--work-tree`` in git's global-option region, or
    a repository variable assigned for this command (``GIT_DIR=x git push``,
    ``env GIT_DIR=x git push``). Applies to ``gh`` too for the environment form,
    because gh asks git which repository it is in.
    """
    if any(_ASSIGN_RE.match(w) for w in _prefix_words(seg)):
        return True
    if getattr(seg, "exe", "") != "git":
        return False
    for tok in _git_global_region(getattr(seg, "argv", None) or []):
        if any(tok == f or tok.startswith(f + "=") for f in REPO_FLAGS):
            return True
    return False


def seg_sets_repo_env(seg) -> bool:
    """Whether this segment leaves a repository variable set for LATER commands.

    Not the command-scoped form — ``GIT_DIR=x git status && git merge main``
    runs the merge in the ordinary checkout. Not a mere mention either —
    ``echo GIT_DIR``, ``unset GIT_DIR``, ``git commit -m GIT_DIR`` set nothing.
    """
    argv = getattr(seg, "argv", None) or []
    if any(_EXPANSION_ASSIGN_RE.search(a) for a in argv):
        return True  # `: ${GIT_DIR:=x}` assigns in the current shell
    prefix = _prefix_words(seg)
    prefix_assigns = any(_ASSIGN_RE.match(w) for w in prefix)
    if not argv:
        # A statement that is ONLY assignments: the variable stays set.
        return prefix_assigns
    name = os.path.basename(argv[0])
    args = argv[1:]
    if prefix_assigns and name in _SPECIAL_BUILTINS:
        return True
    if name in _DECLARERS:
        for a in args:
            if _ASSIGN_RE.match(a) or _NAME_RE.match(a):
                return True
            if "=" in a and _NAME_RE.match(a.split("=", 1)[1]):
                return True  # `declare -n ref=GIT_DIR`: assigning ref assigns it
            head = a.split("=", 1)[0]
            if "$" in head or "`" in head:
                return True  # the NAME itself is expanded — cannot tell which
        return False
    if name in _NAME_ASSIGNERS:
        return any(_NAME_RE.match(a) for a in args)
    if name == "printf":
        for i, a in enumerate(args):
            if a == "-v" and i + 1 < len(args) and _NAME_RE.match(args[i + 1]):
                return True
            if a.startswith("-v") and _NAME_RE.match(a[2:]):
                return True
        return False
    if name == "eval":
        # eval re-parses its words; it may assign anything they name or expand to.
        return any(_MENTION_RE.search(a) or "$" in a or "`" in a for a in args)
    return False


def raw_sets_repo_env(raw: str) -> bool:
    """Whether a top-level segment leaves a repository variable set afterwards.

    Parsed with ``analyze_checked``. Where the parser reports it could not read
    the segment, any mention of a repository variable counts: the outcome of a
    True is a refusal or a question, never an allow.
    """
    segs, blind = analyze_checked(raw)
    if blind is not None and _MENTION_RE.search(raw):
        # Includes a line continuation: the segmenter splits at `\<newline>` but
        # bash joins the lines, so `GIT_DIR=x \<newline> git push` is ONE command.
        # The split-off half ends in a lone backslash, which the parser reports as
        # unreadable, and that is what routes it here.
        return True
    return any(seg_sets_repo_env(s) for s in segs)


def unreadable_mention(raw: str) -> bool:
    """True when the parser could not read ``raw`` and it mentions a repository
    variable or flag — a push hidden there cannot be classified."""
    _, blind = analyze_checked(raw)
    return blind is not None and bool(_ANY_MENTION_RE.search(raw))
