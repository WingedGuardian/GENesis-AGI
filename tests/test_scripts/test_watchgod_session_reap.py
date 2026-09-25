"""YELLOW reaps stale SESSIONS by their contents, not projects by their mtime.

The predicate this replaces was

    find "$CC_TMP_DIR" -mindepth 2 -maxdepth 2 -type d \\
         -path "*/claude-*/???*" -mtime +7 -exec rm -rf {} +

and it was wrong three ways at once:

  * depth 2 is the PROJECT directory, so one ``rm -rf`` took every session
    under it rather than the stale one;
  * ``-mtime +7`` on a directory tests the DIRECTORY's own mtime, and a project
    directory's mtime moves only when a session dir is created or removed
    directly under it — never when a live session writes inside one. It answers
    "when did a session last START here", not "is anything here still in use";
  * ``claude-*`` is unanchored and also matches sibling caches such as
    ``claude-skills`` (#1878, which absorbed #2297).

REPRODUCED against the unfixed predicate before the fix was written: a project
whose newest file was written SECONDS ago, with its own directory mtime
backdated 30 days, was selected for deletion. Work in one project for a month
without starting a new session there and it ages out with every session it
holds.

FAIL-CLOSED is the load-bearing half. The freshness probe separates "nothing
inside is fresh" from "could not look" by EXIT STATUS, never by empty output —
those are the same string, and conflating them is exactly how #2342 shipped a
fail-open twice in one review. MEASURED on GNU findutils 4.9.0, which is what
the daemon resolves ``find`` to: rc=0 for a match AND for no-match, rc=1 for a
missing or unreadable directory.

⚠ An interactive shell on this install shims ``find`` to ``bfs``, which rejects
some GNU date specs outright. A non-interactive ``bash -c`` — which is what the
daemon and this harness both use — gets ``/usr/bin/find`` (GNU). Measure this
behaviour with the engine the DAEMON uses, never from an interactive prompt.

Harness idiom mirrors test_watchgod_zone_b_liveness.py (deliberately
duplicated — repo precedent: the watchgod test files do not share a conftest).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

_WATCHGOD = Path(__file__).resolve().parents[2] / "scripts" / "tmp_watchgod.sh"

_TMUX_STUB = "#!/usr/bin/env bash\nexit 0\n"


def _make_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _sandbox(tmp_path: Path) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    root = home / ".genesis" / "cc-tmp"
    root.mkdir(parents=True)
    (home / ".genesis" / "logs").mkdir(parents=True)
    (home / ".genesis" / "alerts").mkdir(parents=True)
    bind = tmp_path / "bin"
    bind.mkdir()
    _make_exec(bind / "tmux", _TMUX_STUB)
    return home, root, bind


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


def _reap(home: Path, root: Path, bind: Path, days: int = 7):
    return _run(home, bind, f'reap_stale_session_dirs "{root}" {days}')


def _log(home: Path) -> str:
    p = home / ".genesis" / "logs" / "tmp_watchgod.log"
    return p.read_text() if p.exists() else ""


def _age(path: Path, days: int) -> None:
    when = time.time() - days * 86400
    os.utime(path, (when, when))


def _session(root: Path, project: str, name: str, *, uid: str = "claude-1000") -> Path:
    s = root / uid / project / name
    s.mkdir(parents=True)
    (s / "payload.jsonl").write_text("work")
    return s


# ── the acceptance bar ──────────────────────────────────────────────────


def test_a_live_session_under_an_ancient_project_dir_survives(tmp_path):
    """THE DATA-LOSS CASE. Project dir mtime is 30 days old; its session was
    written seconds ago. The old predicate selected the PROJECT for rm -rf."""
    home, root, bind = _sandbox(tmp_path)
    sess = _session(root, "proj-live", "sess-a")
    _age(root / "claude-1000" / "proj-live", 30)  # the directory, NOT its contents

    # guard-the-guard: the hazard must actually exist, or this proves nothing
    proj_mtime = (root / "claude-1000" / "proj-live").stat().st_mtime
    assert time.time() - proj_mtime > 20 * 86400, "fixture did not age the project dir"
    assert time.time() - (sess / "payload.jsonl").stat().st_mtime < 300, (
        "fixture payload is not fresh"
    )

    r = _reap(home, root, bind)
    assert r.returncode == 0, r.stderr
    assert (sess / "payload.jsonl").exists(), (
        "a session written seconds ago was deleted because its PROJECT "
        "directory mtime was old — this is the bug"
    )


def test_a_genuinely_stale_session_is_still_reaped(tmp_path):
    """The other direction. A reaper that spares everything is disabled, not safe."""
    home, root, bind = _sandbox(tmp_path)
    sess = _session(root, "proj-old", "sess-b")
    for p in (sess / "payload.jsonl", sess, root / "claude-1000" / "proj-old"):
        _age(p, 30)

    r = _reap(home, root, bind)
    assert r.returncode == 0, r.stderr
    assert not sess.exists(), "a session with nothing newer than 7d was not reaped"


def test_only_the_stale_session_goes_not_its_live_sibling(tmp_path):
    """Depth-3 reaping: one stale session must not take the project with it."""
    home, root, bind = _sandbox(tmp_path)
    live = _session(root, "shared", "sess-live")
    stale = _session(root, "shared", "sess-stale")
    for p in (stale / "payload.jsonl", stale):
        _age(p, 30)
    _age(root / "claude-1000" / "shared", 30)

    _reap(home, root, bind)
    assert live.exists(), "the live sibling was taken with the stale session"
    assert not stale.exists(), "the stale session survived"


def test_the_unanchored_glob_no_longer_matches_a_cache_sibling(tmp_path):
    """`claude-*` also matched `claude-skills`, a cache this script deletes by
    name elsewhere. Anchored to the numeric-uid form (#1878, absorbing #2297)."""
    home, root, bind = _sandbox(tmp_path)
    cache = root / "claude-skills" / "skill-x" / "inner"
    cache.mkdir(parents=True)
    (cache / "f").write_text("x")
    for p in (cache / "f", cache, cache.parent):
        _age(p, 30)

    _reap(home, root, bind)
    assert (cache / "f").exists(), (
        "claude-skills cache was reaped by the session sweep — the glob is still unanchored"
    )


# ── fail-closed ─────────────────────────────────────────────────────────


def test_an_unreadable_session_is_spared_and_says_so(tmp_path):
    """FAIL CLOSED. `find` returning nothing and `find` failing are the same
    string; only the exit status separates them. An unreadable session must be
    SPARED, and the sparing must be visible — a silent spare is indistinguishable
    from a reaper that has quietly stopped working."""
    home, root, bind = _sandbox(tmp_path)
    sess = _session(root, "proj-sealed", "sess-sealed")
    _age(root / "claude-1000" / "proj-sealed", 30)
    # The session dir's OWN mtime must be old too, or `find` matches it as fresh
    # before it ever needs to descend — the probe then SUCCEEDS and this arm
    # silently exercises the spared-live path instead of the blind one.
    # MEASURED with a fresh session mtime: the log read "spared 1 with content
    # newer than 7d", so the fixture was not building the hazard at all.
    _age(sess, 30)
    sess.chmod(0o000)
    try:
        r = _reap(home, root, bind)
        assert r.returncode == 0, r.stderr
        assert sess.exists(), "an unreadable session was deleted — fail-OPEN"
        log = _log(home)
        assert "could not be determined" in log, f"the spare was silent:\n{log}"
        assert "failing closed" in log
        # guard-the-guard: it must be the BLIND path, not the live-content one
        assert "SPARED 1 session dir(s)" in log, (
            f"spared for the wrong reason — the probe did not fail:\n{log}"
        )
    finally:
        sess.chmod(0o755)


def test_a_session_holding_one_fresh_file_deep_inside_is_spared(tmp_path):
    """Freshness is judged RECURSIVELY. The old form looked only at the top
    directory mtime, which is the whole defect."""
    home, root, bind = _sandbox(tmp_path)
    sess = _session(root, "proj-deep", "sess-deep")
    deep = sess / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "fresh.txt").write_text("recent")
    # age everything EXCEPT the one deep file
    for p in (sess / "payload.jsonl", sess / "a" / "b", sess / "a", sess):
        _age(p, 30)
    _age(root / "claude-1000" / "proj-deep", 30)

    _reap(home, root, bind)
    assert (deep / "fresh.txt").exists(), (
        "a session whose only fresh file was 3 levels down was reaped — the probe is not recursive"
    )


# ── the empty-project pass ──────────────────────────────────────────────


def test_an_empty_project_older_than_an_hour_is_removed(tmp_path):
    """An already-empty, already-old project shell is collected."""
    home, root, bind = _sandbox(tmp_path)
    shell = root / "claude-1000" / "proj-shell"
    shell.mkdir(parents=True)
    _age(shell, 30)

    _reap(home, root, bind)
    assert not shell.exists(), "an hour-old empty project shell was not collected"


def test_a_JUST_emptied_project_waits_for_a_later_poll(tmp_path):
    """Deliberate, and the reason `-mmin +60` is not redundant with `-empty`.

    Removing a directory entry updates the PARENT's mtime (measured), so a
    project emptied by this very sweep is seconds old and is skipped. That is
    what protects the real hazard: a project directory is also empty for the
    instant between its own mkdir and its first session's mkdir, and YELLOW
    polls every 30s whenever cc-tmp is over half its budget. Without the age
    guard a brand-new project is deleted in that window — measured — and the
    live-writer exclusions cannot help, because a directory created a
    millisecond ago holds no open descriptor.
    """
    home, root, bind = _sandbox(tmp_path)
    sess = _session(root, "proj-gone", "sess-only")
    for p in (sess / "payload.jsonl", sess, root / "claude-1000" / "proj-gone"):
        _age(p, 30)

    _reap(home, root, bind)
    assert not sess.exists(), "fixture vacuous: the session was not reaped"
    assert (root / "claude-1000" / "proj-gone").exists(), (
        "a project emptied by THIS sweep was removed in the same pass — the "
        "age guard is missing, and a brand-new project would be deleted too"
    )


def test_a_brand_new_empty_project_is_never_removed(tmp_path):
    """The hazard the age guard exists for, stated directly."""
    home, root, bind = _sandbox(tmp_path)
    fresh = root / "claude-1000" / "brand-new-project"
    fresh.mkdir(parents=True)  # mtime = now, nothing inside yet

    _reap(home, root, bind)
    assert fresh.exists(), (
        "a project directory created this instant was deleted — CC would hit "
        "ENOENT creating its session dir"
    )


def test_a_project_still_holding_a_session_is_never_removed(tmp_path):
    """`-empty` means exactly that, so this can never take a populated project
    — but assert it, because the empty pass runs unconditionally."""
    home, root, bind = _sandbox(tmp_path)
    live = _session(root, "proj-keep", "sess-live")
    _age(root / "claude-1000" / "proj-keep", 30)

    _reap(home, root, bind)
    assert (root / "claude-1000" / "proj-keep").exists(), "populated project removed"
    assert live.exists()


# ── the daemon must survive whatever this does ──────────────────────────


def test_reaping_an_absent_root_does_not_abort(tmp_path):
    """set -euo pipefail: the poll loop dying is worse than any reap defect."""
    home, root, bind = _sandbox(tmp_path)
    r = _run(home, bind, f'reap_stale_session_dirs "{root}/nope" 7; echo REACHED_END')
    assert "REACHED_END" in r.stdout, f"aborted on a missing root: {r.stderr[-300:]!r}"


def test_reaping_an_empty_root_does_not_abort(tmp_path):
    home, root, bind = _sandbox(tmp_path)
    r = _run(home, bind, f'reap_stale_session_dirs "{root}" 7; echo REACHED_END')
    assert "REACHED_END" in r.stdout, f"aborted on an empty root: {r.stderr[-300:]!r}"


def test_a_session_path_containing_a_space_is_handled(tmp_path):
    """The enumeration is -print0/read -d '' precisely so this works."""
    home, root, bind = _sandbox(tmp_path)
    sess = _session(root, "my project", "sess x")
    for p in (sess / "payload.jsonl", sess):
        _age(p, 30)
    _age(root / "claude-1000" / "my project", 30)

    r = _reap(home, root, bind)
    assert r.returncode == 0, r.stderr
    assert not sess.exists(), "a stale session whose path contains spaces survived"


# ── the exclusion plumbing ──────────────────────────────────────────────


def test_a_live_writers_session_is_excluded_even_when_its_contents_are_stale(tmp_path):
    """The integration nothing covered, PROVEN uncovered before it was written.

    MEASURED: stripping `${excl[@]+"${excl[@]}"}` from BOTH finds inside
    reap_stale_session_dirs left all 11 original arms green AND all 93 watchgod
    tests green. Every arm passed an empty exclusion set, so the live-writer
    protection — the one thing between this deletion path and a session being
    written right now — was exercised by nothing.

    Every mtime here is aged, so the open descriptor is the ONLY thing that can
    save the held session. The stale-sibling assertion is the negative control:
    without it this passes when the reaper does nothing at all.
    """
    home, root, bind = _sandbox(tmp_path)
    held = _session(root, "shared", "sess-held")
    stale = _session(root, "shared", "sess-stale")
    for s in (held, stale):
        for p in (s / "payload.jsonl", s):
            _age(p, 30)
    _age(root / "claude-1000" / "shared", 30)

    fd = os.open(held / "payload.jsonl", os.O_RDONLY)
    try:
        r = _run(
            home,
            bind,
            "declare -a ex=(); "
            f'zone_a_live_exclusions ex "$(live_open_paths)" "{root}"; '
            f'reap_stale_session_dirs "{root}" 7 ${{ex[@]+"${{ex[@]}}"}}',
        )
        assert r.returncode == 0, r.stderr
    finally:
        os.close(fd)

    assert not stale.exists(), (
        "fixture vacuous: nothing was reaped, so the spare below proves nothing"
    )
    assert held.exists(), (
        "a session with a live open descriptor was reaped — the exclusion array "
        "is not reaching the depth-3 enumeration"
    )


# ── the perimeter: a malformed age must not widen the cutoff ────────────


def test_a_malformed_age_refuses_to_reap(tmp_path):
    """`find -newermt` does NOT reject garbage — GNU parse_datetime reinterprets
    most of it and returns SUCCESS, so the fail-closed path never fires.

    MEASURED 2026-09-25 (today = 09-25): '7d days ago' -> 2026-09-23, rc=0;
    'X days ago' -> 2026-09-24, rc=0; ' days ago' -> 2026-09-24, rc=0. Each
    moves the cutoff FORWARD, so a caller passing "7d" or an unset variable
    reaps nearly the whole tree while the log still says "newer than 7d".
    """
    for bad in ("7d", "", "X", "-7", "0", "7.5"):
        home, root, bind = _sandbox(tmp_path / f"bad{abs(hash(bad)) % 99999}")
        sess = _session(root, "proj", "sess")
        for p in (sess / "payload.jsonl", sess, root / "claude-1000" / "proj"):
            _age(p, 2)  # 2 days: stale under a corrupted cutoff, fresh under 7
        r = _run(home, bind, f'reap_stale_session_dirs "{root}" "{bad}"')
        assert r.returncode == 0, r.stderr
        assert sess.exists(), (
            f"age_days={bad!r} reaped a 2-day-old session — the malformed age "
            "widened the cutoff instead of refusing"
        )
        assert "not a positive integer" in _log(home), (
            f"age_days={bad!r} was accepted silently:\n{_log(home)}"
        )


def test_a_claude_digit_component_in_the_ROOT_path_does_not_widen_the_sweep(tmp_path):
    """`-path`'s `*` matches `/`, so `*/claude-[0-9]*/*/*` is satisfied by a
    `claude-<digit>` component in the ROOT'S OWN path — after which the filter
    stops discriminating and every depth-3 dir under the root is eligible.

    MEASURED with the unanchored form and a root of `.../claude-1000/fakeroot`:
    `pip-unpack-xyz/wheels/numpy` and `tsx-cache/v1/build` both matched and
    would have been reaped. Reachable on any install whose $HOME contains such
    a component, and this ships to every clone. Same bug class as the
    `claude-*` defect above — fixed at the instance, left open one level up.
    """
    home, _root, bind = _sandbox(tmp_path)
    # a root that itself sits under a claude-<digit> component
    root = home / ".genesis" / "claude-1000" / "fakeroot"
    for rel in ("pip-unpack-xyz/wheels/numpy", "tsx-cache/v1/build"):
        d = root / rel
        d.mkdir(parents=True)
        (d / "f").write_text("cache")
        for p in (d / "f", d, d.parent, d.parent.parent):
            _age(p, 30)

    r = _run(home, bind, f'reap_stale_session_dirs "{root}" 7')
    assert r.returncode == 0, r.stderr
    for rel in ("pip-unpack-xyz/wheels/numpy", "tsx-cache/v1/build"):
        assert (root / rel / "f").exists(), (
            f"{rel} was reaped — the glob is satisfied by the ROOT's own path, "
            "so the session filter is not discriminating at all"
        )
