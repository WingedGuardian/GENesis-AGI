"""The Guardian's drop (#4911): `drop <branch> --expect-branch live --expect-head
<tip> --expect-candidate <pinned> [--restart-server]`, run with the entry's
`--wait 0`. One update.lock hold covers the refusals, the drop, its confirmation
from the engine's own view and the restart enqueue, so nothing can change
between them.

`systemctl` is a stub first on PATH: it records its argv, the fds it was born
with, the checkout's HEAD and whether update.lock was free at that moment. It
never reaches the real user manager. The scratch world is
tests/test_scripts/_deploy_candidates_world.py.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from tests.test_scripts._deploy_candidates_world import World

_STUB = r"""#!/bin/bash
d='@D@'
{
    printf 'argv:'; printf ' %s' "$@"; printf '\n'
    for f in /proc/$$/fd/*; do printf 'fd %s\n' "$(readlink "$f")"; done
    printf 'head %s\n' "$(git -C '@ROOT@' rev-parse HEAD)"
    flock -n -E 75 '@LOCK@' true; printf 'lock %s\n' "$?"
} >> "$d/log"
case " $* " in
    *" show "*)
        [ -f "$d/show_fails" ] && { echo "Failed to connect to bus: No medium found" >&2; exit 1; }
        echo "ActiveState=$(cat "$d/state" 2>/dev/null || echo active)" ;;
    *" restart "*)
        [ -f "$d/sleep" ] && { touch "$d/started"; sleep "$(cat "$d/sleep")"; }
        rc="$(cat "$d/restart_rc" 2>/dev/null || echo 0)"
        [ "$rc" = 0 ] || echo "Failed to restart genesis-server.service: boom" >&2
        exit "$rc" ;;
esac
"""

SHOW = "--user show -p ActiveState genesis-server.service"
RESTART = "--user restart --no-block genesis-server.service"


def _lock_path(w: World) -> Path:
    return w.home / ".genesis" / "locks" / "update.lock"


@pytest.fixture
def stub(dc_ready) -> Path:
    w = dc_ready
    d = w.tmp / "stub"
    d.mkdir()
    _lock_path(w).parent.mkdir(parents=True, exist_ok=True)
    text = _STUB.replace("@D@", str(d)).replace("@ROOT@", str(w.root))
    (d / "systemctl").write_text(text.replace("@LOCK@", str(_lock_path(w))))
    (d / "systemctl").chmod(0o755)
    w.env["PATH"] = f"{d}:{w.env.get('PATH', '/usr/bin:/bin')}"
    assert shutil.which("systemctl", path=w.env["PATH"]) == str(d / "systemctl")
    return d


def _records(d: Path) -> list[dict]:
    """One record per systemctl call: argv, fd targets, HEAD, lock rc."""
    recs: list[dict] = []
    log = d / "log"
    for line in log.read_text().splitlines() if log.exists() else []:
        key, _, val = line.partition(" ")
        if key == "argv:":
            recs.append({"argv": val, "fds": []})
        elif key == "fd":
            recs[-1]["fds"].append(val)
        else:
            recs[-1][key] = val
    return recs


def _calls(d: Path) -> list[str]:
    return [r["argv"] for r in _records(d)]


def _two_live(w: World, dc) -> None:
    w.candidate("feat/a", {"x.txt": "x\n"})
    w.candidate("feat/b", {"y.txt": "y\n"})
    w.write_manifest([w.entry("feat/a"), w.entry("feat/b")])
    assert w.run(dc, "rebuild") == 0


def _guarded(w: World, pinned: str | None = None, *extra: str) -> list[str]:
    return [
        "drop",
        "feat/b",
        "--expect-branch",
        "live",
        "--expect-head",
        w.rev("refs/heads/live"),
        "--expect-candidate",
        pinned or w.rev("refs/heads/feat/b"),
        *extra,
    ]


def _shell(w: World, d: Path, *argv: str) -> subprocess.CompletedProcess:
    return w.plain_shell(*argv, extra_env=[f"PATH={d}:/usr/bin:/bin"])


def _snapshot(w: World) -> tuple:
    return (
        w.manifest_path.read_text(),
        w.rev("refs/heads/live"),
        w.git(w.root, "symbolic-ref", "HEAD").stdout,
        w.git(w.root, "status", "--porcelain", "--untracked-files=no").stdout,
    )


def _dropped(w: World) -> bool:
    return (
        [c["branch"] for c in w.manifest()["candidates"]] == ["feat/a"]
        and [b for b, _ in w.live_merges()] == ["feat/a"]
        and w.git(w.root, "symbolic-ref", "HEAD").stdout.strip() == "refs/heads/live"
    )


# ── exit 0 ─────────────────────────────────────────────────────────────────


def test_the_restart_is_queued_after_the_confirmation_under_the_lock_with_no_lock_fd(
    dc, dc_ready, stub
):
    """Through the real entry: the restart is queued with the drop already on
    disk, while update.lock is still held (the stub's own flock -n reads 75),
    from a child holding no fd on update.lock; the lock is free once it exits."""
    w = dc_ready
    _two_live(w, dc)
    w.install_engine()
    res = _shell(w, stub, *_guarded(w, None, "--restart-server"), "--wait", "0")
    assert res.returncode == 0, res.stdout + res.stderr
    assert _dropped(w)
    show, restart = _records(stub)
    assert (show["argv"], restart["argv"]) == (SHOW, RESTART)
    assert restart["head"] == w.rev("refs/heads/live")  # queued onto the confirmed tree
    assert restart["lock"] == "75"  # still under the one hold
    for rec in (show, restart):
        assert not any(t.endswith("/update.lock") for t in rec["fds"]), rec["fds"]
    assert "confirmed:" in res.stdout and "restart queued" in res.stdout
    fd = os.open(_lock_path(w), os.O_WRONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if anything kept it
    finally:
        os.close(fd)
    assert not (w.home / ".genesis" / "update_in_progress.pid").exists()


def test_the_fd_probe_sees_an_inherited_lock_fd(dc_ready, stub):
    """Control for the test above: the same stub, run from a shell holding the
    lock fd the way the entry does, does list it."""
    w = dc_ready
    subprocess.run(
        ["bash", "-c", f'exec 9>"{_lock_path(w)}"; "{stub}/systemctl" --user show x'],
        check=True,
        env=w.env,
    )
    [rec] = _records(stub)
    assert any(t.endswith("/update.lock") for t in rec["fds"]), rec["fds"]


def test_a_start_in_progress_queues_nothing(dc, dc_ready, stub, capsys):
    w = dc_ready
    _two_live(w, dc)
    (stub / "state").write_text("activating\n")
    capsys.readouterr()
    assert w.run(dc, *_guarded(w, None, "--restart-server")) == 0
    assert "a start is already in progress" in capsys.readouterr().out
    assert _calls(stub) == [SHOW]
    assert _dropped(w)


def test_without_restart_server_it_confirms_and_queues_nothing(dc, dc_ready, stub, capsys):
    w = dc_ready
    _two_live(w, dc)
    capsys.readouterr()
    assert w.run(dc, *_guarded(w)) == 0
    assert "confirmed:" in capsys.readouterr().out
    assert _calls(stub) == []
    assert _dropped(w)


@pytest.mark.parametrize("expect", [False, True])
def test_a_plain_drop_is_unchanged(dc, dc_ready, stub, capsys, expect):
    """Backward compatible: no candidate flag, no confirmation, no systemctl."""
    w = dc_ready
    _two_live(w, dc)
    argv = ["drop", "feat/b"]
    if expect:
        argv += ["--expect-branch", "live", "--expect-head", w.rev("refs/heads/live")]
    capsys.readouterr()
    assert w.run(dc, *argv) == 0
    out = capsys.readouterr().out
    assert "The server was not restarted" in out and "confirmed:" not in out
    assert _calls(stub) == []
    assert _dropped(w)


# ── exit 1: refused, nothing changed ───────────────────────────────────────


def test_a_re_pinned_candidate_is_refused_and_nothing_changes(dc, dc_ready, stub, capsys):
    """CodeRabbit c4624002511: a re-add between the approval and the drop must
    not be erased; the drop names the head it was approved for."""
    w = dc_ready
    _two_live(w, dc)
    approved = w.rev("refs/heads/feat/b")
    w.candidate("feat/b", {"y.txt": "y2\n"})
    assert w.add(dc, "feat/b") == 0
    before = _snapshot(w)
    capsys.readouterr()
    assert w.run(dc, *_guarded(w, approved, "--restart-server")) == 1
    err = capsys.readouterr().err
    assert f"pins feat/b at {w.rev('refs/heads/feat/b')[:12]}, not {approved[:12]}" in err, err
    assert "nothing changed" in err
    assert _snapshot(w) == before
    assert _calls(stub) == []


def test_the_pin_is_checked_again_as_the_manifest_is_written(
    dc, dc_ready, stub, capsys, monkeypatch
):
    """A re-pin landing after the first check (a writer that skips update.lock)
    is still refused at the write, before the checkout moves."""
    w = dc_ready
    _two_live(w, dc)
    other = w.rev("refs/heads/feat/a")
    real = dc.plan.move_checkout

    def repin_then_move(repo, p, source, save):
        data = w.manifest()
        data["candidates"][1]["verified_head"] = other
        w.write_manifest(data["candidates"])
        return real(repo, p, source, save)

    monkeypatch.setattr(dc.plan, "move_checkout", repin_then_move)
    live = w.rev("refs/heads/live")
    capsys.readouterr()
    assert w.run(dc, *_guarded(w, None, "--restart-server")) == 1
    err = capsys.readouterr().err
    assert "it changed since that was read" in err and "The manifest is unchanged" in err, err
    assert w.rev("refs/heads/live") == live
    assert [c["verified_head"] for c in w.manifest()["candidates"]][1] == other
    assert _calls(stub) == []


def test_a_manifest_gone_before_the_write_is_refused_and_nothing_moves(
    dc, dc_ready, stub, capsys, monkeypatch
):
    """CodeRabbit c4637258291: the write's read of a removed manifest gives
    None; the guarded drop refuses there, before the checkout moves, rather
    than report a drop it never saved."""
    w = dc_ready
    _two_live(w, dc)
    real = dc.plan.move_checkout

    def remove_then_move(repo, p, source, save):
        w.manifest_path.unlink()
        return real(repo, p, source, save)

    monkeypatch.setattr(dc.plan, "move_checkout", remove_then_move)
    live = w.rev("refs/heads/live")
    capsys.readouterr()
    assert w.run(dc, *_guarded(w, None, "--restart-server")) == 1
    out, err = capsys.readouterr()
    assert "the deploy manifest is gone" in err and "The manifest is unchanged" in err, err
    assert "dropped from" not in out, out
    assert w.rev("refs/heads/live") == live
    assert _calls(stub) == []


def test_another_candidate_changed_before_the_write_is_refused(
    dc, dc_ready, stub, capsys, monkeypatch
):
    """Round-2 premise check: an edit that keeps the dropped pin but changes
    another candidate would save a manifest `live` was not built from."""
    w = dc_ready
    _two_live(w, dc)
    real = dc.plan.move_checkout

    def edit_then_move(repo, p, source, save):
        w.write_manifest([c for c in w.manifest()["candidates"] if c["branch"] != "feat/a"])
        return real(repo, p, source, save)

    monkeypatch.setattr(dc.plan, "move_checkout", edit_then_move)
    live = w.rev("refs/heads/live")
    capsys.readouterr()
    assert w.run(dc, *_guarded(w, None, "--restart-server")) == 1
    err = capsys.readouterr().err
    assert "changed since the drop read it" in err and "The manifest is unchanged" in err, err
    assert [c["branch"] for c in w.manifest()["candidates"]] == ["feat/b"]
    assert w.rev("refs/heads/live") == live
    assert _calls(stub) == []


def test_the_header_names_a_restart_check_that_exists():
    """CodeRabbit c4637258286: the engine view records no restart; the header
    names the status command, which prints what the server runs."""
    root = Path(__file__).resolve().parents[2]
    header = (root / "scripts/deploy_candidates").read_text()
    assert "nothing records whether\n# the restart was queued" in header
    assert "scripts/deploy_code_only.sh status says whether the\n# server runs" in header
    status = (root / "scripts/lib/deploy_status.sh").read_text()
    assert 'echo "serving: ' in status and 'echo "head: ' in status


@pytest.mark.parametrize("why", ["head", "dirty"])
def test_an_expect_or_move_refusal_changes_nothing(dc, dc_ready, stub, capsys, why):
    w = dc_ready
    _two_live(w, dc)
    argv = _guarded(w, None, "--restart-server")
    if why == "head":
        argv[argv.index("--expect-head") + 1] = w.rev("refs/remotes/origin/main")
    else:
        (w.root / "x.txt").write_text("edited\n")
    before = _snapshot(w)
    capsys.readouterr()
    assert w.run(dc, *argv) == 1
    err = capsys.readouterr().err
    assert ("not where its caller left it" if why == "head" else "uncommitted tracked") in err
    assert _snapshot(w) == before
    assert _calls(stub) == []


def test_a_live_deploy_marker_is_exit_1_and_says_so(dc, dc_ready, stub):
    w = dc_ready
    _two_live(w, dc)
    w.install_engine()
    before = _snapshot(w)
    holder = subprocess.Popen(["sleep", "30"])
    try:
        (w.home / ".genesis" / "update_in_progress.pid").write_text(f"{holder.pid}\n")
        res = _shell(w, stub, *_guarded(w, None, "--restart-server"), "--wait", "0")
    finally:
        holder.kill()
        holder.wait()
    assert res.returncode == 1 and "holds the deploy marker" in res.stderr, res.stderr
    assert _snapshot(w) == before
    assert _calls(stub) == []


# ── exit 1: dropped, but not confirmed ─────────────────────────────────────


def _after_move(w: World, what: str):
    if what == "switched":
        w.git(w.root, "switch", "-q", "-c", "elsewhere")
    elif what == "dirty":
        (w.root / "x.txt").write_text("edited after the move\n")
    elif what == "committed":
        w.git(w.root, "commit", "-q", "--allow-empty", "-m", "a session commit")
    elif what == "manifest unreadable":
        w.manifest_path.write_text("{ not json\n")


@pytest.mark.parametrize(
    "what",
    [
        "switched",
        "dirty",
        "committed",
        "still held",
        "still listed",
        "view unreadable",
        "manifest unreadable",
    ],
)
def test_an_unconfirmed_drop_restarts_nothing_and_names_the_right_remedy(
    dc, dc_ready, stub, capsys, monkeypatch, what
):
    """`rebuild` is named only when the branch may still be in the manifest or
    `live` (it re-fetches origin/main, so it is wrong advice for a drop that
    landed and only lacks its restart). A view that cannot be read is never
    "nothing changed": the manifest was saved."""
    w = dc_ready
    _two_live(w, dc)
    real_move, real_observe = dc.plan.move_checkout, dc.Engine.observe

    def move(repo, p, source, save):
        moved = real_move(repo, p, source, save)
        _after_move(w, what)
        return moved

    def observe(self):
        if what == "view unreadable":
            raise dc.Unknown("git rev-parse failed: Input/output error")
        seen = real_observe(self)
        row = {"branch": "feat/b", "head": "0" * 40}
        (seen["live"]["holds"] if what == "still held" else seen["listed"]).append(row)
        return seen

    monkeypatch.setattr(dc.plan, "move_checkout", move)
    if what in ("still held", "still listed", "view unreadable"):
        monkeypatch.setattr(dc.Engine, "observe", observe)
    capsys.readouterr()
    assert w.run(dc, *_guarded(w, None, "--restart-server")) == 1
    cap = capsys.readouterr()
    err = cap.err
    assert "could not be confirmed" in err and "WAS saved" in err, err
    assert "nothing changed" not in err
    if what in ("still held", "still listed", "view unreadable", "manifest unreadable"):
        assert "scripts/deploy_candidates rebuild" in err, err
    else:
        assert "deploy_candidates rebuild" not in err, err
        assert "the drop itself is done" in err, err
        assert "The server was not restarted" in cap.out, cap.out
    if what == "view unreadable":
        assert "the engine's own view could not be read (git rev-parse failed" in err, err
    assert _calls(stub) == []
    if what == "manifest unreadable":
        assert "the manifest could not be read" in err, err
        return
    assert [c["branch"] for c in w.manifest()["candidates"]] == ["feat/a"]


def test_the_wait_and_the_fault_marker_texts_stay_distinct():
    """The Guardian treats exit 1 with "holds the deploy marker" as a wait; the
    marker WRITE failure is a fault and must not contain that phrase."""
    text = (Path(__file__).resolve().parents[2] / "scripts" / "deploy_candidates").read_text()
    [wait] = [ln for ln in text.splitlines() if "die " in ln and "holds the deploy marker" in ln]
    [fault] = [
        ln for ln in text.splitlines() if "die " in ln and "cannot write the deploy marker" in ln
    ]
    assert "holds the deploy marker" not in fault and "Nothing changed" in wait


# ── exit 2: usage ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "case", ["restart alone", "no expect", "expect main", "no-rebuild", "empty"]
)
def test_the_guarded_flags_go_together(dc, dc_ready, stub, capsys, case):
    w = dc_ready
    _two_live(w, dc)
    head, tip = w.rev("refs/heads/feat/b"), w.rev("refs/heads/live")
    argv = {
        "restart alone": ["drop", "feat/b", "--restart-server"],
        "no expect": ["drop", "feat/b", "--expect-candidate", head],
        "expect main": _guarded(w)[:3] + ["main", "--expect-head", tip, "--expect-candidate", head],
        "no-rebuild": [*_guarded(w), "--no-rebuild"],
        "empty": [*_guarded(w)[:-1], ""],
    }[case]
    before = _snapshot(w)
    capsys.readouterr()
    assert w.run(dc, *argv) == 2
    assert "deploy_candidates: --" in capsys.readouterr().err
    assert _snapshot(w) == before
    assert _calls(stub) == []


# ── exit 4: dropped and confirmed, restart not queued ──────────────────────


@pytest.mark.parametrize("fails", ["restart", "show"])
def test_a_restart_that_cannot_be_queued_is_exit_4_with_the_drop_done(
    dc, dc_ready, stub, capsys, fails
):
    w = dc_ready
    _two_live(w, dc)
    (stub / ("restart_rc" if fails == "restart" else "show_fails")).write_text("1\n")
    capsys.readouterr()
    assert w.run(dc, *_guarded(w, None, "--restart-server")) == 4
    err = capsys.readouterr().err
    assert "restart could not be" in err
    assert ("boom" if fails == "restart" else "No medium found") in err, err
    assert _calls(stub) == ([SHOW, RESTART] if fails == "restart" else [SHOW])
    assert _dropped(w)


# ── the entry's codes: 200, 97, 143 ────────────────────────────────────────


def test_a_held_lock_is_200_and_nothing_runs(dc, dc_ready, stub):
    w = dc_ready
    _two_live(w, dc)
    w.install_engine()
    before = _snapshot(w)
    fd = os.open(_lock_path(w), os.O_WRONLY | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_SH)  # a validation's shared hold
    try:
        res = _shell(w, stub, *_guarded(w, None, "--restart-server"), "--wait", "0")
    finally:
        os.close(fd)
    assert res.returncode == 200, res.stdout + res.stderr
    assert res.stdout == "" and "Nothing changed" in res.stderr
    assert _snapshot(w) == before
    assert _calls(stub) == []


def test_the_test_fence_is_97(dc, dc_ready, stub):
    w = dc_ready
    _two_live(w, dc)
    w.install_engine()
    before = _snapshot(w)
    res = w.plain_shell(
        *_guarded(w, None, "--restart-server"),
        "--wait",
        "0",
        extra_env=[f"PATH={stub}:/usr/bin:/bin", f"GENESIS_TEST_FENCE_LIVE_ROOTS={w.root}"],
    )
    assert res.returncode == 97 and "REFUSED (test fence)" in res.stderr, res.stderr
    assert _snapshot(w) == before
    assert _calls(stub) == []


def test_an_interrupted_drop_is_143(dc, dc_ready, stub):
    w = dc_ready
    _two_live(w, dc)
    w.install_engine()
    (stub / "sleep").write_text("2\n")
    argv = [*_guarded(w, None, "--restart-server"), "--wait", "0"]
    p = subprocess.Popen(
        ["env", "-i", f"HOME={w.home}", "GIT_CONFIG_NOSYSTEM=1", f"PATH={stub}:/usr/bin:/bin"]
        + ["/bin/bash", str(w.root / "scripts" / "deploy_candidates"), *argv],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 60
    while not (stub / "started").exists():
        assert p.poll() is None and time.monotonic() < deadline, p.communicate()
        time.sleep(0.05)
    p.send_signal(signal.SIGTERM)
    out, err = p.communicate(timeout=60)
    assert p.returncode == 143, out + err
