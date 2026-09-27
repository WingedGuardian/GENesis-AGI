"""tmp_watchgod.sh v2 — the whole-disk guardian's actions.

Drives the REAL daemon functions (sourced; `main` is guarded) with measurement
functions overridden per test, a stub `systemctl` that records what it was asked
to start, and the durable alert queue pointed at a tmp dir. The freeze tests use
REAL processes: a `dd` blocked on a FIFO holds an open descriptor under the
downloads directory exactly as a live download does, so eligibility is decided
from /proc, not from a fixture.

What these pin, from the design review:
  * actions are scoped to the filesystem in trouble — the reserve and the
    reclaim unit only for the $HOME filesystem, a freeze only when the
    downloads directory is ON the troubled filesystem;
  * the freeze is ALLOWLIST-only (a non-downloader is named, never stopped)
    and reversible through `scripts/watchgod thaw`, which checks identity;
  * pages dedupe per (filesystem, tier, episode), so a WARNING can never
    swallow the EMERGENCY that follows it;
  * observe mode (WATCHGOD_ACT=0, or any invalid value) changes nothing.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_WATCHGOD = _ROOT / "scripts" / "tmp_watchgod.sh"
_CLI = _ROOT / "scripts" / "watchgod"


def _stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def box(tmp_path):
    home = tmp_path / "home"
    (home / ".genesis" / "logs").mkdir(parents=True)
    (home / ".genesis" / "config").mkdir(parents=True)
    dl = home / "tmp" / "downloads"
    dl.mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "systemctl.calls"
    _stub(
        bindir / "systemctl",
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{calls}"\n'
        'case "$*" in *"show -p MainPID"*) echo 0;; esac\nexit 0\n',
    )
    queue = tmp_path / "queue"
    return {
        "home": home,
        "dl": dl,
        "bin": bindir,
        "calls": calls,
        "queue": queue,
        "state": home / ".genesis" / "watchgod",
        "tmp": tmp_path,
    }


def _run(box, snippet: str, act: int = 1) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(
        HOME=str(box["home"]),
        PATH=f"{box['bin']}:{os.environ['PATH']}",
        GENESIS_ALERT_QUEUE_ROOT=str(box["queue"]),
    )
    script = (
        f"set -euo pipefail\nsource '{_WATCHGOD}'\nload_config\nWATCHGOD_ACT={act}\n"
        f'mkdir -p "$DG_STATE_DIR"\n{snippet}\n'
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=120,
    )
    assert proc.returncode == 0, f"rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    return proc


def _pages(box) -> list[dict]:
    if not box["queue"].exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(box["queue"].glob("*.json"))]


def _calls(box) -> list[str]:
    return box["calls"].read_text().splitlines() if box["calls"].exists() else []


def _log(box) -> str:
    f = box["home"] / ".genesis" / "logs" / "tmp_watchgod.log"
    return f.read_text() if f.exists() else ""


def _home_dev(box) -> str:
    return str(os.stat(box["home"]).st_dev)


# handle_fs args: path dev tier free total eta writers fstype
def _handle(box, tier, *, dev=None, free=1000, total=100_000, eta="-", writers="", act=1):
    dev = dev or _home_dev(box)
    w = writers.replace("'", "")
    # The freeze reads every writer (ALL_WRITERS); attribution reads the top five.
    return _run(
        box,
        f"ALL_WRITERS='{w}'; handle_fs '{box['home']}' '{dev}' {tier} {free} {total} {eta} '{w}' btrfs",
        act=act,
    )


# ── reserve ───────────────────────────────────────────────────────


def test_green_creates_the_reserve_on_the_home_filesystem(box):
    _handle(box, "green", free=50_000, total=6_400)
    reserve = box["state"] / "reserve"
    assert reserve.exists()
    assert reserve.stat().st_size == 64 * 1024 * 1024


def test_green_never_takes_the_disk_below_its_orange_floor(box):
    # 6400 MB total -> 64 MB reserve; orange floor 512 MB. 560 free - 64 < 512.
    _handle(box, "green", free=560, total=6_400)
    assert not (box["state"] / "reserve").exists()


def test_observe_mode_creates_no_reserve(box):
    _handle(box, "green", free=50_000, total=6_400, act=0)
    assert not (box["state"] / "reserve").exists()
    assert "OBSERVE: would create" in _log(box)


def test_red_releases_the_reserve(box):
    _handle(box, "green", free=50_000, total=6_400)
    assert (box["state"] / "reserve").exists()
    _handle(box, "red", free=100, total=6_400)
    assert not (box["state"] / "reserve").exists()
    page = [p for p in _pages(box) if p["severity"] == "emergency"][0]
    assert "released 64 MB" in page["body"]


def test_red_on_another_filesystem_keeps_the_reserve(box):
    _handle(box, "green", free=50_000, total=6_400)
    _handle(box, "red", dev="999999", free=10, total=2048)
    assert (box["state"] / "reserve").exists(), "the reserve relieves only the $HOME filesystem"
    assert not any("pressure" in c for c in _calls(box))


# ── reclaim unit + pages ─────────────────────────────────────────


def test_orange_on_home_starts_standard_reclaim_and_pages_once(box):
    _handle(box, "orange")
    _handle(box, "orange")
    starts = [c for c in _calls(box) if c.startswith("--user start")]
    assert starts == ["--user start --no-block genesis-disk-hygiene-pressure@standard.service"], (
        starts
    )
    warn = [p for p in _pages(box) if p["severity"] == "warning"]
    assert len(warn) == 1
    assert warn[0]["dedupe_key"].endswith(":orange")


def test_orange_elsewhere_pages_without_a_lever(box):
    _handle(box, "orange", dev="999999")
    assert not [c for c in _calls(box) if " start " in f" {c} "], _calls(box)
    assert "has no lever on this filesystem" in _pages(box)[0]["body"]


def test_red_after_orange_still_pages_the_emergency(box):
    _handle(box, "orange")
    _handle(box, "red")
    sev = sorted(p["severity"] for p in _pages(box))
    assert sev == ["emergency", "warning"]
    keys = {p["dedupe_key"] for p in _pages(box)}
    assert len(keys) == 2, "a shared dedupe key would let the WARNING swallow the EMERGENCY"


def test_red_starts_last_resort_reclaim(box):
    _handle(box, "red")
    assert any("pressure@last-resort" in c for c in _calls(box))


def test_green_ends_the_episode_so_the_next_one_pages_again(box):
    _handle(box, "orange")
    _handle(box, "green", free=90_000)
    _handle(box, "orange")
    assert len([p for p in _pages(box) if p["severity"] == "warning"]) == 2


def test_attribution_is_logged_once_per_episode(box):
    _handle(box, "yellow", writers="123 456 1000 5 cp")
    _handle(box, "yellow", writers="123 456 1000 5 cp")
    assert _log(box).count("top writers") == 1


def test_observe_mode_pages_nothing_and_starts_nothing(box):
    _handle(box, "red", act=0)
    assert not _pages(box)
    assert not [c for c in _calls(box) if " start " in f" {c} "], _calls(box)
    assert "OBSERVE: would page EMERGENCY" in _log(box)


def test_invalid_act_value_degrades_to_observe(box):
    (box["home"] / ".genesis" / "config" / "watchgod.local.conf").write_text("WATCHGOD_ACT=yes\n")
    env = dict(os.environ, HOME=str(box["home"]), PATH=f"{box['bin']}:{os.environ['PATH']}")
    out = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\nsource '{_WATCHGOD}'\nload_config\necho $WATCHGOD_ACT"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert out.stdout.strip() == "0"
    assert "OBSERVE mode" in _log(box)


def test_pressure_retrigger_is_rate_limited(box):
    for _ in range(3):
        _handle(box, "orange")
    assert len([c for c in _calls(box) if "@standard" in c]) == 1


# ── freeze ───────────────────────────────────────────────────────


def _proc_state(pid: int) -> str:
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]


def _starttime(pid: int) -> str:
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


@pytest.fixture
def downloader(box):
    """A real `dd` holding an open descriptor under the downloads dir, blocked
    on a FIFO so it lives until the test ends."""
    fifo = box["tmp"] / "feed"
    os.mkfifo(fifo)
    proc = subprocess.Popen(
        ["dd", f"if={fifo}", f"of={box['dl']}/big.part", "status=none"], stdin=subprocess.DEVNULL
    )
    writer = os.open(fifo, os.O_WRONLY)  # unblocks dd's open of the FIFO
    deadline = time.time() + 10
    while time.time() < deadline:
        if any(
            str(box["dl"]) in os.readlink(f"/proc/{proc.pid}/fd/{fd}")
            for fd in os.listdir(f"/proc/{proc.pid}/fd")
        ):
            break
        time.sleep(0.05)
    else:
        pytest.fail("fixture precondition: dd never opened its output under downloads")
    yield proc
    os.close(writer)
    with contextlib.suppress(ProcessLookupError):
        os.kill(proc.pid, 18)  # SIGCONT so it can die cleanly
    proc.kill()
    proc.wait()


def _writers_for(proc, comm="dd", rate=500):
    return f"{proc.pid} {_starttime(proc.pid)} 999999 {rate} {comm}"


def test_red_freezes_a_known_downloader_writing_into_downloads(box, downloader):
    _handle(box, "red", writers=_writers_for(downloader))
    assert _proc_state(downloader.pid) == "T"
    rec = (box["state"] / "frozen").read_text().split("\t")
    assert rec[0] == str(downloader.pid) and rec[2] == "dd"
    page = [p for p in _pages(box) if p["title"].startswith("Disk nearly full")][0]
    assert f"FROZE pid {downloader.pid}" in page["body"]
    assert f"scripts/watchgod thaw {downloader.pid}" in page["body"]
    own = [p for p in _pages(box) if p["title"].startswith("Froze a runaway download")]
    assert len(own) == 1, "each freeze pages on its own, with the thaw command"
    assert f"scripts/watchgod thaw {downloader.pid}" in own[0]["body"]
    assert "PAUSED, not killed" in own[0]["body"]


def test_thaw_resumes_it_and_clears_the_record(box, downloader):
    _handle(box, "red", writers=_writers_for(downloader))
    assert _proc_state(downloader.pid) == "T"
    env = dict(os.environ, HOME=str(box["home"]))
    out = subprocess.run(
        [str(_CLI), "thaw", str(downloader.pid)], env=env, capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    assert f"resumed pid {downloader.pid}" in out.stdout
    assert _proc_state(downloader.pid) != "T"
    assert (box["state"] / "frozen").read_text() == ""
    assert "THAWED" in _log(box)


def test_thaw_refuses_a_recycled_pid(box, downloader):
    _handle(box, "red", writers=_writers_for(downloader))
    f = box["state"] / "frozen"
    parts = f.read_text().split("\t")
    parts[1] = "1"  # a different start time: the record no longer names this process
    f.write_text("\t".join(parts))
    env = dict(os.environ, HOME=str(box["home"]))
    out = subprocess.run([str(_CLI), "thaw", "all"], env=env, capture_output=True, text=True)
    assert "no longer running" in out.stdout
    assert _proc_state(downloader.pid) == "T", (
        "a pid whose identity changed must never be signalled"
    )


def test_a_non_downloader_is_named_not_frozen(box, downloader):
    _handle(box, "red", writers=_writers_for(downloader, comm="python"))
    assert _proc_state(downloader.pid) != "T"
    body = [p for p in _pages(box) if p["title"].startswith("Disk nearly full")][0]["body"]
    assert "No freeze candidate" in body


def test_no_freeze_when_downloads_is_on_another_filesystem(box, downloader):
    _handle(box, "red", dev="999999", writers=_writers_for(downloader))
    assert _proc_state(downloader.pid) != "T"
    assert "No freeze candidate" in _pages(box)[0]["body"]


def test_a_slow_writer_is_not_the_runaway(box, downloader):
    _handle(box, "red", writers=_writers_for(downloader, rate=5))
    assert _proc_state(downloader.pid) != "T"


def test_observe_mode_never_freezes(box, downloader):
    _handle(box, "red", writers=_writers_for(downloader), act=0)
    assert _proc_state(downloader.pid) != "T"
    assert not (box["state"] / "frozen").exists()


def test_orange_never_freezes(box, downloader):
    _handle(box, "orange", writers=_writers_for(downloader))
    assert _proc_state(downloader.pid) != "T"


# ── poll + state file ────────────────────────────────────────────


def _poll(box, measure_lines: dict[str, str], act=1) -> dict:
    """check_disks + write_state with dg_measure answering per path."""
    cases = "\n".join(f"        '{p}') echo '{m}';;" for p, m in measure_lines.items())
    snippet = f"""
    dg_measure() {{
        case "$1" in
{cases}
        *) echo '90000 100000 0 - - ext4';;
        esac
    }}
    dg_io_snapshot() {{ :; }}
    check_disks
    write_state
    echo "next=$NEXT_POLL"
    """
    out = _run(box, snippet, act=act)
    state = json.loads((box["home"] / ".genesis" / "watchgod_state.json").read_text())
    state["_next"] = out.stdout.strip().rsplit("next=", 1)[1]
    return state


def test_state_file_is_valid_json_with_the_compat_keys(box):
    (box["home"] / ".genesis" / "cc-tmp").mkdir()
    st = _poll(box, {})
    assert st["cc_tmp"]["tier"] == "green"
    assert set(st["cc_tmp"]) >= {"tier", "used_mb", "budget_mb", "sacred_mb", "fs_free_mb", "fs_total_mb"}
    assert set(st["system_tmp"]) >= {"tier", "used_pct", "is_tmpfs"}
    assert st["disk"], "every watched filesystem is reported"
    assert st["_next"] == "30"


def test_compat_cc_tier_is_the_floor_tier_not_the_eta_tier(box):
    """A ~20 GB/min burst with lots of room must not degrade routing: the compat
    cc tier (read by TmpPressureStatus) is the FLOOR tier and ignores
    time-to-full, while the action tier rises by one level only.

    In this sandbox cc-tmp shares a device with `/`, so it is reported under
    `/` — exactly what a real install without a separate cc-tmp volume does."""
    cc = box["home"] / ".genesis" / "cc-tmp"
    cc.mkdir()
    snippet_rate = """
    dev=$(stat -c %d /)
    echo "$(( $(date +%s) - 30 )) 1000 20000 quota" > "$DG_STATE_DIR/rate_$dev"
    """
    _run(box, snippet_rate)
    snippet = """
    dg_measure() { echo '60000 100000 1 - - btrfs'; }
    dg_io_snapshot() { :; }
    check_disks
    write_state
    echo "next=$NEXT_POLL"
    """
    out = _run(box, snippet)
    st = json.loads((box["home"] / ".genesis" / "watchgod_state.json").read_text())
    assert st["cc_tmp"]["tier"] == "green"
    assert st["disk"]["/"]["tier"] == "yellow", st["disk"]["/"]
    assert st["disk"]["/"]["floor_tier"] == "green"
    assert out.stdout.strip().endswith("next=5"), "a filling disk switches to the fast poll"


def test_each_filesystem_is_reported_once(box):
    st = _poll(box, {})
    home_like = [p for p in st["disk"] if p in ("/", str(box["home"]))]
    assert len(home_like) == len({os.stat(p).st_dev for p in home_like})


# ── config writers ───────────────────────────────────────────────


@pytest.mark.parametrize("script", ["bootstrap.sh", "install.sh"])
def test_conf_writers_write_only_what_v2_reads(script):
    """The v1 budget keys are dead in v2 (the guardian measures cc-tmp's real
    quota itself). A writer still emitting them would keep a fiction alive in
    every install's config; one that stopped writing CC_TMP_DIR would point the
    guardian at the default instead of the configured volume."""
    text = (_ROOT / "scripts" / script).read_text()
    start = text.index('cat > "$HOME/.genesis/config/watchgod.conf" <<WEOF')
    body = text[start: text.index("\nWEOF\n", start)]
    assert "CC_TMP_DIR=$CC_TMP_DIR" in body
    for dead in ("CC_TMP_BUDGET_MB", "SACRED_GROUND_MB", "CC_TMP_CAPACITY_MB"):
        assert dead not in body, f"{script} still writes the dead v1 key {dead}"


def test_the_daemon_reads_no_v1_key():
    src = _WATCHGOD.read_text()
    for dead in ("CC_TMP_BUDGET_MB", "SACRED_GROUND_MB", "CC_TMP_CAPACITY_MB"):
        assert dead not in src, dead


# ── cc-tmp retention sweep ───────────────────────────────────────
#
# Units: a loose top-level file, a top-level dir, and a SESSION dir
# claude-<uid>/<project>/<session>. Reaped only when nothing alive holds it
# and nothing inside was modified within the retention age. MEASURED on a live
# cc-tmp before shipping: at 7 days 74 of ~1,030 units reapable, 0 of them a
# live session's; at 2 days 447, again 0 live (7 identifiable live sessions).


def _age_tree(p: Path, days: float) -> None:
    t = time.time() - days * 86400
    for sub in sorted(p.rglob("*"), key=lambda x: len(x.parts), reverse=True):
        os.utime(sub, (t, t), follow_symlinks=False)
    os.utime(p, (t, t))


@pytest.fixture
def cctmp(box):
    root = box["home"] / ".genesis" / "cc-tmp"
    proj = root / "claude-1000" / "-home-proj"
    proj.mkdir(parents=True)
    return root, proj


def _sweep(box, age_min=10080, act=1):
    return _run(box, f'sweep_cc_tmp {age_min} test', act=act)


def test_a_dead_old_session_dir_is_reaped_and_a_fresh_one_kept(box, cctmp):
    root, proj = cctmp
    dead = proj / "dead-session"
    (dead / "tasks").mkdir(parents=True)
    (dead / "tasks" / "a.output").write_text("x")
    _age_tree(dead, 10)
    fresh = proj / "fresh-session"
    fresh.mkdir()
    (fresh / "f").write_text("x")
    _sweep(box)
    assert not dead.exists()
    assert fresh.exists()


def test_an_old_session_dir_with_one_fresh_file_is_kept(box, cctmp):
    """Content age, not directory mtime: one recent write anywhere inside keeps it."""
    _, proj = cctmp
    s = proj / "busy"
    (s / "deep" / "er").mkdir(parents=True)
    (s / "deep" / "er" / "old").write_text("x")
    _age_tree(s, 10)
    (s / "deep" / "er" / "new").write_text("x")
    _sweep(box)
    assert s.exists()


def test_an_old_session_dir_a_live_process_holds_is_kept(box, cctmp):
    _, proj = cctmp
    s = proj / "live"
    s.mkdir()
    f = s / "out.log"
    f.write_text("x")
    holder = subprocess.Popen(["bash", "-c", f"exec 3>>'{f}'; echo ready; sleep 60"],
                              stdout=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL)
    assert holder.stdout.readline().strip() == "ready"
    try:
        _age_tree(s, 10)
        _sweep(box)
        assert s.exists()
    finally:
        holder.kill()
        holder.wait()


def test_an_old_dir_that_is_a_process_cwd_is_kept(box, cctmp):
    root, _ = cctmp
    job = root / "pip-build-xyz"
    job.mkdir()
    proc = subprocess.Popen(["bash", "-c", "echo ready; sleep 60"], cwd=job,
                            stdout=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL)
    assert proc.stdout.readline().strip() == "ready"
    try:
        _age_tree(job, 10)
        _sweep(box)
        assert job.exists()
    finally:
        proc.kill()
        proc.wait()


def _bind_unix(directory: Path, name: str):
    """Bind a unix socket by a SHORT relative path: tmp_path runs past the
    ~108-byte sockaddr_un limit."""
    import socket as sk

    here = os.getcwd()
    os.chdir(directory)
    try:
        s = sk.socket(sk.AF_UNIX)
        s.bind(name)
        return s
    finally:
        os.chdir(here)


def test_sockets_and_the_control_plane_are_never_reaped(box, cctmp):
    root, proj = cctmp
    socks = root / "cc-socks"
    socks.mkdir()
    sock = _bind_unix(socks, "123.sock")
    try:
        empty_socks = root / "cc-socks-1000"
        empty_socks.mkdir()
        s = proj / "has-socket"
        s.mkdir()
        inner = _bind_unix(s, "x.sock")
        _age_tree(socks, 30)
        _age_tree(empty_socks, 30)
        _age_tree(s, 30)
        _run(box, "id() { echo 1000; }; sweep_cc_tmp 10080 test")
        assert socks.exists() and (socks / "123.sock").exists()
        assert empty_socks.exists(), "an EMPTY control-plane dir is still control plane"
        assert s.exists(), "a unit holding a socket is never reaped"
        inner.close()
    finally:
        sock.close()


def test_the_container_is_never_a_unit(box, cctmp):
    """One live session must not pin its siblings, and an old container must
    not take a live session with it."""
    root, proj = cctmp
    old_sibling = proj / "old"
    old_sibling.mkdir()
    _age_tree(old_sibling, 10)
    new = proj / "new"
    new.mkdir()
    _age_tree(root / "claude-1000", 10)  # container + project dirs look ancient
    (new / "f").write_text("x")          # but this session is fresh
    _sweep(box)
    assert not old_sibling.exists()
    assert new.exists() and (root / "claude-1000").exists()


def test_old_loose_files_are_reaped_and_held_ones_kept(box, cctmp):
    root, _ = cctmp
    old = root / "lobby-picker.abc"
    old.write_text("x")
    held = root / "guidance_marker"
    held.write_text("x")
    holder = subprocess.Popen(["bash", "-c", f"exec 3>>'{held}'; echo ready; sleep 60"],
                              stdout=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL)
    assert holder.stdout.readline().strip() == "ready"
    try:
        _age_tree(old, 10)
        _age_tree(held, 10)
        _sweep(box)
        assert not old.exists()
        assert held.exists()
    finally:
        holder.kill()
        holder.wait()


def test_a_name_with_a_newline_is_never_reaped(box, cctmp):
    root, _ = cctmp
    odd = root / "we\nird"
    odd.mkdir()
    _age_tree(odd, 30)
    _sweep(box)
    assert odd.exists()


def test_emptied_project_dirs_are_removed_once_they_too_are_old(box, cctmp):
    """Reaping a project's last session bumps the project dir's own mtime, so
    it goes on a LATER pass, once it too has aged. Deliberate: CC creates
    project dirs on demand, and removing a just-emptied one could race a new
    session's mkdir inside it."""
    root, proj = cctmp
    s = proj / "gone"
    s.mkdir()
    _age_tree(proj, 10)
    _sweep(box)
    assert not s.exists()
    assert proj.exists(), "just emptied: not old yet"
    _age_tree(proj, 10)
    _sweep(box)
    assert not proj.exists()
    assert (root / "claude-1000").exists(), "the container itself is never removed"


def test_observe_mode_reaps_nothing(box, cctmp):
    _, proj = cctmp
    s = proj / "dead"
    s.mkdir()
    _age_tree(s, 10)
    _sweep(box, act=0)
    assert s.exists()
    assert "OBSERVE: cc-tmp sweep would reap" in _log(box)


def test_a_blind_liveness_snapshot_skips_the_sweep(box, cctmp):
    _, proj = cctmp
    s = proj / "dead"
    s.mkdir()
    _age_tree(s, 10)
    _run(box, "live_open_paths() { :; }; sweep_cc_tmp 10080 test")
    assert s.exists(), "no deletion without being able to see writers"
    assert "sweep skipped" in _log(box)


def test_the_pressure_age_reaps_what_the_hourly_age_keeps(box, cctmp):
    _, proj = cctmp
    s = proj / "three-days"
    s.mkdir()
    _age_tree(s, 3)
    _sweep(box, age_min=10080)
    assert s.exists()
    _sweep(box, age_min=2880)
    assert not s.exists()


def test_orange_on_cc_tmp_runs_the_pressure_sweep(box, cctmp):
    root, proj = cctmp
    s = proj / "three-days"
    s.mkdir()
    _age_tree(s, 3)
    dev = str(os.stat(root).st_dev)
    _run(box, f"home_dev() {{ echo not-this; }}; handle_fs '{root}' '{dev}' orange 100 2048 - '' btrfs")
    assert not s.exists()
    assert "retention sweep at 2 days" in _pages(box)[0]["body"]


def test_the_hourly_sweep_is_rate_limited(box, cctmp):
    _, proj = cctmp
    _run(box, "maybe_sweep_cc_tmp hourly")
    s = proj / "dead"
    s.mkdir()
    _age_tree(s, 10)
    _run(box, "maybe_sweep_cc_tmp hourly")
    assert s.exists(), "a second pass inside the hour must not run"


def test_a_retention_age_under_a_day_is_refused(box):
    (box["home"] / ".genesis" / "config" / "watchgod.local.conf").write_text(
        "CC_SWEEP_AGE_MIN=10\nCC_SWEEP_PRESSURE_AGE_MIN=5\n")
    out = _run(box, 'echo "$CC_SWEEP_AGE_MIN $CC_SWEEP_PRESSURE_AGE_MIN"')
    assert out.stdout.strip().splitlines()[-1] == "10080 2880"


def test_a_garbage_threshold_is_reset_not_fatal(box):
    """Every threshold reaches shell arithmetic; under set -u a typo like
    'abc' would be an unset-variable abort that kills the daemon."""
    (box["home"] / ".genesis" / "config" / "watchgod.local.conf").write_text(
        "DG_RED_MIN_MB=abc\nDG_ORANGE_PCT='8%'\n")
    out = _run(box, 'echo "$DG_RED_MIN_MB $DG_ORANGE_PCT"; dg_floor_tier 50 1000 - -')
    assert out.stdout.strip().splitlines() == ["3072 8", "red"]
    assert "is not a whole number" in _log(box)


def test_a_downloader_outside_the_top_five_is_still_frozen(box, downloader):
    """MEASURED in the E2E on a busy box: a 300 MB/min downloader never reached
    the global top five behind test runs and a database. Candidates are ALL
    writers above the rate floor."""
    busy = "\n".join(f"{90000 + i} 1 99999 {2000 - i} python" for i in range(20))
    writers = busy + "\n" + _writers_for(downloader, rate=300)
    _handle(box, "red", writers=writers)
    assert _proc_state(downloader.pid) == "T"


def test_non_downloaders_are_not_listed_as_freeze_refusals(box, downloader):
    _handle(box, "red", writers="424242 1 99999 900 python\n" + _writers_for(downloader, rate=5))
    body = [p for p in _pages(box) if p["title"].startswith("Disk nearly full")][0]["body"]
    assert "424242" not in body.split("Top writers")[0], "a python writer is attribution, not a freeze refusal"


def test_removing_a_conf_line_restores_its_default(box):
    """Sourcing a file only SETS variables; without restoring the baseline, a
    deleted threshold kept its value until restart. MEASURED in the E2E: a
    forced-RED threshold outlived its removal and pinned the disk at RED."""
    conf = box["home"] / ".genesis" / "config" / "watchgod.local.conf"
    conf.write_text("DG_RED_PCT=100\nWATCHGOD_ACT=0\n")
    snippet = f"""
    load_config; echo "$DG_RED_PCT $WATCHGOD_ACT"
    : > '{conf}'
    load_config; echo "$DG_RED_PCT $WATCHGOD_ACT"
    """
    out = _run(box, snippet).stdout.strip().splitlines()
    assert out[-2:] == ["100 0", "3 1"]


def test_freeze_refuses_a_recycled_pid(box, downloader):
    """The writers list was measured a poll ago; if that pid now names a
    different process (start time changed), it must not be stopped."""
    _handle(box, "red", writers=f"{downloader.pid} 1 999999 500 dd")
    assert _proc_state(downloader.pid) != "T"
    assert "pid reused" in [p for p in _pages(box) if p["title"].startswith("Disk nearly full")][0]["body"]


# ── end-of-build review fixes ────────────────────────────────────


@pytest.mark.parametrize("name", ["trail ", " lead", " "])
def test_whitespace_in_a_unit_name_neither_fails_open_nor_kills_the_daemon(box, cctmp, name):
    """IFS=' ' used to trim the key: 'trail ' was recorded as 'trail', its fresh
    file never matched, and it was reaped; ' ' became an EMPTY key, which
    aborted the daemon under set -e. Reproduced before fixing."""
    root, _ = cctmp
    u = root / name
    u.mkdir()
    (u / "fresh").write_text("x")
    _sweep(box)
    assert u.exists() and (u / "fresh").exists()


def test_a_huge_writer_list_does_not_sigpipe_the_poll(box):
    """`printf | head -n 5` on a list past the pipe buffer SIGPIPEs printf,
    and under pipefail the daemon died. ~200 KB of writers must be fine."""
    snippet = """
    big="$(for i in $(seq 1 4000); do echo "$((100000 + i)) 1 99999 70 python_writer_padding"; done)"
    dg_io_snapshot() { echo x; }
    dg_io_top() { printf '%s\\n' "$big"; }
    _IO_PREV=x; _IO_PREV_T=$(( $(date +%s) - 30 ))
    dg_measure() { echo '90000 100000 0 - - ext4 10000'; }
    check_disks
    echo "ok $(printf '%s\\n' "$writers" 2>/dev/null | wc -l)"
    """
    out = _run(box, snippet)
    assert out.stdout.strip().startswith("ok")


def test_the_sweep_sees_writers_through_a_symlinked_ancestor(box, tmp_path):
    """/proc reports resolved paths. If an ANCESTOR of cc-tmp is a symlink
    (e.g. /home -> /var/home), an unresolved root never prefix-matches a held
    path, the held table is empty, and a live session's temp is judged by age
    alone — a silent fail-open of the liveness guard."""
    real_parent = tmp_path / "real-parent"
    cc = real_parent / "cc"
    live = cc / "claude-1000" / "p" / "live"
    live.mkdir(parents=True)
    f = live / "out.log"
    f.write_text("x")
    link_parent = tmp_path / "link-parent"
    link_parent.symlink_to(real_parent)
    (box["home"] / ".genesis" / "config" / "watchgod.local.conf").write_text(
        f"CC_TMP_DIR='{link_parent}/cc'\n")
    holder = subprocess.Popen(["bash", "-c", f"exec 3>>'{f}'; echo ready; sleep 60"],
                              stdout=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL)
    assert holder.stdout.readline().strip() == "ready"
    try:
        _age_tree(live, 10)
        dead = cc / "claude-1000" / "p" / "dead"
        dead.mkdir()
        _age_tree(dead, 10)
        _sweep(box)
        assert live.exists(), "a held session reached through a symlinked ancestor is kept"
        assert not dead.exists(), "control: the sweep does run through the symlinked ancestor"
    finally:
        holder.kill()
        holder.wait()


def test_the_episode_start_logs_a_dry_freeze_evaluation(box, downloader):
    """The broad-freeze decision waits on evidence of what the freeze WOULD
    do; it is logged once, at the start of each episode, and stops nothing."""
    _handle(box, "yellow", writers=_writers_for(downloader) + "\n424242 1 99999 900 python")
    assert _proc_state(downloader.pid) != "T"
    log = _log(box)
    assert f"freeze candidate (dry): would freeze pid {downloader.pid}" in log
    assert "freeze candidate (dry): not on the downloader allowlist: pid 424242" in log


def test_frozen_count_ignores_records_of_gone_or_recycled_processes(box):
    live = subprocess.Popen(["sleep", "60"])
    try:
        st = _starttime(live.pid)
        f = box["state"]
        f.mkdir(parents=True, exist_ok=True)
        (f / "frozen").write_text(
            "999999\t1\tdd\tts\t59\t100\n"            # gone
            f"{live.pid}\t1\tdd\tts\t59\t100\n"        # pid alive, different process
            f"{live.pid}\t{st}\tsleep\tts\t59\t100\n"  # the same process: counts
        )
        assert _run(box, "frozen_count").stdout.strip() == "1"
    finally:
        live.kill()
        live.wait()


def test_json_strings_stay_valid_for_any_control_byte(box):
    """Security review LOW: the state file must stay parseable whatever a
    configured path contains."""
    out = _run(box, "_wg_json_str \"$(printf 'a\\001b\\tc\\\"d\\\\e\\037f')\"").stdout
    assert json.loads(out) == 'a?b\tc"d\\e?f'


def test_a_process_name_with_a_tab_cannot_split_the_snapshot(box):
    """Security review WARNING: comm is process-chosen and may hold a tab; it
    must not shift the fields of the io snapshot (or the frozen record)."""
    # `sleep 60 & wait` keeps bash itself alive: a trailing plain `sleep`
    # would be exec'd and the process would be named "sleep" again.
    proc = subprocess.Popen(["bash", "-c", "printf 'dl\\tfake' > /proc/self/comm; echo ready; sleep 60 & wait"],
                            stdout=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL)
    assert proc.stdout.readline().strip() == "ready"
    try:
        assert Path(f"/proc/{proc.pid}/comm").read_text() == "dl\tfake\n", "fixture precondition: renamed"
        out = _run(box, "dg_io_snapshot").stdout.splitlines()
        mine = [l for l in out if l.split(" ")[0] == str(proc.pid)]
        assert mine, "fixture precondition: the renamed process is in the snapshot"
        assert mine[0].split(" ")[3] == "dl_fake" and len(mine[0].split(" ")) == 4
    finally:
        proc.kill()
        proc.wait()
