"""Guardrail: a `cd` into a `dirname` result must clear CDPATH first.

`cd ARG` consults CDPATH whenever ARG is relative and does not begin with `.` or
`..`. For the script-directory idiom that is wrong twice over, because `cd`
SEARCHES CDPATH: the capture can collect cd's own echoed line, and it can
resolve into a DIFFERENT tree that happens to hold the same relative path. The
second is the dangerous one -- redirecting the echo away turns a loud failure
into a silently wrong root, which is why the remedy is `unset CDPATH` INSIDE the
substitution (a subshell, so it leaks nothing) rather than a redirect.

The check is the EXACT REMEDY TEXT, not a grammar of safe spellings. Every
`cd [opts] "$(dirname ...` must sit immediately after `$(unset CDPATH; ` or
`$(CDPATH= ` -- the two spellings the tree uses (51 and 1 dirname-cd sites,
plus the 3 allowlisted lines, measured 2026-10-02 with `_classify`;
`CDPATH=''`/`CDPATH=""` had 0). Anything else is a violation, so a
spelling nobody has written yet fails by default. An earlier revision accepted a
grammar of equivalent spellings through a quote-aware parser; every review
finding on it was a new edge of that grammar (`CDPATH=""/decoy`,
`unset CDPATH=foo`, a `$'...'` string), and the grammar bought nothing the two
literals do not. A valid but unusual spelling (`unset -v CDPATH`) is flagged
and has to be rewritten, which is acceptable for a style check.

There is no reachability judgement: anything matching the shape is
relative-capable by construction, and a caller can change to a relative
invocation at any time. The exceptions (a second cd inside a substitution that
already ran `unset CDPATH;`) are listed in `_ALLOWED_LINES` with their reason,
and a test fails if any listed line stops existing.

A cd at TOP LEVEL has no accepted spelling, because both accepted prefixes open
a substitution. Write it as `cd -- "$(unset CDPATH; cd -- "$(dirname -- "$0")"
&& pwd)"`: the outer cd's argument is not the dirname shape, so it is not
matched, and the inner capture carries the remedy.

What the shape covers, and what it does not:

  COVERED    `cd` anywhere on a line (top level or inside a command
             substitution) whose argument is a
             `"$(dirname ...)"` -- whatever the dirname argument is, whether
             `$0`, `${BASH_SOURCE[0]}`, or a variable. The variable form matters:
             scripts/cc-slot.sh resolves its own directory from
             `$_CC_SLOT_SCRIPT`, so a guard keyed on the literal `$0` spelling
             would be blind to the precedent it exists to enforce.

  NOT COVERED  three classes, each searched for in the tree and each currently
             having NO member, so this is a stated bound rather than live debt:

             * a capture split across multiple lines. MEASURED (with the span parser
               an earlier revision of this PR shipped): 66 of the 1266 `$( )` spans in the scan
               roots were multi-line, and 0 of those 66 contained a `cd`.
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
               found nowhere. Also unseen, and also absent from the tree:
               `pushd "$(dirname ...)"` and a quoted command name (`"cd"`,
               `'cd'`), both of which bash still resolves through CDPATH.

             The COVERED paragraph above says "whatever the dirname argument is",
             which is true of the ARGUMENT and must not be read as "whatever the
             spelling" -- the list here is the difference.

WHAT THE REMEDY DOES NOT COVER: a `readonly CDPATH`.

`unset CDPATH` fails with "cannot unset: readonly variable" and returns 1; the
capture's `cd` still runs, because the two are separated by `;`, so the search
happens against the unchanged CDPATH and the fix is INERT. The other accepted
spelling has the same limit: `CDPATH= cd` reports the readonly error and still
runs the cd against the unchanged CDPATH. MEASURED: with
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

# A `cd` (with any options) whose argument is a command substitution running
# dirname. `(?<![\w./=$-])` keeps `abcd`, `x=$cd` and `./cd` out of it.
_DIRNAME_CD = re.compile(r'(?<![\w./=$-])cd\s+(?:-\S+\s+)*"\$\(dirname\b')
# The ONLY accepted text immediately before such a cd. Literal on purpose: see
# the module docstring for why a grammar of equivalent spellings was removed.
_REMEDY_PREFIXES = ("$(unset CDPATH; ", "$(CDPATH= ")

# Lines that match the shape without the literal prefix and are safe for a
# reason the literal rule cannot see, keyed by (path, stripped line).
_ALLOWED_LINES = {
    (
        ".claude/hooks/genesis-hook",
        'MAIN_ROOT="$(unset CDPATH; cd "$GENESIS_ROOT" 2>/dev/null && cd '
        '"$(dirname "$_common_dir")" 2>/dev/null && pwd)" || MAIN_ROOT=""',
    ): "the second cd runs inside the same substitution, after `unset CDPATH;`",
    (
        "scripts/guardian-gateway.sh",
        'PKG="$(unset CDPATH; cd "$(dirname "$SHADOW")" 2>/dev/null && cd '
        '"$(dirname "$T")" 2>/dev/null && pwd || true)"',
    ): "the second cd runs inside the same substitution, after `unset CDPATH;`",
    (
        "scripts/lib/cc_version.sh",
        'pkg_dir="$(unset CDPATH; cd "$(dirname "$candidate")" 2>/dev/null && cd '
        '"$(dirname "$target")" 2>/dev/null && pwd)"',
    ): "the second cd runs inside the same substitution, after `unset CDPATH;`",
}

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


def _classify(line: str) -> list[str]:
    """``compliant``/``violation`` for each dirname-cd on a line ([] if none).

    A cd is compliant only when the text immediately before it is one of
    ``_REMEDY_PREFIXES``. Comment lines are not shell and are skipped.
    """
    if line.lstrip().startswith("#"):
        return []
    return [
        "compliant" if line[: m.start()].endswith(_REMEDY_PREFIXES) else "violation"
        for m in _DIRNAME_CD.finditer(line)
    ]


def _violations():
    """Every dirname-argument cd that is not immediately preceded by a remedy."""
    found, bad, allowed_seen = [], [], set()
    for path in _shell_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            verdicts = _classify(line)
            found.extend(rel for _ in verdicts)
            if "violation" not in verdicts:
                continue
            key = (rel, line.strip())
            if key in _ALLOWED_LINES:
                allowed_seen.add(key)
                continue
            bad.append(f"{rel}:{lineno}: {' '.join(line.split())[:110]}")
    return found, bad, allowed_seen


def test_every_dirname_cd_capture_clears_cdpath():
    """No `cd "$(dirname ...)"` may run without the exact remedy before it."""
    _, bad, _ = _violations()
    assert not bad, (
        "cd into a dirname result without `$(unset CDPATH; ` or `$(CDPATH= ` "
        "directly before it -- `cd` will SEARCH CDPATH and can resolve into a "
        "different tree. At top level, write `cd -- \"$(unset CDPATH; cd -- "
        "\"$(dirname -- \"$0\")\" && pwd)\"`:\n  " + "\n  ".join(bad)
    )


def test_every_allowed_line_still_exists():
    """An allowlist entry whose line is gone (or was respelled) is stale and
    must be removed, so the exception list cannot outlive its reason."""
    _, _, allowed_seen = _violations()
    stale = set(_ALLOWED_LINES) - allowed_seen
    assert not stale, f"allowlisted lines no longer present: {sorted(stale)}"


def test_the_scan_actually_looked_at_the_corpus():
    """Guard-the-guard: an empty or broken scan must fail, not pass.

    Without this, a matcher that stops matching reports exactly what a clean
    corpus reports. Both this session's earlier enumerators failed that way --
    one returned zero matches for a pattern that should have matched dozens,
    another skipped whole files and missed 14 real sites.
    """
    found, _, _ = _violations()
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
        # --- the two accepted spellings ---
        ('X="$(unset CDPATH; cd "$(dirname "$0")" && pwd)"', "compliant"),
        ('X="$(CDPATH= cd "$(dirname "$0")" && pwd)"', "compliant"),
        ('X="$(CDPATH= cd -- "$(dirname "$0")" && pwd -P)"', "compliant"),
        # --- every spelling a review finding raised is a violation by
        # construction: none of them is one of the two literals ---
        ('X="$(CDPATH=/decoy cd "$(dirname "$0")" && pwd)"', "violation"),
        ('X="$(CDPATH=$HOME cd "$(dirname "$0")" && pwd)"', "violation"),
        ('X="$(CDPATH=. cd "$(dirname "$0")" && pwd)"', "violation"),
        ('X="$(CDPATH=\'\'decoy cd "$(dirname "$0")" && pwd)"', "violation"),
        ('X="$(CDPATH=""decoy cd "$(dirname "$0")" && pwd)"', "violation"),
        ('X="$(CDPATH=""/decoy cd "$(dirname "$0")" && pwd)"', "violation"),
        ('X="$(unset CDPATH=foo; cd "$(dirname "$0")" && pwd)"', "violation"),
        ('X="$(unset -f CDPATH; cd "$(dirname "$0")" && pwd)"', "violation"),
        ('X="$(unset -n CDPATH; cd "$(dirname "$0")" && pwd)"', "violation"),
        ('X="$(unset -x CDPATH; cd "$(dirname "$0")" && pwd)"', "violation"),
        ('X="$(printf \')\'; cd "$(dirname "$0")" && pwd)"', "violation"),
        ('X="$(printf $\'\\\' )\'; cd "$(dirname "$0")" && pwd)"', "violation"),
        # a remedy on a DIFFERENT, earlier command does not protect a later cd
        ('A="$(unset CDPATH; true)"; X="$(cd "$(dirname "$0")" && pwd)"', "violation"),
        # a valid but unusual spelling is flagged too: rewrite it to a literal
        ('X="$(unset -v CDPATH; cd "$(dirname "$0")" && pwd)"', "violation"),
        ('X="$(unset -f -v CDPATH; cd "$(dirname "$0")" && pwd)"', "violation"),
        # the `$(` is part of the literal: a look-alike variable is not a remedy
        ('X="$(MY_CDPATH= cd "$(dirname "$0")" && pwd)"', "violation"),
        ('X="$(unset MY_CDPATH; cd "$(dirname "$0")" && pwd)"', "violation"),
        # a remedy AFTER the cd protects nothing
        ('X="$(cd "$(dirname "$0")"; unset CDPATH; pwd)"', "violation"),
        # `CDPATH= ` protects only the one cd it prefixes; every cd is checked
        ('X="$(CDPATH= cd "$(dirname "$0")" && cd "$(dirname "$1")" && pwd)"', "violation"),
        # a correct top-level form: the outer cd is not the shape, the inner is remedied
        ('cd -- "$(unset CDPATH; cd -- "$(dirname -- "$0")" && pwd)"', "compliant"),
        # --- not the shape: a cd with no dirname is out of this guard's scope ---
        ('X="$(cd "$SCRIPT_DIR/.." && pwd)"', "ignored"),
        ('X="$(cd "$HOME/genesis" && pwd)"', "ignored"),
        ('cd "$BACKUP_DIR"', "ignored"),
        # --- not a cd at all ---
        ("cdrom=/dev/sr0", "ignored"),
        ("x=$cd", "ignored"),
        # the lookbehind keeps a longer word or a path ending in cd out of it
        ('abcd "$(dirname "$0")"', "ignored"),
        ('./cd "$(dirname "$0")"', "ignored"),
        ('# cd "$(dirname "$0")" && pwd', "ignored"),
    ],
)
def test_matcher_recall_and_precision(line: str, expected: str) -> None:
    """Every spelling of the shape is classified the way the guard classifies it.

    THREE outcomes, not two. A remediated line MATCHES the shape and is safe
    because of the remedy; collapsing "matches" into "violation" would assert
    that the fix's own output is a finding.
    """
    verdicts = _classify(line)
    if expected == "ignored":
        assert not verdicts, f"matcher flagged a non-member: {line!r}"
        return
    assert verdicts, f"matcher MISSED a spelling it must catch: {line!r}"
    if expected == "violation":
        assert "violation" in verdicts, f"not flagged: {line!r}"
    else:
        assert verdicts == ["compliant"] * len(verdicts), f"remediated line flagged: {line!r}"


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
