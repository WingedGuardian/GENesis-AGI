"""tmp_watchgod.sh v2 — the whole-disk guardian's actions.

Drives the REAL daemon functions (sourced; `main` is guarded) with measurement
functions overridden per test, a stub `systemctl` that records what it was asked
to start, and the durable alert queue pointed at a tmp dir.

What these pin, from the design review and two review rounds:
  * actions are scoped to the limit domain in trouble — the reserve and the
    reclaim unit only for the $HOME domain, the pressure sweep only for
    cc-tmp's;
  * pages dedupe per (domain, tier, episode) and per mode, and a page is
    recorded as sent only once it was actually queued;
  * every action stamp is written only after the action succeeded;
  * observe mode (WATCHGOD_ACT=0, or any invalid value) changes nothing.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_WATCHGOD = _ROOT / "scripts" / "tmp_watchgod.sh"


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
    state = home / ".genesis" / "watchgod"
    yield {
        "home": home,
        "dl": dl,
        "bin": bindir,
        "calls": calls,
        "queue": queue,
        "state": state,
        "tmp": tmp_path,
    }
    # The reserve is a REAL fallocate on the real disk. Every test deletes its
    # own, and a reserve over the sandbox cap fails the test: uncapped, these
    # suites left 156 x 1 GB reserves behind and took a live container's quota
    # to within 611 MB of full (2026-09-29).
    reserve = state / "reserve"
    if reserve.exists():
        size = reserve.stat().st_size
        reserve.unlink()
        assert size <= _TEST_RESERVE_MAX_MB * 1024 * 1024, f"sandbox reserve of {size} bytes exceeds the test cap"


# Cap for the reserve file in every sandbox (see the box fixture's teardown).
_TEST_RESERVE_MAX_MB = 64


def _run(box, snippet: str, act: int = 1) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(
        HOME=str(box["home"]),
        PATH=f"{box['bin']}:{os.environ['PATH']}",
        GENESIS_ALERT_QUEUE_ROOT=str(box["queue"]),
    )
    script = (
        f"set -euo pipefail\nsource '{_WATCHGOD}'\nload_config\nWATCHGOD_ACT={act}\n"
        # Both the live value AND the reload baseline: load_config restores
        # _WG_BASE, so capping only the variable would let any snippet that
        # reloads the config fallocate the real 2 GB default.
        f"RESERVE_MAX_MB={_TEST_RESERVE_MAX_MB}; _WG_BASE[RESERVE_MAX_MB]={_TEST_RESERVE_MAX_MB}\n"
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
    # ALL_WRITERS is every writer measured this poll; attribution reads the top five.
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
    assert "Reclaim: none on this filesystem" in _pages(box)[0]["body"]


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


# ── poll + state file ────────────────────────────────────────────


def _poll(box, measure_lines: dict[str, str], act=1, raw_sizes: dict[str, int] | None = None) -> dict:
    """check_disks + write_state with dg_measure answering per path.

    raw_sizes stubs a path's raw statvfs size in dg_raw_sizes_mb (the domain
    identity); its mount keeps the real size, and unstubbed paths are real."""
    cases = "\n".join(f"        '{p}') echo '{m}';;" for p, m in measure_lines.items())
    raw = "\n".join(
        f"        '{p}') echo \"{v} $(dg_raw_total_mb \"$2\")\"; return;;" for p, v in (raw_sizes or {}).items()
    )
    raw_fn = (
        f"""
    eval "_real_sizes() $(declare -f dg_raw_sizes_mb | tail -n +2)"
    dg_raw_sizes_mb() {{
        case "$1" in
{raw}
        esac
        _real_sizes "$1" "$2"
    }}"""
        if raw_sizes
        else ""
    )
    snippet = f"""{raw_fn}
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
    read -r ck _ < <(printf / | cksum)
    echo "$(( $(date +%s) - 30 )) 1000 20000 quota" > "$DG_STATE_DIR/rate_${dev}m${ck}_quota"
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
    # A /proc showing no other process (unmounted, hidden): blind, not "idle".
    blind = box["tmp"] / "noproc"
    blind.mkdir()
    _run(box, f"TL_PROC='{blind}'; sweep_cc_tmp 10080 test")
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
    assert "cc-tmp sweep at 2 days reaped" in _pages(box)[0]["body"]


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


def test_json_strings_stay_valid_for_any_control_byte(box):
    """Security review LOW: the state file must stay parseable whatever a
    configured path contains."""
    out = _run(box, "_wg_json_str \"$(printf 'a\\001b\\tc\\\"d\\\\e\\037f')\"").stdout
    assert json.loads(out) == 'a?b\tc"d\\e?f'


def test_a_process_name_with_a_tab_cannot_split_the_snapshot(box):
    """Security review WARNING: comm is process-chosen and may hold a tab; it
    must not shift the fields of the io snapshot."""
    # `sleep 60 & wait` keeps bash itself alive: a trailing plain `sleep`
    # would be exec'd and the process would be named "sleep" again.
    proc = subprocess.Popen(["bash", "-c", "printf 'dl\\tfake' > /proc/self/comm; echo ready; sleep 60 & wait"],
                            stdout=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL)
    assert proc.stdout.readline().strip() == "ready"
    try:
        assert Path(f"/proc/{proc.pid}/comm").read_text() == "dl\tfake\n", "fixture precondition: renamed"
        out = _run(box, "dg_io_snapshot").stdout.splitlines()
        mine = [ln for ln in out if ln.split(" ")[0] == str(proc.pid)]
        assert mine, "fixture precondition: the renamed process is in the snapshot"
        assert mine[0].split(" ")[3] == "dl_fake" and len(mine[0].split(" ")) == 4
    finally:
        proc.kill()
        proc.wait()


# ── round-1 review findings ──────────────────────────────────────


def test_a_separately_limited_domain_on_a_shared_device_is_watched(box):
    """Codex P1 / Devin: a path on the SAME device but under its own limit (an
    incus dir-pool volume with a project quota reports that quota through
    statvfs) must be measured and acted on as its own domain, not skipped as a
    device already seen."""
    cc = box["home"] / ".genesis" / "cc-tmp"
    cc.mkdir()
    assert os.stat(cc).st_dev == os.stat(box["home"]).st_dev, "fixture precondition: one device"
    # A project quota shows through statvfs: its raw size differs from $HOME's.
    st = _poll(box, {str(cc): "100 2048 0 - - ext4"}, raw_sizes={str(cc): 2048})
    assert st["disk"][str(cc)]["tier"] == "red", st["disk"]
    assert st["cc_tmp"]["tier"] == "red"
    assert st["cc_tmp"]["used_mb"] == 1948, "cc-tmp owns its limited domain: usage = total - free"
    red = [p for p in _pages(box) if p["title"].startswith("Disk nearly full")]
    assert len(red) == 1 and str(cc) in red[0]["title"]
    # Levers stay with the domain they relieve: the $HOME domain is green, so
    # no last-resort reclaim and no reserve release on its behalf.
    assert not any("last-resort" in c for c in _calls(box))
    assert "released" not in red[0]["body"]


def test_cc_usage_on_a_shared_quota_domain_is_measured_not_derived(box):
    """Devin: a quota on a SHARED root subvolume says nothing about cc-tmp's own
    usage; total - free there is every other file on the subvolume."""
    cc = box["home"] / ".genesis" / "cc-tmp"
    cc.mkdir()
    (cc / "work").write_bytes(b"x" * (3 * 1024 * 1024))
    shared = "60000 100000 1 - - btrfs"
    st = _poll(box, {"/": shared, str(box["home"]): shared, str(cc): shared})
    assert st["cc_tmp"]["used_mb"] < 100, st["cc_tmp"]


def test_the_daemon_survives_a_log_it_cannot_write(box):
    """Same class: the log lives on the guarded filesystem. An append that
    fails at RED must not take the guardian down under set -e."""
    logs = box["home"] / ".genesis" / "logs"
    logf = logs / "tmp_watchgod.log"
    logf.write_text("")
    logf.chmod(0o444)
    logs.chmod(0o555)
    try:
        _handle(box, "red")  # asserts rc == 0
        _poll(box, {})
    finally:
        logs.chmod(0o755)
        logf.chmod(0o644)


@pytest.mark.parametrize("tier,title", [("red", "Disk nearly full"), ("orange", "Disk filling")])
def test_switching_observe_to_act_mid_episode_still_pages(box, tier, title):
    """Codex P2: an observe-mode poll must not consume the page an acting poll
    owes. Flipping WATCHGOD_ACT 0 -> 1 while still in trouble pages."""
    _handle(box, tier, act=0)
    assert not [p for p in _pages(box) if p["title"].startswith(title)], "observe pages nothing"
    _handle(box, tier, act=1)
    assert len([p for p in _pages(box) if p["title"].startswith(title)]) == 1
    _handle(box, tier, act=1)
    assert len([p for p in _pages(box) if p["title"].startswith(title)]) == 1, "still once per episode"


def test_uninstall_stops_the_watchgod_and_both_pressure_instances():
    """Codex P2: a pressure run can be deleting ~/tmp for up to its timeout;
    both uninstall paths (in-container and host-driven) must stop it — and the
    watchgod that starts it — before files are removed."""
    # The instance names are built from a variable in uninstall.sh (CI's
    # email scan reads a literal name@word.service as an address), so match
    # the instance suffixes next to that variable.
    text = (_ROOT / "scripts" / "uninstall.sh").read_text()
    assert text.count("genesis-tmp-watchgod.service") >= 2
    assert text.count("PRESSURE_UNIT=genesis-disk-hygiene-pressure") == 1
    assert text.count("P=genesis-disk-hygiene-pressure;") == 1
    for suffix in ("@standard.service", "@last-resort.service"):
        assert text.count("${PRESSURE_UNIT}" + suffix) == 1, f"in-container path misses {suffix}"
        assert text.count("\\${P}" + suffix) == 1, f"host-driven path misses {suffix}"


def test_the_domain_key_ignores_which_wall_binds(box):
    """Review finding: grouping by dg_measure's EFFECTIVE total split / and
    $HOME into two domains whenever a btrfs quota's crossover put them on
    different sides between two reads. The key comes from the mount and the
    RAW statvfs sizes, which a btrfs qgroup never changes."""
    assert os.stat("/").st_dev == os.stat(box["home"]).st_dev or pytest.skip("needs one device")
    st = _poll(box, {"/": "5000 267000 1 - - btrfs", str(box["home"]): "5000 357000 0 - - btrfs"})
    home_like = [p for p in st["disk"] if p in ("/", str(box["home"]))]
    assert home_like == ["/"], st["disk"].keys()


def test_episode_markers_of_a_vanished_domain_are_dropped(box):
    """Review NOTE: a marker left by a domain that no longer exists (a quota
    resized away) must not suppress that domain's page if it comes back."""
    box["state"].mkdir(parents=True, exist_ok=True)
    stale = box["state"] / "episode_4242q2048_red"
    stale.write_text("")
    _poll(box, {})
    assert not stale.exists()


# ── round-2 review findings (classes, not instances) ─────────────


def test_a_page_that_could_not_be_queued_is_retried_not_marked_sent(box):
    """Class B: the episode marker used to be written BEFORE the enqueue, and a
    failed enqueue was swallowed — at RED the queue lives on the full disk, so
    the one EMERGENCY page could be lost for the whole episode."""
    blocker = box["tmp"] / "not-a-dir"
    blocker.write_text("")
    _run(box, f"_ALERT_QUEUE_ROOT='{blocker}/queue'; handle_fs '{box['home']}' '{_home_dev(box)}' red 1000 100000 - '' btrfs")
    assert not list(box["state"].glob("episode_*_red")), "an undelivered page must not be marked sent"
    assert "could not queue the page" in _log(box)
    assert not _pages(box)
    _handle(box, "red")
    assert len([p for p in _pages(box) if p["title"].startswith("Disk nearly full")]) == 1, "retried next poll"


def test_a_failed_reclaim_start_is_retried_next_poll(box):
    """Class B: the rate-limit stamp was written before `systemctl start`, so
    one transient failure silenced reclaim for PRESSURE_RETRIGGER_S."""
    out = _run(box, """
        n=0
        systemctl() { n=$((n + 1)); return 1; }
        start_pressure_unit standard
        start_pressure_unit standard
        echo "attempts=$n"
    """)
    assert "attempts=2" in out.stdout
    assert not (box["state"] / "pressure_standard").exists()


def test_observe_mode_never_uses_up_an_acting_sweep(box):
    """Class D: the sweep stamp written by an observe-mode poll suppressed the
    first ACTING pressure sweep for up to 600 s after WATCHGOD_ACT flipped."""
    out = _run(box, """
        sweep_cc_tmp() { echo "SWEPT act=$WATCHGOD_ACT"; }
        WATCHGOD_ACT=0; maybe_sweep_cc_tmp pressure
        WATCHGOD_ACT=1; maybe_sweep_cc_tmp pressure
        WATCHGOD_ACT=1; maybe_sweep_cc_tmp pressure
    """)
    assert out.stdout.count("SWEPT act=0") == 1
    assert out.stdout.count("SWEPT act=1") == 1, "one acting sweep, then rate-limited"


def test_leading_zero_settings_are_decimal_not_octal_and_never_fatal(box):
    """Class H: "08" aborted the daemon ("value too great for base") and
    "0600" silently meant 384."""
    (box["home"] / ".genesis" / "config" / "watchgod.local.conf").write_text(
        "DG_ORANGE_PCT=08\nPRESSURE_RETRIGGER_S=0600\nCC_SWEEP_AGE_MIN=09\nDG_RED_MIN_MB=0000000000000000099\n"
    )
    out = _run(box, 'load_config; echo "$DG_ORANGE_PCT $PRESSURE_RETRIGGER_S $CC_SWEEP_AGE_MIN $DG_RED_MIN_MB"; dg_floor_tier 50 1000 - -')
    vals = out.stdout.split("\n")[0].split()
    assert vals == ["8", "600", "10080", "3072"], vals  # 09 < the 1-day floor; 19 digits is not a number


def test_cc_usage_survives_one_unreadable_entry(box):
    """Class H: du exits 1 on any unreadable entry but still prints the total;
    under pipefail the total was thrown away and 0 cached for 5 minutes."""
    _stub(box["bin"] / "du", '#!/usr/bin/env bash\nprintf "123\\t%s\\n" "${@: -1}"\nexit 1\n')
    (box["home"] / ".genesis" / "cc-tmp").mkdir()
    out = _run(box, 'CC_COMPAT="green 1000 5000 0"; SYS_COMPAT=""; DISK_JSON=""; write_state; cat "$STATE_FILE"')
    assert json.loads(out.stdout)["cc_tmp"]["used_mb"] == 123


def test_the_domain_key_does_not_depend_on_which_paths_were_seen(box):
    """Class A: the key used to be the bare device for whichever path came
    first, so a poll where `/` could not be measured re-keyed $HOME's domain
    (a new rate series, new episode markers). $HOME's key must be the same
    whether or not `/` was measured first."""
    probe = "dg_io_snapshot() { :; }; check_disks; echo \"HOME_KEY=$HOME_KEY\""
    blind_root = "dg_measure() { [[ \"$1\" == / ]] && return 1; echo '90000 100000 0 - - ext4'; }; "
    normal = "dg_measure() { echo '90000 100000 0 - - ext4'; }; "
    k1 = _run(box, blind_root + probe).stdout.strip().rsplit("HOME_KEY=", 1)[1]
    k2 = _run(box, normal + probe).stdout.strip().rsplit("HOME_KEY=", 1)[1]
    assert k1 and k1 == k2, (k1, k2)


def test_update_restarts_the_watchgod_only_after_a_successful_deploy():
    """Class F: update.sh restarted only server and bridge; daemon-reload does
    not make a running bash loop re-read its script, so an update never
    reached the watchgod. The restart sits after the success disarm, so a
    rolled-back deploy never leaves it on the new code."""
    text = (_ROOT / "scripts" / "update.sh").read_text()
    disarm = text.index("# ── Success: disarm trap")
    restart = text.index("_restart_tmp_watchgod_if_stale\n", disarm)
    assert disarm < restart < text.index("_guardian_resume\n", disarm)
    assert text.count("genesis-tmp-watchgod") >= 1


def test_the_daemon_survives_a_state_dir_it_cannot_write(box):
    """At RED the state directory's filesystem can be the full one: episode
    markers, stamps and the reserve release must fail soft, never exit the
    daemon under set -e."""
    state = box["state"]
    state.mkdir(parents=True, exist_ok=True)
    state.chmod(0o555)
    try:
        _handle(box, "red")      # asserts rc == 0
        _handle(box, "orange")
        _poll(box, {})
    finally:
        state.chmod(0o755)
    assert [p for p in _pages(box) if p["title"].startswith("Disk nearly full")], "the page still went out"


def test_the_domain_key_survives_a_moving_size(box):
    """Round-3 review BLOCKER: a ZFS dataset's reported size moves with its
    neighbours' usage. A size-based key re-keyed the domain every poll — a new
    EMERGENCY page each time. Keyed on the mount, a size that moves in step for
    the path and its mount changes nothing."""
    probe = "dg_io_snapshot() { :; }; check_disks; echo \"HOME_KEY=$HOME_KEY\""
    keys = []
    for size in (100000, 99999, 99998):
        out = _run(box, f"dg_raw_sizes_mb() {{ echo {size} {size}; }}; " + probe).stdout
        keys.append(out.strip().rsplit("HOME_KEY=", 1)[1])
    assert len(set(keys)) == 1, keys
    red = "dg_measure() { echo '100 100000 0 - - ext4'; }; "
    for size in (100000, 99999, 99998):
        _run(box, f"dg_raw_sizes_mb() {{ echo {size} {size}; }}; " + red + probe)
    assert len([p for p in _pages(box) if p["title"] == "Disk nearly full: / RED"]) == 1


def test_poll_intervals_are_normalised_settings(box):
    """Round-3 review: POLL_INTERVAL=08 aborted the daemon's arithmetic and
    restart-looped the unit; deleting the line never restored the default."""
    conf = box["home"] / ".genesis" / "config" / "watchgod.local.conf"
    conf.write_text("POLL_INTERVAL=08\nFAST_POLL_INTERVAL=abc\n")
    out = _run(box, 'load_config; echo "$POLL_INTERVAL $FAST_POLL_INTERVAL"; conf_restore=1')
    assert out.stdout.split()[:2] == ["8", "5"]
    out = _run(box, f'load_config; echo "$POLL_INTERVAL"; : > "{conf}"; load_config; echo "$POLL_INTERVAL"')
    assert out.stdout.split()[:2] == ["8", "30"], "removing the line restores the default"


def test_rate_series_of_a_vanished_domain_are_dropped(box):
    box["state"].mkdir(parents=True, exist_ok=True)
    stale = box["state"] / "rate_4242q2048_fs"
    stale.write_text("1 1 0 fs\n")
    _poll(box, {})
    assert stale.exists(), "a key gone for one poll keeps its series (grace)"
    old = time.time() - 2 * 3600
    os.utime(stale, (old, old))
    _poll(box, {})
    assert not stale.exists(), "gone for an hour: dropped"
    assert list(box["state"].glob("rate_*")), "control: current domains keep their series"


def test_a_stamp_from_the_future_does_not_silence_a_lever(box):
    """A clock stepped back must not hold the reclaim start off until it catches up."""
    out = _run(box, """
        mkdir -p "$DG_STATE_DIR"; echo $(( $(date +%s) + 100000 )) > "$DG_STATE_DIR/pressure_standard"
        if _wg_due pressure_standard 600; then echo DUE; else echo HELD; fi
        date +%s > "$DG_STATE_DIR/pressure_standard"
        if _wg_due pressure_standard 600; then echo DUE; else echo HELD; fi
    """)
    assert out.stdout.split() == ["DUE", "HELD"]


def test_update_restarts_a_stale_watchgod_even_with_nothing_to_merge():
    """Round-3 review: update.sh exited at "Nothing to do" before the restart,
    so a tree pulled by hand never reached the running daemon."""
    text = (_ROOT / "scripts" / "update.sh").read_text()
    nothing = text.index('    echo "  Nothing to do."')
    call = text.rindex("_restart_tmp_watchgod_if_stale\n", 0, nothing)
    assert nothing - call < 200, "called just before the no-op exit"
    assert text.index("_restart_tmp_watchgod_if_stale() {") < call


def test_a_one_poll_size_jitter_between_path_and_mount_does_not_rekey(box):
    """Final review: a ZFS commit between the two size reads made the path
    look like its own quota for one poll — a new key, a new page. Sizes
    within 1 % are one mount; a one-poll disappearance keeps the episode."""
    probe = "dg_io_snapshot() { :; }; check_disks; echo \"HOME_KEY=$HOME_KEY\""
    red = "dg_measure() { echo '100 100000 0 - - ext4'; }; "
    keys = []
    for sizes in ("100000 100000", "100000 99990", "100000 100000"):
        out = _run(box, f"dg_raw_sizes_mb() {{ echo {sizes}; }}; " + red + probe).stdout
        keys.append(out.strip().rsplit("HOME_KEY=", 1)[1])
    assert len(set(keys)) == 1, keys
    assert len([p for p in _pages(box) if p["title"] == "Disk nearly full: / RED"]) == 1


def test_dg_same_size_tolerance(box):
    out = _run(box, """
        for pair in "100000 100000" "100000 99001" "100000 98999" "2048 280000" "0 0"; do
            if dg_same_size $pair; then echo S; else echo D; fi
        done
    """).stdout.split()
    assert out == ["S", "S", "D", "D", "D"], out


def test_a_future_dated_usage_cache_is_not_trusted(box):
    """Final review: after a clock step-back a cached cc-tmp `du` from the
    future read as fresh forever, pinning used_mb to a stale value."""
    _stub(box["bin"] / "du", '#!/usr/bin/env bash\nprintf "123\\t%s\\n" "${@: -1}"\n')
    (box["home"] / ".genesis" / "cc-tmp").mkdir()
    box["state"].mkdir(parents=True, exist_ok=True)
    (box["state"] / "cc_used_du").write_text(f"{int(time.time()) + 100000} 7\n")
    out = _run(box, 'CC_COMPAT="green 1000 5000 0"; SYS_COMPAT=""; DISK_JSON=""; write_state; cat "$STATE_FILE"')
    assert json.loads(out.stdout)["cc_tmp"]["used_mb"] == 123


# ── #2521: round-3 review fixes (act-mode prerequisites) ─────────


def test_a_full_tiny_filesystem_still_reaches_red(box):
    """#2521 item 2: under ~25 MB the capped RED floor rounded to 0 MB, and
    `free < 0` is never true — a completely full one stayed ORANGE."""
    out = _run(box, "dg_floor_tier 0 20 - -; dg_floor_tier 0 512 - -").stdout.split()
    assert out == ["red", "red"], out


def test_a_new_episode_gets_its_levers_back(box):
    """#2521 item 3: a GREEN between two episodes left the reclaim cooldown
    running, so the second episode paged but reclaimed nothing."""
    _handle(box, "orange")
    _handle(box, "green", free=50_000, total=100_000)
    _handle(box, "orange")
    assert len([c for c in _calls(box) if "@standard" in c]) == 2


def test_green_without_an_episode_keeps_the_cooldown(box):
    """Control for the above: only the END of an episode resets the levers."""
    _handle(box, "orange")
    box["state"].joinpath(f"episode_{_home_dev(box)}_yellow").unlink()
    for p in box["state"].glob("episode_*"):
        p.unlink()
    _handle(box, "green", free=50_000, total=100_000)
    _handle(box, "orange")
    assert len([c for c in _calls(box) if "@standard" in c]) == 1


def test_the_warning_page_states_what_reclaim_actually_did(box):
    """#2521 item 4: the page said "started" even when the start failed."""
    _stub(box["bin"] / "systemctl", '#!/usr/bin/env bash\ncase "$*" in *"show -p MainPID"*) echo 0; exit 0;; esac\nexit 1\n')
    _handle(box, "orange")
    body = [p for p in _pages(box) if p["title"].startswith("Disk filling")][0]["body"]
    assert "could NOT start" in body and "started genesis" not in body


def test_pages_label_writers_as_process_wide(box):
    """#2521 item 5: /proc/<pid>/io is process-wide; a page must not present
    it as this filesystem's writers."""
    _handle(box, "red")
    body = [p for p in _pages(box) if p["title"].startswith("Disk nearly full")][0]["body"]
    assert "block-device writes on every filesystem; other users' processes are not visible" in body
    assert "whole machine" not in body, "dg_io_snapshot reads only own-user processes (#2570)"


def test_observe_mode_message_does_not_overclaim():
    """#2521 item 7: OOM capture still pages in observe mode."""
    text = _WATCHGOD.read_text()
    assert "no disk action is taken and no disk page is sent (OOM capture still pages)" in text
    assert "nothing is reclaimed, released or paged" not in text


def test_episode_end_resets_last_resort_and_observe_cooldowns(box):
    """Review of #2521 item 3: the reset covers the last-resort instance and
    the observe-mode stamps too, not only the standard instance."""
    _handle(box, "red")
    _handle(box, "green", free=50_000, total=100_000)
    _handle(box, "red")
    assert len([c for c in _calls(box) if "@last-resort" in c]) == 2
    _handle(box, "orange", act=0)
    assert (box["state"] / "pressure_standard_observe").exists()
    _handle(box, "green", free=50_000, total=100_000, act=0)
    assert not (box["state"] / "pressure_standard_observe").exists()


def test_episode_end_resets_the_cc_tmp_pressure_sweep(box, cctmp):
    root, _ = cctmp
    cc_dev = str(os.stat(root).st_dev)
    out = _run(box, f"""
        CC_KEY='{cc_dev}'; HOME_KEY=none
        sweep_cc_tmp() {{ echo SWEPT; }}
        handle_fs '{root}' '{cc_dev}' orange 100 2048 - '' btrfs
        handle_fs '{root}' '{cc_dev}' green 1900 2048 - '' btrfs
        handle_fs '{root}' '{cc_dev}' orange 100 2048 - '' btrfs
    """).stdout
    assert out.count("SWEPT") == 2, out



def test_a_sandbox_poll_never_allocates_more_than_the_test_reserve_cap(box):
    """A full poll against a large stubbed filesystem sizes the reserve at
    min(RESERVE_MAX_MB, 1 %) = 1 GB unless the sandbox caps it."""
    _poll(box, {"/": "90000 100000 0 - - ext4", str(box["home"]): "90000 100000 0 - - ext4"})
    reserve = box["state"] / "reserve"
    assert reserve.exists(), "control: the poll does create a reserve"
    assert reserve.stat().st_size <= _TEST_RESERVE_MAX_MB * 1024 * 1024


def test_a_config_reload_keeps_the_test_reserve_cap(box):
    """Review: load_config restores the startup baseline; the sandbox cap must
    survive a reload, or a reloading snippet fallocates the real default."""
    _run(box, f"load_config; handle_fs '{box['home']}' '{_home_dev(box)}' green 90000 100000 - '' btrfs")
    r = box["state"] / "reserve"
    assert r.exists() and r.stat().st_size <= _TEST_RESERVE_MAX_MB * 1024 * 1024


def test_the_warning_page_says_queued_not_started(box):
    """Review of #2570: `systemctl start --no-block` returning 0 proves only
    that the job was queued."""
    _handle(box, "orange")
    body = [p for p in _pages(box) if p["title"].startswith("Disk filling")][0]["body"]
    assert "queued genesis-disk-hygiene-pressure@standard" in body
    assert "started genesis" not in body


def test_the_sweep_spares_a_unit_that_holds_a_mount(box, cctmp):
    """Review of #2570: a same-device bind mount inside an aged cc-tmp unit
    passed --one-file-system; the sweep now checks the mount table first."""
    _, proj = cctmp
    s = proj / "bound"
    (s / "data").mkdir(parents=True)
    (s / "data" / "precious").write_text("x")
    _age_tree(s, 10)
    # The mount is BELOW the unit, so only the mount table can reveal it.
    mi = _mountinfo(box, ["/", s / "data"])
    _run(box, f"export TL_MOUNTINFO='{mi}'; sweep_cc_tmp 10080 test")
    assert (s / "data" / "precious").exists()
    assert "it is, or holds, a separate mount" in _log(box)


def test_the_sweep_counts_only_units_it_removed(box, cctmp):
    """Review of #2570: a failed rm was counted as reaped, so the page claimed
    reclaim that never happened."""
    if os.geteuid() == 0:
        pytest.skip("root removes read-only trees")
    root, proj = cctmp
    s = proj / "stuck"
    (s / "ro").mkdir(parents=True)
    (s / "ro" / "f").write_text("x")
    _age_tree(proj, 3)
    (s / "ro").chmod(0o555)
    try:
        dev = str(os.stat(root).st_dev)
        _run(box, f"home_dev() {{ echo not-this; }}; handle_fs '{root}' '{dev}' orange 100 2048 - '' btrfs")
        body = _pages(box)[0]["body"]
        assert "reaped 0 unit(s)" in body and "could NOT remove 1" in body
    finally:
        (s / "ro").chmod(0o755)


def _mountinfo(box, mounts) -> Path:
    """A crafted /proc/self/mountinfo for TL_MOUNTINFO (field 5 = mount point)."""
    f = box["tmp"] / "mountinfo"

    def esc(m) -> str:  # the kernel's octal escapes for field 5
        return (str(m).replace("\\", "\\134").replace(" ", "\\040")
                .replace("\t", "\\011").replace("\n", "\\012"))

    f.write_text("".join(f"{i} 1 0:{i} / {esc(m)} rw - x x rw\n" for i, m in enumerate(mounts, 20)))
    return f


def test_an_unreadable_mount_table_refuses_the_sweep_and_says_so(box, cctmp):
    """#2570 premise check: with no table, no unit can be proven free of
    mounts. The whole sweep refuses with its own outcome on the page -- never
    a per-unit "kept" that reads like real mounts were spared."""
    root, proj = cctmp
    s = proj / "dead"
    s.mkdir()
    _age_tree(s, 10)
    dev = str(os.stat(root).st_dev)
    _run(box, f"export TL_MOUNTINFO='{box['tmp'] / 'no-such-table'}'; home_dev() {{ echo not-this; }}; "
              f"handle_fs '{root}' '{dev}' orange 100 2048 - '' btrfs")
    assert s.exists(), "nothing is deleted without a table"
    assert "cc-tmp sweep REFUSED to run (mount table unreadable)" in _pages(box)[0]["body"]


def test_a_tmpfs_page_says_writers_cannot_be_attributed(box):
    """#2570 premise check: write_bytes never counts tmpfs writes, so on a
    tmpfs page the writer list would name processes that cannot be the
    culprit. Say that instead of listing them."""
    dev = _home_dev(box)
    _run(box, f"ALL_WRITERS='1 2 999 0 innocent'; handle_fs '{box['home']}' '{dev}' red 1 100000 - "
              f"'1 2 999 0 innocent' tmpfs")
    body = [p for p in _pages(box) if p["title"].startswith("Disk nearly full")][0]["body"]
    assert "Writer attribution unavailable: tmpfs writes are not counted" in body
    assert "innocent" not in body


def test_the_sweep_spares_sessions_inside_a_mounted_project(box, cctmp):
    """Review of #2570 round 3: the sweep walks claude-<uid>/<project>/<session>;
    a mount at <project> puts every session inside it, which a check for
    mounts at or below the candidate cannot see."""
    root, proj = cctmp
    s = proj / "old-session"
    s.mkdir()
    (s / "data").write_text("x")
    _age_tree(proj, 10)
    mi = _mountinfo(box, ["/", proj])
    _run(box, f"export TL_MOUNTINFO='{mi}'; sweep_cc_tmp 10080 test")
    assert (s / "data").exists(), "a session inside a mounted project is never deleted"
    assert "it is, or holds, a separate mount" in _log(box)


def test_a_retried_page_repeats_a_refused_sweep(box, cctmp):
    """Review of #2570 round 4: when the first page could not be queued, the
    retry came after the sweep's attempt stamp and said it "already ran",
    hiding that it had REFUSED. The retry repeats the last outcome."""
    r = _run(box, f"export TL_MOUNTINFO='{box['tmp'] / 'no-such-table'}'\n"
                  "maybe_sweep_cc_tmp pressure\nmaybe_sweep_cc_tmp pressure\n"
                  'printf "LEVER=%s\\n" "$_WG_LEVER_SWEEP"')
    lever = [ln for ln in r.stdout.splitlines() if ln.startswith("LEVER=")][-1]
    assert "last attempted" in lever and "REFUSED to run (mount table unreadable)" in lever, lever
