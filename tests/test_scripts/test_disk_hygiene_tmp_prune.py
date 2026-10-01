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
    other readable process, outside the caller's tree, makes it visible."""
    fake = tmp_path / "proc"
    snippet = (
        f"source '{_HYGIENE}'\nTL_PROC='{fake}'\nmkdir -p \"$TL_PROC/$$/fd\"\n"
        "if liveness_visible; then echo VISIBLE; else echo BLIND; fi\n"
        "mkdir -p \"$TL_PROC/4242424/fd\"\n"
        "printf 'State:\\tS (x)\\nPPid:\\t1\\n' > \"$TL_PROC/4242424/status\"\n"
        "if liveness_visible; then echo VISIBLE; else echo BLIND; fi\n"
    )
    r = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert r.stdout.split() == ["BLIND", "VISIBLE"], r.stdout + r.stderr


def _liveness_with(tmp_path, procs: str) -> str:
    """Run liveness_visible against a fake /proc. `procs` is bash that adds
    entries with `fake <pid> <ppid> [state]`; "$$" is the caller. The caller's
    own entry always exists, as it does in a real /proc."""
    fake = tmp_path / "proc"
    snippet = (
        f"source '{_HYGIENE}'\nTL_PROC='{fake}'\n"
        'fake() { mkdir -p "$TL_PROC/$1/fd"; '
        "printf 'Name:\\tx\\nState:\\t%s (x)\\nPPid:\\t%s\\n' \"${3:-S}\" \"$2\" "
        '> "$TL_PROC/$1/status"; }\n'
        'fake $$ 1\n'
        f"{procs}\n"
        "if liveness_visible; then echo VISIBLE; else echo BLIND; fi\n"
    )
    r = subprocess.run(["bash", "-c", snippet], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_seeing_only_your_own_child_is_blind(tmp_path):
    # #2515: in a private PID namespace /proc shows only the caller's tree.
    assert _liveness_with(tmp_path, "fake 4242 $$") == "BLIND"


def test_seeing_only_your_own_grandchild_is_blind(tmp_path):
    assert _liveness_with(tmp_path, "fake 4242 $$\nfake 4243 4242") == "BLIND"


def test_an_unrelated_process_beside_your_child_is_visible(tmp_path):
    assert _liveness_with(tmp_path, "fake 4242 $$\nfake 5151 1") == "VISIBLE"


def test_a_process_that_vanishes_mid_check_proves_nothing(tmp_path):
    # Its fd/ was readable, then its status was gone: it exited between the two
    # reads. That is no evidence of another live process (review of #2515).
    assert _liveness_with(tmp_path, 'mkdir -p "$TL_PROC/5151/fd"') == "BLIND"


def test_a_chain_whose_ancestor_vanished_proves_nothing(tmp_path):
    # 4243's parent 4242 has an fd/ but no status: reaped mid-walk.
    procs = 'mkdir -p "$TL_PROC/4242/fd"\nfake 4243 4242'
    assert _liveness_with(tmp_path, procs) == "BLIND"


def test_an_unrelated_zombie_proves_nothing(tmp_path):
    assert _liveness_with(tmp_path, "fake 5151 1 Z") == "BLIND"


def test_a_process_whose_chain_ends_at_ppid_0_is_visible(tmp_path):
    # A parent outside the namespace reads as PPid 0: not ours.
    assert _liveness_with(tmp_path, "fake 5151 0") == "VISIBLE"


def _can_unshare_pid() -> bool:
    try:
        r = subprocess.run(
            ["sudo", "-n", "unshare", "-pf", "--mount-proc", "true"],
            capture_output=True, stdin=subprocess.DEVNULL, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def test_in_a_real_private_pid_namespace_your_own_child_is_blind():
    # The #2515 scenario for real: inside its own PID namespace the caller is
    # pid 1 and /proc shows only its own tree, so its child's PPid is 1 — which
    # must read as OURS (the $$ match is checked before the PPid<=1 stop).
    # Probed here rather than in a skipif, so the sudo call runs only when this
    # test is selected, not at every collection of the module.
    if not _can_unshare_pid():
        pytest.skip("needs a private PID namespace (sudo -n unshare -pf --mount-proc)")
    lib = _HYGIENE.parent / "lib" / "tmp_liveness.sh"
    snippet = (
        f"source '{lib}'\n"
        "sleep 20 & c=$!\n"
        'if liveness_visible; then echo "$$ VISIBLE"; else echo "$$ BLIND"; fi\n'
        'kill "$c"\n'
    )
    r = subprocess.run(
        ["sudo", "-n", "unshare", "-pf", "--mount-proc", "bash", "-c", snippet],
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60,
    )
    assert r.stdout.split() == ["1", "BLIND"], r.stdout + r.stderr


def test_a_chain_deeper_than_the_bound_terminates_and_counts(tmp_path):
    # 70 generations under the caller: past the 64-hop bound the walk gives up
    # and counts the process (visible) rather than looping or hanging.
    chain = "p=$$; for i in $(seq 1 70); do fake $((6000+i)) $p; p=$((6000+i)); done"
    assert _liveness_with(tmp_path, chain) == "VISIBLE"


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


def _mountinfo(tmp_path: Path, mounts=(), readable: bool = True) -> dict:
    """The env for a crafted mount table (TL_MOUNTINFO, /proc/self/mountinfo
    format, field 5 octal-escaped like the kernel's). A separate filesystem and
    a bind mount are both just ENTRIES here, which is the point: the table sees
    what a device number cannot. readable=False leaves no file at all."""
    f = tmp_path / "mountinfo"

    def esc(m) -> str:
        return (str(m).replace("\\", "\\134").replace(" ", "\\040")
                .replace("\t", "\\011").replace("\n", "\\012"))

    if readable:
        f.write_text("".join(f"{i} 1 0:{i} / {esc(m)} rw - x x rw\n" for i, m in enumerate(["/", *mounts], 20)))
    return dict(os.environ, TL_MOUNTINFO=str(f))


def _hyg(call: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", f"source '{_HYGIENE}'\n{call}"], env=env,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)


def test_prune_never_recurses_into_a_separate_filesystem(tmp_path):
    """#2521 item 6: a direct child that is its own mount frees nothing here."""
    d = tmp_path / "tmp"
    d.mkdir()
    mnt, plain = d / "downloads", d / "old_job"
    mnt.mkdir()
    plain.mkdir()
    (mnt / "keep.bin").write_text("x")
    for q in (mnt / "keep.bin", mnt, plain):
        _age(q, 10)
    r = _hyg(f"prune_tmp '{d}'", _mountinfo(tmp_path, [mnt.resolve()]))
    assert mnt.exists() and (mnt / "keep.bin").exists(), r.stdout + r.stderr
    assert "a separate filesystem" in r.stdout
    assert not plain.exists(), "control: an ordinary old child is still pruned"


def test_bg_sandbox_reap_never_recurses_into_a_separate_filesystem(tmp_path):
    root = tmp_path / "bg-cc-sessions"
    mnt, dead = root / "mounted", root / "dead"
    mnt.mkdir(parents=True)
    dead.mkdir()
    for q in (mnt, dead):
        _age(q, 2)
    r = _hyg(f"reap_bg_sandboxes '{root}'", _mountinfo(tmp_path, [mnt.resolve()]))
    assert mnt.exists() and "a separate filesystem" in r.stdout, r.stdout + r.stderr
    assert not dead.exists(), "control: an ordinary old sandbox is still reaped"


def test_prune_spares_a_child_with_a_mount_inside_it(tmp_path):
    """The mount is BELOW the child, which only the table can reveal."""
    d = tmp_path / "tmp"
    d.mkdir()
    outer = d / "job"
    (outer / "data").mkdir(parents=True)
    _age(outer / "data", 10)
    _age(outer, 10)
    r = _hyg(f"prune_tmp '{d}'", _mountinfo(tmp_path, [(outer / "data").resolve()]))
    assert outer.exists() and "a separate filesystem" in r.stdout, r.stdout + r.stderr


def test_bg_sandbox_reap_spares_a_mount_below_a_sandbox(tmp_path):
    root = tmp_path / "bg-cc-sessions"
    bound, dead = root / "bound", root / "dead"
    (bound / "data").mkdir(parents=True)
    dead.mkdir()
    for q in (bound / "data", bound, dead):
        _age(q, 2)
    r = _hyg(f"reap_bg_sandboxes '{root}'", _mountinfo(tmp_path, [(bound / "data").resolve()]))
    assert bound.exists() and "a separate filesystem" in r.stdout, r.stdout + r.stderr
    assert not dead.exists()


def test_an_unreadable_mount_table_refuses_every_prune_with_a_reason(tmp_path):
    """#2570 premise check: with no table, nothing can be proven free of
    mounts, so the WHOLE pass refuses and says why -- never a per-candidate
    "spared" that reads like real mounts, and never a fallback that cannot
    see a mount below a candidate."""
    d = tmp_path / "tmp"
    old = d / "old_job"
    old.mkdir(parents=True)
    _age(old, 10)
    root = tmp_path / "bg-cc-sessions"
    dead = root / "dead"
    dead.mkdir(parents=True)
    _age(dead, 2)
    env = _mountinfo(tmp_path, readable=False)
    r1 = _hyg(f"prune_tmp '{d}'", env)
    r2 = _hyg(f"reap_bg_sandboxes '{root}'", env)
    assert old.exists() and dead.exists(), r1.stdout + r2.stdout
    assert "tmp prune SKIPPED: the mount table" in r1.stdout
    assert "bg-cc sandbox reap SKIPPED: the mount table" in r2.stdout


def test_the_daily_groom_leaves_the_indexes_to_the_guardians_red_pass():
    """Review of #2521 item 1: the daily groom's disk_reclaim call must not
    fall back to the 95 % last-resort default either."""
    text = _HYGIENE.read_text()
    main = text[text.index("\nmain() {"):]
    call = main[main.index("disk_reclaim.py"):main.index("disk_reclaim_rc=$?")]
    assert "--last-resort-above 101" in call


def test_every_recursive_delete_goes_through_the_mount_guard():
    """Review of #2570: the mount-table guard existed in two of four recursive
    deleters, so the cc-tmp sweep and the sessions prune could follow a
    same-device bind mount. Allowlist polarity: the ONLY recursive `rm` in the
    guardian's scripts is the one inside remove_tree_one_fs, so a deleter
    added later fails here until it uses the helper."""
    import re

    scripts = Path(__file__).resolve().parents[2] / "scripts"
    lib = scripts / "lib"
    files = [scripts / "disk_hygiene.sh", scripts / "tmp_watchgod.sh",
             *(lib / n for n in ("tmp_liveness.sh", "disk_guardian.sh", "watchgod_oom.sh", "alert_queue.sh"))]
    # Short (-r, -rf, -Rf) and long (--recursive) spellings, after any
    # number of other flags (review of #2570: --recursive was missed).
    rm_r = re.compile(r"(?<![\w-])rm\s+(?:-\S*\s+)*(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)(?![\w-])")
    # A comment starts at a # that opens the line or follows whitespace;
    # `$#` and `${#arr[@]}` are code, and must not hide what follows them.
    comment = re.compile(r"(?:^|\s)#.*$")
    hits = []
    for f in files:
        for n, line in enumerate(f.read_text().splitlines(), 1):
            code = comment.sub("", line)
            if rm_r.search(code):
                hits.append(f"{f.name}:{n}")
    helper = scripts / "lib" / "tmp_liveness.sh"
    body = helper.read_text()
    start = body.index("remove_tree_one_fs() {")
    end = body.index("\n}\n", start)
    first = body[:start].count("\n") + 1
    last = body[:end].count("\n") + 1
    inside = [h for h in hits if h.startswith("tmp_liveness.sh:") and first <= int(h.split(":")[1]) <= last]
    assert inside, "the helper itself must perform the removal"
    assert sorted(set(hits) - set(inside)) == [], hits


def test_the_recursive_delete_scan_sees_every_spelling():
    """Control for the allowlist test: its detector must catch the spellings
    a later deleter could use, or the allowlist passes vacuously."""
    import re

    rm_r = re.compile(r"(?<![\w-])rm\s+(?:-\S*\s+)*(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)(?![\w-])")
    comment = re.compile(r"(?:^|\s)#.*$")
    for line in ('rm -rf -- "$x"', 'rm -f --recursive "$x"', 'rm --recursive "$x"',
                 'n=${#arr[@]}; rm -Rf "$x"', '[ $# -gt 0 ] && rm -r "$x"'):
        assert rm_r.search(comment.sub("", line)), line
    for line in ('rm -f -- "$x"', '# rm -rf "$x"', 'echo x  # rm -rf "$x"', 'find . -delete'):
        assert not rm_r.search(comment.sub("", line)), line
