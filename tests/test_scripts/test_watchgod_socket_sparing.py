"""tmp_watchgod socket-sparing — the RED reaper must never delete unix sockets.

Origin (measured on a live install, 2026-09-05): Zone A RED's depth-1 sweep
(`clean_cc_red`) `rm -rf`s every top-level directory of CC_TMP_DIR except the
one holding the newest `*/claude-*` session dir. `cc-socks/` — where the
Claude Code binary binds one unix socket per session for cross-session
messaging — is such a directory and was deleted wholesale, leaving live
sessions listening on bound-but-unlinked sockets: inbound connects fail
ENOENT and the local coordination plane fails silently. The sockets are
0 bytes, so deleting them reclaims nothing.

These tests pin the fix (object-level socket-sparing deletion at the RED
depth-1 sweep) and that the yellow/orange sweeps cannot touch a socket in
the cc-socks layout CC actually creates.

Harness idiom mirrors test_watchgod_loopbreak.py (deliberately duplicated —
repo precedent: the existing watchgod test files do not share a conftest):
HOME-redirected sandbox so the script's HOME-derived paths land in tmp_path,
overrides injected as a bash snippet after sourcing (the script assigns
CC_TMP_BUDGET_MB unconditionally, so env vars cannot set it), and a tmux
stub on a PATH-prepended bin dir so the RED tier's session-kill loop sees no
sessions.
"""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
import time
from pathlib import Path

_WATCHGOD = Path(__file__).resolve().parents[2] / "scripts" / "tmp_watchgod.sh"

# No-op-ish tmux stub: list-sessions prints nothing (no killable sessions),
# every other verb exits 0 — the RED kill loop is out of scope here.
_TMUX_STUB = """#!/usr/bin/env bash
exit 0
"""

# Tier functions are called directly, so the budget only feeds orange's
# post-cleanup re-measure; keep it tiny. Stub queue_alert so the
# RED path's emergency alert becomes a log line instead of a real queue write.
_PRELUDE = (
    'CC_TMP_BUDGET_MB=4; queue_alert() { echo "ALERT $*" >> "$HOME/.genesis/alerts/calls.log"; }; '
)


def _make_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _sandbox(tmp_path: Path) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    (home / ".genesis" / "logs").mkdir(parents=True)
    (home / ".genesis" / "alerts").mkdir(parents=True)
    cctmp = home / ".genesis" / "cc-tmp"  # the script's default CC_TMP_DIR
    cctmp.mkdir(parents=True)
    bind = tmp_path / "bin"
    bind.mkdir()
    _make_exec(bind / "tmux", _TMUX_STUB)
    return home, cctmp, bind


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


def _mksock(path: Path) -> None:
    """A real socket-type inode via mknod — no AF_UNIX sun_path length limit,
    unprivileged on Linux, and exactly what `find -type s` matches."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mknod(path, stat.S_IFSOCK | 0o600)


def _log(home: Path) -> str:
    return (home / ".genesis" / "logs" / "tmp_watchgod.log").read_text()


def test_red_preserves_socket_deletes_sibling_junk(tmp_path):
    """The incident replay: RED must delete reclaimable depth-1 dirs but never
    the control-plane socket. Pre-fix, rm -rf takes cc-socks with the rest."""
    home, cctmp, bind = _sandbox(tmp_path)
    sock = cctmp / "cc-socks" / "2501887.sock"
    _mksock(sock)
    (cctmp / "pyright-x").mkdir()
    (cctmp / "pyright-x" / "blob").write_bytes(b"x" * 4096)
    (cctmp / "gh-cli-cache").mkdir()
    (cctmp / "gh-cli-cache" / "f").write_bytes(b"y" * 4096)
    # a session dir, so newest_session resolves like production
    (cctmp / "claude-1000" / "some-session-uuid").mkdir(parents=True)

    proc = _run(home, bind, _PRELUDE + "clean_cc_red")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    assert sock.exists() and stat.S_ISSOCK(sock.stat().st_mode), (
        "RED deleted the control-plane socket"
    )
    assert not (cctmp / "pyright-x").exists(), "junk dir should be reclaimed"
    assert not (cctmp / "gh-cli-cache").exists(), "junk dir should be reclaimed"
    assert "preserved 1 unix socket" in _log(home)


def test_red_preserves_socket_with_no_session_dir(tmp_path):
    """The measured worst case: with NO claude-* session dir anywhere,
    newest_session is empty and the pre-fix guard spares NOTHING — every
    depth-1 dir including cc-socks is rm -rf'd. The socket must survive."""
    home, cctmp, bind = _sandbox(tmp_path)
    sock = cctmp / "cc-socks" / "999.sock"
    _mksock(sock)
    (cctmp / "junkdir").mkdir()
    (cctmp / "junkdir" / "f").write_bytes(b"z" * 4096)

    proc = _run(home, bind, _PRELUDE + "clean_cc_red")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    assert sock.exists() and stat.S_ISSOCK(sock.stat().st_mode), (
        "RED deleted the socket in the empty-newest_session case"
    )
    assert (cctmp / "cc-socks").is_dir()


def test_red_reclaims_files_inside_socket_dir(tmp_path):
    """Object-level, not skip-wholesale: regular files sharing a directory
    with a socket ARE reclaimed; the socket and its parent dir survive."""
    home, cctmp, bind = _sandbox(tmp_path)
    sock = cctmp / "cc-socks" / "1234.sock"
    _mksock(sock)
    junk = cctmp / "cc-socks" / "junk.log"
    junk.write_bytes(b"j" * 4096)
    (cctmp / "claude-1000" / "some-session-uuid").mkdir(parents=True)

    proc = _run(home, bind, _PRELUDE + "clean_cc_red")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    assert not junk.exists(), "regular file beside the socket should be reclaimed"
    assert sock.exists() and stat.S_ISSOCK(sock.stat().st_mode)
    assert (cctmp / "cc-socks").is_dir()


def test_orange_never_touches_old_socket_pin(tmp_path):
    """Pin: the yellow/orange sweeps are dir-name- and -type f-scoped and must
    never delete a socket, however old. Guards future sweep edits."""
    home, cctmp, bind = _sandbox(tmp_path)
    sock = cctmp / "cc-socks" / "999.sock"
    _mksock(sock)
    eight_days_ago = time.time() - 8 * 86400
    os.utime(sock, (eight_days_ago, eight_days_ago))
    # give orange a cache to evict so the tier does real work
    (cctmp / "claude-skills").mkdir()
    (cctmp / "claude-skills" / "blob").write_bytes(b"c" * 4096)

    proc = _run(home, bind, _PRELUDE + "clean_cc_orange")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    assert sock.exists() and stat.S_ISSOCK(sock.stat().st_mode), (
        "yellow/orange sweep deleted a socket"
    )
    assert not (cctmp / "claude-skills").exists(), "orange should evict the cache"


# ── Control-plane TREES, not just the socket inodes ──────────────────────
#
# `-not -type s` protects a socket FILE. It cannot protect the DIRECTORY that
# holds it, because a directory is not a socket — a sockets dir survives the
# sweep only while something inside keeps it non-empty, i.e. by accident.
# REPRODUCED against the unfixed predicate at both sites: an empty sockets
# directory and an empty daemon root were deleted, and only a socket-holding
# sibling survived.
#
# The exclusions are EXACT PATHS, not name globs. Two separate reasons, both
# load-bearing and both pinned below:
#   * a `-path '*/cc-socks*/*'` glob makes an unbounded subtree immortal
#     (-path's `*` matches `/`), so any cc-socks-prefixed directory would pin
#     everything beneath it against the watchdog forever;
#   * `cc-daemon-*` is ALSO a CC mkdtemp prefix — cc-daemon-<random> holding a
#     single stderr.log — so a name glob would empty each husk and then spare
#     it permanently, making the empty-directory reaper leak inodes.


def _uid() -> int:
    return os.getuid()


def _control_plane_tree(root: Path) -> None:
    """The Zone A shapes the guard must keep. Only `cc-socks` is a real CC path
    under the temp dir; the /tmp-only names are exercised by the Zone B arms."""
    (root / "cc-socks").mkdir()  # bare: the empty-sockets-dir defect itself
    (root / "cc-socks" / "nested").mkdir()  # subtree sparing
    _mksock(root / "cc-socks-live" / "a.sock")  # the accidental-protection case


def test_red_spares_an_empty_control_plane_tree(tmp_path):
    """THE ACCEPTANCE BAR. The sockets directory survives RED with no socket
    inside to keep it non-empty, and so does a directory beneath it."""
    home, cctmp, bind = _sandbox(tmp_path)
    _control_plane_tree(cctmp)
    (cctmp / "claude-1000" / "some-session-uuid").mkdir(parents=True)

    proc = _run(home, bind, _PRELUDE + "clean_cc_red")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    assert (cctmp / "cc-socks").is_dir(), "RED deleted the empty sockets directory"
    assert (cctmp / "cc-socks" / "nested").is_dir(), "RED deleted a dir inside the tree"
    assert (cctmp / "cc-socks-live" / "a.sock").exists()


def test_red_still_reaps_lookalikes_and_unrelated_dirs(tmp_path):
    """The negative control, and it is what makes the guard EXACT rather than a
    prefix match. Without it a guard that spared everything — or one that used
    a `cc-socks*` glob — would pass every arm above.

    `cc-socks-backup` is the concrete cost of the glob form: under it, that
    directory and its whole subtree become permanently unreapable."""
    home, cctmp, bind = _sandbox(tmp_path)
    _control_plane_tree(cctmp)
    (cctmp / "unrelated-empty").mkdir()
    (cctmp / "cc-socks-backup" / "junk").mkdir(parents=True)
    (cctmp / "cc-socksomething").mkdir()
    # CC's own mkdtemp husk: cc-daemon-<random> holding one stderr.log. Under a
    # `cc-daemon-*` name glob this is emptied and then spared forever.
    (cctmp / "cc-daemon-Ab3xY9").mkdir()
    (cctmp / "cc-daemon-Ab3xY9" / "stderr.log").write_bytes(b"e" * 512)
    (cctmp / "claude-1000" / "some-session-uuid").mkdir(parents=True)

    proc = _run(home, bind, _PRELUDE + "clean_cc_red")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    for gone in ("unrelated-empty", "cc-socks-backup", "cc-socksomething", "cc-daemon-Ab3xY9"):
        assert not (cctmp / gone).exists(), (
            f"{gone} survived — the guard is a prefix match, not an exact path"
        )
    assert (cctmp / "cc-socks").is_dir(), "…and the acceptance bar was not vacuous"


def test_red_spares_the_directory_but_not_a_file_inside_it(tmp_path):
    """Sparing is DIRECTORY-scoped on purpose. A regular file inside the tree is
    still reclaimed — the object-level contract
    `test_red_reclaims_files_inside_socket_dir` pins with a socket present; this
    arm pins it with no socket, where only the new guard keeps the parent."""
    home, cctmp, bind = _sandbox(tmp_path)
    _control_plane_tree(cctmp)
    stale = cctmp / "cc-socks" / "stale.log"
    stale.write_bytes(b"j" * 4096)
    (cctmp / "claude-1000" / "some-session-uuid").mkdir(parents=True)

    proc = _run(home, bind, _PRELUDE + "clean_cc_red")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    assert not stale.exists(), "a regular file inside the tree should be reclaimed"
    assert (cctmp / "cc-socks").is_dir(), "…but its directory must survive"


# ── The resolver itself, and the trap inside it ──────────────────────────


def _resolve_paths(home: Path, bind: Path, cctmp: Path, extra_env=None) -> list[str]:
    env_prefix = ""
    if extra_env:
        env_prefix = "".join(f"export {k}={v}; " for k, v in extra_env.items())
    proc = _run(home, bind, _PRELUDE + env_prefix + "cc_control_plane_paths")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def test_control_plane_paths_cover_every_location_cc_can_bind(tmp_path):
    """All four concrete paths, enumerated rather than computed from one
    expression. CC picks between them from ITS environment, which is not this
    daemon's."""
    home, cctmp, bind = _sandbox(tmp_path)
    paths = _resolve_paths(home, bind, cctmp)
    assert f"{cctmp}/cc-socks" in paths, paths
    assert "/tmp/cc-socks" in paths, paths
    assert f"/tmp/cc-socks-{_uid()}" in paths, paths
    assert f"/tmp/cc-daemon-{_uid()}" in paths, paths


def test_XDG_RUNTIME_DIR_does_not_displace_the_in_budget_sockets_dir(tmp_path):
    """THE REGRESSION THIS ARM EXISTS FOR, and it is the reason the resolver is
    a list rather than a `${XDG_RUNTIME_DIR:-$CC_TMP_DIR}/cc-socks` expression.

    MEASURED on a live install: this daemon runs as a systemd user unit WITH
    XDG_RUNTIME_DIR=/run/user/<uid>, while a CC session under tmux has it unset
    and falls back to its TMPDIR — and both `<cc-tmp>/cc-socks` and
    `/run/user/<uid>/cc-socks` existed at the same time. A default-expansion
    evaluated in THIS process resolves to the runtime dir and silently stops
    sparing the cc-tmp one, which is the only one inside the budget we sweep.
    The simplification is tempting and this arm is what refuses it."""
    home, cctmp, bind = _sandbox(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    paths = _resolve_paths(home, bind, cctmp, {"XDG_RUNTIME_DIR": str(runtime)})
    assert f"{cctmp}/cc-socks" in paths, (
        "XDG_RUNTIME_DIR displaced the in-budget sockets directory:\n" + "\n".join(paths)
    )


# ── Zone B: the same defect, reached through the /tmp sweeps ─────────────
#
# `clean_sys_yellow` hardcodes `/tmp`, so a behavioural arm would have to let
# the function loose on live machine state. The repo's precedent for that case
# is a structural arm. These go further: the exclusion argv is taken from the
# script's OWN resolver at runtime and replayed against a sandbox, so a change
# to the resolver changes what these arms do rather than leaving a hand-copied
# duplicate behind.


def _sweep_argv(home: Path, bind: Path) -> list[str]:
    """`clean_sys_yellow`'s empty-directory sweep, with the script's own
    control-plane exclusions expanded by the script itself."""
    body = _WATCHGOD.read_text()
    start = body.index("clean_sys_yellow() {")
    fn = body[start : body.index("\n}\n", start)]
    joined = fn.replace("\\\n", " ")
    line = next(
        (
            s
            for s in (ln.strip() for ln in joined.splitlines())
            if s.startswith("find /tmp") and "-type d" in s and "-empty" in s
        ),
        None,
    )
    assert line is not None, f"no empty-directory sweep in clean_sys_yellow:\n{fn}"
    assert "_cp_excl" in line, "the sweep no longer carries the control-plane exclusions"
    # Let the SCRIPT expand its own exclusion array, rather than re-deriving it.
    proc = _run(
        home,
        bind,
        _PRELUDE + 'declare -a _e=(); cc_control_plane_excl _e; printf "%s\\n" "${_e[@]}"',
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    excl = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert excl, "the resolver produced no exclusions"
    static = shlex.split(line.split("${_cp_excl")[0])
    # Drop the shell tail if shlex swept one up; `find` would treat a
    # redirection as a path operand, delete nothing, and make every assertion
    # below a tautology.
    return static + excl + ["-delete"]


def test_zone_b_sweep_spares_the_control_plane_and_reaps_the_rest(tmp_path):
    """/tmp is where CC's long-path sockets fallback lives, and where its
    background daemon ALWAYS lives."""
    root = tmp_path / "faketmp"
    root.mkdir()
    for name in ("cc-socks", f"cc-socks-{_uid()}", f"cc-daemon-{_uid()}"):
        (root / name).mkdir()
    (root / f"cc-daemon-{_uid()}" / "deadbeef").mkdir()
    (root / "unrelated-empty").mkdir()
    (root / "cc-daemon-Ab3xY9").mkdir()  # CC's mkdtemp husk — must still go

    home, cctmp, bind = _sandbox(tmp_path)
    argv = _sweep_argv(home, bind)
    assert argv[1] == "/tmp", f"the sweep root is no longer /tmp: {argv[1]}"
    # Rewrite BOTH the root and the absolute exclusion paths onto the sandbox.
    # A relative root is used because `-path`'s `*` matches `/`, and pytest's
    # basetemp is `.../pytest-of-<user>/...`, so `-not -path "*/pytest-*"` would
    # otherwise be satisfied by the root's own path and exclude everything —
    # deleting nothing and making every assertion here vacuous. (Production is
    # unaffected: its root is literally /tmp.)
    # The rewrite must produce RELATIVE exclusion paths, because `-path`
    # compares against the path find builds from its starting point — and the
    # starting point here is relative. An absolute exclusion silently matches
    # nothing, and the sweep then deletes the very directories this arm is
    # asserting it spares. (Caught by that assertion, which is the point of
    # keeping the spare-list and the reap-list in one arm.)
    argv = [root.name if a == "/tmp" else a.replace("/tmp/", f"{root.name}/", 1) for a in argv]
    subprocess.run(argv, capture_output=True, cwd=root.parent)

    for kept in (
        "cc-socks",
        f"cc-socks-{_uid()}",
        f"cc-daemon-{_uid()}",
        f"cc-daemon-{_uid()}/deadbeef",
    ):
        assert (root / kept).is_dir(), f"the Zone B sweep deleted {kept}"
    for gone in ("unrelated-empty", "cc-daemon-Ab3xY9"):
        assert not (root / gone).exists(), (
            f"{gone} survived — the sweep deleted nothing, or the guard is a prefix match"
        )


def test_zone_b_sweep_still_calls_the_shared_resolver(tmp_path):
    """Structural companion. The replay arm would keep passing if the
    exclusions were dropped AND the sweep stopped deleting anything; this one
    names the call, so the two fail for different reasons."""
    body = _WATCHGOD.read_text()
    start = body.index("clean_sys_yellow() {")
    fn = body[start : body.index("\n}\n", start)]
    assert "cc_control_plane_excl" in fn, (
        "clean_sys_yellow no longer builds the control-plane exclusions"
    )
    # Strip comments first: this function's rationale block NAMES the glob
    # forms it rejects, and a naive substring scan reads its own explanation as
    # a regression.
    code = "\n".join(ln for ln in fn.splitlines() if not ln.lstrip().startswith("#"))
    assert "cc-socks*" not in code and "cc-daemon-*" not in code, (
        "a name GLOB is back in the Zone B sweep; it makes an unbounded subtree "
        "immortal and spares CC's mkdtemp husks forever"
    )
