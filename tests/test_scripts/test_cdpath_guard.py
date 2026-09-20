"""Guardrail: a `cd` into a `dirname` result must clear CDPATH first.

`cd ARG` consults CDPATH whenever ARG is relative and does not begin with `.` or
`..`. For the script-directory idiom that is wrong twice over, because `cd`
SEARCHES CDPATH: the capture can collect cd's own echoed line, and it can
resolve into a DIFFERENT tree that happens to hold the same relative path. The
second is the dangerous one -- redirecting the echo away turns a loud failure
into a silently wrong root, which is why the remedy is `unset CDPATH` INSIDE the
substitution (a subshell, so it leaks nothing) rather than a redirect.

Polarity is ALLOWLIST. Anything matching the shape is relative-capable by
construction, so there is no allowlist and no judgement call about whether a
particular site is "reachable enough" -- reachability is a property of the
CALLER, and a caller can change to a relative invocation at any time.

What the shape covers, and what it does not:

  COVERED    `cd` inside a command substitution whose argument is a
             `"$(dirname ...)"` -- whatever the dirname argument is, whether
             `$0`, `${BASH_SOURCE[0]}`, or a variable. The variable form matters:
             scripts/cc-slot.sh resolves its own directory from
             `$_CC_SLOT_SCRIPT`, so a guard keyed on the literal `$0` spelling
             would be blind to the precedent it exists to enforce.

  NOT COVERED  three classes, each searched for in the tree and each currently
             having NO member, so this is a stated bound rather than live debt:

             * a capture split across multiple lines. MEASURED: 66 of the 1266
               `$( )` spans in the scan roots are multi-line, and 0 of those 66
               contain a `cd`.
             * a `cd` into a caller-supplied path with no dirname at all
               (`cd "$REPO_PATH"`, `cd "$BACKUP_DIR"`). Those carry the same
               defect but not the same shape, and covering them would need an
               allowlist for the many absolute-valued ones -- so they are listed
               explicitly in the PR body instead of being silently implied safe.
             * four spellings the matcher does not see: no outer quote
               (`$(cd $(dirname "$0") && pwd)`), a backticked dirname, a
               two-step `d="$(dirname "$0")"; cd "$d"`, and `$( dirname` with a
               space after `$(`. Each was inserted into a scratch copy and
               confirmed unflagged; each was then searched for repo-wide and
               found nowhere.

             The COVERED paragraph above says "whatever the dirname argument is",
             which is true of the ARGUMENT and must not be read as "whatever the
             spelling" -- the list here is the difference.

WHAT THE REMEDY DOES NOT COVER: a `readonly CDPATH`.

`unset CDPATH` fails with "cannot unset: readonly variable" and returns 1; the
capture's `cd` still runs, because the two are separated by `;`, so the search
happens against the unchanged CDPATH and the fix is INERT. MEASURED: with
CDPATH readonly and pointed at a decoy, the remediated capture resolves into the
decoy exactly as the un-remediated one does.

The guard accepts it anyway, and that is a deliberate, stated limit rather than
an oversight: CDPATH is set nowhere in this repository, and `readonly CDPATH` is
something an operator would have to add on purpose, so the case is a hardening
gap rather than a live defect. The CDPATH-immune spelling is to normalize a
relative dirname to `./...` before the `cd` (bash does not search CDPATH for a
name beginning with `.`) -- measured working for a bare relative path, a
`/..` form, and an absolute argument. It is not used here because it turns a
one-token remedy into a multi-statement block at every site. Tracked by issue;
if that issue is taken, this docstring is the thing to update.

The scan is line-oriented WITHOUT quote handling, deliberately. An earlier
attempt at this enumeration was quote-aware and silently skipped whole files
wherever a comment carried an apostrophe, missing 14 real sites while reporting
a clean result.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
# Scan roots. `.claude/hooks` and `.claude/mcp` are here because they carry the
# same idiom and are reachable by the same relative invocation -- `.claude/mcp/
# run-gitnexus` is documented as `.claude/mcp/run-gitnexus status` in the
# code-intelligence skill, so a session following its own skill supplies a
# relative `$0`. They are listed as roots rather than reached by walking
# `.claude/` wholesale, which would descend into every linked worktree.
_SCAN_ROOTS = (
    REPO_ROOT / "scripts",
    REPO_ROOT / ".claude" / "hooks",
    REPO_ROOT / ".claude" / "mcp",
)

# A `cd` command word: not preceded by a word char / dot / slash / equals /
# dollar, and not followed by one. Deliberately over-matches (a `cd` inside a
# string still matches) because a false candidate costs a read while a missed
# one costs the class.
_CD_WORD = re.compile(r"(?<![\w./=$-])cd(?![\w-])")
# The cd argument is a command substitution running dirname.
_DIRNAME_ARG = re.compile(r'\bcd\s+(?:-\S+\s+)*"\$\(dirname\b')
# An ACCEPTED REMEDY, anchored to the head of the substitution so it must
# actually precede the cd it protects. An unanchored `CDPATH` substring match
# classified four unsafe spellings as compliant -- a remedy placed after the cd,
# `unset CDPATHX`, `MY_CDPATH=1`, and a remedy separated from a later unprotected
# cd -- while flagging the valid `unset -v CDPATH`. Measured against the shipped
# corpus this anchored form changes nothing: 0 of the fixed sites newly flagged.
#
# The `CDPATH=` alternative requires an ACTUALLY EMPTY assignment -- `(?=\s)`.
# Without it the prefix matched `$(CDPATH=/decoy cd ...)`, which is not a remedy
# at all: bash searches /decoy and can resolve into the wrong tree. That is a
# FALSE NEGATIVE in this guard, i.e. exactly what it exists to prevent in future
# code (Codex P2, PR #2171). The three spellings are pinned as table arms below.
_REMEDY = re.compile(r"^\$\(\s*(?:unset\s+(?:-\w+\s+)*CDPATH\b|CDPATH=(?=\s))")

# A site that MUST be found. If the matcher stops matching the corpus, this
# disappears and the test fails LOUDLY instead of passing over an empty scan --
# a matcher that looks at nothing and a clean corpus are otherwise the same
# result, which this session measured four separate times.
_CANARY = "scripts/bootstrap.sh"
# Implausibly few means the scan is broken, not that the tree got tidy.
_MIN_EXPECTED = 30


def _shell_files():
    """Every shell-executable file under scripts/, recursively.

    rglob, not glob: nested entry points (scripts/lib, scripts/hooks) are
    runnable too, and a non-recursive scan missed them once already.
    """
    for root in _SCAN_ROOTS:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                head = path.read_bytes()[:200].decode("utf-8", "replace")
            except OSError:
                continue
            if path.suffix == ".sh" or re.match(r"#!.*\b(ba|da|k|z)?sh\b", head):
                yield path


def _substitutions(line: str):
    """Balanced ``$( )`` spans on one line.

    If the parens never balance, the rest of the line is treated as one span --
    over-including is the safe direction for a guard whose job is to find things.
    """
    i, n = 0, len(line)
    while i < n:
        if line.startswith("$(", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if line.startswith("$(", j):
                    depth += 1
                    j += 2
                    continue
                if line[j] == "(":
                    depth += 1
                elif line[j] == ")":
                    depth -= 1
                j += 1
            yield line[i:j]
            i = j
            continue
        i += 1


def _violations():
    """Every dirname-argument cd inside a substitution that does not clear CDPATH."""
    found, bad = [], []
    for path in _shell_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            if line.lstrip().startswith("#"):
                continue
            for sub in _substitutions(line):
                if not _CD_WORD.search(sub) or not _DIRNAME_ARG.search(sub):
                    continue
                found.append(rel)
                if not _REMEDY.search(sub):
                    bad.append(f"{rel}:{lineno}: {' '.join(sub.split())[:90]}")
    return found, bad


def test_every_dirname_cd_capture_clears_cdpath():
    """No `$(cd "$(dirname ...)" ...)` may run without clearing CDPATH."""
    _, bad = _violations()
    assert not bad, (
        "cd into a dirname result without clearing CDPATH -- `cd` will SEARCH "
        "CDPATH and can resolve into a different tree:\n  " + "\n  ".join(bad)
    )


def test_the_scan_actually_looked_at_the_corpus():
    """Guard-the-guard: an empty or broken scan must fail, not pass.

    Without this, a matcher that stops matching reports exactly what a clean
    corpus reports. Both this session's earlier enumerators failed that way --
    one returned zero matches for a pattern that should have matched dozens,
    another skipped whole files and missed 14 real sites.
    """
    found, _ = _violations()
    assert _CANARY in found, (
        f"{_CANARY} was not found by the matcher -- the scan is not seeing the "
        f"corpus it is supposed to guard (found {len(found)} sites)"
    )
    assert len(found) >= _MIN_EXPECTED, (
        f"matcher found only {len(found)} dirname captures, expected at least "
        f"{_MIN_EXPECTED} -- implausible enough that the matcher, not the tree, "
        "is what changed"
    )


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # --- spellings the guard must flag ---
        ('X="$(cd "$(dirname "$0")" && pwd)"', "violation"),
        ('X="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"', "violation"),
        ('X="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)"', "violation"),
        # the variable form -- cc-slot.sh's own remedy, and the reason the
        # matcher cannot key on the literal `$0` spelling
        ('X="$(cd -P "$(dirname "$_CC_SLOT_SCRIPT")" && pwd)"', "violation"),
        ('X="$( cd "$(dirname "$0")" && pwd )"', "violation"),
        ('X="$(cd "$(dirname "$0")/.." && pwd)"', "violation"),
        ('. "$(cd "$(dirname "$0")" && pwd)/lib/x.sh"', "violation"),
        ('X="$(cd -- "$(dirname "$0")" && pwd)"', "violation"),
        # --- the shape, already remediated: matches, must NOT be a finding ---
        ('X="$(unset CDPATH; cd "$(dirname "$0")" && pwd)"', "compliant"),
        ('X="$(CDPATH= cd "$(dirname "$0")" && pwd)"', "compliant"),
        # --- a NON-empty CDPATH assignment is NOT a remedy: bash searches it ---
        ('X="$(CDPATH=/decoy cd "$(dirname "$0")" && pwd)"', "violation"),
        ('X="$(CDPATH=$HOME cd "$(dirname "$0")" && pwd)"', "violation"),
        ('X="$(CDPATH=. cd "$(dirname "$0")" && pwd)"', "violation"),
        # --- not the shape: a cd with no dirname is out of this guard's scope ---
        ('X="$(cd "$SCRIPT_DIR/.." && pwd)"', "ignored"),
        ('X="$(cd "$HOME/genesis" && pwd)"', "ignored"),
        ('cd "$BACKUP_DIR"', "ignored"),
        # --- not a cd at all ---
        ("cdrom=/dev/sr0", "ignored"),
        ("x=$cd", "ignored"),
        ('# cd "$(dirname "$0")" && pwd', "ignored"),
    ],
)
def test_matcher_recall_and_precision(line: str, expected: str) -> None:
    """Every spelling of the shape is classified the way the guard classifies it.

    THREE outcomes, not two. A remediated line MATCHES the shape and is safe
    because of the remedy; collapsing "matches" into "violation" would assert
    that the fix's own output is a finding.

    The violation arms are the coverage proof -- a corpus replay only ever shows
    shapes already in the tree, so a spelling nobody has written yet is
    invisible to it by construction.
    """
    hits = [
        sub for sub in _substitutions(line) if _CD_WORD.search(sub) and _DIRNAME_ARG.search(sub)
    ]
    if expected == "ignored":
        assert not hits, f"matcher flagged a non-member: {line!r}"
        return
    assert hits, f"matcher MISSED a spelling it must catch: {line!r}"
    violated = any(not _REMEDY.search(sub) for sub in hits)
    if expected == "violation":
        assert violated, f"matcher did not flag an unremediated spelling: {line!r}"
    else:
        assert not violated, f"matcher flagged a remediated spelling: {line!r}"


# ── functional arm: EXECUTE the shipped line, do not just read it ───────────
#
# The corpus scan above is a static claim. This runs the real shipped capture
# under a hostile CDPATH and observes where it lands -- the same shape as the
# arms in test_bootstrap_guards.py, extended from two sites to one per distinct
# spelling. Representative rather than exhaustive on purpose: the static guard
# already covers every site, and the marginal site adds no new shape.
#
# The probe is placed at the script's own relative depth so a `/..` capture
# resolves to the checkout root just as the real one would.
_FUNCTIONAL = [
    "backup.sh",  # ${BASH_SOURCE[0]}, plain
    "install.sh",  # dirname "$0", plain
    "cc_align_host.sh",  # repo-root (/..)
    "code_intel_runner.sh",  # pwd -P
    "check_hook_versions_complete.sh",  # dirname "$0" + /..
    "inbox_sync.sh",  # dirname "$0", plain
]

# Matched on SHAPE -- an assignment of `$(... cd ...)` -- not on the current
# remedy spelling. Pinning it to `unset CDPATH;` made the arm report "no capture
# found" for a site remediated the other legal way (`$(CDPATH= cd ...)`), which
# the table below declares compliant: the arm would fail while naming the wrong
# cause. tests/test_scripts/test_bootstrap_guards.py states the same rule for
# its own harness ("so a different CDPATH remedy does not read as 'capture not
# found'"), and this file mirrors that intent.
_CAPTURE = re.compile(r'^(?P<var>[A-Za-z_][A-Za-z0-9_]*)="\$\(.*\bcd\b.*\)"\s*$')


@pytest.mark.parametrize("script", _FUNCTIONAL)
def test_shipped_capture_resolves_locally_under_hostile_cdpath(script: str, tmp_path) -> None:
    """With CDPATH pointing at a decoy, the capture must resolve into OUR tree.

    The decoy is a tree holding the same relative path, which is what makes this
    catch mis-resolution rather than only the echoed line: redirecting cd's
    output would silence the echo and still land in the decoy. Asserting the
    result merely "has no newline" would pass for a capture holding the decoy
    path alone -- so the assertion is on WHERE it resolved.
    """
    text = (REPO_ROOT / "scripts" / script).read_text(encoding="utf-8")
    hit = next(
        ((m.group("var"), ln.strip()) for ln in text.splitlines() if (m := _CAPTURE.match(ln))),
        None,
    )
    assert hit, f"no self-dir capture found in {script} -- has it been renamed or reworked?"
    var, line = hit

    checkout = tmp_path / "checkout"
    decoy = tmp_path / "decoy"
    entry = checkout / "scripts" / script
    entry.parent.mkdir(parents=True)
    (decoy / "scripts").mkdir(parents=True)
    # The SHIPPED line, verbatim: re-emitting it from parsed parts would test a
    # second copy of the code rather than the one that ships.
    entry.write_text(f'set -u\n{line}\nprintf %s "${{{var}}}"\n')

    proc = subprocess.run(
        ["bash", f"scripts/{script}"],  # relative, so CDPATH is consulted
        cwd=str(checkout),
        env={"PATH": "/usr/bin:/bin", "CDPATH": str(decoy), "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0, proc.stderr
    root = str(checkout.resolve())
    assert str(decoy.resolve()) not in proc.stdout, (
        f"{script}: capture resolved into the DECOY tree -- {proc.stdout!r}"
    )
    assert proc.stdout.startswith(root), (
        f"{script}: resolved {proc.stdout!r}, expected a path under {root!r}"
    )
    assert "\n" not in proc.stdout, (
        f"{script}: the capture holds more than one line -- {proc.stdout!r}"
    )
