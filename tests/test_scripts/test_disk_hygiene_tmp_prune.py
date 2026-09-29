"""Tests for the ~/tmp age-prune in scripts/disk_hygiene.sh (the ``prune_tmp`` fn).

Sources the script (which only DEFINES functions when sourced — ``main`` is guarded) and
calls ``prune_tmp`` against a fixture dir, mirroring test_watchgod_instrumentation's
source-and-call pattern. Age is set via os.utime -> wall-clock-independent.
"""

import os
import subprocess
import time
from pathlib import Path

import pytest

_HYGIENE = Path(__file__).resolve().parents[2] / "scripts" / "disk_hygiene.sh"


def _age(p: Path, days: float) -> None:
    t = time.time() - days * 86400
    os.utime(p, (t, t))


def _run_prune(tmp_dir: Path) -> subprocess.CompletedProcess:
    """Source disk_hygiene.sh (defines functions, does NOT run main) then call prune_tmp."""
    return subprocess.run(
        ["bash", "-c", f"source '{_HYGIENE}'\nprune_tmp '{tmp_dir}'"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )


def test_old_file_pruned(tmp_path):
    d = tmp_path / "tmp"
    d.mkdir()
    old = d / "old_job.log"
    old.write_text("x")
    _age(old, 10)
    _run_prune(d)
    assert not old.exists()


def test_old_dir_pruned_whole(tmp_path):
    d = tmp_path / "tmp"
    d.mkdir()
    old = d / "old_job"
    old.mkdir()
    (old / "inner").write_text("x")
    _age(old / "inner", 10)
    _age(old, 10)  # set dir mtime AFTER creating contents
    _run_prune(d)
    assert not old.exists()


def test_recent_file_kept(tmp_path):
    d = tmp_path / "tmp"
    d.mkdir()
    fresh = d / "running.log"
    fresh.write_text("x")
    _age(fresh, 1)
    _run_prune(d)
    assert fresh.exists()


def test_bg_cc_sessions_excluded(tmp_path):
    """bg-cc-sessions is reaped separately at 24h — the 7d prune must never touch it."""
    d = tmp_path / "tmp"
    d.mkdir()
    bg = d / "bg-cc-sessions"
    bg.mkdir()
    (bg / "sess").mkdir()
    _age(bg, 30)
    _run_prune(d)
    assert bg.exists(), "bg-cc-sessions must be excluded from the 7d prune"


def test_missing_dir_is_noop(tmp_path):
    r = _run_prune(tmp_path / "does_not_exist")
    assert r.returncode == 0


# ── liveness: an old child still held open is spared (watchgod v2) ────────
#
# The pressure mode prunes at 2 days, where "a download that started days ago
# is still running" stops being hypothetical. The holder is a real process with
# a real open descriptor, so the prune reads /proc exactly as it does live.


def _hold_open(path: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        ["bash", "-c", f"exec 3>>'{path}'; echo ready; sleep 60"],
        stdout=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL,
    )
    assert proc.stdout.readline().strip() == "ready"
    return proc


def _run_prune_args(tmp_dir: Path, *args: str) -> subprocess.CompletedProcess:
    quoted = " ".join(f"'{a}'" for a in args)
    return subprocess.run(
        ["bash", "-c", f"source '{_HYGIENE}'\nprune_tmp '{tmp_dir}' {quoted}"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )


def test_old_dir_with_a_live_writer_is_spared(tmp_path):
    d = tmp_path / "tmp"
    d.mkdir()
    job = d / "long_download"
    job.mkdir()
    part = job / "big.part"
    part.write_text("x")
    holder = _hold_open(part)
    try:
        _age(part, 10)
        _age(job, 10)
        dead = d / "dead_job"
        dead.mkdir()
        _age(dead, 10)
        r = _run_prune(d)
        assert job.exists(), r.stdout + r.stderr
        assert "sparing" in r.stdout
        assert not dead.exists(), "the control arm: an unheld old dir is still pruned"
    finally:
        holder.kill()
        holder.wait()


def test_old_file_with_a_live_writer_is_spared(tmp_path):
    d = tmp_path / "tmp"
    d.mkdir()
    f = d / "capture.bin"
    f.write_text("x")
    holder = _hold_open(f)
    try:
        _age(f, 10)
        _run_prune(d)
        assert f.exists()
    finally:
        holder.kill()
        holder.wait()
    _run_prune(d)
    assert not f.exists(), "once the writer is gone the same file is pruned"


def test_pressure_age_cut_is_minutes(tmp_path):
    d = tmp_path / "tmp"
    d.mkdir()
    three = d / "three_days"
    three.write_text("x")
    _age(three, 3)
    one = d / "one_day"
    one.write_text("x")
    _age(one, 1)
    _run_prune_args(d, "2880")
    assert not three.exists()
    assert one.exists()


def test_pressure_age_cut_still_skips_bg_cc_sessions(tmp_path):
    d = tmp_path / "tmp"
    d.mkdir()
    bg = d / "bg-cc-sessions"
    bg.mkdir()
    _age(bg, 5)
    _run_prune_args(d, "2880")
    assert bg.exists()


def test_a_name_with_spaces_and_newline_is_pruned_and_spared_correctly(tmp_path):
    d = tmp_path / "tmp"
    d.mkdir()
    odd = d / "a b\nc"
    odd.write_text("x")
    _age(odd, 10)
    _run_prune(d)
    assert not odd.exists()


def test_unknown_argument_is_refused(tmp_path):
    r = subprocess.run(
        ["bash", "-c", f"source '{_HYGIENE}'\nVENV_PY=/bin/true main --bogus"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    assert r.returncode == 2
    assert "unknown argument" in r.stderr


def test_old_dir_that_is_a_process_cwd_is_spared(tmp_path):
    """A job running FROM ~/tmp/<job> with no descriptor open inside it."""
    d = tmp_path / "tmp"
    d.mkdir()
    job = d / "runner"
    job.mkdir()
    proc = subprocess.Popen(["bash", "-c", "echo ready; sleep 60"], cwd=job,
                            stdout=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL)
    assert proc.stdout.readline().strip() == "ready"
    try:
        _age(job, 10)
        _run_prune(d)
        assert job.exists()
    finally:
        proc.kill()
        proc.wait()
    _run_prune(d)
    assert not job.exists()


def test_an_old_dir_written_deep_inside_is_kept(tmp_path):
    """A directory's OWN mtime moves only when direct children change; a tree
    written deep inside (a session scratchpad under ~/tmp/claude-<uid>/) must
    be judged by its newest content, not its top-level mtime."""
    d = tmp_path / "tmp"
    d.mkdir()
    tree = d / "claude-1000"
    deep = tree / "proj" / "session" / "scratchpad"
    deep.mkdir(parents=True)
    (deep / "today.txt").write_text("x")
    for p in (tree / "proj" / "session", tree / "proj", tree):
        _age(p, 10)
    _run_prune(d)
    assert (deep / "today.txt").exists()
    _age(deep / "today.txt", 10)
    _age(deep, 10)
    _run_prune(d)
    assert not tree.exists(), "once nothing inside is recent, it goes"


def _pressure(tmp_path: Path, lock: Path) -> float:
    """Run the real pressure_main (disk_reclaim stubbed out, a sandbox HOME)
    and return how long it took."""
    home = tmp_path / "home"
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, HOME=str(home), RECLAIM_LOCK=str(lock))
    t0 = time.monotonic()
    r = subprocess.run(
        ["bash", "-c", f"source '{_HYGIENE}'\nVENV_PY=/bin/true\npressure_main last-resort"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, env=env, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert "PRESSURE done" in r.stdout
    return time.monotonic() - t0


def test_pressure_reclaim_waits_for_a_run_already_holding_the_lock(tmp_path):
    """Codex P2 / Devin: systemd serializes one unit, not the two pressure
    instances or the daily groom. A last-resort run arriving while another
    reclaim holds the shared lock waits for it, then runs (not lost)."""
    lock = tmp_path / "reclaim.lock"
    assert _pressure(tmp_path, lock) < 2.5, "control: uncontended, it does not wait"
    holder = subprocess.Popen(["flock", str(lock), "sleep", "3"])
    try:
        deadline = time.time() + 5
        while subprocess.run(["flock", "-n", str(lock), "true"]).returncode == 0:
            if time.time() > deadline:
                pytest.fail("fixture precondition: the holder never took the lock")
            time.sleep(0.05)
        assert _pressure(tmp_path, lock) >= 2.0, "it ran while another reclaim held the lock"
    finally:
        holder.kill()
        holder.wait()


def test_the_daily_groom_takes_the_same_lock_around_its_deleting_steps():
    """The daily unit deletes the same trees; it must hold the lock across
    cache reclamation, sandbox reaping and the ~/tmp prune."""
    text = _HYGIENE.read_text()
    main = text[text.index("\nmain() {"):]
    lock_at = main.index("if ! reclaim_lock ")
    assert lock_at < main.index("disk_reclaim.py") < main.index('prune_tmp "$HOME/tmp"') < main.index("reclaim_unlock")


def _pressure_out(tmp_path: Path, lock: Path, tier: str, wait_s: int) -> str:
    home = tmp_path / "home"
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, HOME=str(home), RECLAIM_LOCK=str(lock), RECLAIM_WAIT_S=str(wait_s))
    r = subprocess.run(
        ["bash", "-c", f"source '{_HYGIENE}'\nVENV_PY=/bin/true\npressure_main {tier}"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, env=env, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_lock_waits_are_bounded_last_resort_runs_standard_yields(tmp_path):
    """Review finding: an unbounded wait could outlast the unit's timeout. At
    RED the last-resort pass runs anyway once its wait expires; a standard
    pass yields to the reclaim already running."""
    lock = tmp_path / "reclaim.lock"
    holder = subprocess.Popen(["flock", str(lock), "sleep", "20"])
    try:
        deadline = time.time() + 5
        while subprocess.run(["flock", "-n", str(lock), "true"]).returncode == 0:
            if time.time() > deadline:
                pytest.fail("fixture precondition: the holder never took the lock")
            time.sleep(0.05)
        lr = _pressure_out(tmp_path, lock, "last-resort", 1)
        assert "last-resort runs unserialized" in lr and "cache reclamation" in lr
        std = _pressure_out(tmp_path, lock, "standard", 1)
        assert "skipping this standard pass" in std and "cache reclamation" not in std
    finally:
        holder.kill()
        holder.wait()


def test_a_blind_liveness_view_refuses_the_prune(tmp_path):
    """Devin severe / Codex P2: a /proc showing no other process proves nothing
    about what is in use; the prune must refuse rather than delete old jobs."""
    d = tmp_path / "tmp"
    d.mkdir()
    old = d / "old_job"
    old.mkdir()
    _age(old, 10)
    blind = tmp_path / "noproc"
    blind.mkdir()
    r = subprocess.run(
        ["bash", "-c", f"source '{_HYGIENE}'\nTL_PROC='{blind}'\nprune_tmp '{d}'"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    assert old.exists()
    assert "prune SKIPPED" in r.stdout
    _run_prune(d)
    assert not old.exists(), "control: with a normal /proc the same dir is pruned"


def _reap(sandboxes: Path, extra: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f"source '{_HYGIENE}'\n{extra}\nreap_bg_sandboxes '{sandboxes}'"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )


def test_bg_sandbox_reap_spares_one_still_in_use(tmp_path):
    """Devin: a surviving child of an ended background session can still run
    in its sandbox; the 24 h reap now spares anything held or used as a cwd."""
    root = tmp_path / "bg-cc-sessions"
    live, dead = root / "live", root / "dead"
    live.mkdir(parents=True)
    dead.mkdir()
    holder = subprocess.Popen(["sleep", "60"], cwd=live)
    try:
        _age(live, 2)
        _age(dead, 2)
        r = _reap(root)
        assert live.exists(), r.stdout
        assert not dead.exists()
        assert "sparing" in r.stdout
    finally:
        holder.kill()
        holder.wait()


def test_bg_sandbox_reap_refuses_when_blind(tmp_path):
    root = tmp_path / "bg-cc-sessions"
    dead = root / "dead"
    dead.mkdir(parents=True)
    _age(dead, 2)
    blind = tmp_path / "noproc"
    blind.mkdir()
    r = _reap(root, f"TL_PROC='{blind}'")
    assert dead.exists() and "SKIPPED" in r.stdout


def test_seeing_only_yourself_in_proc_is_blind(tmp_path):
    """A /proc that shows only the calling process (the snapshot's own find
    would still find its own descriptors) proves nothing: blind. Control: one
    other readable process makes it visible."""
    fake = tmp_path / "proc"
    snippet = (
        f"source '{_HYGIENE}'\nTL_PROC='{fake}'\nmkdir -p \"$TL_PROC/$$/fd\"\n"
        "if liveness_visible; then echo VISIBLE; else echo BLIND; fi\n"
        "mkdir -p \"$TL_PROC/4242424/fd\"\n"
        "if liveness_visible; then echo VISIBLE; else echo BLIND; fi\n"
    )
    r = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert r.stdout.split() == ["BLIND", "VISIBLE"], r.stdout + r.stderr


def test_standard_pressure_never_clears_the_code_intel_indexes(tmp_path):
    """#2521 item 1: left to disk_reclaim.py's 95 % default, an ORANGE pass
    on a >=95 % disk deleted the index DBs before RED. Only last-resort may."""
    home = tmp_path / "home"
    (home / "tmp").mkdir(parents=True)
    log = tmp_path / "args"
    fake = tmp_path / "fake_py"
    fake.write_text(f'#!/usr/bin/env bash\necho "$*" >> "{log}"\n')
    fake.chmod(0o755)
    env = dict(os.environ, HOME=str(home), RECLAIM_LOCK=str(tmp_path / "lock"))
    for tier in ("standard", "last-resort"):
        subprocess.run(["bash", "-c", f"source '{_HYGIENE}'\nVENV_PY='{fake}'\npressure_main {tier}"],
                       env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, check=True)
    std, lr = log.read_text().splitlines()
    assert "--last-resort-above 101" in std
    assert "--last-resort-above 0" in lr


def _stat_shim(tmp_path: Path, other_dev_path: Path) -> Path:
    """A `stat` that reports a different device for one path — a stand-in for a
    separate mount, which a test cannot create without root."""
    bindir = tmp_path / "shim"
    bindir.mkdir(exist_ok=True)
    shim = bindir / "stat"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'if [[ "$*" == *"%d"* && "${{@: -1}}" == "{other_dev_path}" ]]; then echo 999999; exit 0; fi\n'
        'exec /usr/bin/stat "$@"\n'
    )
    shim.chmod(0o755)
    return bindir


def test_prune_never_recurses_into_a_separate_filesystem(tmp_path):
    """#2521 item 6: a direct child that is its own mount frees nothing here."""
    d = tmp_path / "tmp"
    d.mkdir()
    mnt, plain = d / "downloads", d / "old_job"
    mnt.mkdir()
    plain.mkdir()
    (mnt / "keep.bin").write_text("x")
    for p in (mnt / "keep.bin", mnt, plain):
        _age(p, 10)
    shim = _stat_shim(tmp_path, mnt.resolve())
    env = dict(os.environ, PATH=f"{shim}:{os.environ['PATH']}")
    r = subprocess.run(["bash", "-c", f"source '{_HYGIENE}'\nprune_tmp '{d}'"],
                       env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert mnt.exists() and (mnt / "keep.bin").exists(), r.stdout + r.stderr
    assert "a separate filesystem" in r.stdout
    assert not plain.exists(), "control: an ordinary old child is still pruned"


def test_bg_sandbox_reap_never_recurses_into_a_separate_filesystem(tmp_path):
    root = tmp_path / "bg-cc-sessions"
    mnt, dead = root / "mounted", root / "dead"
    mnt.mkdir(parents=True)
    dead.mkdir()
    for p in (mnt, dead):
        _age(p, 2)
    shim = _stat_shim(tmp_path, mnt.resolve())
    r = subprocess.run(["bash", "-c", f"source '{_HYGIENE}'\nreap_bg_sandboxes '{root}'"],
                       env=dict(os.environ, PATH=f"{shim}:{os.environ['PATH']}"),
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert mnt.exists() and "a separate filesystem" in r.stdout, r.stdout + r.stderr
    assert not dead.exists(), "control: an ordinary old sandbox is still reaped"


def _mount_shim(tmp_path: Path, mountpoint_path: Path | None = None, table: list[str] | None = None) -> Path:
    """`mountpoint` / `findmnt` stand-ins: a bind mount keeps its parent's
    device number, so only the mount table can see it."""
    bindir = tmp_path / "mshim"
    bindir.mkdir(exist_ok=True)
    mp = bindir / "mountpoint"
    target = str(mountpoint_path) if mountpoint_path else "/nonexistent-mount"
    mp.write_text(f'#!/usr/bin/env bash\n[[ "${{@: -1}}" == "{target}" ]] && exit 0\nexit 1\n')
    mp.chmod(0o755)
    fm = bindir / "findmnt"
    fm.write_text("#!/usr/bin/env bash\ncat <<'EOF'\n/\n" + "".join(f"{t}\n" for t in (table or [])) + "EOF\n")
    fm.chmod(0o755)
    return bindir


def test_prune_spares_a_bind_mount_the_device_number_cannot_see(tmp_path):
    """Review of #2521 item 6: a bind mount (or an incus dir-pool volume) keeps
    its parent's device; the mount table is what tells."""
    d = tmp_path / "tmp"
    d.mkdir()
    bind, plain = d / "downloads", d / "old_job"
    for p in (bind, plain):
        p.mkdir()
        _age(p, 10)
    shim = _mount_shim(tmp_path, mountpoint_path=bind.resolve())
    r = subprocess.run(["bash", "-c", f"source '{_HYGIENE}'\nprune_tmp '{d}'"],
                       env=dict(os.environ, PATH=f"{shim}:{os.environ['PATH']}"),
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert bind.exists() and "a separate filesystem" in r.stdout, r.stdout + r.stderr
    assert not plain.exists()


def test_prune_spares_a_child_with_a_mount_inside_it(tmp_path):
    d = tmp_path / "tmp"
    d.mkdir()
    outer = d / "job"
    (outer / "data").mkdir(parents=True)
    _age(outer / "data", 10)
    _age(outer, 10)
    shim = _mount_shim(tmp_path, table=[str((outer / "data").resolve())])
    r = subprocess.run(["bash", "-c", f"source '{_HYGIENE}'\nprune_tmp '{d}'"],
                       env=dict(os.environ, PATH=f"{shim}:{os.environ['PATH']}"),
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert outer.exists() and "a separate filesystem" in r.stdout, r.stdout + r.stderr


def test_bg_sandbox_reap_spares_a_bind_mount(tmp_path):
    root = tmp_path / "bg-cc-sessions"
    bind, dead = root / "bound", root / "dead"
    for p in (bind, dead):
        p.mkdir(parents=True)
        _age(p, 2)
    shim = _mount_shim(tmp_path, mountpoint_path=bind.resolve())
    r = subprocess.run(["bash", "-c", f"source '{_HYGIENE}'\nreap_bg_sandboxes '{root}'"],
                       env=dict(os.environ, PATH=f"{shim}:{os.environ['PATH']}"),
                       capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert bind.exists() and "a separate filesystem" in r.stdout, r.stdout + r.stderr
    assert not dead.exists()


def test_the_daily_groom_leaves_the_indexes_to_the_guardians_red_pass():
    """Review of #2521 item 1: the daily groom's disk_reclaim call must not
    fall back to the 95 % last-resort default either."""
    text = _HYGIENE.read_text()
    main = text[text.index("\nmain() {"):]
    call = main[main.index("disk_reclaim.py"):main.index("disk_reclaim_rc=$?")]
    assert "--last-resort-above 101" in call
