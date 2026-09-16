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

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = REPO_ROOT / "scripts" / "systemd"
CONFIG_DIR = REPO_ROOT / "config"
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
BOOTSTRAP_SH = REPO_ROOT / "scripts" / "bootstrap.sh"
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"

UNIT_SUFFIXES = ("*.service", "*.timer")

# The repo's own location, spelled the ways a unit file could hardcode it:
# systemd's %h, either shell HOME form, a bare ~, or a literal /home/<user>
# (what a maintainer pastes while debugging on their own box). `/+` catches a
# doubled slash and the character class catches a capitalised checkout.
#
# Anchored so `%h/.local/share/genesis-guardian` — a DEPLOY target, not the
# repo — does not match: the guardian units legitimately point there.
HARDCODED_REPO_PATH = re.compile(r"(?:%h|\$\{?HOME\}?|~|/home/[^/\s]+)/+[Gg]enesis(?:/|\s|$)")


# A copy verb anywhere on the line — start, after a `&&`, or behind `sudo`.
# `cat` is deliberately absent: the installer renders one unit it has no
# template for through a heredoc, and that is correct.
_COPY_VERB = re.compile(r"(?:^|[\s;&|])(?:sudo\s+)?(?:cp|install|rsync|ln)\s")


def copies_into_systemd_dir(line: str) -> bool:
    """True when `line` copies something INTO `$SYSTEMD_USER_DIR`.

    Direction is the whole point: the destination is the final token, so
    copying a unit OUT of that directory (a backup before modifying it) is
    correctly ignored.
    """
    if not _COPY_VERB.search(line) or "SYSTEMD_USER_DIR" not in line:
        return False
    tokens = line.split()
    return bool(tokens) and "SYSTEMD_USER_DIR" in tokens[-1]


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
