"""Zone A's futility signal: did that RED pass actually reclaim anything?

The daemon logged ``Zone A RED — nuclear cleanup complete`` after every pass,
whether or not the pass freed a single byte. MEASURED 2026-09-25 on a live
install: 356 such passes across 2h45m, every one reclaiming 0 MB, while usage
climbed 379 → 430 MB. The line was true and useless.

This adds a post-clean re-measure. LOG-ONLY — nothing here changes a tier, a
verdict, or what gets deleted.

WHY THE MEASURE IS ``free`` AND NOT ``du``, which is the whole design and the
one thing a future edit must not quietly reverse. MEASURED on this btrfs
backend, 500 MB written then unlinked while a descriptor stayed open:

    state            du     df-used
    baseline           0     151752
    after 500MB      500     152253
    unlinked+held      0     152253   <-- du says 500MB freed; NOTHING was
    after close        0     151752

``du`` walks the visible tree, so an unlinked-but-held file leaves it the
instant the name goes away while its blocks stay allocated. The failure this
signal exists to catch — cleanup unlinked something a live process still
holds — is EXACTLY the case ``du`` reports as a success. ``test_the_real_defect_*``
arms pin both directions, and the du-would-have-been-fooled arm is the
acceptance bar: if it ever goes green for the wrong reason, the signal has
been silently inverted.

Harness idiom mirrors test_watchgod_zone_b_liveness.py (deliberately
duplicated — repo precedent: the watchgod test files do not share a conftest).
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

_WATCHGOD = Path(__file__).resolve().parents[2] / "scripts" / "tmp_watchgod.sh"

_TMUX_STUB = """#!/usr/bin/env bash
exit 0
"""


def _make_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _sandbox(tmp_path: Path) -> tuple[Path, Path]:
    """A fake HOME with cc-tmp under it, plus a bin dir holding a tmux stub."""
    home = tmp_path / "home"
    (home / ".genesis" / "cc-tmp").mkdir(parents=True)
    (home / ".genesis" / "logs").mkdir(parents=True)
    (home / ".genesis" / "alerts").mkdir(parents=True)
    bind = tmp_path / "bin"
    bind.mkdir()
    _make_exec(bind / "tmux", _TMUX_STUB)
    return home, bind


def _run(home: Path, bind: Path, snippet: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(HOME=str(home), PATH=f"{bind}:{os.environ['PATH']}")
    return subprocess.run(
        ["bash", "-c", f"source '{_WATCHGOD}'\n{snippet}"],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def _log(home: Path) -> str:
    p = home / ".genesis" / "logs" / "tmp_watchgod.log"
    return p.read_text() if p.exists() else ""


# ── the snapshot primitive ──────────────────────────────────────────────


def test_snapshot_emits_both_measures_as_numbers(tmp_path):
    """`<du>:<free>` — a malformed pair silently breaks every delta downstream."""
    home, bind = _sandbox(tmp_path)
    out = _run(home, bind, f'cc_reclaim_snapshot "{tmp_path}"').stdout.strip()
    assert re.fullmatch(r"\d+:\d+", out), f"expected <du>:<free>, got {out!r}"


def test_snapshot_survives_a_missing_directory(tmp_path):
    """Must not abort the daemon — both halves have documented fallbacks.

    The free half is DELIBERATELY allowed to be empty here: the strict reader
    reports "no usable reading" as an empty field rather than substituting a
    plausible number, and the caller renders that as UNMEASURABLE. `<du>:` is
    therefore a valid snapshot, not a malformed one.
    """
    home, bind = _sandbox(tmp_path)
    r = _run(home, bind, f'cc_reclaim_snapshot "{tmp_path}/nope"')
    assert r.returncode == 0, r.stderr
    assert re.fullmatch(r"\d+:\d*", r.stdout.strip()), (
        f"expected <du>:<free-or-empty>, got {r.stdout.strip()!r}"
    )


# ── the acceptance bar: the real defect ─────────────────────────────────


def _snapshot_pair(home, bind, target: Path, mutate) -> tuple[int, int, int, int]:
    """(du_before, du_after, free_before, free_after) around `mutate()`."""

    def snap() -> tuple[int, int]:
        out = _run(home, bind, f'cc_reclaim_snapshot "{target}"').stdout.strip()
        du, free = out.split(":")
        return int(du), int(free)

    du_b, fr_b = snap()
    mutate()
    subprocess.run(["sync"], check=False)
    time.sleep(2)
    du_a, fr_a = snap()
    return du_b, du_a, fr_b, fr_a


def test_the_real_defect_unlinked_but_held_reads_as_futile(tmp_path):
    """THE ACCEPTANCE BAR. Cleanup unlinks a file a live process holds.

    `free` must show ~nothing reclaimed. This is the exact shape of the
    incident: the sweep removes names, the blocks stay, usage does not fall.
    """
    home, bind = _sandbox(tmp_path)
    target = home / ".genesis" / "cc-tmp"
    big = target / "held.bin"
    big.write_bytes(os.urandom(200 * 1024 * 1024))
    subprocess.run(["sync"], check=False)

    fh = open(big, "rb")  # noqa: SIM115 — the held descriptor IS the fixture
    try:
        du_b, du_a, fr_b, fr_a = _snapshot_pair(home, bind, target, lambda: big.unlink())
        freed = fr_a - fr_b
        du_claimed = du_b - du_a

        # guard-the-guard: the fixture must really have built the hazard
        assert du_b >= 190, f"fixture never wrote the payload (du_before={du_b})"

        assert freed < 5, f"free says {freed}MB reclaimed; nothing was"
        assert du_claimed >= 190, (
            "du did NOT report a phantom reclaim, so this arm is no longer "
            f"exercising the defect (du {du_b}->{du_a})"
        )
    finally:
        fh.close()


def test_a_du_based_detector_would_have_been_fooled(tmp_path):
    """The other half of the bar, stated as its own claim.

    Without this arm, `test_the_real_defect_*` could pass on a build where the
    signal reads du — because du and free agree whenever nothing holds the
    file. The two arms together are what pin the CHOICE of measure.
    """
    home, bind = _sandbox(tmp_path)
    target = home / ".genesis" / "cc-tmp"
    big = target / "held.bin"
    big.write_bytes(os.urandom(200 * 1024 * 1024))
    subprocess.run(["sync"], check=False)

    fh = open(big, "rb")  # noqa: SIM115
    try:
        du_b, du_a, fr_b, fr_a = _snapshot_pair(home, bind, target, lambda: big.unlink())

        # The second field must really be FILESYSTEM FREE SPACE, not a second
        # reading of the directory. MEASURED: a mutation returning du for both
        # fields SURVIVED the original version of this arm, because du falls
        # while free rises, so `after - before` went negative and still read as
        # "futile" — the arm was pinning the SIGN CONVENTION, not the measure.
        #
        # Compare against the primitives themselves rather than against an
        # absolute size. An earlier version asserted `fr_b > 10_000` on the
        # reasoning that free space dwarfs a sandbox directory; that is FALSE
        # wherever pytest's basetemp lands on a small tmpfs — measured at 138MB
        # on this install's 512MB /tmp — so it would have failed on CI and on
        # any install with a small temp volume. Install-agnostic tests.
        probe = _run(
            home,
            bind,
            f'echo "$(dir_usage_mb "{target}"):$(fs_free_mb "{target}")"',
        ).stdout.strip()
        want_du, want_free = (int(x) for x in probe.split(":"))
        assert abs(fr_a - want_free) <= 5, (
            f"snapshot's second field ({fr_a}) does not track fs_free_mb "
            f"({want_free}) — it is probably reporting directory size twice"
        )
        assert want_free != want_du, (
            "fixture degenerate: free space and directory size happen to be "
            "equal here, so this arm cannot tell the two measures apart"
        )

        floor = 5
        du_verdict = "productive" if (du_b - du_a) >= floor else "futile"
        free_verdict = "productive" if (fr_a - fr_b) >= floor else "futile"
        assert du_verdict == "productive"
        assert free_verdict == "futile"
        assert du_verdict != free_verdict, "instrument inert — both measures agree"
    finally:
        fh.close()


def test_a_genuine_reclaim_is_reported_as_genuine(tmp_path):
    """The opposite direction. A detector that calls everything futile is broken."""
    home, bind = _sandbox(tmp_path)
    target = home / ".genesis" / "cc-tmp"
    big = target / "free.bin"
    big.write_bytes(os.urandom(200 * 1024 * 1024))
    subprocess.run(["sync"], check=False)

    _du_b, _du_a, fr_b, fr_a = _snapshot_pair(home, bind, target, lambda: big.unlink())
    assert (fr_a - fr_b) >= 190, f"real 200MB delete reported only {fr_a - fr_b}MB"


# ── attribution helper ──────────────────────────────────────────────────


def test_pid_helper_names_the_holding_pid(tmp_path):
    """Positive control: our own pid and path must both appear."""
    home, bind = _sandbox(tmp_path)
    probe = tmp_path / "probe.txt"
    probe.write_text("x")
    fh = open(probe, "rb")  # noqa: SIM115
    try:
        out = _run(home, bind, f'live_open_paths_with_pid | grep -F "{probe}"').stdout
        assert out.strip(), "helper found no holder for a file we are holding open"
        # TAB-separated <pid>\t<deleted|live>\t<path> — a space separator would
        # truncate any path containing a space, so the field order is pinned here
        # and the VALUE of the pid is pinned in test_pid_helper_reports_the_ACTUAL_*.
        rows = [ln.split("\t") for ln in out.strip().splitlines() if ln.strip()]
        assert rows, f"no parseable rows: {out!r}"
        assert all(len(r) == 3 for r in rows), f"expected 3 tab fields, got: {rows}"
        assert all(re.fullmatch(r"\d+", r[0]) for r in rows), f"pid field: {rows}"
        assert all(r[1] in ("live", "deleted") for r in rows), f"state field: {rows}"
        assert any(r[2] == str(probe) for r in rows), f"path field: {rows}"
    finally:
        fh.close()


def test_pid_helper_drops_the_row_once_closed(tmp_path):
    """Negative control — otherwise the helper could be matching the filesystem."""
    home, bind = _sandbox(tmp_path)
    probe = tmp_path / "probe.txt"
    probe.write_text("x")
    fh = open(probe, "rb")  # noqa: SIM115
    fh.close()
    out = _run(home, bind, f'live_open_paths_with_pid | grep -F "{probe}"').stdout
    assert not out.strip(), f"closed descriptor still reported: {out!r}"


def test_existing_live_open_paths_still_starts_at_column_one(tmp_path):
    """REGRESSION GUARD for the reason a separate helper exists at all.

    `dir_has_live_writer` and `live_writer_units` both assume `live_open_paths`
    emits a bare path in column 1. Adding a pid prefix THERE would break them
    silently — no parse error, no test failure, just a guard that stops
    matching. Pin the shape.
    """
    home, bind = _sandbox(tmp_path)
    probe = tmp_path / "probe.txt"
    probe.write_text("x")
    fh = open(probe, "rb")  # noqa: SIM115
    try:
        out = _run(home, bind, f'live_open_paths | grep -F "{probe}"').stdout
        assert out.strip(), "sibling helper found nothing — fixture broken"
        for line in out.strip().splitlines():
            assert line.startswith("/"), (
                f"live_open_paths row does not start with a path: {line!r} — "
                "a prefix here breaks dir_has_live_writer and live_writer_units"
            )
    finally:
        fh.close()


def test_attribution_line_names_holders_when_present(tmp_path):
    home, bind = _sandbox(tmp_path)
    ccdir = home / ".genesis" / "cc-tmp"
    probe = ccdir / "held.txt"
    probe.write_text("x")
    fh = open(probe, "rb")  # noqa: SIM115
    try:
        out = _run(home, bind, "cc_pinned_by_live_holders").stdout
        assert "live pid(s)" in out, f"no attribution produced: {out!r}"
        assert re.search(r"pid \d+\(\d+ fds\)", out), f"no pid named: {out!r}"
    finally:
        fh.close()


def test_attribution_does_not_claim_absence_when_it_cannot_see(tmp_path):
    """Fails OPEN, and SAYS so. An unreadable /proc and an empty one are the
    same string, so the line must not assert 'no holders'."""
    home, bind = _sandbox(tmp_path)
    out = _run(
        home,
        bind,
        "live_open_paths_with_pid() { :; }\ncc_pinned_by_live_holders",
    ).stdout
    assert "not proof of absence" in out, f"empty snapshot rendered as a claim of absence: {out!r}"


# ── the floor, and the protocol it must not disturb ─────────────────────


def test_non_numeric_floor_is_restored_loudly(tmp_path):
    """A garbage override evaluates to 0 inside (( )), which would make
    'freed < 0' permanently false and silence the signal with no error."""
    home, bind = _sandbox(tmp_path)
    # CONF_FILE is $HOME/.genesis/CONFIG/watchgod.conf. Writing one directory
    # up makes load_config a no-op, and the floor then reads 5 because that is
    # the DEFAULT — a vacuous pass indistinguishable from a real restoration.
    # That is not hypothetical: this test was written with the wrong path and
    # its first assertion passed for exactly that reason.
    conf = home / ".genesis" / "config" / "watchgod.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text('CC_RECLAIM_FLOOR_MB="banana"\nSENTINEL_CONF_WAS_READ=yes\n')
    r = _run(
        home,
        bind,
        'load_config; echo "FLOOR=$CC_RECLAIM_FLOOR_MB SENTINEL=${SENTINEL_CONF_WAS_READ:-no}"',
    )
    # guard-the-guard FIRST: prove the conf was actually sourced, so a restored
    # floor cannot be confused with a floor that was never overridden.
    assert "SENTINEL=yes" in r.stdout, (
        f"watchgod.conf was never sourced — this arm proves nothing: {r.stdout!r}"
    )
    assert "FLOOR=5" in r.stdout, f"floor not restored: {r.stdout!r}"
    assert "not a non-negative integer" in _log(home), "restoration was silent"


def test_tier_protocol_still_carries_exactly_two_fields(tmp_path):
    """HARD CONSTRAINT. `main` parses the tier line with `${cc_result##*:}`,
    which takes the field after the LAST colon. A third field silently becomes
    `used_mb`, is passed to write_state, and is interpolated UNQUOTED into the
    state JSON — a corrupted dashboard figure or invalid JSON, not an error.

    This arm exists because the futility work is under pressure to smuggle a
    counter out of the `cc_result=$(check_cc_tmp)` subshell, and this string is
    the tempting channel. It is spoken for.
    """
    src = _WATCHGOD.read_text()
    emits = re.findall(r'^\s*echo "\$tier:[^"]*"', src, re.M)
    assert emits, "could not find the tier emission — has the protocol moved?"
    for line in emits:
        assert line.count("$") == 2, (
            f"tier line carries more than two fields: {line!r} — "
            "this breaks ${cc_result##*:} in main()"
        )


# ── the log branch itself ───────────────────────────────────────────────


def test_clean_cc_red_reports_a_futile_pass_as_futile(tmp_path):
    """Drives the REAL function, not its ingredients.

    Added because a mutation inverting the futile branch
    (`_freed_mb < FLOOR` -> `_freed_mb > 999999`) SURVIVED the whole suite:
    every other arm tests the measurement primitive or the predicate's inputs,
    and nothing exercised the branch that actually emits the line. A signal
    whose emission is untested is a signal that can be switched off silently.
    """
    home, bind = _sandbox(tmp_path)
    # Nothing reclaimable in the sandbox, so the pass frees ~0 by construction.
    r = _run(home, bind, "clean_cc_red 999")
    assert r.returncode == 0, f"clean_cc_red failed: {r.stderr[-400:]}"

    log = _log(home)
    assert "nuclear cleanup complete" in log, f"function did not run to the end:\n{log}"
    assert "reclaimed ~nothing" in log, (
        "a pass that freed nothing was not reported as futile — the branch is "
        f"inverted or dead:\n{log}"
    )
    assert re.search(r"freed=-?\d+MB", log), f"no freed figure in the line:\n{log}"
    assert "floor 5MB" in log, f"the floor is not named, so the verdict is unreadable:\n{log}"
    assert "holders:" in log, f"futile line carries no attribution:\n{log}"


def test_clean_cc_red_still_names_the_du_delta_for_diagnosis(tmp_path):
    """du and free DISAGREEING is the diagnosis (unlinked-but-held), so the
    futile line must carry both or the reader cannot tell which case it is."""
    home, bind = _sandbox(tmp_path)
    _run(home, bind, "clean_cc_red 999")
    log = _log(home)
    assert re.search(r"du \d+→\d+MB", log), f"du delta missing from futile line:\n{log}"


# ── the abort class: a malformed df must never kill the daemon ───────────
#
# These are the most important arms in the file. This is a PROTECTIVE daemon
# under `set -euo pipefail`; aborting its poll loop is strictly worse than any
# log defect, because the thing that stops CC sessions being reaped stops.
#
# MEASURED 2026-09-25, on the real script, before the fix:
#   df prints `not-a-number` -> fs_free_mb returned it verbatim (the `${x:-N}`
#   fallback fires only on EMPTY) -> `$(( a - b ))` parsed it as the expression
#   `not - a - number` -> `not` is an unset name -> set -u -> the poll DIED with
#   `line 924: not: unbound variable`, mid-cleanup.
#
# `-` is the realistic trigger rather than a contrived one: some pseudo-
# filesystems report `-` in the avail column, and `$(( x - - ))` is a syntax
# error, also fatal.


def _with_stub_df(tmp_path: Path, body: str) -> tuple[Path, Path]:
    home, bind = _sandbox(tmp_path)
    _make_exec(bind / "df", body)
    return home, bind


_DF_GARBAGE = "#!/usr/bin/env bash\necho Avail\necho not-a-number\n"
_DF_DASH = "#!/usr/bin/env bash\necho Avail\necho -\n"
_DF_DEAD = "#!/usr/bin/env bash\nexit 1\n"


def test_garbage_df_does_not_abort_the_daemon(tmp_path):
    """THE BLOCKER REGRESSION. Non-empty, non-numeric df output."""
    home, bind = _with_stub_df(tmp_path, _DF_GARBAGE)
    r = _run(home, bind, "clean_cc_red 999; echo REACHED_END")
    assert "REACHED_END" in r.stdout, (
        f"clean_cc_red aborted — the poll loop would die here.\n"
        f"stdout={r.stdout!r}\nstderr={r.stderr[-400:]!r}"
    )
    assert "unbound variable" not in r.stderr, r.stderr[-400:]


def test_dash_avail_does_not_abort_the_daemon(tmp_path):
    """Same class, the realistic spelling: `-` in the avail column."""
    home, bind = _with_stub_df(tmp_path, _DF_DASH)
    r = _run(home, bind, "clean_cc_red 999; echo REACHED_END")
    assert "REACHED_END" in r.stdout, f"aborted on a `-` avail column.\nstderr={r.stderr[-400:]!r}"
    assert "syntax error" not in r.stderr, r.stderr[-400:]


def test_unmeasurable_is_reported_as_unmeasurable_not_as_a_reclaim(tmp_path):
    """A failed reading must not be rendered as a number of any size.

    `fs_free_mb`'s 999999 fallback is right for a THRESHOLD question and wrong
    for a DELTA: 999999 minus a real 300 renders as `freed=999699MB`, a
    fabricated success that silences the signal. The strict reader returns
    empty instead, and this is the line that proves the caller honours it.
    """
    home, bind = _with_stub_df(tmp_path, _DF_GARBAGE)
    _run(home, bind, "clean_cc_red 999")
    log = _log(home)
    assert "UNMEASURABLE" in log, f"failed reading not surfaced:\n{log}"
    assert "999999" not in log, f"fallback number leaked into the log:\n{log}"
    assert not re.search(r"freed=\d{5,}MB", log), f"a fabricated large reclaim was reported:\n{log}"


def test_primitives_never_emit_a_non_integer(tmp_path):
    """The class fix, at the primitive. Every caller does arithmetic on these."""
    for stub in (_DF_GARBAGE, _DF_DASH, _DF_DEAD):
        home, bind = _with_stub_df(tmp_path / f"s{hash(stub) & 0xFFFF}", stub)
        out = _run(home, bind, f'fs_free_mb "{tmp_path}"').stdout.strip()
        assert re.fullmatch(r"\d+", out), f"fs_free_mb emitted {out!r} for stub"
        strict = _run(home, bind, f'fs_free_mb_strict "{tmp_path}"').stdout.strip()
        assert strict == "", f"strict reader should be empty, got {strict!r}"


def test_a_leading_zero_floor_is_rejected(tmp_path):
    """`08` passes `^[0-9]+$` and then errors in (( )) with 'value too great
    for base' — the same silencing the validator exists to prevent, through a
    different door. `010` is worse: it is accepted and read as OCTAL 8."""
    home, bind = _sandbox(tmp_path)
    conf = home / ".genesis" / "config" / "watchgod.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text("CC_RECLAIM_FLOOR_MB=08\nSENTINEL_CONF_WAS_READ=yes\n")
    r = _run(
        home,
        bind,
        'load_config; echo "FLOOR=$CC_RECLAIM_FLOOR_MB SENTINEL=${SENTINEL_CONF_WAS_READ:-no}"',
    )
    assert "SENTINEL=yes" in r.stdout, f"conf not sourced: {r.stdout!r}"
    assert "FLOOR=5" in r.stdout, f"08 was accepted: {r.stdout!r}"


# ── attribution: the pid must be the REAL pid, and deleted must be flagged ──


def test_pid_helper_reports_the_ACTUAL_holding_pid(tmp_path):
    """Pins the VALUE, not just the shape.

    MEASURED: replacing the pid extraction with a row counter
    (`print NR, $2`) produced plausible `<digits> <path>` rows and SURVIVED
    every shape-matching assertion in this file. A wrong pid is worse than no
    pid — it sends the next investigation at an innocent process.
    """
    home, bind = _sandbox(tmp_path)
    probe = tmp_path / "probe.txt"
    probe.write_text("x")
    fh = open(probe, "rb")  # noqa: SIM115
    try:
        out = _run(home, bind, f'live_open_paths_with_pid | grep -F "{probe}"').stdout
        rows = [ln.split("\t") for ln in out.strip().splitlines() if ln.strip()]
        assert rows, f"no rows for a file we hold open: {out!r}"
        pids = {r[0] for r in rows}
        assert str(os.getpid()) in pids, (
            f"helper did not report the REAL holding pid {os.getpid()}; got {sorted(pids)}"
        )
    finally:
        fh.close()


def test_pid_helper_flags_a_deleted_inode(tmp_path):
    """The discriminator. deleted-but-pinned and spared-and-live are the two
    causes of a futile pass and they have OPPOSITE remedies, so one string for
    both hands the operator the news and not the answer."""
    home, bind = _sandbox(tmp_path)
    probe = tmp_path / "gone.bin"
    probe.write_bytes(b"x" * 1024)
    fh = open(probe, "rb")  # noqa: SIM115
    try:
        probe.unlink()  # unlinked, still held
        out = _run(home, bind, f'live_open_paths_with_pid | grep -F "{probe}"').stdout
        rows = [ln.split("\t") for ln in out.strip().splitlines() if ln.strip()]
        assert rows, f"unlinked-but-held file not reported at all: {out!r}"
        assert any(r[1] == "deleted" for r in rows), (
            f"held-and-unlinked inode not flagged deleted: {rows}"
        )
        assert all(not r[2].endswith("(deleted)") for r in rows), (
            f"the ' (deleted)' suffix leaked into the path field: {rows}"
        )
    finally:
        fh.close()


def test_pid_helper_survives_a_path_containing_a_space(tmp_path):
    """The docstring promises `<pid> <sep> <path>`; a space-split `$2` would
    truncate `/tmp/my file.bin` to `/tmp/my` and quietly lie."""
    home, bind = _sandbox(tmp_path)
    probe = tmp_path / "my file.bin"
    probe.write_text("x")
    fh = open(probe, "rb")  # noqa: SIM115
    try:
        out = _run(home, bind, "live_open_paths_with_pid").stdout
        rows = [ln.split("\t") for ln in out.strip().splitlines() if "my file.bin" in ln]
        assert rows, "path with a space was not reported"
        assert any(r[2] == str(probe) for r in rows), (
            f"path truncated at the space: {[r[2] for r in rows]}"
        )
    finally:
        fh.close()
