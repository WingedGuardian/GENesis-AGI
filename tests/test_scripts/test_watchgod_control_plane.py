"""tmp_watchgod's severed-control-plane detector.

WHAT IT DETECTS. Claude Code binds one unix socket per session for cross-session
messaging. Deleting the socket's PATH does not stop the listener: the process
keeps the inode, so `ss` still reports LISTEN and the session looks healthy from
the inside, while every peer resolves BY PATH and gets ENOENT. Nothing re-binds
after startup, so the session is unreachable for the rest of its life and cannot
notice. Measured 2026-09-05, when a cleanup sweep deleted those paths: 3 of 4
sessions severed on one install and 6 of 6 on a sibling — and the only signal
anyone had was peers mysteriously failing to answer.

The sweeps now spare sockets, which stops recurrence but cannot heal a session
already severed. This detector makes the state visible so a new severance is
never silent again.

WHAT THESE TESTS PIN, in order of how they fail:

  * severed  — a listener whose path is gone, or has been replaced by something
    that is not a socket. This is the incident.
  * stale    — a socket file with no listener. Reported and NEVER deleted:
    deleting sockets from this daemon is what caused the outage.
  * unknown  — ss missing or failing. A monitor whose probe did not run must not
    report a clean plane; "could not measure" and "measured, and it is fine"
    never share a value.
  * empty    — the probe ran and saw nothing. Usually true, and also what a
    detector that has gone blind looks like, so it is not reported as health.
  * the paging rules — confirmation before paging, and a high-water mark that
    follows the count back down.

WHAT THESE ARE NOT: regression cover. `check_control_plane` and
`control_plane_page_decision` are new, so running these against `origin/main` only
proves a function name is absent. They are CONTRACT tests for a new detector, and
the two `write_state` cases are the only ones that exercise a pre-existing
function. The one lock worth naming is
`test_stale_socket_file_is_counted_and_kept`, which fails the moment anyone adds
a delete to a function whose whole contract is that it never deletes.

`ss` is a PATH-prepended STUB emitting whatever rows a test wants, so no test
depends on the machine's real sockets. Socket inodes come from ``os.mknod``,
which has no AF_UNIX ``sun_path`` length limit — a real ``bind()`` under pytest's
tmp_path would exceed 108 bytes and fail (the same trap the sibling
socket-sparing suite documents).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

_WATCHGOD = Path(__file__).resolve().parents[2] / "scripts" / "tmp_watchgod.sh"

# Reproduces `ss -xlpH state listening` output shape: netid, queues, path, inode,
# peer, peer-port, then the users:(...) column. STUB_SS_ROWS holds one
# "<path> <pid>" pair per line; STUB_SS_RC forces a failure exit.
_SS_STUB = r"""#!/usr/bin/env bash
rc="${STUB_SS_RC:-0}"
if (( rc != 0 )); then exit "$rc"; fi
while IFS=' ' read -r p pid; do
  [[ -z "$p" ]] && continue
  printf 'u_str 0      512        %s 12345 * 0 users:(("claude",pid=%s,fd=11))\n' "$p" "$pid"
done <<< "${STUB_SS_ROWS:-}"
exit 0
"""

_TMUX_STUB = "#!/usr/bin/env bash\nexit 0\n"


def _make_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _sandbox(tmp_path: Path) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    (home / ".genesis" / "logs").mkdir(parents=True)
    (home / ".genesis" / "alerts").mkdir(parents=True)
    cctmp = home / ".genesis" / "cc-tmp"
    cctmp.mkdir(parents=True)
    bind = tmp_path / "bin"
    bind.mkdir()
    _make_exec(bind / "ss", _SS_STUB)
    _make_exec(bind / "tmux", _TMUX_STUB)
    return home, cctmp, bind


def _run(
    home: Path, bind: Path, snippet: str, rows: str = "", ss_rc: int = 0, ss_bin: str = ""
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(
        HOME=str(home),
        PATH=f"{bind}:{os.environ['PATH']}",
        STUB_SS_ROWS=rows,
        STUB_SS_RC=str(ss_rc),
    )
    if ss_bin:
        env["SS_BIN"] = ss_bin
    return subprocess.run(
        ["bash", "-c", f"source '{_WATCHGOD}'\n{snippet}"],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def _mksock(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mknod(path, stat.S_IFSOCK | 0o600)


def _probe(home: Path, bind: Path, rows: str = "", **kw) -> str:
    """The COUNTS tuple — `status:severed:stale:listeners`.

    The check also emits a fifth field carrying the severed socket identities;
    `_probe_ids` is for that. Splitting them keeps the counts assertions readable
    and stops every one of them having to restate an id list it does not care
    about.
    """
    return ":".join(_probe_full(home, bind, rows, **kw).split(":")[:4])


def _probe_full(home: Path, bind: Path, rows: str = "", **kw) -> str:
    proc = _run(home, bind, "check_control_plane", rows=rows, **kw)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    return proc.stdout.strip()


def _probe_ids(home: Path, bind: Path, rows: str = "", **kw) -> list[str]:
    """The severed socket identities, as a list."""
    parts = _probe_full(home, bind, rows, **kw).split(":")
    return parts[4].split() if len(parts) > 4 else []


# ── severed ──────────────────────────────────────────────────────────────


def test_severed_listener_is_counted(tmp_path):
    """The incident: a live listener whose socket path has been deleted."""
    home, cctmp, bind = _sandbox(tmp_path)
    gone = cctmp / "cc-socks" / "111.sock"
    gone.parent.mkdir(parents=True)  # dir survives, file does not
    assert _probe(home, bind, f"{gone} 111") == "ok:1:0:1"


def test_healthy_listener_is_not_severed(tmp_path):
    """A listener whose path is on disk is exactly what health looks like."""
    home, cctmp, bind = _sandbox(tmp_path)
    live = cctmp / "cc-socks" / "222.sock"
    _mksock(live)
    assert _probe(home, bind, f"{live} 222") == "ok:0:0:1"


def test_non_socket_at_the_path_counts_as_severed(tmp_path):
    """A path occupied by an ordinary file is no more reachable than a missing
    one — `-e` would call this healthy, which is why the check is `-S`."""
    home, cctmp, bind = _sandbox(tmp_path)
    impostor = cctmp / "cc-socks" / "333.sock"
    impostor.parent.mkdir(parents=True)
    impostor.write_text("not a socket")
    assert _probe(home, bind, f"{impostor} 333") == "ok:1:0:1"


def test_incident_replay_mixed_population(tmp_path):
    """The measured live shape at build time: several severed listeners, one
    healthy survivor, and one socket file left by a dead process — reported as
    `ok:4:1:5`, which is what this install's real `ss` produced."""
    home, cctmp, bind = _sandbox(tmp_path)
    socks = cctmp / "cc-socks"
    socks.mkdir(parents=True)
    survivor = socks / "2501887.sock"
    _mksock(survivor)
    _mksock(socks / "3794276.sock")  # dead pid's leftover — stale, not severed
    rows = "\n".join(
        [
            f"{socks}/3689517.sock 3689517",
            f"{socks}/2864896.sock 2864896",
            f"{socks}/3265893.sock 3265893",
            f"{socks}/1859653.sock 1859653",
            f"{survivor} 2501887",
        ]
    )
    assert _probe(home, bind, rows) == "ok:4:1:5"


# ── stale ────────────────────────────────────────────────────────────────


def test_stale_socket_file_is_counted_and_kept(tmp_path):
    """A socket with no listener is counted — and must still exist afterwards.
    This detector is read-only by contract."""
    home, cctmp, bind = _sandbox(tmp_path)
    orphan = cctmp / "cc-socks" / "444.sock"
    _mksock(orphan)
    assert _probe(home, bind, "") == "ok:0:1:0"
    assert orphan.exists(), "the detector deleted a socket; it must never delete"


def test_stale_is_found_with_no_listeners_at_all(tmp_path):
    """A fully-severed install has no listener path on disk to derive a directory
    from, which is exactly when the leftovers still need counting — so cc-tmp's
    own socket dir is always scanned."""
    home, cctmp, bind = _sandbox(tmp_path)
    _mksock(cctmp / "cc-socks" / "555.sock")
    gone = cctmp / "cc-socks" / "666.sock"
    assert _probe(home, bind, f"{gone} 666") == "ok:1:1:1"


# ── path-agnostic ────────────────────────────────────────────────────────


def test_listener_outside_cc_tmp_is_covered(tmp_path):
    """Directories come from the live listeners, not from a hardcoded location,
    so this keeps working unchanged when the sockets move to the runtime dir."""
    home, cctmp, bind = _sandbox(tmp_path)
    runtime = tmp_path / "run" / "user" / "1000" / "cc-socks"
    runtime.mkdir(parents=True)
    healthy = runtime / "777.sock"
    _mksock(healthy)
    _mksock(runtime / "888.sock")  # stale, in the non-cc-tmp directory
    assert _probe(home, bind, f"{healthy} 777") == "ok:0:1:1"


# ── unknown ──────────────────────────────────────────────────────────────


def test_ss_failure_reports_unknown_not_healthy(tmp_path):
    """A probe that could not run must never look like a clean plane."""
    home, _cctmp, bind = _sandbox(tmp_path)
    assert _probe(home, bind, "", ss_rc=1) == "unknown:0:0:0"


def test_ss_absent_reports_unknown(tmp_path):
    """Same when the tool is not installed at all — the box simply cannot answer.
    Pointed at a name that does not exist rather than emptying PATH, which would
    take `bash` and the rest of the script's own tools with it."""
    home, _cctmp, bind = _sandbox(tmp_path)
    _mksock(_cctmp / "cc-socks" / "999.sock")  # present, and still not counted
    assert _probe(home, bind, "", ss_bin="watchgod-no-such-tool") == "unknown:0:0:0"


# ── empty ────────────────────────────────────────────────────────────────


def test_nothing_visible_is_empty_not_ok(tmp_path):
    """The residual hole in a two-value design, closed with a third value.

    A probe that ran and saw nothing is usually the truth (no CC session is up),
    but it is ALSO exactly what a detector that has gone blind looks like — an
    `ss` output change, a netns move, sockets relocating out of `cc-socks`. Both
    of those would otherwise report as `ok`, which is the one thing they are not.
    """
    home, _cctmp, bind = _sandbox(tmp_path)
    assert _probe(home, bind, "") == "empty:0:0:0"


def test_a_lone_stale_socket_is_still_ok_not_empty(tmp_path):
    """`empty` means the probe saw NOTHING. A leftover socket file is something,
    so the plane is visible and the status is `ok` with a stale count."""
    home, cctmp, bind = _sandbox(tmp_path)
    _mksock(cctmp / "cc-socks" / "111.sock")
    assert _probe(home, bind, "") == "ok:0:1:0"


# ── state file ───────────────────────────────────────────────────────────


def test_state_file_carries_the_control_plane(tmp_path):
    """The state JSON is the only channel a reader has — the counts must land in
    it, and it must stay parseable."""
    home, cctmp, bind = _sandbox(tmp_path)
    proc = _run(home, bind, 'write_state green 10 green 5 "ok:2:1:6"')
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    state = json.loads((home / ".genesis" / "watchgod_state.json").read_text())
    assert state["control_plane"] == {
        "status": "ok",
        "severed_sockets": 2,
        "stale_sockets": 1,
        "listeners": 6,
    }


def test_state_file_without_a_control_plane_argument_stays_valid(tmp_path):
    """The argument is defaulted, so a caller that predates the field still
    writes valid JSON — and it says `unknown`, never a fabricated zero."""
    home, cctmp, bind = _sandbox(tmp_path)
    proc = _run(home, bind, "write_state green 10 green 5")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    state = json.loads((home / ".genesis" / "watchgod_state.json").read_text())
    assert state["control_plane"]["status"] == "unknown"
    assert state["cc_tmp"]["tier"] == "green"


# ── paging rules ─────────────────────────────────────────────────────────


def _decide(home: Path, bind: Path, prev: str, paged: str, cur: str) -> str:
    proc = _run(home, bind, f"control_plane_page_decision '{prev}' '{paged}' '{cur}'")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    return proc.stdout.strip()


def test_severed_identities_are_reported_not_just_counted(tmp_path):
    """The decision is made on WHICH sockets are severed, so the check has to say
    which — a bare tally cannot support the rules below."""
    home, cctmp, bind = _sandbox(tmp_path)
    socks = cctmp / "cc-socks"
    socks.mkdir(parents=True)
    _mksock(socks / "77.sock")
    rows = f"{socks}/11.sock 11\n{socks}/77.sock 77"
    assert _probe_ids(home, bind, rows) == ["11.sock"]
    assert _probe(home, bind, rows) == "ok:1:0:2"


def test_first_sighting_does_not_page(tmp_path):
    """Confirmation: an id must be seen on two consecutive polls. A session
    exiting between ss's snapshot and its own unlink shows up as severed for a
    single poll, and a monitor that cries wolf gets ignored."""
    home, _cctmp, bind = _sandbox(tmp_path)
    assert _decide(home, bind, "", "", "a.sock b.sock") == "0:"


def test_confirmed_severance_pages_once(tmp_path):
    """Seen twice running — page, then record it so the next poll stays quiet."""
    home, _cctmp, bind = _sandbox(tmp_path)
    assert _decide(home, bind, "a.sock b.sock", "", "a.sock b.sock") == "1:a.sock b.sock"
    assert (
        _decide(home, bind, "a.sock b.sock", "a.sock b.sock", "a.sock b.sock") == "0:a.sock b.sock"
    ), "a steady severance must not re-page"


def test_a_new_severance_pages_even_when_the_COUNT_falls(tmp_path):
    """The defect identities exist to fix (Codex P2 on #1856).

    A previously-paged population is replaced between polls: four severed
    sessions exit and one newly severed session appears. The count goes 4 -> 1,
    so a high-water COUNT that follows the number down lands at 1, and the next
    poll's `severed > paged` is false — the new unreachable session NEVER pages,
    silently losing the one alert this detector exists to send. An id that has
    not been reported is news whatever the tally did.
    """
    home, _cctmp, bind = _sandbox(tmp_path)
    paged_four = "a.sock b.sock c.sock d.sock"
    assert _decide(home, bind, "z.sock", paged_four, "z.sock") == "1:z.sock"


def test_a_severance_that_recovers_is_forgotten(tmp_path):
    """An id that is no longer severed drops out of the reported set, so if that
    pid is ever severed again it is news again rather than permanently silenced."""
    home, _cctmp, bind = _sandbox(tmp_path)
    assert _decide(home, bind, "", "a.sock", "") == "0:", "recovery itself is not a page"
    assert _decide(home, bind, "a.sock", "", "a.sock") == "1:a.sock"


def test_a_healthy_plane_says_nothing(tmp_path):
    home, _cctmp, bind = _sandbox(tmp_path)
    assert _decide(home, bind, "", "", "") == "0:"
    assert _decide(home, bind, "a.sock", "a.sock", "") == "0:"
