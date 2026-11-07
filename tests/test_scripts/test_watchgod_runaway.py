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
    queue. A PAGED file is remembered for DG_RUNAWAY_PAGED_FORGET_S out of sight
    (the drainer's 24 h dedupe window), not the shorter DG_RUNAWAY_FORGET_S; only
    after that is it queued again, under the same key."""
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
        echo AFTER_HOUR=$(ls '{box["queue"]}' | wc -l)
        {hide}
        for g in "${{!_RW_LAST[@]}}"; do _RW_LAST[$g]=$(( _RW_LAST[$g] - DG_RUNAWAY_PAGED_FORGET_S - 1 )); done
        wg_runaway_check '{dom}'; {show}
        wg_runaway_check '{dom}'
        """,
    )
    assert "AFTER_SHORT=1" in out.stdout
    assert "AFTER_HOUR=0" in out.stdout, "a paged file outlives DG_RUNAWAY_FORGET_S"
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


# ── episodes and retained baselines (#3023, #3024) ────────────────


def _legacy_key(f: Path) -> str:
    st = os.stat(f)
    birth = subprocess.run(
        ["stat", "-c", "%W", str(f)], capture_output=True, text=True, check=True
    ).stdout.strip()
    return f"watchgod:runaway:{st.st_dev}:{st.st_ino}:{birth}"


def _alloc(f: Path, mb: float) -> str:
    """Shell step: allocate `mb` MiB through a writer's descriptor."""
    return (
        "python3 -c 'import os; fd = os.open(\"" + str(f) + '", os.O_WRONLY); '
        f"os.posix_fallocate(fd, 0, int({mb} * 1048576))'"
    )


def _empty(f: Path) -> str:
    """Shell step: empty the file in place (same inode, same birth time)."""
    return "python3 -c 'import os; os.truncate(\"" + str(f) + "\", 0)'"


def test_an_emptied_and_refilled_file_pages_again_under_an_episode_key(box):
    """Emptied while its writer still holds it, then refilled: a new episode,
    so it pages again under its own key. The first episode keeps the legacy
    key, so a restart still collapses with the old page (#3023)."""
    f = _file(box, "out.log", 200)
    _holder(box, 5, f)
    dom = _domain(box, 400)
    _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        {_empty(f)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        {_alloc(f, 60)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        {_alloc(f, 120)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        """,
    )
    pages = _pages(box)
    assert len(pages) == 2, [p["title"] for p in pages]
    legacy = _legacy_key(f)
    assert pages[0]["dedupe_key"] == legacy
    assert pages[1]["dedupe_key"].startswith(legacy + ":e")
    assert pages[1]["dedupe_key"][len(legacy) + 2 :].isdigit()  # a timestamp
    assert "(again since " in pages[1]["title"]


def test_a_small_shrink_does_not_open_an_episode(box):
    """Allocated size moves a little (compaction, reallocation): only a shrink
    to half the retained baseline or less ends an episode."""
    f = _file(box, "out.log", 200)
    _holder(box, 5, f)
    dom = _domain(box, 400)
    _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        {_empty(f)}; {_alloc(f, 120)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        {_alloc(f, 200)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        """,
    )
    assert len(_pages(box)) == 1


def test_a_shrink_of_a_file_never_paged_opens_no_episode(box):
    f = _file(box, "out.log", 120)
    _holder(box, 5, f)
    dom = _domain(box, 4000)  # 3%: never a runaway
    out = _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        {_empty(f)}; {_alloc(f, 55)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        echo EP=${{#_RW_EP[@]}}
        """,
    )
    assert "EP=0" in out.stdout
    assert not _pages(box)


def test_a_complete_poll_absence_keeps_the_growth_baseline(box):
    """A writer that closes its file for one COMPLETE poll and reopens it is
    judged against its retained baseline, not as a first sighting (#3024)."""
    f = _file(box, "grow.log", 60)
    _holder(box, 7, f)
    dom = _domain(box, 2048)
    hide = f"mv '{box['proc']}/7' '{box['tmp']}/hidden7'"
    show = f"mv '{box['tmp']}/hidden7' '{box['proc']}/7'"
    out = _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        {hide}; wg_runaway_check '{dom}'; {show}
        for g in "${{!_RW_PREV_AT[@]}}"; do _RW_PREV_AT[$g]=$(( _RW_PREV_AT[$g] - 60 )); done
        {_alloc(f, 360)}
        wg_runaway_check '{dom}'
        echo FAST=$RUNAWAY_FAST
        """,
    )
    pages = _pages(box)
    assert len(pages) == 1 and "it grew 300 MB" in pages[0]["body"], pages
    assert "FAST=1" in out.stdout


def test_an_unpaged_file_is_still_forgotten_after_the_short_window(box):
    """Only PAGED records are kept for the long window: an unpaged file out of
    sight for DG_RUNAWAY_FORGET_S is forgotten, baseline included."""
    f = _file(box, "quiet.log", 60)
    _holder(box, 5, f)
    dom = _domain(box, 4000)
    hide = f"mv '{box['proc']}/5' '{box['tmp']}/hidden5'"
    out = _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        {hide}
        for g in "${{!_RW_LAST[@]}}"; do _RW_LAST[$g]=$(( _RW_LAST[$g] - DG_RUNAWAY_FORGET_S - 1 )); done
        wg_runaway_check '{dom}'
        echo LAST=${{#_RW_LAST[@]}} PREV=${{#_RW_PREV[@]}} AT=${{#_RW_PREV_AT[@]}}
        """,
    )
    assert "LAST=0 PREV=0 AT=0" in out.stdout


def test_paged_forget_window_is_the_drainers_dedupe_window():
    src = (_ROOT / "scripts" / "lib" / "watchgod_runaway.sh").read_text()
    assert "DG_RUNAWAY_PAGED_FORGET_S=86400" in src


def test_a_forgotten_file_comes_back_under_the_legacy_key(box):
    """Forgetting a paged file drops its episode too: seen again a day later it
    is a first episode, under the bare generation key."""
    f = _file(box, "out.log", 200)
    _holder(box, 5, f)
    dom = _domain(box, 400)
    hide = f"mv '{box['proc']}/5' '{box['tmp']}/hidden5'"
    show = f"mv '{box['tmp']}/hidden5' '{box['proc']}/5'"
    out = _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        {_empty(f)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        {_alloc(f, 60)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        {_alloc(f, 120)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        echo EPISODES=$(ls '{box["queue"]}' | wc -l)
        rm -f '{box["queue"]}'/*.json   # delivered
        {hide}
        for g in "${{!_RW_LAST[@]}}"; do _RW_LAST[$g]=$(( _RW_LAST[$g] - DG_RUNAWAY_PAGED_FORGET_S - 1 )); done
        wg_runaway_check '{dom}'; {show}
        wg_runaway_check '{dom}'
        """,
    )
    assert "EPISODES=2" in out.stdout
    assert [p["dedupe_key"] for p in _pages(box)] == [_legacy_key(f)]


def test_an_open_episode_outlives_the_short_forget_window(box):
    """Between episodes the record is kept for the long window: a refill that
    takes longer than DG_RUNAWAY_FORGET_S (an operator thawing hours later)
    still pages under its episode key, never the bare key the drainer holds."""
    f = _file(box, "out.log", 200)
    _holder(box, 5, f)
    dom = _domain(box, 400)
    hide = f"mv '{box['proc']}/5' '{box['tmp']}/hidden5'"
    show = f"mv '{box['tmp']}/hidden5' '{box['proc']}/5'"
    _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        {_empty(f)}; {_alloc(f, 60)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        {_empty(f)}; {hide}
        for g in "${{!_RW_LAST[@]}}"; do _RW_LAST[$g]=$(( _RW_LAST[$g] - DG_RUNAWAY_FORGET_S - 1 )); done
        wg_runaway_check '{dom}'; {show}
        {_alloc(f, 120)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        """,
    )
    keys = [p["dedupe_key"] for p in _pages(box)]
    assert len(keys) == 2 and keys[1].startswith(_legacy_key(f) + ":e"), keys


def test_a_partial_shrink_still_over_threshold_does_not_page_at_once(box):
    """Cutting a paged file to half still leaves it over the size threshold:
    that poll opens the episode but pages nothing (nothing refilled), and
    neither do the polls after it while it stays there (review finding, PR
    #3290: the size alone re-paged on the next poll)."""
    f = _file(box, "out.log", 300)
    _holder(box, 5, f)
    dom = _domain(box, 400)
    out = _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        {_empty(f)}; {_alloc(f, 120)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        echo EP=${{#_RW_EP[@]}}
        """,
    )
    assert "EP=1" in out.stdout, "guard: the episode did open"
    assert len(_pages(box)) == 1


def test_exactly_half_ends_the_episode(box):
    f = _file(box, "out.log", 200)
    _holder(box, 5, f)
    dom = _domain(box, 400)
    out = _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        {_empty(f)}; {_alloc(f, 100)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        echo EP=${{#_RW_EP[@]}}
        """,
    )
    assert "EP=1" in out.stdout


def test_a_small_paged_file_emptied_below_the_floor_pages_again(box):
    """Paged at 60 MB: emptied, it drops below the scan's 50 MB floor, so it is
    never SEEN at half. Back from out of sight smaller than its baseline, it
    has shrunk all the same."""
    f = _file(box, "out.log", 60)
    _holder(box, 5, f)
    dom = _domain(box, 200)
    _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        {_empty(f)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        {_alloc(f, 52)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        {_alloc(f, 70)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        """,
    )
    keys = [p["dedupe_key"] for p in _pages(box)]
    assert len(keys) == 2 and keys[1].startswith(_legacy_key(f) + ":e"), keys


def test_a_reopened_file_a_little_smaller_is_not_a_new_episode(box):
    """Closed for a poll and reopened slightly smaller (compaction,
    reallocation) is not an emptied file: out of sight, a return counts as a
    shrink only once it is at least halfway down from its baseline to the
    scan floor (review finding, PR #3290)."""
    f = _file(box, "out.log", 200)
    _holder(box, 5, f)
    dom = _domain(box, 400)
    hide = f"mv '{box['proc']}/5' '{box['tmp']}/hidden5'"
    show = f"mv '{box['tmp']}/hidden5' '{box['proc']}/5'"
    out = _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        {hide}; _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'; {show}
        {_empty(f)}; {_alloc(f, 199)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        echo EP=${{#_RW_EP[@]}}
        """,
    )
    assert "EP=0" in out.stdout
    assert len(_pages(box)) == 1


# ── Codex round 2 (PR #3290): a refill is growth or a crossing ────


def test_a_paged_file_seen_empty_below_the_floor_then_refilled_pages_again(box):
    """Paged at 60 MB on a 200 MB domain (threshold 50), emptied while held:
    under the scan floor the scan no longer reports it, but it is followed
    through its writer's descriptor, so the empty is SEEN and the episode
    opens. Refilled to 52 it crosses the threshold and pages again; left at 52
    it pages nothing more (review findings, PR #3290)."""
    f = _file(box, "out.log", 60)
    _holder(box, 5, f)
    dom = _domain(box, 200)
    out = _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        {_empty(f)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        echo EP=${{#_RW_EP[@]}}
        {_alloc(f, 52)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        """,
    )
    assert "EP=1" in out.stdout, "the empty was seen on the poll it happened"
    keys = [p["dedupe_key"] for p in _pages(box)]
    assert len(keys) == 2 and keys[1].startswith(_legacy_key(f) + ":e"), keys


def test_a_file_reopened_a_little_smaller_near_the_floor_is_not_paged_again(box):
    """60 MB, closed for a poll, back at 55: out of sight, that is far enough
    down toward the floor to open an episode, but it is still over the
    threshold and never crossed it, so it does not page again."""
    f = _file(box, "out.log", 60)
    _holder(box, 5, f)
    dom = _domain(box, 200)
    hide = f"mv '{box['proc']}/5' '{box['tmp']}/hidden5'"
    show = f"mv '{box['tmp']}/hidden5' '{box['proc']}/5'"
    _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        {hide}; _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'; {show}
        {_empty(f)}; {_alloc(f, 55)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        """,
    )
    assert len(_pages(box)) == 1


def test_a_slow_refill_pages_again_when_it_crosses_the_threshold(box):
    """Shrunk under the size threshold (100 MB of 400), then refilled more
    slowly than the growth threshold (20 MB a poll): it pages again on the
    poll it crosses the size threshold, not before."""
    f = _file(box, "out.log", 300)
    _holder(box, 5, f)
    dom = _domain(box, 400)
    _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        {_empty(f)}; {_alloc(f, 60)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        {_alloc(f, 75)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        {_alloc(f, 90)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        {_alloc(f, 105)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        """,
    )
    pages = _pages(box)
    assert len(pages) == 2, [p["title"] for p in pages]
    assert pages[1]["dedupe_key"].startswith(_legacy_key(f) + ":e")
    assert "alone uses" in pages[1]["body"], "paged for its size, at the crossing"
    assert "grew" not in pages[1]["body"], "guard: the refill was slower than the growth threshold"


def test_a_file_shrunk_but_still_over_the_threshold_regrowing_slowly_is_the_stated_limit(box):
    """The accepted limit: cut to a size still over the threshold, then
    regrowing slowly, it never crossed from below and grows too slowly for the
    growth threshold, so it is not paged again. Pinned so a change to it is a
    decision, not an accident."""
    f = _file(box, "out.log", 300)
    _holder(box, 5, f)
    dom = _domain(box, 400)
    _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        {_empty(f)}; {_alloc(f, 120)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        {_alloc(f, 135)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        {_alloc(f, 150)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        """,
    )
    assert len(_pages(box)) == 1


def test_a_page_that_could_not_be_queued_is_retried_on_the_next_poll(box):
    """The crossing rule applies only between episodes: a file whose first page
    could not be queued is still unpaged and still over the threshold, and it
    pages on the next poll (it never had an episode to re-arm)."""
    f = _file(box, "out.log", 300)
    _holder(box, 5, f)
    dom = _domain(box, 400)
    q = box["queue"]
    out = _run(
        box,
        f"""
        : > '{q}'
        wg_runaway_check '{dom}'
        echo PAGED_FIRST=${{#_RW_PAGED[@]}}
        rm -f '{q}'
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        """,
    )
    assert "PAGED_FIRST=0" in out.stdout, "guard: the first enqueue really failed"
    assert len(_pages(box)) == 1


def test_a_crossing_is_judged_in_the_same_units_as_the_size(box):
    """401 MB domain: the size threshold is 100.25 MB, and the size test reads
    whole MB, so 100.5 MB is under it. The next poll's crossing must see the
    same: 100.5 then 115 MB is a crossing, and it pages (review finding).
    Every step is under the growth threshold (20 MB a poll)."""
    f = _file(box, "out.log", 300)
    _holder(box, 5, f)
    dom = _domain(box, 401)
    _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        {_empty(f)}; {_alloc(f, 60)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        {_alloc(f, 75)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        {_alloc(f, 90)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        {_alloc(f, 100.5)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        {_alloc(f, 115)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        """,
    )
    pages = _pages(box)
    assert len(pages) == 2, [p["title"] for p in pages]
    assert "alone uses" in pages[1]["body"]


def test_a_file_emptied_for_one_poll_then_refilled_past_its_old_size_pages_again(box):
    """Codex (PR #3290): 200 MB, empty for one complete poll, then 250 MB before
    the next. No size rule can see that as a shrink; following the writer's
    descriptor sees the empty file directly."""
    f = _file(box, "out.log", 200)
    _holder(box, 5, f)
    dom = _domain(box, 400)
    _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        {_empty(f)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        {_alloc(f, 250)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        """,
    )
    keys = [p["dedupe_key"] for p in _pages(box)]
    assert len(keys) == 2 and keys[1].startswith(_legacy_key(f) + ":e"), keys


def test_a_page_that_could_not_be_queued_at_the_crossing_is_sent_later(box):
    """Codex (PR #3290): 300 -> 60 -> 105 MB on a 400 MB domain, with the queue
    unwritable only on the crossing poll. The next poll, unchanged, has no
    crossing and no growth; the page is still due and is sent."""
    f = _file(box, "out.log", 300)
    _holder(box, 5, f)
    dom = _domain(box, 400)
    q = box["queue"]
    hold = f"mv '{q}' '{box['tmp']}/q.held'; : > '{q}'"
    back = f"rm -f '{q}'; mv '{box['tmp']}/q.held' '{q}'"
    out = _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        {_empty(f)}; {_alloc(f, 60)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        {_alloc(f, 75)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        {_alloc(f, 90)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        {_alloc(f, 105)}
        {hold}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        echo DUE=${{#_RW_DUE[@]}}
        {back}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        echo DUE_AFTER=${{#_RW_DUE[@]}}
        """,
    )
    assert "DUE=1" in out.stdout, "guard: the crossing page really failed"
    assert "DUE_AFTER=0" in out.stdout
    pages = _pages(box)
    assert len(pages) == 2, [p["title"] for p in pages]
    assert "could not be queued" in pages[1]["body"]


def test_a_growth_page_that_could_not_be_queued_is_sent_after_growth_stops(box):
    """Paged only for its growth, on a poll whose enqueue failed; it then
    stops growing. The page is still due and is sent on the next poll."""
    f = _file(box, "out.log", 60)
    _holder(box, 5, f)
    dom = _domain(box, 4000)
    q = box["queue"]
    out = _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        {_alloc(f, 400)}
        : > '{q}'
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        echo DUE=${{#_RW_DUE[@]}}
        rm -f '{q}'
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        """,
    )
    assert "DUE=1" in out.stdout
    pages = _pages(box)
    assert len(pages) == 1 and "grew" in pages[0]["body"], [p["body"] for p in pages]


def test_a_followed_descriptor_now_on_another_file_opens_no_episode(box):
    """The writer closed the paged file and its descriptor number now leads to
    a different, small file: that is not the paged file emptied."""
    f = _file(box, "out.log", 200)
    other = _file(box, "other.log", 1)
    _holder(box, 5, f)
    dom = _domain(box, 400)
    relink = f"ln -sfn '{other}' '{box['proc']}/5/fd/1'"
    out = _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        echo PREV0=${{_RW_PREV[*]}}
        {relink}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        echo EP=${{#_RW_EP[@]}} PREV1=${{_RW_PREV[*]}}
        """,
    )
    assert "EP=0" in out.stdout
    prev0 = out.stdout.split("PREV0=")[1].split()[0]
    assert f"PREV1={prev0}" in out.stdout, "the other file did not stand in for the paged one"
    assert len(_pages(box)) == 1


def test_a_followed_file_under_the_floor_is_not_paged_until_it_is_over_it(box):
    """Following a paged file only keeps its baseline: refilled fast but still
    under the scan's 50 MB floor it pages nothing; over the floor it pages."""
    f = _file(box, "out.log", 60)
    _holder(box, 5, f)
    dom = _domain(box, 100)
    out = _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        {_empty(f)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        {_alloc(f, 40)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        echo AT40=$(ls '{box["queue"]}' | wc -l)
        {_alloc(f, 60)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        """,
    )
    assert "AT40=1" in out.stdout
    assert len(_pages(box)) == 2


def test_a_followed_file_is_judged_by_its_own_size(box):
    """Paged at 60 MB, cut to 40 MB while held: under the floor, followed, and
    above half its size, so no episode opens; growing back to 60 MB is not a
    refill. The followed size is the file's real size, not a placeholder."""
    f = _file(box, "out.log", 60)
    _holder(box, 5, f)
    dom = _domain(box, 200)
    out = _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        {_empty(f)}; {_alloc(f, 40)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        echo EP=${{#_RW_EP[@]}}
        {_alloc(f, 60)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        """,
    )
    assert "EP=0" in out.stdout
    assert len(_pages(box)) == 1


def test_a_new_writer_that_truncates_and_refills_pages_again(box):
    """The writer exits, the file is emptied, and a NEW process refills it
    (`while :; do cmd > out; done`). No writer was seen while it was empty, so
    nothing could be followed: on return the size is the only evidence, and
    halfway down to the floor counts (review finding, PR #3290)."""
    f = _file(box, "out.log", 60)
    _holder(box, 6, f)
    (box["proc"] / "6").rename(box["tmp"] / "new6")  # the next writer, not yet started
    _holder(box, 5, f)
    dom = _domain(box, 200)
    gone = f"rm -rf '{box['proc']}/5'"
    start = f"mv '{box['tmp']}/new6' '{box['proc']}/6'"
    out = _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        {gone}; {_empty(f)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        {start}; {_alloc(f, 52)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        {_alloc(f, 150)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        echo EP=${{#_RW_EP[@]}}
        """,
    )
    assert "EP=1" in out.stdout
    keys = [p["dedupe_key"] for p in _pages(box)]
    assert len(keys) == 2 and keys[1].startswith(_legacy_key(f) + ":e"), keys


def test_a_followed_descriptor_reopened_read_only_is_not_followed(box):
    """The paged file's descriptor number now holds it read-only (written,
    closed, opened again to read back): that is not the writer, so it is not
    followed and no page names a reader as a writer."""
    f = _file(box, "out.log", 200)
    _holder(box, 5, f)
    dom = _domain(box, 400)
    ro = f"printf 'pos:\\t0\\nflags:\\t0100000\\n' > '{box['proc']}/5/fdinfo/1'"
    out = _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        {_empty(f)}; {ro}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        echo EP=${{#_RW_EP[@]}}
        """,
    )
    assert "EP=0" in out.stdout, "a reader is not followed"
    assert len(_pages(box)) == 1


def test_a_due_page_is_sent_only_in_the_mode_that_owes_it(box):
    """A page that failed in ACT is not sent by an observing poll: the observe
    page would stand in for the one an acting poll owes."""
    f = _file(box, "out.log", 60)
    _holder(box, 5, f)
    dom = _domain(box, 4000)
    q = box["queue"]
    out = _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        {_alloc(f, 400)}
        : > '{q}'
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        rm -f '{q}'
        WATCHGOD_ACT=0
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        echo OBSERVED=$(ls '{q}' 2>/dev/null | wc -l)
        WATCHGOD_ACT=1
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        """,
    )
    assert "OBSERVED=0" in out.stdout, out.stdout
    pages = _pages(box)
    assert len(pages) == 1 and "observe" not in pages[0]["dedupe_key"], [
        p["dedupe_key"] for p in pages
    ]


def test_a_page_in_one_mode_does_not_clear_the_debt_of_the_other(box):
    """Codex (PR #3290, round 4): a growth page fails in ACT, the file keeps
    growing and an OBSERVE page succeeds, growth stops, ACT resumes: ACT still
    owes its page and sends it."""
    f = _file(box, "out.log", 60)
    _holder(box, 5, f)
    dom = _domain(box, 4000)
    q = box["queue"]
    out = _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        {_alloc(f, 460)}
        : > '{q}'
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        rm -f '{q}'
        WATCHGOD_ACT=0
        {_alloc(f, 860)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        WATCHGOD_ACT=1
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        echo DUE=${{#_RW_DUE[@]}}
        """,
    )
    keys = [p["dedupe_key"] for p in _pages(box)]
    assert any(k.endswith(":observe") for k in keys), keys
    assert any(not k.endswith(":observe") for k in keys), "the ACT page was still sent"
    assert "DUE=0" in out.stdout


def test_every_remembered_generation_of_an_inode_is_followed(box):
    """Codex (PR #3290, round 4): two paged generations remembered for one
    device:inode (an inode reused after a paged file was removed). Whichever
    the descriptor now leads to is followed; the other never stands in for it,
    whatever order the array is walked in."""
    f = _file(box, "out.log", 200)
    _holder(box, 5, f)
    dom = _domain(box, 400)
    out = _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        g=$(printf '%s\\n' "${{!_RW_LINKS[@]}}")
        key="${{g%:*}}"
        # Stale, older generations of the same inode, also paged, with dead
        # links: several, so the live one is not simply walked first.
        for b in 1 2 3 4 5 6 7 8; do
            _RW_LINKS["$key:$b"]="99$b/9"; _RW_PAGED["$key:$b"]=" mode"; _RW_LAST["$key:$b"]=$(date +%s)
            _RW_PREV["$key:$b"]=$(( 300 * 1048576 ))
        done
        {_empty(f)}
        _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        echo EP=${{_RW_EP[$g]:+open}}
        """,
    )
    assert "EP=open" in out.stdout, out.stdout


def test_a_descriptor_shared_by_two_generations_is_kept_once(box):
    """The same writer descriptor in a stale generation's and the live
    generation's list (a rotated log reopened on the same fd, its inode reused)
    is followed and remembered once, poll after poll (review finding)."""
    f = _file(box, "out.log", 200)
    _holder(box, 5, f)
    dom = _domain(box, 400)
    out = _run(
        box,
        f"""
        wg_runaway_check '{dom}'
        g=$(printf '%s\\n' "${{!_RW_LINKS[@]}}")
        key="${{g%:*}}"
        _RW_LINKS["$key:1"]="5/1"; _RW_PAGED["$key:1"]=" mode"; _RW_LAST["$key:1"]=$(date +%s)
        _RW_PREV["$key:1"]=$(( 300 * 1048576 ))
        {_empty(f)}
        for i in 1 2 3 4 5; do
            _RW_PREV_T=$(( _RW_PREV_T - 30 )); wg_runaway_check '{dom}'
        done
        echo "L=[${{_RW_LINKS[$g]}}]"
        """,
    )
    assert "L=[5/1]" in out.stdout, out.stdout


# ── holder identity (scripts/lib/wg_proc.py) ──────────────────────


def _stat(box, pid: int, comm: str, ppid: int, st: int) -> None:
    """Give a fake holder a stat record (field 22 = start time)."""
    rest = ["S", str(ppid)] + ["0"] * 17 + [str(st)] + ["0"] * 10
    (box["proc"] / str(pid) / "stat").write_text(f"{pid} ({comm}) {' '.join(rest)}\n")


def test_a_holders_identity_is_read_through_its_own_handle(box):
    f = _file(box, "out.log", 60)
    _holder(box, 4242, f, comm="grep", ppid=4241)
    _holder(box, 4241, f, comm="bash", ppid=1)
    # The stat records name differently from comm: the page must show what
    # was read through the handle.
    _stat(box, 4242, "grep-h", 4241, 9001)
    _stat(box, 4241, "bash-h", 1, 9000)
    r = _run(
        box, f"wg_runaway_check '{_domain(box, 200)}'; printf 'ID=%s\\n' \"${{_RW_IDENT[4242]}}\""
    )
    assert "ID=9001\tS\tgrep-h\t4241\tbash-h" in r.stdout, r.stdout + r.stderr
    assert "pid 4242 grep-h (parent 4241: bash-h)" in _pages(box)[0]["body"]


def test_a_newline_in_a_holders_name_cannot_forge_page_lines(box):
    f = _file(box, "out.log", 60)
    _holder(box, 4242, f, comm="evil\nFAKE: all clear", ppid=1)
    _check(box, _domain(box, 200))
    body = _pages(box)[0]["body"]
    assert "pid 4242 evil?FAKE: all clear" in body
    assert "\nFAKE" not in body


def test_a_holder_the_identity_read_does_not_confirm_is_not_a_writer(box, tmp_path):
    """The read ran to completion and confirmed nobody (the descriptor closed,
    or its process exited, after the stat): the file has no writer to name."""
    f = _file(box, "out.log", 60)
    _holder(box, 4242, f, comm="grep")
    stub = tmp_path / "none.py"
    stub.write_text("import sys; sys.stdin.read()\n")
    _run(box, f"_RW_PROC_PY='{stub}'; wg_runaway_check '{_domain(box, 200)}'")
    assert not _pages(box)
    log = (box["home"] / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
    assert "confirmed none of the 1 writers" in log, "a read that confirms nobody is never silent"


def test_detection_survives_an_identity_read_that_fails(box, tmp_path):
    """Without the identity read (it failed, or there is no python), the holders
    are the ones the stat found, and names still lose their control bytes."""
    f = _file(box, "out.log", 60)
    _holder(box, 4242, f, comm="evil\tname", ppid=1)
    stub = tmp_path / "fail.py"
    stub.write_text("import sys; sys.exit(1)\n")
    _run(box, f"_RW_PROC_PY='{stub}'; wg_runaway_check '{_domain(box, 200)}'")
    pages = _pages(box)
    assert len(pages) == 1
    assert "pid 4242 evil?name" in pages[0]["body"]
    log = (box["home"] / ".genesis" / "logs" / "tmp_watchgod.log").read_text()
    assert "identity read failed (status 1" in log, "a failed read is never silent"


def test_a_file_name_cannot_forge_page_lines(box):
    """The runaway file's own name is chosen by whoever created it: a newline,
    a line separator or a bidi control in it reaches the page as "?", like a
    process name (review finding). The real name still keys the domain."""
    name = "evil\nWriters:\n  pid 1 sshd\u2028x\u202egol.txt"
    f = _file(box, name, 60)
    _holder(box, 4242, f, comm="grep")
    _check(box, _domain(box, 200))
    pages = _pages(box)
    assert len(pages) == 1, "still detected: the raw name keys the domain"
    page = pages[0]
    for text in (page["title"], page["body"]):
        assert "\n  pid 1 sshd" not in text
        assert "\u2028" not in text and "\u202e" not in text
    assert "evil?Writers:?  pid 1 sshd?x?gol.txt" in page["body"]
    assert page["body"].count("\nWriters:\n") == 1


def test_without_the_identity_read_names_keep_printable_ascii_only(box, tmp_path):
    """The fallback (no identity read) cannot rely on the locale to know a
    multibyte line separator or bidi control: it keeps printable ASCII and
    replaces every other byte (review finding)."""
    f = _file(box, "out.log", 60)
    _holder(box, 4242, f, comm="evil\u2028FAKE: all clear", ppid=1)
    stub = tmp_path / "fail.py"
    stub.write_text("import sys; sys.exit(1)\n")
    _run(box, f"_RW_PROC_PY='{stub}'; wg_runaway_check '{_domain(box, 200)}'")
    body = _pages(box)[0]["body"]
    assert "pid 4242 evil???FAKE: all clear" in body
    assert "\u2028" not in body
