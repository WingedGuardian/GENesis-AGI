"""One source of truth for every Genesis systemd unit the installer places.

`scripts/systemd/*.template` is that source: each template carries
`__REPO_DIR__` (and friends), and both `scripts/install.sh` and
`scripts/bootstrap.sh` render it against wherever the repo actually is. A unit
that names the repo any other way is installed dead on every clone that is not
at `~/genesis`.

The defect these tests pin, measured on a GitHub runner via PR #1859's install
check (repo at ``/home/runner/work/GENesis-AGI/GENesis-AGI``):
``genesis-tmp-watchgod.service`` was rendered correctly from its template and
then OVERWRITTEN by ``cp``-ing a second, stale copy that lived at
``config/genesis-tmp-watchgod.service`` and hardcoded
``ExecStart=%h/genesis/scripts/tmp_watchgod.sh``. systemd reported
``status=203/EXEC`` — the unit enabled, never running — and the installer's
``|| true`` swallowed it. The portable template had existed since 2026-05-26
and had been inert the whole time, because the copy ran last.

This is the second instance of the shape: ``config/genesis-bridge.service`` was
already removed as the same kind of duplicate (see
``tests/test_autonomy/test_protection.py::test_critical_systemd_unit``). These
tests are DERIVED from the two directories rather than naming files, so a third
one fails here instead of on someone's fresh install.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = REPO_ROOT / "scripts" / "systemd"
CONFIG_DIR = REPO_ROOT / "config"
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
BOOTSTRAP_SH = REPO_ROOT / "scripts" / "bootstrap.sh"
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"

UNIT_SUFFIXES = ("*.service", "*.timer")

# The repo's own location, spelled the ways a unit file could hardcode it:
# systemd's %h, either shell HOME form, a bare ~, or an absolute /home/<user>
# path (what a maintainer pastes while debugging on their own box, and what a
# CI checkout looks like). `/+` catches a doubled slash.
#
# The checkout DIRECTORY NAME is not fixed — `genesis` locally, `GENesis-AGI` as
# GitHub names it — so the name is matched case-insensitively with an optional
# `-AGI` suffix, and intermediate directories are allowed: a CI checkout sits at
# /home/<user>/work/GENesis-AGI/GENesis-AGI, several levels down.
#
# Anchored so `%h/.local/share/genesis-guardian` — a DEPLOY target, not the
# repo — still does not match: `-guardian` is neither the `-AGI` suffix nor a
# path separator. That negative control is asserted below.
# The intermediate-directory part is OUTSIDE the alternation on purpose. An
# earlier revision widened only the `/home/` branch, so `%h/work/genesis/x` —
# the very prefix the real defect used — stayed unmatched while the new test
# rows, all drawn from the widened branch, reported success.
#
# KNOWN OVER-FIRE, stated rather than papered over: this also matches a
# non-repo deploy path that happens to end in a `genesis` directory, e.g.
# `%h/.local/share/genesis/bin/x`. MEASURED 0 hits across all 23 real unit
# sources in this repo, and the guardian units (`genesis-guardian`) do not match
# because `-guardian` is neither the `-AGI` suffix nor a separator. If a real
# deploy target ever lands on that shape, narrow this — do not delete it.
HARDCODED_REPO_PATH = re.compile(
    r"(?:%h|\$\{?HOME\}?|~|/home/[^\s/]+)(?:/[^\s/]+)*?/+genesis(?:-agi)?(?:/|\s|$)",
    re.IGNORECASE,
)


# A copy verb anywhere on the line — start, after a `&&`, or behind `sudo`.
# `mv` counts: rendering to a temp file and moving it into place overwrites the
# rendered unit exactly as `cp` does. `(?:\S*/)?` catches an absolute spelling
# like `/bin/cp`. `cat` is deliberately absent — the installer renders one unit
# it has no template for through a heredoc, and that is correct.
_COPY_VERB = re.compile(r"(?:^|[\s;&|])(?:sudo\s+)?(?:\S*/)?(?:cp|mv|install|rsync|ln)\s")

# Shell operators that end one command and start another.
_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|(?<![0-9<>])\|(?!\|)")

# A redirection: `2>/dev/null`, a bare `2>` with its target next, or `&>` —
# which is the HOUSE IDIOM here (80 occurrences across the three scanned
# scripts, 41 in install.sh alone), and which an earlier revision missed
# entirely because `&` is not a digit.
_REDIRECTION = re.compile(r"^(?:\d*|&)(?:>>|>|<)")


def copies_into_systemd_dir(line: str) -> bool:
    """True when `line` copies something INTO `$SYSTEMD_USER_DIR`.

    Direction is the whole point: the destination is the last real argument, so
    copying a unit OUT of that directory (a backup before modifying it) is
    correctly ignored.

    The line is split into shell segments first and redirections are dropped,
    because `cp "$src" "$SYSTEMD_USER_DIR/" 2>/dev/null || true` is the house
    idiom in these installers — taking the final whitespace token there yields
    `true`, and the exact overwrite this test exists to catch would pass.
    """
    if not _COPY_VERB.search(line) or "SYSTEMD_USER_DIR" not in line:
        return False
    for segment in _SEGMENT_SPLIT.split(line):
        if not _COPY_VERB.search(segment) or "SYSTEMD_USER_DIR" not in segment:
            continue
        args, skip_next = [], False
        for token in segment.split():
            if token.startswith("#"):
                break  # a trailing comment is not an argument
            if skip_next:
                skip_next = False
                continue
            if _REDIRECTION.match(token):
                # `2>/dev/null` carries its target; a bare `2>` takes the NEXT
                # token as one.
                skip_next = token[-1] in "<>"
                continue
            args.append(token)
        if args and "SYSTEMD_USER_DIR" in args[-1]:
            return True
    return False


def _unit_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    found: list[Path] = []
    for pattern in UNIT_SUFFIXES:
        found.extend(sorted(directory.glob(pattern)))
    return found


def test_fixture_sees_the_directories_it_claims_to_scan():
    """Guard-the-guard: an empty scan would pass every test below vacuously."""
    assert TEMPLATE_DIR.is_dir(), f"missing template dir: {TEMPLATE_DIR}"
    assert CONFIG_DIR.is_dir(), f"missing config dir: {CONFIG_DIR}"
    assert list(TEMPLATE_DIR.glob("*.template")), "no unit templates found to check"
    assert INSTALL_SH.is_file(), f"missing installer: {INSTALL_SH}"
    # The duplicate scan and the config half of the hardcode scan both iterate
    # THIS list. An empty config/ would make both pass while checking nothing,
    # and `is_dir()` above does not catch that.
    assert _unit_files(CONFIG_DIR), (
        "no checked-in unit files found — the duplicate scan would pass vacuously"
    )


def test_no_config_unit_duplicates_a_systemd_template():
    """A unit with a template must not ALSO exist as a checked-in unit file.

    Two copies means one of them wins by ordering, and the loser is silently
    the one everybody reads. Derived from both directories, so a newly added
    duplicate fails without anyone remembering to list it.
    """
    duplicates = [
        f"config/{unit.name} duplicates scripts/systemd/{unit.name}.template"
        for unit in _unit_files(CONFIG_DIR)
        if (TEMPLATE_DIR / f"{unit.name}.template").is_file()
    ]
    assert not duplicates, (
        "a systemd unit exists both as a template and as a checked-in copy; "
        "delete the copy and let the render loop own it: " + "; ".join(duplicates)
    )


def test_no_tracked_unit_source_hardcodes_the_repo_path():
    """No unit source may name the repo as `~/genesis` — that is `__REPO_DIR__`.

    This is the symptom the duplicate produced: an ExecStart that resolves only
    on an install that happens to sit at ~/genesis, and 203/EXEC everywhere else.
    """
    offenders = []
    sources = _unit_files(CONFIG_DIR) + sorted(TEMPLATE_DIR.glob("*.template"))
    for source in sources:
        for lineno, line in enumerate(source.read_text().splitlines(), start=1):
            if HARDCODED_REPO_PATH.search(line):
                rel = source.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, (
        "systemd unit source hardcodes the repo location; use __REPO_DIR__ so "
        "the installer renders it against the real checkout:\n  " + "\n  ".join(offenders)
    )


def test_no_installer_copies_a_repo_file_into_the_systemd_user_dir():
    """Units reach `$SYSTEMD_USER_DIR` by RENDERING, never by copying in.

    Copy verbs only. `install.sh` legitimately writes one unit it has no
    template for (`qdrant.service`, through a heredoc that substitutes real
    paths), so banning every write would fail on correct code — and `cat` is
    not a copy verb, so that exemption holds by construction rather than by a
    carve-out someone has to maintain.

    The destination is what matters: `$SYSTEMD_USER_DIR` must be the LAST
    argument. Copying a unit OUT of that directory — backing one up before
    modifying it — is a good thing a future installer might do, and an earlier
    version of this test forbade it.

    All three scripts that write into that directory are scanned, not just
    `install.sh`: `bootstrap.sh` and `update.sh` are equally able to
    reintroduce this. Known blind spot, stated rather than implied: a copy
    split across a line continuation, or one whose destination is held in
    another variable, is not caught here. The duplicate-source test above is
    the check that actually kills the bug at its root; this one catches the
    mechanism.
    """
    offenders = []
    for script in (INSTALL_SH, BOOTSTRAP_SH, UPDATE_SH):
        if not script.is_file():
            continue
        for lineno, line in enumerate(script.read_text().splitlines(), start=1):
            if copies_into_systemd_dir(line):
                offenders.append(f"{script.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "an installer copies a file INTO the systemd user dir, which overwrites "
        "the rendered unit with an unrendered one:\n  " + "\n  ".join(offenders)
    )


# The matchers above are the whole test suite's instrument, so they get their own
# cells rather than being trusted. Both directions: a spelling that must be
# caught, and one that must NOT be (an over-firing guard gets deleted by whoever
# hits it next). The `2>/dev/null || true` and `GENesis-AGI` rows were review
# findings — each was a real miss that would have let the defect back in green.


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # --- must be caught -------------------------------------------------
        ('    cp "$WATCHGOD_SRC" "$SYSTEMD_USER_DIR/"', True),
        ('cp "$SRC" "$SYSTEMD_USER_DIR/genesis-x.service"', True),
        ('cp "$SRC" "$SYSTEMD_USER_DIR/" 2>/dev/null', True),
        ('cp "$SRC" "$SYSTEMD_USER_DIR/" || true', True),
        ('cp "$SRC" "$SYSTEMD_USER_DIR/" 2>/dev/null || true', True),
        ('cp "$SRC" "$SYSTEMD_USER_DIR/" 2> /dev/null', True),
        ('cp "$SRC" "$SYSTEMD_USER_DIR/" >/dev/null 2>&1', True),
        ('cp "$SRC" "$SYSTEMD_USER_DIR/"  # install the unit', True),
        # `&>` is the prevailing spelling in these installers (80 occurrences).
        ('cp "$SRC" "$SYSTEMD_USER_DIR/" &>/dev/null', True),
        ('cp "$SRC" "$SYSTEMD_USER_DIR/" &> /dev/null', True),
        ('cp "$SRC" "$SYSTEMD_USER_DIR/" &>>"$LOG"', True),
        # Render-to-temp then move is the same overwrite by another verb.
        ('mv "$TMP" "$SYSTEMD_USER_DIR/genesis-x.service"', True),
        ('/bin/cp "$SRC" "$SYSTEMD_USER_DIR/"', True),
        ('mkdir -p "$SYSTEMD_USER_DIR" && cp "$S" "$SYSTEMD_USER_DIR/"', True),
        ('sudo cp "$SRC" "$SYSTEMD_USER_DIR/"', True),
        ('sudo install -m 644 "$S" "$SYSTEMD_USER_DIR/"', True),
        ('rsync -a "$SRC" "$SYSTEMD_USER_DIR/"', True),
        # --- must NOT be caught ---------------------------------------------
        ('cp "$SYSTEMD_USER_DIR/genesis-server.service" "$BAK/"', False),
        ('cp "$SYSTEMD_USER_DIR/x.service" "$BAK/" 2>/dev/null', False),
        ('    cat > "$SYSTEMD_USER_DIR/qdrant.service" <<QDSERVICE', False),
        ('mkdir -p "$SYSTEMD_USER_DIR"', False),
        ('if [ -f "$SYSTEMD_USER_DIR/x.service" ]; then', False),
    ],
)
def test_copy_detector_reads_the_destination_not_the_last_word(line, expected):
    assert copies_into_systemd_dir(line) is expected, line


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # --- must be caught -------------------------------------------------
        ("ExecStart=%h/genesis/scripts/tmp_watchgod.sh", True),
        ("WorkingDirectory=%h/genesis", True),
        ("EnvironmentFile=~/genesis/secrets.env", True),
        ("ExecStart=${HOME}/genesis/scripts/x.sh", True),
        ("ExecStart=/home/someuser/genesis/scripts/x.sh", True),
        ("ExecStart=%h//genesis/x.sh", True),
        ("ExecStart=%h/Genesis/x.sh", True),
        ("ExecStart=%h/GENesis-AGI/scripts/x.sh", True),
        ("ExecStart=~/genesis-agi/scripts/x.sh", True),
        # A CI checkout: the repo name twice, several levels down.
        ("ExecStart=/home/runner/work/GENesis-AGI/GENesis-AGI/scripts/x.sh", True),
        # Intermediate directories on EVERY prefix, not just /home/. `%h` is the
        # spelling the real defect used, so a checkout one level down under it
        # is the case most likely to recur.
        ("ExecStart=%h/work/GENesis-AGI/GENesis-AGI/scripts/x.sh", True),
        ("ExecStart=%h/work/genesis/scripts/x.sh", True),
        ("ExecStart=~/code/genesis/scripts/x.sh", True),
        ("ExecStart=$HOME/src/genesis/scripts/x.sh", True),
        # --- must NOT be caught ---------------------------------------------
        # The guardian units deploy OUTSIDE the repo and are correct as written.
        ("ExecStart=%h/.local/share/genesis-guardian/.venv/bin/python", False),
        ("WorkingDirectory=%h/.local/share/genesis-guardian", False),
        ("Environment=PATH=%h/.npm-global/bin:%h/.local/bin", False),
        # A placeholder is the RIGHT answer, not a hardcode.
        ("WorkingDirectory=__AZ_ROOT__", False),
        ("ExecStart=__REPO_DIR__/scripts/tmp_watchgod.sh", False),
    ],
)
def test_hardcoded_repo_path_matcher(line, expected):
    assert bool(HARDCODED_REPO_PATH.search(line)) is expected, line
