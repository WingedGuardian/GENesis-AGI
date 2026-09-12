"""Does this tool call TOUCH the secrets file? Answered by resolved path, not by string.

The first version of this gate matched the literal ``secrets.env`` in a Bash
command. An adversarial review broke it in seconds with shapes nobody would call
exotic — ``cat ~/genesis/secrets.*``, ``cat secrets.e*``, ``cat s*.env`` — and it
never saw ``Read``/``Grep`` at all, since it was wired only to ``Bash``. That is
the hand-rolled-matcher tar pit the genesis-development skill names: every round
finds one more spelling, and it does not converge.

So this module asks a different question. Not "does the text look like the
secrets file" but **"does anything in this call resolve to the secrets file"** —
expand the globs, follow the symlinks, compare by ``st_ino``. A spelling the
author never imagined still resolves to the same inode.

Two honest residuals, declared rather than hidden:

* **Shell variables are not expanded.** ``f=secrets; cat $f.env`` cannot be
  resolved without executing the shell, and this module never executes anything.
  It is handled by the SUSPICION fallback below — a token carrying a variable
  alongside secrets-ish wording is reported as unresolved-and-suspicious, and the
  caller gates on it. That trades a rare extra prompt for not having a hole.
* **A copy is gated once, at the copy.** ``cp secrets.env /tmp/x`` prompts; later
  reads of ``/tmp/x`` do not, because by then it is a different file with no
  marking. Inherent to gating at the filesystem boundary.

MEASURED, because a gate nobody measured is a gate nobody knows the cost of.
Replayed against 5,713 real Bash commands from the 60 most recent transcripts on
one install:

    fired            148/5,713 (2.591%)  ->  63/5,713 (1.103%)
    of which TRUE     (names secrets.env as an operand)  59  (1.033%)
    FALSE POSITIVES   89  (1.558%)       ->   4  (0.070%)

The surviving 1.033% is a FIRE rate, not an error rate: those commands really do
source, grep or sed the credentials file, which is the whole point. The number
that mattered was the other one, and the reason it was so high is that the first
version was a string matcher wearing an inode matcher's clothes — it gated on the
word ``secrets.env`` appearing anywhere, including in this module's own commit
message. Re-derive by replaying the corpus through ``touches_secrets``.

Stdlib only, no ``genesis`` import, and every path operation is wrapped: a
resolution failure must never crash the hook it guards — including
``ValueError``, which ``Path.stat()`` (not ``OSError``) raises on an embedded NUL,
and which reached CC as exit 1, i.e. a NON-blocking error that lets the tool run.
"""

from __future__ import annotations

import glob
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook_input import brace_expand, strip_quoted  # noqa: E402

#: Real secrets files. `secrets.env.example` is the shipped TEMPLATE and holds
#: nothing — prompting on it would train the owner to click through the prompt
#: that matters, which is worse than not prompting at all.
_SECRET_BASENAMES = ("secrets.env",)


def _candidate_roots() -> list[Path]:
    """Where a real secrets.env plausibly lives on this install."""
    roots = []
    explicit = os.environ.get("GENESIS_SECRETS_PATH")
    if explicit:
        roots.append(Path(explicit).expanduser())
    roots.append(Path.home() / "genesis" / "secrets.env")
    repo = os.environ.get("CLAUDE_PROJECT_DIR")
    if repo:
        roots.append(Path(repo) / "secrets.env")
    return roots


def _secret_inodes() -> set[tuple[int, int]]:
    """(st_dev, st_ino) of every real secrets file we can find.

    Identity by inode rather than by path string: a symlink, a relative path, a
    ``..`` walk and an absolute path all collapse to the same pair, so none of
    them needs its own pattern.
    """
    out: set[tuple[int, int]] = set()
    for p in _candidate_roots():
        try:
            st = p.resolve().stat()
            out.add((st.st_dev, st.st_ino))
        except OSError:
            continue
    return out


def _is_secret_path(raw: str, inodes: set[tuple[int, int]], *, allow_bare: bool = True) -> bool:
    """True when `raw` resolves to — or names — a real secrets file.

    ``allow_bare`` governs the ONE ambiguous case: a token that does not resolve
    AND carries no path separator, i.e. the word ``secrets.env`` on its own. That
    spelling is a real operand after a ``cd``, and it is also what every sentence
    ABOUT the file looks like. The caller decides, because only the caller knows
    whether the token was quoted. See ``touches_secrets``.

    ``ValueError`` is caught alongside ``OSError`` because ``Path.stat()`` raises
    it — not OSError — on an embedded NUL byte. The narrower catch let that
    escape as an uncaught exception, and an uncaught exception in a PreToolUse
    hook is exit 1, which Claude Code treats as a NON-blocking error: the tool
    runs. A credentials gate must not fail open on a malformed argument.
    """
    if not raw:
        return False
    try:
        p = Path(os.path.expanduser(raw))
        if p.name.endswith(".example"):
            return False
        st = p.resolve().stat()
        if (st.st_dev, st.st_ino) in inodes:
            return True
        # Not one of the known installs, but named like the real thing — a
        # backup, a second checkout, a copy under another root. Still secrets.
        return p.name in _SECRET_BASENAMES
    except (OSError, ValueError):
        # Cannot stat: fall back to the NAME, so a path that does not exist yet
        # (a `cp … secrets.env` destination) is still recognised.
        stripped = raw.rstrip("/")
        if os.path.basename(stripped) not in _SECRET_BASENAMES:
            return False
        return allow_bare or (os.sep in stripped or stripped.startswith("~"))


#: A token worth expanding: contains a path separator, a glob metacharacter, or
#: reads like a secrets filename. Deliberately generous — expansion is cheap and
#: a missed token is a hole.
#: ``$`` and a backtick are included so a variable-built path reaches the
#: SUSPICIOUS check below — without them ``$f.env`` was filtered out here and the
#: fallback never ran, which is exactly how that shape slipped the first draft.
_PATHY = re.compile(r"[/~*?\[\]$`]|secret", re.IGNORECASE)

#: Unresolvable-but-suspicious: shell variable or command substitution sitting
#: next to secrets-ish wording. Cannot be resolved without running the shell, so
#: it is reported and the caller decides (we gate).
_SUSPICIOUS = re.compile(r"(\$\{?\w+|`|\$\()", re.IGNORECASE)

#: Does this command mention a secrets FILE, as opposed to the word "secret"?
#:
#: The distinction is load-bearing because the suspicion arm below is
#: command-LEVEL: it gates whenever secrets-wording and an unresolvable shell
#: construct appear anywhere in the same command. Testing for the bare substring
#: ``secret`` therefore fired on every non-trivial command run inside a worktree
#: named ``secrets-guard``, and on any ``gh api --body`` quoting the word — the
#: single largest false-positive class in the corpus measurement.
#:
#: So it takes one of two shapes: ``secrets`` carrying the real EXTENSION or a
#: glob (``secrets.env``, ``secrets.*``), or ``secret``/``secrets`` standing alone
#: as a word glued to nothing. Excluded by the lookarounds, each a measured false
#: positive: ``secrets-guard`` (a worktree name), ``secret_scrub`` (a module), and
#: ``dashboard/routes/secrets.py`` (a source file that is not the credentials
#: file). Still included: ``f=secrets; cat $f.env``, the declared shell-variable
#: residual this arm exists for.
#: ``.example`` is excluded here for the same reason ``_is_secret_path`` excludes
#: it: the template holds nothing. Without the lookahead, ANY command mentioning
#: ``secrets.env.example`` alongside a ``$`` gated at the command level, even
#: though the per-token check would have cleared it.
_SECRETISH = re.compile(
    r"(?<![\w-])secrets?\.(?:env\b(?!\.example)|[*?\[])|(?<![\w-])secrets?(?![\w.-])",
    re.IGNORECASE,
)


#: Heredoc bodies are DATA, not operands. `git commit -F - <<'EOF' … secrets.env
#: … EOF` is a commit message discussing the file; nothing in it is opened. Left
#: in, it fires the gate on this module's own commit message.
_HEREDOC = re.compile(r"<<-?\s*'?\"?(\w+)'?\"?\n.*?^\s*\1\s*$", re.DOTALL | re.MULTILINE)

#: Ceilings on the glob walk below. A glob is expanded against the REAL
#: filesystem, so a token like ``/*/*/*/*/*/*`` walks an unbounded subtree —
#: MEASURED on this box at 1.1s for ``/usr/*/*/*`` and 6.0s for ``/sys/*/*/*/*``,
#: against a hook budget of 10s. Two separate harms: every ordinary `ls`/`rg`
#: carrying a wide glob stalls, and a walk that outruns the budget is killed
#: without emitting a decision — which is an ALLOW. So the walk is bounded, and
#: exhausting the bound GATES rather than falls through. Unresolvable is treated
#: the same way everywhere else in this module.
_GLOB_BUDGET_S = 0.15
_GLOB_MAX_HITS = 500


def _glob_hits_secret(tok: str, inodes: set[tuple[int, int]]) -> bool:
    """Expand one glob token, bounded. True = it is (or may be) a secrets file."""
    # A pattern that cannot name a secrets file is not worth walking for at all.
    # This is what keeps `ls /*/*/*/*` off the expensive path entirely.
    if not re.search(r"secret|\.env", tok, re.IGNORECASE):
        return False
    deadline = time.monotonic() + _GLOB_BUDGET_S
    try:
        for n, hit in enumerate(glob.iglob(os.path.expanduser(tok))):
            if n > _GLOB_MAX_HITS or time.monotonic() > deadline:
                return True  # over budget -> GATE; never let a slow walk allow
            if _is_secret_path(hit, inodes):
                return True
    except (OSError, ValueError):
        pass
    return False


def touches_secrets(*, paths: list[str] | None = None, command: str = "") -> bool:
    """True if any explicit path, or anything the command resolves to, is the secrets file.

    ``paths``   — from structured tools (Read/Edit/Write ``file_path``, Grep
                  ``path``/``pattern``/``glob``, Glob ``pattern``). Real paths or
                  patterns; checked directly and glob-expanded.
    ``command`` — Bash text. Tokens are brace- and glob-expanded and resolved;
                  nothing is executed.

    **Talking about the file is not touching it.** The first version gated on the
    bare word ``secrets.env`` anywhere in a command, which fired on
    ``grep -n "secrets.env" scripts/bootstrap.sh``, on a ``gh pr --body`` describing
    the gate, and on this module's own commit message — MEASURED at 2.56% of real
    commands, mostly mentions. In a foreground session that is a prompt the owner
    learns to click through; in a dispatched session it is a hard DENY plus a
    critical alert. So a separator-less token only counts when it survives
    ``strip_quoted`` — i.e. it appears as a bare operand (``cd ~/genesis && cat
    secrets.env``) rather than inside a quoted string. Tokens WITH a separator are
    unaffected: quoting a path is normal and stays gated.
    """
    inodes = _secret_inodes()

    for p in paths or []:
        if not isinstance(p, str):
            continue
        if _is_secret_path(p, inodes):
            return True
        if any(ch in p for ch in "*?[") and _glob_hits_secret(p, inodes):
            return True

    if not command:
        return False

    # Heredoc bodies are data. Everything below reasons about operands.
    scan = _HEREDOC.sub(" ", command)
    secretish = _SECRETISH.search(scan) is not None

    # Command-LEVEL suspicion. Tokenising splits on parentheses, so a command
    # substitution like `cat $(echo secrets).env` leaves no single token holding
    # the `$(` — the per-token check below can never see it. Rather than add
    # another token pattern (the tar pit), ask the whole command: does it mention
    # secrets AND contain something only a shell can resolve? If so, gate. This
    # fails toward asking, which in a foreground session costs one prompt.
    if secretish and _SUSPICIOUS.search(scan):
        return True

    # Tokens that are NOT inside quotes. Used only to decide the bare-basename
    # case; see the docstring.
    unquoted = set(_tokens(strip_quoted(scan)))

    for raw_tok in _tokens(scan):
        if not _PATHY.search(raw_tok):
            continue
        # Bash brace-expands before the command ever runs, so
        # `cp ~/genesis/{secrets.env,secrets.env.bak}` opens the real file while
        # the single opaque token matches nothing. The repo already ships the
        # expander for exactly this class (the rm guards hit it first).
        try:
            expansions = brace_expand(raw_tok)
        except ValueError:
            return True  # a brace bomb is unresolvable -> gate
        for tok in expansions:
            if _is_secret_path(tok, inodes, allow_bare=raw_tok in unquoted):
                return True
            # Expand globs against the real filesystem — this is what catches
            # `secrets.*`, `secrets.e*`, `s*.env` without enumerating spellings.
            if any(ch in tok for ch in "*?[") and _glob_hits_secret(tok, inodes):
                return True
            # Unresolvable shape next to secrets wording -> gate rather than guess.
            if secretish and _SUSPICIOUS.search(tok):
                return True

    return False


def _tokens(text: str) -> list[str]:
    """Split shell text into candidate operands. Never executes anything."""
    return [t for t in (t.strip() for t in re.split(r"[\s;|&<>()\"']+", text)) if t]
