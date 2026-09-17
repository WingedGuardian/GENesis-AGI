"""One source of truth for every Genesis systemd unit the installer places.

`scripts/systemd/*.template` is that source: each template carries
`__REPO_DIR__` (and friends), and both `scripts/install.sh` and
`scripts/bootstrap.sh` render it against wherever the repo actually is. A unit
that names the repo any other way is installed dead on every clone that is not
at `~/genesis`.

The defect this pins, measured on a GitHub runner via PR #1859's install check
(repo at ``/home/runner/work/GENesis-AGI/GENesis-AGI``):
``genesis-tmp-watchgod.service`` was rendered correctly from its template and
then OVERWRITTEN by ``cp``-ing a second, stale copy that lived at
``config/genesis-tmp-watchgod.service`` and hardcoded
``ExecStart=%h/genesis/scripts/tmp_watchgod.sh``. systemd reported
``status=203/EXEC`` — the unit enabled, never running — and the installer's
``|| true`` swallowed it. The portable template had existed since 2026-05-26
and had been inert the whole time, because the copy ran last.

This is the second instance of the shape: ``config/genesis-bridge.service`` was
already removed as the same kind of duplicate (see
``tests/test_autonomy/test_protection.py::test_critical_systemd_unit``). The
test below is DERIVED from the two directories rather than naming files, so a
third one fails here instead of on someone's fresh install.

WHY THESE CHECKS ARE SHAPED THE WAY THEY ARE
--------------------------------------------
Earlier revisions carried two PATTERN matchers — one banning unit sources that
spell the repo as ``~/genesis``, one banning a ``cp`` into the systemd user
directory. Both were DENYLISTS: they enumerated the spellings of a bad thing.
Across three review rounds they produced findings every round and never
converged — ``GENesis-AGI`` as a checkout name, then intermediate directories on
three of four prefixes, then ``&>`` (the prevailing redirection idiom in the very
files being scanned), then ``mv`` and ``/bin/cp``. Each fix was correct and each
round found the next spelling, which is the signature of the design being wrong
rather than the pattern being too narrow. CC memory
``contract_guard_allowlist_polarity`` puts it in one line: allowlist, never
denylist.

So the spelling matchers are gone, and what replaces them is an ALLOWLIST of the
permitted path ROOTS — a closed set this repo owns, derived from what the
templates actually use. An unlisted root fails BY CONSTRUCTION, which is the
thing a denylist can never do, and a genuinely new root is a deliberate one-line
addition with a reviewer attached. That converges; enumerating spellings does not.

The two tests are deliberately different in kind, because the bug had two halves:
a duplicate SOURCE FILE (structural — compares two directories, reads no
spellings) and a unit that NAMES the repo directly (the root allowlist).
"""

import shlex
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = REPO_ROOT / "scripts" / "systemd"
CONFIG_DIR = REPO_ROOT / "config"
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"

UNIT_SUFFIXES = ("*.service", "*.timer")


def _unit_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    found: list[Path] = []
    for pattern in UNIT_SUFFIXES:
        found.extend(sorted(directory.glob(pattern)))
    return found


def test_fixture_sees_the_directories_it_claims_to_scan():
    """Guard-the-guard: an empty scan would pass the test below vacuously."""
    assert TEMPLATE_DIR.is_dir(), f"missing template dir: {TEMPLATE_DIR}"
    assert CONFIG_DIR.is_dir(), f"missing config dir: {CONFIG_DIR}"
    assert list(TEMPLATE_DIR.glob("*.template")), "no unit templates found to check"
    # The duplicate scan iterates THIS list, and `is_dir()` above does not catch
    # an empty one. Non-vacuous today only because the guardian units live here.
    assert _unit_files(CONFIG_DIR), (
        "no checked-in unit files found — the duplicate scan would pass vacuously"
    )


def test_no_config_unit_duplicates_a_systemd_template():
    """A unit with a template must not ALSO exist as a checked-in unit file.

    Two copies means one wins by ordering, and the loser is silently the one
    everybody reads. Derived from both directories, so a newly added duplicate
    fails without anyone remembering to list it — and without this test needing
    to know how the duplicate spells anything.
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


# systemd directives whose value STARTS with a path we care about.
_PATH_DIRECTIVES = ("ExecStart", "ExecStartPre", "ExecStartPost", "ExecStop",
                    "ExecStopPost", "ExecReload", "ExecCondition",
                    "WorkingDirectory", "EnvironmentFile")

# The permitted ROOTS, derived from what the shipped templates actually use.
# This is an ALLOWLIST: anything not here fails, including a spelling nobody has
# thought of yet. That is the whole point — `%h/genesis/...`, the form that
# caused this bug, is excluded because it is not listed, not because some
# pattern recognises it.
#
#   __ANY_PLACEHOLDER__  the installer substitutes it against the real checkout
#   /bin/ /usr/ /sbin/   system binaries (`/bin/bash <script>`, systemctl)
#   %h/.local/share/     a DEPLOY target outside the repo; the guardian units
#                        legitimately live there and must not be flagged
_ALLOWED_ROOT_PREFIXES = ("/bin/", "/usr/", "/sbin/", "%h/.local/share/")


def _path_tokens(value: str) -> list[str]:
    """EVERY path-looking token in a directive value, not just the program.

    Reading only argv[0] was a real hole: seven of the shipped templates are
    `ExecStart=/bin/bash __REPO_DIR__/scripts/...`, so `/bin/bash` satisfied the
    allowlist and the repo path after it — the part that actually has to be
    portable — was never checked. Writing `/bin/bash %h/genesis/scripts/x.sh`
    into any of them reproduced the original non-portable failure with the suite
    green.

    Widening from argv[0] to every element moved the narrowness rather than
    removing it — the first version of this function dropped any token that
    matched no start-character, which is FAIL-OPEN, and it read tokens the
    shell would have unquoted. Three things it got wrong, all measured:

    * systemd allows `@ + ! !! : -` as exec prefixes, alone or combined, and
      only on the PROGRAM. Stripping just `-` meant `ExecStart=+%h/genesis/x.sh`
      produced no tokens at all and sailed through.
    * `.split()` leaves quotes attached, so `/bin/bash -c '%h/genesis/x.sh'`
      was invisible — and that `-c '...'` shape already ships, in
      `config/genesis-guardian-watchman.service`.
    * `$MAINPID`, `%n`, `%i` are not paths. Flagging them failed legitimate
      units and pointed the author at `__REPO_DIR__`, which is nonsense advice.

    So: unquote the way the shell would, strip the full prefix set from argv[0]
    only, and require a token to contain a separator before judging its root —
    a bare specifier or environment variable has no root to check.
    """
    try:
        parts = shlex.split(value, posix=True)
    except ValueError:
        # Unbalanced quotes: systemd would reject the unit anyway. Fall back to
        # a naive split rather than returning nothing, which would be fail-open.
        parts = value.strip().split()
    tokens = []
    for index, raw in enumerate(parts):
        token = (raw.lstrip("@+!:-") or raw) if index == 0 else raw
        if "/" in token and token.startswith(("/", "~", "%", "__", "$")):
            tokens.append(token)
    return tokens


def _unit_sources() -> list[Path]:
    return _unit_files(CONFIG_DIR) + sorted(TEMPLATE_DIR.glob("*.template"))


def test_every_unit_path_starts_at_an_ALLOWED_root():
    """A unit may only name a placeholder, a system path, or the deploy dir.

    The bug this closes: `ExecStart=%h/genesis/scripts/tmp_watchgod.sh` resolves
    only on an install that happens to sit at ~/genesis, and gives 203/EXEC
    everywhere else. The duplicate that carried it is gone, but nothing stopped
    the same line being typed into the TEMPLATE — a plausible one-line edit while
    debugging on a box that IS at ~/genesis — which would reproduce the defect
    with the rest of this file green.

    Allowlist rather than a ban on `~/genesis` spellings: three review rounds
    established that enumerating spellings does not converge. An unrecognised
    root fails here and someone decides about it, which is the outcome wanted.
    """
    offenders = []
    for source in _unit_sources():
        for lineno, line in enumerate(source.read_text().splitlines(), start=1):
            name, sep, value = line.partition("=")
            if not sep or name.strip() not in _PATH_DIRECTIVES:
                continue
            for token in _path_tokens(value):
                if token.startswith("__") or token.startswith(_ALLOWED_ROOT_PREFIXES):
                    continue
                offenders.append(
                    f"{source.relative_to(REPO_ROOT)}:{lineno}: {name.strip()} "
                    f"names {token!r}, which is not an allowed root"
                )
    assert not offenders, (
        "a systemd unit names a path root that is not allowed. Use __REPO_DIR__ "
        "(or another placeholder) so the installer renders it against the real "
        "checkout; if this is a genuinely new root, add it to "
        "_ALLOWED_ROOT_PREFIXES deliberately:\n  " + "\n  ".join(offenders)
    )


def _rejected(value: str) -> bool:
    """What the allowlist test would conclude about one directive value."""
    toks = _path_tokens(value)
    return bool(toks) and any(
        not (t.startswith("__") or t.startswith(_ALLOWED_ROOT_PREFIXES)) for t in toks
    )


# Built from systemd's OWN GRAMMAR — exec prefixes and quoting — not from the
# shipped units. "All 23 sources still pass" only measures the corpus the author
# already handled; every row below was a real miss in some earlier revision of
# this predicate, in one direction or the other.
_MUST_REJECT = [
    # The original defect, as the program.
    "%h/genesis/scripts/tmp_watchgod.sh",
    # ...and as an ARGUMENT, the shape seven shipped templates use.
    "/bin/bash %h/genesis/scripts/x.sh",
    # Every systemd exec prefix, alone and combined. Stripping only `-` made
    # each of these produce NO tokens at all, which is fail-open.
    "+%h/genesis/scripts/x.sh",
    "@%h/genesis/scripts/x.sh argv0",
    "!%h/genesis/scripts/x.sh",
    "!!%h/genesis/scripts/x.sh",
    ":%h/genesis/scripts/x.sh",
    "-+%h/genesis/scripts/x.sh",
    # Quoted. `.split()` left the quote attached so the token matched nothing —
    # and `-c '...'` already ships in config/genesis-guardian-watchman.service.
    "/bin/bash -c '%h/genesis/scripts/x.sh'",
    '/bin/bash -c "/home/someuser/genesis/x.sh"',
    # Other spellings of the same root.
    "~/code/genesis/x.sh",
    "$HOME/src/genesis/x.sh",
    "%h/genesis",
]

_MUST_PASS = [
    # Canonical systemd idioms. These are NOT paths, and flagging them told the
    # author to "use __REPO_DIR__", which is nonsense advice.
    "/bin/kill -HUP $MAINPID",
    "/usr/bin/foo $OPTIONS",
    "/usr/bin/foo --name %n",
    "/usr/bin/foo --instance %i",
    # The correct forms, in both positions.
    "__REPO_DIR__/scripts/tmp_watchgod.sh",
    "/bin/bash __REPO_DIR__/scripts/backup.sh",
    "/bin/bash __REPO_DIR__/scripts/x.sh __REPO_DIR__",
    # The guardian deploy target is outside the repo and stays legal.
    "%h/.local/share/genesis-guardian/.venv/bin/python",
    # System binaries, with and without the ignore-failure prefix.
    "/usr/bin/systemctl --user stop code-intel-*.scope",
    "-/usr/bin/systemctl --user stop x.scope",
    # Shipped verbatim in config/genesis-guardian-watchman.service.
    "/bin/bash -c 'systemctl --user is-active genesis-guardian.timer"
    " || systemctl --user start genesis-guardian.timer'",
    # A quoted shell fragment containing a bare `/` is not a path root.
    "/bin/bash -c 'df --output=pcent / | tail -1'",
]


@pytest.mark.parametrize("value", _MUST_REJECT)
def test_allowlist_rejects_a_hardcoded_repo_path(value):
    assert _rejected(value), f"fail-open: {value}"


@pytest.mark.parametrize("value", _MUST_PASS)
def test_allowlist_passes_legitimate_unit_values(value):
    assert not _rejected(value), f"over-fire: {value}"


def test_watchgod_uses_Type_exec_so_a_failed_exec_cannot_read_active():
    """`Type=simple` reports a unit started at FORK, before exec can fail.

    That is the window this whole PR is about: the installer asks systemd
    whether the service is alive, and under `simple` a unit whose ExecStart
    does not exist can answer `active` in the moment between the fork and the
    failed exec. MEASURED at 0 of 12 immediate samples, which makes it narrow
    and does NOT make it impossible — a null result is a lead, not a clearance.
    `Type=exec` waits for the exec to succeed, so the state is unconstructible
    rather than unlikely, and a healthy unit still reads `active` immediately
    (measured).

    Pinned because reverting one word here would silently reopen it.
    """
    template = TEMPLATE_DIR / "genesis-tmp-watchgod.service.template"
    body = template.read_text()
    assert "\nType=exec\n" in body, (
        "genesis-tmp-watchgod must be Type=exec; under Type=simple the "
        "installer's liveness check can read `active` for a unit that never ran"
    )
    assert "\nType=simple\n" not in body


def test_installer_requires_the_literal_enabled_state_not_an_exit_code():
    """`is-enabled` exit 0 does not mean "will survive a reboot".

    It also succeeds for `static`, `alias`, `indirect`, `generated`,
    `transient` — and for `enabled-runtime`, which lives under /run and
    disappears at the next boot. A unit runtime-enabled by something earlier,
    plus a persistent `enable --now` that failed (a read-only user config
    directory, say), would be reported as durably enabled while its activation
    symlink is already gone.

    That is the same defect this area was fixed for once already: treating a
    success exit code as proof of a durable property.

    Text-level, and honest about it: this pins the DECISION and its reason so a
    future edit cannot quietly revert to the exit code. It does not exercise
    systemd — the behavioural check for that is the install job, which runs the
    real installer on a clean machine.
    """
    body = INSTALL_SH.read_text()
    assert 'is-enabled --quiet genesis-tmp-watchgod' not in body, (
        "the installer is back to reading is-enabled's EXIT CODE, which is also "
        "0 for enabled-runtime and does not mean the unit survives a reboot"
    )
    assert '_wg_state=$(systemctl --user is-enabled genesis-tmp-watchgod.service' in body
    assert '[ "$_wg_state" = "enabled" ]' in body, (
        "the state must be compared to the literal 'enabled'"
    )
