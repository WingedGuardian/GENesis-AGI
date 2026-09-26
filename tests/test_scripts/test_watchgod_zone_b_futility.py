"""tmp_watchgod Zone B futility — a sweep that reclaims nothing must say so.

Origin (MEASURED on a live install, 2026-09-26): `/tmp` sat at 74% of a 512MB
tmpfs and the Zone B ORANGE tier ran every 30 seconds for four days — over
1,250 passes — reclaiming nothing, with no log line saying so. Most of the
space in use belonged to a DIFFERENT uid, and `/tmp` is mode 1777: on a sticky
directory only the entry's owner may unlink it. Every Zone B sweep ends
`-delete 2>/dev/null || true`, so EPERM and "nothing matched" produce
byte-for-byte identical output. The failure was not merely unfixed, it was
invisible.

REPORT-ONLY by decision: re-measure after the cleaner, log once per tier with
attribution, behind a dedupe flag. No paging, no change to what is deleted.
Breaking the futile loop is deferred until the Zone A futility work lands, so
the two designs converge in one place instead of diverging in two.

Harness idiom mirrors the sibling watchgod test files (deliberately duplicated;
repo precedent is that they do not share a conftest): a HOME-redirected sandbox
so the script's HOME-derived paths land in tmp_path, overrides injected as a
bash snippet after sourcing, and PATH-prepended stubs.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

_WATCHGOD = Path(__file__).resolve().parents[2] / "scripts" / "tmp_watchgod.sh"

# A PASS-THROUGH stat stub: any path whose basename begins with "foreign" is
# reported as owned by uid 9999, "alien" as 8888; everything else defers to the
# real stat. A test cannot chown without privilege, and the ownership branch is
# the whole point of the function, so the stub is what makes it reachable.
_STAT_STUB = r"""#!/usr/bin/env bash
if [[ "$1" == "-c" && "$2" == "%u" ]]; then
  base="$(basename "$3")"
  case "$base" in
    foreign*) echo 9999; exit 0 ;;
    alien*)   echo 8888; exit 0 ;;
    # The policy-spared names are reported FOREIGN on purpose: otherwise the
    # own-uid check spares them first and the policy arm passes for the wrong
    # reason. A mutation deleting the policy skip survived the entire suite
    # until this line existed.
    tmux-*|pytest-*|claude-*) echo 9999; exit 0 ;;
  esac
fi
exec /usr/bin/stat "$@"
"""


def _make_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _sandbox(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    (home / ".genesis" / "logs").mkdir(parents=True)
    (home / ".genesis" / "alerts").mkdir(parents=True)
    (home / ".genesis" / "cc-tmp").mkdir(parents=True)
    bind = tmp_path / "bin"
    bind.mkdir()
    _make_exec(bind / "stat", _STAT_STUB)
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


def _flag(home: Path, tier: str) -> Path:
    return home / ".genesis" / "alerts" / f"sys_tmp_stuck.{tier}"


def _mb(path: Path, mb: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * (mb * 1024 * 1024))


def _helper(home: Path, bind: Path, root: Path) -> tuple[int, str, int]:
    """`<mb> <uids> <unreadable>` from the script's own resolver."""
    proc = _run(home, bind, f'sys_unreclaimable_mb "{root}"')
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    mb, uids, unreadable = proc.stdout.split()
    return int(mb), uids, int(unreadable)


# ── the attribution helper ───────────────────────────────────────────────


def test_unreclaimable_reports_nothing_when_everything_is_ours(tmp_path):
    """The negative control. Without it, a helper that reported every entry as
    foreign would pass the arms below and the log line would name a cause that
    is not there."""
    home, bind = _sandbox(tmp_path)
    root = tmp_path / "faketmp"
    _mb(root / "mine" / "blob", 2)

    assert _helper(home, bind, root) == (0, "none", 0)


def test_unreclaimable_names_the_bytes_and_the_owning_uid(tmp_path):
    """The real shape: a tree this daemon cannot unlink because it belongs to
    someone else. The line must carry HOW MUCH and WHOSE — a log that proves
    only `when` buys another occurrence."""
    home, bind = _sandbox(tmp_path)
    root = tmp_path / "faketmp"
    _mb(root / "foreign-tree" / "blob", 3)
    _mb(root / "mine" / "blob", 1)

    mb, uids, unreadable = _helper(home, bind, root)
    assert mb >= 3, f"expected at least the 3MB foreign tree, got {mb}"
    assert uids == "9999", uids
    assert unreadable == 0


def test_unreclaimable_does_not_count_our_own_data(tmp_path):
    """Guard against the easy over-count: only the foreign tree is attributed,
    so the figure in the log is actionable rather than alarming."""
    home, bind = _sandbox(tmp_path)
    root = tmp_path / "faketmp"
    _mb(root / "foreign-small" / "blob", 1)
    _mb(root / "mine-large" / "blob", 8)

    mb, _, _ = _helper(home, bind, root)
    assert mb < 8, f"our own 8MB was counted as unreclaimable: {mb}"


def test_an_UNREADABLE_foreign_tree_is_reported_separately_from_its_size(tmp_path):
    """The commonest shape in the field: a foreign tree at mode 700. `du` can
    stat the entry and not descend, so it returns 0 — and a helper that
    collapsed "0 megabytes" into "no foreign owner" would make the report state
    the OPPOSITE of what it found. Size and readability are two facts."""
    home, bind = _sandbox(tmp_path)
    root = tmp_path / "faketmp"
    blocked = root / "foreign-locked"
    _mb(blocked / "blob", 4)
    blocked.chmod(0o000)
    try:
        mb, uids, unreadable = _helper(home, bind, root)
    finally:
        blocked.chmod(0o755)  # so pytest can clean the tmp tree up

    assert uids == "9999", "the owner was lost along with the size"
    assert unreadable == 1, f"the unreadable tree was not counted: {unreadable}"
    assert mb == 0, f"du should not have been able to measure it: {mb}"


def test_policy_spared_trees_are_not_attributed_to_ownership(tmp_path):
    """tmux/pytest/claude trees survive the sweep for a reason that has nothing
    to do with who owns them. Naming them sends the reader to the wrong fix —
    "give that writer a different TMPDIR" does not apply to a directory the
    sweep is deliberately configured to spare."""
    home, bind = _sandbox(tmp_path)
    root = tmp_path / "faketmp"
    for name in ("tmux-1001", "pytest-of-someone", "claude-1001"):
        _mb(root / name / "blob", 2)

    mb, uids, _ = _helper(home, bind, root)
    assert uids == "none", f"a policy-spared tree was attributed to ownership: {uids}"
    assert mb == 0, mb


def test_multiple_foreign_uids_are_listed_once_each(tmp_path):
    """The dedupe in the uid accumulator, which nothing else exercises."""
    home, bind = _sandbox(tmp_path)
    root = tmp_path / "faketmp"
    _mb(root / "foreign-a" / "blob", 2)
    _mb(root / "foreign-b" / "blob", 1)
    _mb(root / "alien-c" / "blob", 3)

    _, uids, _ = _helper(home, bind, root)
    assert sorted(uids.split(",")) == ["8888", "9999"], uids


# ── the futility decision ────────────────────────────────────────────────

# `tmp_usage_pct` is stubbed so the tier and the post-cleanup measurement are
# both controlled; the real one reads a live filesystem this test must not
# depend on. The cleaners are stubbed to no-ops so nothing touches the real
# /tmp — which is exactly why a behavioural arm over the real function is not
# written here.
_STUB_TIERS = "clean_sys_red() { :; }; clean_sys_orange() { :; }; clean_sys_yellow() { :; }; "


def _pct(before: int, after: int) -> str:
    """A tmp_usage_pct returning `before` on its first call and `after` on every
    later one — the pre-cleanup tier decision, then the re-measure.

    The call counter lives in a FILE, not a variable. `pct_after=$(tmp_usage_pct)`
    is a command substitution, so a shell variable incremented inside it is
    incremented in a SUBSHELL and discarded — the stub would return `before`
    forever and every futility arm would silently exercise the wrong branch."""
    return (
        '_pctf="$(mktemp)"; printf 0 > "$_pctf"; '
        'tmp_usage_pct() { local n; n=$(cat "$_pctf"); n=$((n+1)); '
        'printf "%s" "$n" > "$_pctf"; '
        f"if (( n == 1 )); then echo {before}; else echo {after}; fi; }}; "
    )


def _pct_at(value: int) -> str:
    """A constant tmp_usage_pct, for arms that call `sys_report_futility`
    DIRECTLY. Those skip `check_sys_tmp`, so nothing has consumed a pre-cleanup
    reading — the single call they make IS the re-measure, and a two-value stub
    would hand them the `before` figure instead."""
    return f"tmp_usage_pct() {{ echo {value}; }}; "


def test_a_sweep_that_reclaims_nothing_logs_once_with_attribution(tmp_path):
    """THE ACCEPTANCE BAR, replaying the measured episode: still ORANGE after
    the sweep, nothing reclaimed, and the space owned by another uid."""
    home, bind = _sandbox(tmp_path)
    root = tmp_path / "faketmp"
    _mb(root / "foreign-tree" / "blob", 3)

    snippet = (
        _STUB_TIERS
        + _pct_at(74)
        + f'sys_report_futility orange 74 70 "{root}"; '
        + f'sys_report_futility orange 74 70 "{root}"'  # second poll: dedupe
    )
    proc = _run(home, bind, snippet)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    text = _log(home)
    assert text.count("sys-tmp STUCK ORANGE") == 1, (
        f"expected one STUCK line across two polls, got {text.count('sys-tmp STUCK ORANGE')}"
    )
    assert "uid(s) 9999" in text, f"the owning uid is not named:\n{text}"
    assert "sticky directory" in text, "the line does not say WHY it cannot reclaim"
    assert _flag(home, "orange").exists()


def test_an_escalation_to_RED_is_not_suppressed_by_the_ORANGE_record(tmp_path):
    """The regression that matters most operationally.

    A single un-scoped dedupe flag lets the ORANGE record suppress the RED one
    that follows as usage climbs — and the clear conditions never fire while
    things are getting WORSE, so the severe tier is exactly the one that goes
    unrecorded. Zone A documents this failure for its own flag; reproducing it
    here would be committing a mistake this file already warns about."""
    home, bind = _sandbox(tmp_path)
    root = tmp_path / "faketmp"
    _mb(root / "foreign-tree" / "blob", 3)

    snippet = (
        _STUB_TIERS
        + _pct_at(90)
        + f'sys_report_futility orange 74 70 "{root}"; '
        + f'sys_report_futility red 90 85 "{root}"'
    )
    proc = _run(home, bind, snippet)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    text = _log(home)
    assert "sys-tmp STUCK ORANGE" in text, text
    assert "sys-tmp STUCK RED" in text, (
        "the RED escalation was suppressed by the ORANGE dedupe flag:\n" + text
    )


def test_progress_clears_every_tier_flag(tmp_path):
    """A stale flag must not suppress the NEXT episode, at any tier.

    The figures matter: 75 is BELOW the 80 before-reading (so the progress
    clause fires) and still ABOVE the 70 threshold (so the under-threshold
    clause cannot). An earlier version of this arm used numbers that cleared
    via the threshold instead, and a mutation INVERTING the progress comparison
    survived the whole suite."""
    home, bind = _sandbox(tmp_path)
    root = tmp_path / "faketmp"
    _mb(root / "foreign-tree" / "blob", 3)
    _flag(home, "orange").touch()
    _flag(home, "red").touch()

    proc = _run(
        home, bind, _STUB_TIERS + _pct_at(75) + f'sys_report_futility orange 80 70 "{root}"'
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert not _flag(home, "orange").exists(), "progress must clear the dedupe flag"
    assert not _flag(home, "red").exists(), "progress must clear EVERY tier's flag"
    assert "STUCK" not in _log(home)


def test_dropping_under_the_threshold_is_not_futility(tmp_path):
    """Reclaiming nothing while already back under the line is not a stuck
    tier — it is a tier that had nothing to do."""
    home, bind = _sandbox(tmp_path)
    root = tmp_path / "faketmp"
    _mb(root / "foreign-tree" / "blob", 3)

    proc = _run(
        home, bind, _STUB_TIERS + _pct_at(70) + f'sys_report_futility orange 71 70 "{root}"'
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "STUCK" not in _log(home)
    assert not _flag(home, "orange").exists()


def test_stuck_with_no_foreign_owner_says_so_rather_than_inventing_one(tmp_path):
    """Fail honest: when nothing foreign-owned explains the pressure, the line
    must not name a cause it did not find. An attribution that is wrong is
    worse than none — it sends the reader to the wrong place."""
    home, bind = _sandbox(tmp_path)
    root = tmp_path / "faketmp"
    _mb(root / "mine" / "blob", 3)

    proc = _run(
        home, bind, _STUB_TIERS + _pct_at(74) + f'sys_report_futility orange 74 70 "{root}"'
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    text = _log(home)
    assert "sys-tmp STUCK ORANGE" in text
    assert "uid(s)" not in text, f"named an owner it did not find:\n{text}"
    assert "no foreign-owned data explains it" in text


def test_an_unreadable_owner_is_named_not_denied(tmp_path):
    """The reporting-layer half of the unreadable-tree case. A foreign tree we
    cannot measure must be reported as an owner we FOUND and could not size —
    never as "no foreign-owned data explains it", which is the opposite of what
    the helper returned."""
    home, bind = _sandbox(tmp_path)
    root = tmp_path / "faketmp"
    blocked = root / "foreign-locked"
    _mb(blocked / "blob", 4)
    blocked.chmod(0o000)
    try:
        proc = _run(
            home, bind, _STUB_TIERS + _pct_at(74) + f'sys_report_futility orange 74 70 "{root}"'
        )
    finally:
        blocked.chmod(0o755)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    text = _log(home)
    assert "uid(s) 9999" in text, f"the owner we found was not named:\n{text}"
    assert "unmeasurable" in text, f"a size we could not take was stated anyway:\n{text}"
    assert "no foreign-owned data explains it" not in text, (
        "the report denied the very owner it had just found:\n" + text
    )


# ── the tier contract, unchanged ─────────────────────────────────────────


def test_check_sys_tmp_actually_REACHES_the_futility_report(tmp_path):
    """THE WIRING ARM, and it exists because its absence was measured.

    Every arm above calls `sys_report_futility` directly, which proves the
    helper works and proves NOTHING about whether the tier ever calls it. A
    mutation replacing the call site with `:` left the whole suite green — the
    report could have been dead code and nothing would have noticed."""
    home, bind = _sandbox(tmp_path)
    snippet = (
        _STUB_TIERS
        + _pct(74, 74)
        + 'sys_unreclaimable_mb() { printf "%s %s %s" 42 4242 0; }; '
        + "check_sys_tmp"
    )
    proc = _run(home, bind, snippet)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert proc.stdout.strip() == "orange:74"

    text = _log(home)
    assert "sys-tmp STUCK ORANGE" in text, (
        "the tier never reached the futility report — the call site is dead:\n" + text
    )
    assert "42MB" in text and "uid(s) 4242" in text, (
        f"the tier reached the report but did not carry its attribution:\n{text}"
    )


def test_YELLOW_does_not_report_futility(tmp_path):
    """YELLOW sweeps files untouched for 7+ days, so a /tmp holding live content
    above half has nothing for it to delete — reclaiming nothing there is the
    HEALTHY steady state. MEASURED: this install sits at 57% with no problem at
    all, and reporting on YELLOW would fire a warning on the first poll after
    deploy. A check that cries wolf gets silenced."""
    home, bind = _sandbox(tmp_path)
    snippet = (
        _STUB_TIERS
        + _pct(57, 57)
        + 'sys_unreclaimable_mb() { printf "%s %s %s" 289 1001 0; }; '
        + "check_sys_tmp"
    )
    proc = _run(home, bind, snippet)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert proc.stdout.strip() == "yellow:57"
    assert "STUCK" not in _log(home), (
        "YELLOW reported futility — a healthy filesystem above half will warn "
        "on every deploy:\n" + _log(home)
    )


def test_check_sys_tmp_still_echoes_the_pre_cleanup_pct(tmp_path):
    """REGRESSION GUARD. Two sibling suites and the dashboard parse this
    string. The re-measure is for the LOG; leaking it into the return value
    would silently change the figure the dashboard renders — and so would any
    stray byte from the new helper."""
    home, bind = _sandbox(tmp_path)
    proc = _run(home, bind, _STUB_TIERS + _pct(74, 40) + "check_sys_tmp")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert proc.stdout.strip() == "orange:74", (
        f"the tier string changed: {proc.stdout.strip()!r} (expected 'orange:74' — "
        f"the PRE-cleanup figure, not the re-measure)"
    )


def test_green_clears_every_stale_tier_flag(tmp_path):
    """A tier that never fires still has to retire the previous episode, or the
    next real one is suppressed by a flag nobody cleared."""
    home, bind = _sandbox(tmp_path)
    for tier in ("orange", "red"):
        _flag(home, tier).touch()
    proc = _run(home, bind, _STUB_TIERS + _pct(10, 10) + "check_sys_tmp")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert proc.stdout.strip() == "green:10"
    for tier in ("orange", "red"):
        assert not _flag(home, tier).exists(), f"green must clear the {tier} flag"
