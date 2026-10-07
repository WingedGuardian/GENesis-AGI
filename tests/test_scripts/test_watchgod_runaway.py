"""scripts/lib/watchgod_runaway.sh: naming the file that is filling a disk.

Drives the REAL functions (tmp_watchgod.sh sourced; `main` is guarded) against
a fake process table (DG_PROC) whose fd links point at real sparse files, with
the durable alert queue pointed at a tmp dir.

What these pin:
  * a file held open for WRITING that alone is a large share of its filesystem
    pages CRITICAL, naming the file and the processes holding it;
  * growth of a large share between two polls pages, and asks for the fast
    poll interval; a first sighting never counts as growth;
  * a reader, a small file, and a file on an unwatched filesystem page nothing;
  * one page per file per mode, and observe mode still sends it;
  * a file under cc-tmp names its Claude Code session;
  * detection changes nothing: no signal, no file touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_WATCHGOD = _ROOT / "scripts" / "tmp_watchgod.sh"
_MB = 1024 * 1024


@pytest.fixture
def box(tmp_path):
    home = tmp_path / "home"
    (home / ".genesis" / "logs").mkdir(parents=True)
    (home / ".genesis" / "config").mkdir(parents=True)
    proc = tmp_path / "proc"
    proc.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    return {"home": home, "proc": proc, "data": data, "queue": tmp_path / "queue", "tmp": tmp_path}


def _file(box, rel: str, mb: int) -> Path:
    """A file USING `mb` MiB: blocks allocated (fallocate, fast, no data written),
    because the detector measures allocated space, never apparent length."""
    path = box["data"] / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as fh:
        os.posix_fallocate(fh.fileno(), 0, mb * _MB)
    return path


def _sparse(box, rel: str, mb: int) -> Path:
    """A file whose LENGTH is `mb` MiB but which uses almost no space."""
    path = box["data"] / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as fh:
        fh.truncate(mb * _MB)
    return path


def _holder(
    box,
    pid: int,
    target: Path,
    *,
    fd: int = 1,
    flags: str = "0102001",
    comm: str = "bash",
    ppid: int = 1,
    cmdline: str = "bash -c grep -r x .",
) -> None:
    """Fake /proc/<pid> holding `target` on descriptor `fd` with octal `flags`."""
    d = box["proc"] / str(pid)
    (d / "fd").mkdir(parents=True, exist_ok=True)
    (d / "fdinfo").mkdir(exist_ok=True)
    link = d / "fd" / str(fd)
    if link.is_symlink():
        link.unlink()
    link.symlink_to(target)
    (d / "fdinfo" / str(fd)).write_text(f"pos:\t0\nflags:\t{flags}\nmnt_id:\t1\n")
    (d / "comm").write_text(comm + "\n")
    (d / "status").write_text(f"Name:\t{comm}\nPPid:\t{ppid}\n")
    (d / "cmdline").write_bytes(cmdline.replace(" ", "\0").encode() + b"\0")


def _domain(box, total_mb: int, path: Path | None = None) -> str:
    """One check_disks domain line for the sandbox filesystem."""
    p = path or box["data"]
    dev = os.stat(p).st_dev
    return f"{dev}m1 {dev} {total_mb} {p}"


def _run(box, snippet: str, act: int = 1) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(
        HOME=str(box["home"]), GENESIS_ALERT_QUEUE_ROOT=str(box["queue"]), DG_PROC=str(box["proc"])
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


def _check(
    box, domains: str, act: int = 1, polls: int = 1, between: str = "", gap: int = 30
) -> subprocess.CompletedProcess:
    body = []
    for i in range(polls):
        if i:
            # Age the previous sighting so the next poll has a time base.
            body.append(f"_RW_PREV_T=$(( _RW_PREV_T - {gap} ))")
            body.append(between)
        body.append(f"wg_runaway_check '{domains}'; echo FAST=$RUNAWAY_FAST")
    return _run(box, "\n".join(body), act=act)


# ── the size rule ─────────────────────────────────────────────────


def test_a_writer_holding_a_large_share_of_its_filesystem_pages(box):
    f = _file(box, "out.log", 60)
    _holder(box, 4242, f, comm="grep", ppid=4241, cmdline="grep -r needle /home")
    _holder(box, 4241, f, comm="bash", ppid=1)
    _check(box, _domain(box, 200))  # 60 of 200 MB = 30% >= 25%
    pages = _pages(box)
    assert len(pages) == 1, pages
    page = pages[0]
    assert page["severity"] == "critical"
    assert page["title"] == f"Runaway file on {box['data']}: out.log"
    assert str(f) in page["body"]
    assert "pid 4242 grep" in page["body"] and "pid 4241 bash" in page["body"]
    assert "needle" not in page["body"], "arguments never reach a page (they can carry credentials)"
    assert "30% of" in page["body"]
    assert page["dedupe_key"].startswith("watchgod:runaway:")


def test_a_reader_is_not_a_writer(box):
    f = _file(box, "big.db", 120)
    _holder(box, 77, f, flags="0100000", comm="python")  # O_RDONLY
    _check(box, _domain(box, 200))
    assert not _pages(box)


def test_rdwr_counts_as_a_writer(box):
    f = _file(box, "big.db", 120)
    _holder(box, 77, f, flags="0100002", comm="python")  # O_RDWR
    _check(box, _domain(box, 200))
    assert len(_pages(box)) == 1


def test_a_file_below_the_minimum_is_not_followed(box):
    f = _file(box, "small.log", 10)
    _holder(box, 5, f)
    _check(box, _domain(box, 20))  # 50% of the domain, but under DG_RUNAWAY_MIN_MB
    assert not _pages(box)


def test_a_file_on_an_unwatched_filesystem_is_ignored(box):
    f = _file(box, "out.log", 60)
    _holder(box, 5, f)
    _check(box, "999999m1 999999 200 /elsewhere")
    assert not _pages(box)


def test_a_file_well_under_the_share_pages_nothing(box):
    f = _file(box, "out.log", 60)
    _holder(box, 5, f)
    _check(box, _domain(box, 10_000))  # 0.6%
    assert not _pages(box)


# ── the growth rule ───────────────────────────────────────────────


def test_fast_growth_between_polls_pages_and_asks_for_the_fast_poll(box):
    f = _file(box, "grow.output", 60)
    _holder(box, 9, f)
    # 60 MB of a 10,000 MB domain is 0.6%; then +640 MB in one poll is 6.4%.
    out = _check(box, _domain(box, 10_000), polls=2, between=f"fallocate -l {700 * _MB} '{f}'")
    assert out.stdout.splitlines() == ["FAST=0", "FAST=1"]
    pages = _pages(box)
    assert len(pages) == 1, pages
    assert "it grew 640 MB" in pages[0]["body"]


def test_a_first_sighting_is_never_growth(box):
    """A file that first appears on a LATER poll has no previous size: its
    whole size is not one poll's growth."""
    f = _file(box, "new.output", 700)
    dom = _domain(box, 10_000)
    hidden = box["tmp"] / "hidden9"
    _holder(box, 9, f)
    (box["proc"] / "9").rename(hidden)
    out = _run(
        box,
        f"""
        wg_runaway_check '{dom}'; echo FAST=$RUNAWAY_FAST
        mv '{hidden}' '{box["proc"]}/9'
        _RW_PREV_T=$(( _RW_PREV_T - 30 ))
        wg_runaway_check '{dom}'; echo FAST=$RUNAWAY_FAST
        """,
    )
    # Counted as growth, its 700 MB would be 7% of the domain in one poll.
    assert out.stdout.splitlines() == ["FAST=0", "FAST=0"]
    assert not _pages(box)


def test_half_the_rate_threshold_asks_for_the_fast_poll_without_paging(box):
    f = _file(box, "grow.output", 60)
    _holder(box, 9, f)
    # +300 MB of 10,000 MB = 3%: at least half the 5% threshold, under it.
    out = _check(box, _domain(box, 10_000), polls=2, between=f"fallocate -l {360 * _MB} '{f}'")
    assert out.stdout.splitlines() == ["FAST=0", "FAST=1"]
    assert not _pages(box)


# ── paging discipline ─────────────────────────────────────────────


def test_one_page_per_file_per_mode(box):
    f = _file(box, "out.log", 60)
    _holder(box, 5, f)
    _check(box, _domain(box, 200), polls=3)
    assert len(_pages(box)) == 1
    _check(box, _domain(box, 200), act=0)
    pages = _pages(box)
    assert len(pages) == 2
    assert pages[1]["title"].startswith("[observe mode, nothing was done] Runaway file on")
    assert pages[1]["dedupe_key"].endswith(":observe")


def test_two_holders_of_one_file_page_once(box):
    f = _file(box, "out.log", 60)
    _holder(box, 5, f, fd=1)
    _holder(box, 5, f, fd=2)
    _holder(box, 6, f, fd=1)
    _check(box, _domain(box, 200))
    pages = _pages(box)
    assert len(pages) == 1
    assert pages[0]["body"].count("pid 5 ") == 1, "one line per holder, not per descriptor"
    assert "pid 6 " in pages[0]["body"]


def test_a_short_absence_does_not_queue_the_page_again(box):
    """A writer that closes and reopens its file must not re-queue the page on
    each reappearance: during a delivery outage those copies pile up in the
    queue. Only after DG_RUNAWAY_FORGET_S out of sight is it queued again, under
    the same key."""
    f = _file(box, "out.log", 60)
    _holder(box, 5, f)
    dom = _domain(box, 200)
    hide = f"mv '{box['proc']}/5' '{box['tmp']}/hidden5'"
    show = f"mv '{box['tmp']}/hidden5' '{box['proc']}/5'"
    out = _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        {hide}; wg_runaway_check '{dom}'; {show}
        wg_runaway_check '{dom}'
        echo AFTER_SHORT=$(ls '{box["queue"]}' | wc -l)
        echo FIRST=$(cat '{box["queue"]}'/*.json | python3 -c 'import json,sys; print(json.load(sys.stdin)["dedupe_key"])')
        rm -f '{box["queue"]}'/*.json   # delivered
        {hide}
        for g in "${{!_RW_LAST[@]}}"; do _RW_LAST[$g]=$(( _RW_LAST[$g] - DG_RUNAWAY_FORGET_S - 1 )); done
        wg_runaway_check '{dom}'; {show}
        wg_runaway_check '{dom}'
        """,
    )
    assert "AFTER_SHORT=1" in out.stdout
    first = next(ln.split("=", 1)[1] for ln in out.stdout.splitlines() if ln.startswith("FIRST="))
    keys = [p["dedupe_key"] for p in _pages(box)]
    assert keys == [first], keys


def test_a_new_file_is_a_new_incident_even_at_the_same_path(box):
    """A recreated file has a new birth time, so a new key: the drainer's 24 h
    dedupe cannot swallow it, even where the inode number is reused at once."""
    f = _file(box, "out.log", 60)
    _holder(box, 5, f)
    dom = _domain(box, 200)
    _check(box, dom)
    f.unlink()
    time.sleep(1.1)
    f = _file(box, "out.log", 60)
    _holder(box, 5, f)
    _check(box, dom)
    keys = [p["dedupe_key"] for p in _pages(box)]
    assert len(keys) == 2 and keys[0] != keys[1], keys


def test_a_restart_re_sends_the_same_key(box):
    """A crash-looping daemon must not mint a new key each start: the drainer
    collapses one key, so a restart storm stays one page."""
    f = _file(box, "out.log", 60)
    _holder(box, 5, f)
    _check(box, _domain(box, 200))
    first = [p["dedupe_key"] for p in _pages(box)]
    for q in box["queue"].glob("*.json"):  # delivered; an undelivered copy collapses
        q.unlink()
    time.sleep(1.1)  # a clock-derived key would differ across this gap
    _check(box, _domain(box, 200))
    keys = first + [p["dedupe_key"] for p in _pages(box)]
    assert len(keys) == 2 and keys[0] == keys[1], keys


def test_the_key_is_device_inode_and_birth_time(box):
    """Pinned exactly: on a filesystem that reuses inode numbers at once, the
    birth time is the only part that tells a new file from the old one."""
    f = _file(box, "out.log", 60)
    _holder(box, 5, f)
    _check(box, _domain(box, 200))
    st = os.stat(f)
    birth = subprocess.run(
        ["stat", "-c", "%W", str(f)], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert _pages(box)[0]["dedupe_key"] == f"watchgod:runaway:{st.st_dev}:{st.st_ino}:{birth}"


def test_the_writer_list_is_capped(box):
    f = _file(box, "out.log", 60)
    for pid in range(100, 125):
        _holder(box, pid, f)
    _check(box, _domain(box, 200))
    body = _pages(box)[0]["body"]
    assert body.count("  pid ") == 20
    assert "+5 more" in body


def test_a_cc_tmp_file_names_its_session(box):
    cc = box["data"] / "cc-tmp"
    f = _file(box, "cc-tmp/claude-1000/-proj/sess-abc123/tasks/b1.output", 600)
    _holder(box, 31, f, comm="bash")
    _run(box, f"CC_TMP_DIR='{cc}'\nwg_runaway_check '{_domain(box, 2048)}'")
    pages = _pages(box)
    assert len(pages) == 1
    assert "Claude Code session: sess-abc123" in pages[0]["body"]


def test_detection_changes_nothing(box):
    f = _file(box, "out.log", 60)
    _holder(box, 5, f)
    before = f.stat()
    out = _run(box, f"kill() {{ echo KILLED \"$@\"; }}\nwg_runaway_check '{_domain(box, 200)}'")
    after = f.stat()
    assert "KILLED" not in out.stdout
    assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)


def test_runaway_thresholds_are_conf_tunables(box):
    conf = box["home"] / ".genesis" / "config" / "watchgod.local.conf"
    conf.write_text("DG_RUNAWAY_PCT=10\nDG_RUNAWAY_MIN_MB=5\n")
    out = _run(
        box,
        'echo "$DG_RUNAWAY_PCT $DG_RUNAWAY_RATE_PCT $DG_RUNAWAY_MIN_MB"; : > '
        + f"'{conf}'"
        + '; load_config; echo "$DG_RUNAWAY_PCT $DG_RUNAWAY_MIN_MB"',
    )
    assert out.stdout.splitlines() == ["10 5 5", "25 50"]


def test_check_disks_feeds_every_domain_to_the_detector():
    """Wiring: the domains check_disks tiers are the ones the detector sees,
    and fast growth shortens the next poll."""
    text = _WATCHGOD.read_text()
    assert 'rw_domains+="${key} ${key%%[qm]*} ${total} $(wg_canon "$p")"' in text
    assert 'wg_runaway_check "$rw_domains"' in text
    assert "(( RUNAWAY_FAST )) && fast=1" in text


def test_growth_is_judged_per_poll_interval_not_per_poll(box):
    """At the 5 s fast poll the same writer shows a sixth of a 30 s poll's
    growth. Normalised to POLL_INTERVAL it still pages and stays fast."""
    f = _file(box, "grow.output", 60)
    _holder(box, 9, f)
    # +160 MB in ~5 s = 960 MB per 30 s = 9.6% of 10,000 MB; per POLL it is only
    # 1.6%, so this pages only if growth is normalised. Margin: still >= 5% at a
    # 9 s gap, should a loaded box stretch the 5 s between the two polls.
    out = _check(
        box, _domain(box, 10_000), polls=2, gap=5, between=f"fallocate -l {220 * _MB} '{f}'"
    )
    assert out.stdout.splitlines() == ["FAST=0", "FAST=1"]
    pages = _pages(box)
    assert len(pages) == 1, pages
    assert "it grew 160 MB in " in pages[0]["body"]


def test_a_multiline_command_line_cannot_make_the_page_undeliverable(box):
    """A Claude Code Bash call's command line can span thousands of lines; cut
    per line kept them all, and the oversized body could not be queued."""
    f = _file(box, "out.log", 60)
    _holder(box, 5, f)
    (box["proc"] / "5" / "cmdline").write_bytes(b"bash\0-c\0" + b"echo line\n" * 3000 + b"\0")
    _check(box, _domain(box, 200))
    pages = _pages(box)
    assert len(pages) == 1
    assert "echo line" not in pages[0]["body"]
    assert len(pages[0]["body"]) < 2000


def test_a_reused_descriptor_does_not_name_the_wrong_file(box):
    """Between the walk and the naming, the fd number can be closed and reused:
    the page names the file only if the link still leads to the measured inode."""
    big = _file(box, "big.log", 60)
    other = _file(box, "other.log", 1)
    _holder(box, 5, big)
    link = box["proc"] / "5" / "fd" / "1"
    _run(
        box,
        f"""
        saved_scan=$(_rw_scan)
        [[ -n "$saved_scan" ]] || {{ echo "fixture: the walk saw nothing"; exit 1; }}
        rm -f '{link}'; ln -s '{other}' '{link}'
        _rw_scan() {{ printf '%s\\n' "$saved_scan"; }}
        wg_runaway_check '{_domain(box, 200)}'
        """,
    )
    assert not _pages(box)


@pytest.mark.parametrize("bad", ["high", "25%", "-5", "0", ""])
def test_a_bad_runaway_setting_falls_back_to_its_default(box, bad):
    """A hand-edited value must never crash-loop the daemon (set -u) or turn
    detection off silently: it is reset to the default, loudly."""
    conf = box["home"] / ".genesis" / "config" / "watchgod.local.conf"
    conf.write_text(f"DG_RUNAWAY_PCT={bad}\nDG_RUNAWAY_MIN_MB={bad}\n")
    f = _file(box, "out.log", 60)
    _holder(box, 5, f)
    out = _run(
        box,
        f"""echo "$DG_RUNAWAY_PCT $DG_RUNAWAY_MIN_MB"
wg_runaway_check '{_domain(box, 200)}'""",
    )
    assert out.stdout.splitlines()[0] == "25 50"
    assert len(_pages(box)) == 1
    log = (box["home"] / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
    assert "DG_RUNAWAY_PCT=" in log and "using 25" in log


@pytest.mark.parametrize("rc", [124, 125, 127, 137])
def test_a_walk_that_did_not_complete_says_so(box, rc):
    """Every status above find's normal 1 means the walk did not complete; none
    may read as all clear."""
    f = _file(box, "out.log", 60)
    _holder(box, 5, f)
    _run(
        box,
        f"""timeout() {{ while [[ "$1" != find ]]; do shift; done; "$@" >/dev/null; return {rc}; }}
wg_runaway_check '{_domain(box, 200)}'""",
    )
    log = (box["home"] / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
    assert f"did not complete (status {rc}" in log


def test_a_normal_walk_logs_no_failure(box):
    f = _file(box, "out.log", 60)
    _holder(box, 5, f)
    _check(box, _domain(box, 200))
    log = (box["home"] / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
    assert "did not complete" not in log


def test_a_sparse_file_is_not_a_runaway(box):
    """Length is not usage: a 600 MB sparse file on a 200 MB domain uses almost
    nothing and must not page."""
    f = _sparse(box, "sparse.img", 600)
    _holder(box, 5, f)
    _check(box, _domain(box, 200))
    assert not _pages(box)


def test_a_replacement_file_under_a_reused_inode_starts_afresh(box):
    """Same device and inode, new birth time: a different file. It pages on its
    own, and does not inherit the old file's growth baseline."""
    f = _file(box, "out.log", 60)
    _holder(box, 5, f)
    dom = _domain(box, 200)
    # The second poll sees the same inode with a later birth time, as after an
    # unlink and an immediate inode reuse.
    _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        # stat runs under timeout, which would exec the real binary: pass through.
        timeout() {{ while [[ "$1" != stat && "$1" != find ]]; do shift; done; "$@"; }}
        stat() {{ command stat "$@" | awk -F'\t' '{{ split($2, a, " "); printf "%s\t%s %d\\n", $1, a[1], a[2] + 100 }}'; }}
        _RW_PREV_T=$(( _RW_PREV_T - 30 ))
        wg_runaway_check '{dom}'; echo FAST=$RUNAWAY_FAST
        """,
    )
    keys = [p["dedupe_key"] for p in _pages(box)]
    assert len(keys) == 2 and keys[0] != keys[1], keys


def test_an_unmatched_file_is_never_judged_by_a_quota_domain(box):
    """A quota bounds only its own tree. A file on the same device outside every
    watched root may fall back to the filesystem-wide domain, never a quota."""
    f = _file(box, "out.log", 60)
    _holder(box, 5, f)
    dev = os.stat(box["data"]).st_dev
    quota_only = f"{dev}q200 {dev} 200 /somewhere/else"
    _check(box, quota_only)
    assert not _pages(box), "60 MB would be 30% of the 200 MB quota it is not in"
    with_mount = f"{dev}q200 {dev} 200 /somewhere/else\n{dev}m1 {dev} 200 /mnt/whole"
    _check(box, with_mount)
    assert len(_pages(box)) == 1


def test_watched_roots_are_compared_with_symlinks_resolved(box):
    """/proc names a held file by its resolved path; a symlinked root must be
    resolved too, or its files fall through to another domain."""
    real = box["data"] / "real-cc"
    real.mkdir()
    link = box["tmp"] / "cc-link"
    link.symlink_to(real)
    out = _run(box, f"wg_canon '{link}'; echo; wg_canon '{box['tmp']}/missing'")
    assert out.stdout.splitlines() == [str(real), f"{box['tmp']}/missing"]


def test_a_file_directly_in_a_project_dir_names_no_session(box):
    """claude-<uid>/<project>/<session>/…: a file one level up has no session,
    and its own name must not be reported as one."""
    cc = box["data"] / "cc-tmp"
    f = _file(box, "cc-tmp/claude-1000/-proj/x.output", 600)
    _holder(box, 32, f)
    _run(box, f"CC_TMP_DIR='{cc}'\nwg_runaway_check '{_domain(box, 2048)}'")
    pages = _pages(box)
    assert len(pages) == 1
    assert "Claude Code session" not in pages[0]["body"]


def test_a_fallback_domain_is_labelled_with_the_files_own_mount(box):
    """Judged by a filesystem-wide domain whose root is elsewhere, the page names
    the file's mount point, not that other root."""
    f = _file(box, "out.log", 60)
    _holder(box, 6, f)
    dev = os.stat(box["data"]).st_dev
    mnt = subprocess.run(
        ["stat", "-c", "%m", str(box["data"])], capture_output=True, text=True, check=True
    ).stdout.strip()
    _check(box, f"{dev}m1 {dev} 200 /mnt/elsewhere")
    pages = _pages(box)
    assert len(pages) == 1
    assert f"Runaway file on {mnt}:" in pages[0]["title"]
    assert "/mnt/elsewhere" not in pages[0]["title"]


def test_a_holder_gone_before_its_fdinfo_is_read_is_silent(box):
    """A pid that exits between the walk and the fdinfo read must not write an
    error to the journal on every poll."""
    f = _file(box, "big.log", 600)
    _holder(box, 33, f)
    (box["proc"] / "33" / "fdinfo" / "1").unlink()
    out = _check(box, _domain(box, 2048))
    assert "No such file" not in out.stderr
    assert not _pages(box)


def test_an_incomplete_walk_keeps_the_growth_baseline_of_files_it_missed(box):
    """A file a timed-out walk did not report keeps its last size and time, so
    the next complete poll still judges its growth instead of a first sighting."""
    f = _file(box, "grow.log", 60)
    _holder(box, 7, f)
    dom = _domain(box, 2048)
    out = _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        timeout() {{ return 124; }}
        wg_runaway_check '{dom}'
        unset -f timeout
        for g in "${{!_RW_PREV_AT[@]}}"; do _RW_PREV_AT[$g]=$(( _RW_PREV_AT[$g] - 60 )); done
        python3 -c 'import os; fd = os.open("{f}", os.O_WRONLY); os.posix_fallocate(fd, 0, 360 * 1048576)'
        wg_runaway_check '{dom}'
        echo FAST=$RUNAWAY_FAST
        """,
    )
    pages = _pages(box)
    assert len(pages) == 1 and "it grew 300 MB" in pages[0]["body"], pages
    assert "FAST=1" in out.stdout


def test_a_stalled_revalidation_is_bounded_and_reported(box):
    """stat -L on a held file whose mount stalled after the walk must not hang
    the poll: it runs under the same timeout, and a timeout is logged."""
    f = _file(box, "big.log", 600)
    _holder(box, 8, f)
    _run(
        box,
        f"""timeout() {{
    local a=("$@"); while [[ "${{a[0]}}" != find && "${{a[0]}}" != stat ]]; do a=("${{a[@]:1}}"); done
    [[ "${{a[0]}}" == stat ]] && return 124
    "${{a[@]}}"
}}
wg_runaway_check '{_domain(box, 2048)}'""",
    )
    log = (box["home"] / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
    assert "revalidation did not complete (status 124" in log
    assert not _pages(box)


def test_a_long_walk_outage_does_not_expire_the_baselines_it_carries(box):
    """Nothing is forgotten while the walk is incomplete: an outage longer than
    DG_RUNAWAY_FORGET_S must not turn a fast grower into a first sighting."""
    f = _file(box, "grow2.log", 60)
    _holder(box, 9, f)
    dom = _domain(box, 2048)
    _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        timeout() {{ return 124; }}
        for g in "${{!_RW_LAST[@]}}"; do _RW_LAST[$g]=$(( _RW_LAST[$g] - DG_RUNAWAY_FORGET_S - 10 )); done
        wg_runaway_check '{dom}'
        unset -f timeout
        for g in "${{!_RW_PREV_AT[@]}}"; do _RW_PREV_AT[$g]=$(( _RW_PREV_AT[$g] - 60 )); done
        python3 -c 'import os; fd = os.open("{f}", os.O_WRONLY); os.posix_fallocate(fd, 0, 360 * 1048576)'
        wg_runaway_check '{dom}'
        """,
    )
    pages = _pages(box)
    assert len(pages) == 1 and "it grew 300 MB" in pages[0]["body"], pages
