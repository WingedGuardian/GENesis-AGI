"""genesis.browser.profile_backup: a copy of a browser profile, taken before a
different browser build first opens it.

A browser upgrades its profile in place the first time a newer build opens it, and an
older build then refuses or damages it. The copy is what makes going back possible.
Every case runs on a profile built under tmp_path; no browser is started.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from genesis.browser import profile_backup as pb

FF_LAST = "135.0.1-beta.24_20250315105650"
FF_NEW = "156.0.1-beta.36_20260901000000"


@pytest.fixture(autouse=True)
def _roomy_disk(monkeypatch):
    """A disk with room to spare, whatever the host's: the floor tests set their own."""
    monkeypatch.setattr(pb, "tier_after_write", lambda _p, _n: ("green", "green"))


def _age(p: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(p, (t, t), follow_symlinks=False)


def _firefox_profile(d: Path, last: str | None = FF_LAST) -> Path:
    d.mkdir(parents=True)
    (d / "cookies.sqlite").write_bytes(b"cookies")
    (d / "prefs.js").write_text("user_pref('a', 1);")
    if last is not None:
        (d / "compatibility.ini").write_text(
            f"[Compatibility]\nLastVersion={last}/20250315105650\nLastOSABI=Linux_x86_64-gcc3\n"
        )
    (d / "cache2").mkdir()
    (d / "cache2" / "entry").write_bytes(b"x" * 1000)
    return d


def _engine(d: Path, version="156.0.1-beta.36", build_id="20260901000000") -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "application.ini").write_text(f"[App]\nVersion={version}\nBuildID={build_id}\n")
    binary = d / "camoufox-bin"
    binary.write_bytes(b"\x7fELF")
    return binary


def _copies(profile: Path) -> list[Path]:
    return sorted(profile.parent.glob(f"{profile.name}.pre-*"))


# ── version readers ───────────────────────────────────────────────────────────


def test_firefox_profile_version_keeps_the_build_id(tmp_path):
    """LastVersion is <version>_<buildid>/<platform buildid> (MEASURED on a live
    135.0.1-beta.24 profile); Firefox's downgrade check compares the build id too."""
    p = _firefox_profile(tmp_path / "p")
    assert pb.firefox_profile_version(p) == FF_LAST


@pytest.mark.parametrize(
    "content", [None, "[Compatibility]\n", "not ini at all \x00", "[Other]\nx=1\n"]
)
def test_unreadable_firefox_profile_version_is_none(tmp_path, content):
    p = _firefox_profile(tmp_path / "p", last=None)
    if content is not None:
        (p / "compatibility.ini").write_text(content)
    assert pb.firefox_profile_version(p) is None


def test_firefox_engine_version_matches_the_profile_format(tmp_path):
    """application.ini Version + BuildID is the string a profile records, so an
    unchanged engine is a no-op (MEASURED live: equal on 135.0.1-beta.24)."""
    binary = _engine(tmp_path / "e", "135.0.1-beta.24", "20250315105650")
    assert pb.firefox_engine_version(binary) == FF_LAST


def test_firefox_engine_version_without_build_id_is_none(tmp_path):
    binary = _engine(tmp_path / "e")
    (binary.parent / "application.ini").write_text("[App]\nVersion=156.0.1-beta.36\n")
    assert pb.firefox_engine_version(binary) is None


def test_chromium_versions(tmp_path):
    profile = tmp_path / "browser-profile"
    profile.mkdir()
    (profile / "Last Version").write_text("145.0.7632.6")
    pkg = tmp_path / "playwright"
    (pkg / "driver" / "package").mkdir(parents=True)
    (pkg / "driver" / "package" / "browsers.json").write_text(
        json.dumps(
            {
                "browsers": [
                    {"name": "chromium-headless-shell", "browserVersion": "1.0"},
                    {"name": "chromium", "revision": "1208", "browserVersion": "145.0.7632.6"},
                ]
            }
        )
    )
    assert pb.chromium_profile_version(profile) == "145.0.7632.6"
    assert pb.chromium_engine_version(pkg) == "145.0.7632.6"
    assert pb.chromium_engine_version(tmp_path / "missing") is None


# ── is another browser using the profile? (same semantics as ProfileManager in #2912) ─


def _lock(profile: Path, name: str, target: str) -> None:
    (profile / name).symlink_to(target)


def _spawn(*args: str) -> subprocess.Popen:
    """A sleeping process whose command line carries ``args``, returned only once it
    has exec'd: until then /proc/<pid>/cmdline still shows the forked parent's."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", *args])
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if b"time.sleep(30)" in Path(f"/proc/{proc.pid}/cmdline").read_bytes():
                return proc
        except OSError:
            pass
        time.sleep(0.01)
    proc.kill()
    raise AssertionError("helper process never exec'd")


def test_no_lock_is_not_in_use(tmp_path):
    assert not pb.profile_in_use(_firefox_profile(tmp_path / "p"), "lock")


def test_lock_naming_a_dead_pid_is_not_in_use(tmp_path):
    """Camoufox leaves the lock behind after a clean close (MEASURED: the live
    profile's lock names a pid from days ago)."""
    p = _firefox_profile(tmp_path / "p")
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    _lock(p, "lock", f"127.0.1.1:+{dead.pid}")
    assert not pb.profile_in_use(p, "lock")


def test_live_pid_whose_cmdline_names_the_profile_is_in_use(tmp_path):
    p = _firefox_profile(tmp_path / "p")
    proc = _spawn("-profile", str(p))
    try:
        _lock(p, "lock", f"127.0.1.1:+{proc.pid}")
        assert pb.profile_in_use(p, "lock")
    finally:
        proc.kill()
        proc.wait()


def test_live_pid_of_an_unrelated_process_is_not_in_use(tmp_path):
    """A pid reused by something else after the browser closed."""
    p = _firefox_profile(tmp_path / "p")
    proc = _spawn()
    try:
        _lock(p, "lock", f"127.0.1.1:+{proc.pid}")
        assert not pb.profile_in_use(p, "lock")
    finally:
        proc.kill()
        proc.wait()


def test_chromium_singleton_lock_format(tmp_path):
    p = tmp_path / "browser-profile"
    p.mkdir()
    proc = _spawn(f"--user-data-dir={p}")
    try:
        _lock(p, "SingletonLock", f"myhost-{proc.pid}")
        assert pb.profile_in_use(p, "SingletonLock")
    finally:
        proc.kill()
        proc.wait()


def test_live_pid_of_another_user_is_unknown_and_still_copied(tmp_path, monkeypatch):
    """kill(pid, 0) raising PermissionError means alive and not ours, its command
    line unreadable. Unknown is not "in use": skipping would let the new engine
    upgrade the profile with no copy, while a copy that a write tore is discarded."""
    p = _firefox_profile(tmp_path / "p")
    _lock(p, "lock", "127.0.1.1:+424242")

    def kill(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(pb.os, "kill", kill)
    assert pb.profile_in_use(p, "lock") is None
    assert _backup(p) is not None


def test_a_sibling_directory_in_the_command_line_is_not_this_profile(tmp_path):
    """Matching by substring would take <profile>.pre-v1-... (one of the copies
    this module makes) for the profile itself (#2912 parses the flag value)."""
    p = _firefox_profile(tmp_path / "camoufox-profile")
    sibling = tmp_path / "camoufox-profile.pre-v1-20260101T000000"
    sibling.mkdir()
    proc = _spawn("-profile", str(sibling))
    try:
        _lock(p, "lock", f"127.0.1.1:+{proc.pid}")
        assert pb.profile_in_use(p, "lock") is False
    finally:
        proc.kill()
        proc.wait()


def _spawn_titled(title: str) -> subprocess.Popen:
    """A process whose /proc cmdline is ONE space-joined string, the way Chromium
    rewrites its process title on Linux (MEASURED on chromium-1208)."""
    # cat with no arguments waits on stdin, so the title is the WHOLE command line.
    proc = subprocess.Popen(["bash", "-c", f'exec -a "{title}" cat'], stdin=subprocess.PIPE)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if Path(f"/proc/{proc.pid}/cmdline").read_bytes().startswith(title.split()[0].encode()):
                return proc
        except OSError:
            pass
        time.sleep(0.01)
    proc.kill()
    raise AssertionError("helper process never exec'd")


@pytest.mark.parametrize("named", ["self", "sibling"])
def test_chromium_single_string_title(tmp_path, named):
    p = tmp_path / "browser-profile"
    p.mkdir()
    target = p if named == "self" else tmp_path / "browser-profile.pre-v1-20260101T000000"
    proc = _spawn_titled(
        f"/opt/chrome/chrome --no-sandbox --user-data-dir={target} --remote-debugging-pipe"
    )
    try:
        _lock(p, "SingletonLock", f"myhost-{proc.pid}")
        assert pb.profile_in_use(p, "SingletonLock") is (named == "self")
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.parametrize(
    "flag",
    [
        ("-profile", "{p}"),
        ("--profile", "{p}/"),
        ("--user-data-dir", "{p}"),
        ("--user-data-dir={p}",),
    ],
)
def test_flag_spellings_naming_the_profile_are_in_use(tmp_path, flag):
    p = _firefox_profile(tmp_path / "p")
    proc = _spawn(*(a.format(p=p) for a in flag))
    try:
        _lock(p, "lock", f"127.0.1.1:+{proc.pid}")
        assert pb.profile_in_use(p, "lock") is True
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.parametrize("target", ["127.0.1.1:+1", "127.0.1.1:+0", "garbage", "host-"])
def test_lock_never_probes_init_or_garbage(tmp_path, target):
    p = _firefox_profile(tmp_path / "p")
    _lock(p, "lock", target)
    assert not pb.profile_in_use(p, "lock")


# ── copy_aside ────────────────────────────────────────────────────────────────


def test_copy_aside_copies_without_caches_or_locks(tmp_path):
    p = _firefox_profile(tmp_path / "p")
    _lock(p, "lock", "127.0.1.1:+99999999")
    dest = pb.copy_aside(p, "v1")
    assert dest.name.startswith("p.pre-v1-") and dest.is_dir()
    assert (dest / "cookies.sqlite").read_bytes() == b"cookies"
    assert not (dest / "cache2").exists()
    assert not (dest / "lock").is_symlink()
    assert not list(tmp_path.glob("*.tmp"))


def test_copy_aside_keeps_older_copies_with_the_same_label(tmp_path):
    """Deleting copies is retention's job (disk_hygiene), never the copy's."""
    p = _firefox_profile(tmp_path / "p")
    first = pb.copy_aside(p, "v1")
    second = pb.copy_aside(p, "v1")
    assert first != second and first.is_dir() and second.is_dir()


def test_copy_aside_failure_leaves_nothing(tmp_path, monkeypatch):
    p = _firefox_profile(tmp_path / "p")

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(pb.shutil, "copytree", boom)
    with pytest.raises(OSError):
        pb.copy_aside(p, "v1")
    assert _copies(p) == []


def test_copy_aside_stamps_the_copy_now(tmp_path):
    """copytree keeps the profile's old mtime; retention ages a copy from when it was made."""
    p = _firefox_profile(tmp_path / "p")
    _age(p, 30 * 86400)
    dest = pb.copy_aside(p, "v1")
    assert time.time() - dest.stat().st_mtime < 60


# ── backup_before_launch ──────────────────────────────────────────────────────


def _backup(p, recorded=FF_LAST, engine=FF_NEW, lock="lock"):
    return pb.backup_before_launch(p, recorded, engine, lock_name=lock)


def test_same_version_is_a_no_op(tmp_path):
    p = _firefox_profile(tmp_path / "p")
    assert _backup(p, FF_LAST, FF_LAST) is None
    assert _copies(p) == []


def test_a_copy_whose_own_record_is_not_its_label_is_not_published(tmp_path, monkeypatch):
    # Another launch opened the profile with the new build after this one read the
    # recorded version: the copy would hold the upgraded profile under the old label.
    p = _firefox_profile(tmp_path / "p")
    real = pb._snapshot

    def upgraded_first(profile):
        if profile == p and FF_NEW not in (p / "compatibility.ini").read_text():
            (p / "compatibility.ini").write_text(
                f"[Compatibility]\nLastVersion={FF_NEW}/20260901000000\n"
            )
        return real(profile)

    monkeypatch.setattr(pb, "_snapshot", upgraded_first)
    dest = pb.backup_before_launch(
        p, FF_LAST, FF_NEW, lock_name="lock", read_version=pb.firefox_profile_version
    )
    assert dest is None
    assert _copies(p) == []
    assert not list(tmp_path.glob("*.tmp"))


def test_a_copy_whose_own_record_is_its_label_is_published(tmp_path):
    p = _firefox_profile(tmp_path / "p")
    dest = pb.backup_before_launch(
        p, FF_LAST, FF_NEW, lock_name="lock", read_version=pb.firefox_profile_version
    )
    assert dest is not None and pb.firefox_profile_version(dest) == FF_LAST


def test_both_profile_kinds_check_the_copy_against_its_label(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(pb, "backup_before_launch", lambda *a, **k: seen.append(k["read_version"]))
    pb.backup_camoufox_profile(tmp_path / "p", tmp_path / "bin")
    pb.backup_chromium_profile(tmp_path / "p", "playwright")
    assert seen == [pb.firefox_profile_version, pb.chromium_profile_version]


@pytest.mark.parametrize("make", ["missing", "empty"])
def test_new_profile_is_a_no_op(tmp_path, make):
    p = tmp_path / "p"
    if make == "empty":
        p.mkdir()
    assert _backup(p) is None
    assert _copies(p) == []


def test_different_version_copies_once_labelled_by_the_old_version(tmp_path):
    p = _firefox_profile(tmp_path / "p")
    dest = _backup(p)
    assert dest is not None and dest.name.startswith(f"p.pre-v{FF_LAST}-")


def test_older_engine_copies_too(tmp_path):
    """Any difference counts: going back to an older build is also a one-way trip."""
    p = _firefox_profile(tmp_path / "p")
    assert _backup(p, FF_NEW, FF_LAST) is not None


@pytest.mark.parametrize("recorded,engine", [(None, FF_NEW), (FF_LAST, None)])
def test_unreadable_version_backs_up(tmp_path, recorded, engine):
    """A missed copy of a one-way upgrade cannot be recovered; an extra one is pruned."""
    p = _firefox_profile(tmp_path / "p")
    dest = _backup(p, recorded, engine)
    assert dest is not None
    if recorded is None:
        assert dest.name.startswith("p.pre-vunknown-")


def test_label_from_a_hostile_version_string_is_safe(tmp_path):
    p = _firefox_profile(tmp_path / "p")
    dest = _backup(p, "../../etc; rm -rf ~", FF_NEW)
    assert dest is not None and dest.parent == p.parent
    assert dest.name.startswith("p.pre-vunknown-")


def test_repeated_launches_make_one_copy(tmp_path):
    """A launch that fails before opening the profile leaves it at the old version;
    the next attempt must not copy it again."""
    p = _firefox_profile(tmp_path / "p")
    _age_tree(p, 3600)
    assert _backup(p) is not None
    assert _backup(p) is None
    assert len(_copies(p)) == 1


def test_lock_file_churn_does_not_force_a_new_copy(tmp_path):
    p = _firefox_profile(tmp_path / "p")
    _age_tree(p, 3600)
    _backup(p)
    (p / ".parentlock").write_text("")  # a failed launch touched only its locks
    _lock(p, "lock", "127.0.1.1:+99999999")
    later = time.time() + 5  # creating them moved the directory's own mtime
    os.utime(p, (later, later))
    assert _backup(p) is None


def test_profile_used_since_the_copy_is_copied_again(tmp_path):
    """After a pin-back the user browses on the same version: compatibility.ini does
    not change, the cookies do. The next upgrade must copy that newer state."""
    p = _firefox_profile(tmp_path / "p")
    _age_tree(p, 3600)
    first = _backup(p)
    _age(first, 1800)
    (p / "cookies.sqlite").write_bytes(b"newer cookies")
    second = _backup(p)
    assert second is not None and second != first
    assert (second / "cookies.sqlite").read_bytes() == b"newer cookies"


def test_unfinished_copy_never_counts_as_one(tmp_path):
    p = _firefox_profile(tmp_path / "p")
    _age_tree(p, 3600)
    (tmp_path / f"p.pre-v{FF_LAST}-20260101T000000.abc.tmp").mkdir()
    assert _backup(p) is not None


def test_cache_churn_does_not_force_a_new_copy(tmp_path):
    p = _firefox_profile(tmp_path / "p")
    _age_tree(p, 3600)
    _backup(p)
    (p / "cache2" / "new").write_bytes(b"y")
    assert _backup(p) is None


def test_in_use_profile_is_not_copied(tmp_path):
    p = _firefox_profile(tmp_path / "p")
    proc = _spawn("-profile", str(p))
    try:
        _lock(p, "lock", f"127.0.1.1:+{proc.pid}")
        assert _backup(p) is None
    finally:
        proc.kill()
        proc.wait()
    assert _copies(p) == []


@pytest.mark.parametrize(
    "now,after,copied",
    [
        ("green", "green", True),
        ("green", "yellow", True),
        ("yellow", "yellow", True),
        ("yellow", "orange", False),  # the copy would page the watchdog
        ("green", "orange", False),
        ("orange", "orange", True),  # already there: the way back still matters
        ("orange", "red", False),  # never into RED
        ("red", "red", False),
    ],
)
def test_the_copy_never_moves_the_disk_into_orange_or_red(
    tmp_path, monkeypatch, now, after, copied
):
    """The disk guardian's tiers decide (owner ruling 2026-10-11): a copy may not be
    what moves the disk into ORANGE or RED, and on a disk already ORANGE it is still
    made unless it would reach RED, since without it a one-way upgrade is final."""
    p = _firefox_profile(tmp_path / "p")
    monkeypatch.setattr(pb, "tier_after_write", lambda _p, _n: (now, after))
    assert (_backup(p) is not None) is copied
    assert len(_copies(p)) == int(copied)


def test_the_tier_is_judged_for_the_copy_beside_the_profile(tmp_path, monkeypatch):
    p = _firefox_profile(tmp_path / "p")
    seen = []
    monkeypatch.setattr(
        pb, "tier_after_write", lambda at, n: seen.append((at, n)) or ("green",) * 2
    )
    _backup(p)
    size = sum(e[1] for e in pb._snapshot(p).values())
    assert seen == [(p.parent, size * pb._FREE_FACTOR)]


def test_a_write_during_the_copy_discards_it_and_copies_again(tmp_path, monkeypatch):
    """A copy that overlaps a write is torn; stamped at the end it would read as
    fresh and stop every later copy."""
    p = _firefox_profile(tmp_path / "p")
    _age_tree(p, 3600)
    real = shutil.copytree
    calls = []

    def copytree(src, dst, **kw):
        out = real(src, dst, **kw)
        calls.append(dst)
        if len(calls) == 1:
            (p / "cookies.sqlite").write_bytes(b"written mid-copy")
            now = time.time()  # file times tick coarsely; state the write time exactly
            os.utime(p / "cookies.sqlite", (now, now))
        return out

    monkeypatch.setattr(pb.shutil, "copytree", copytree)
    started = time.time()
    dest = _backup(p)
    assert len(calls) == 2 and dest is not None
    assert (dest / "cookies.sqlite").read_bytes() == b"written mid-copy"
    assert started - 1 <= dest.stat().st_mtime <= time.time()
    assert not list(tmp_path.glob("*.tmp"))


def _mutate_once(monkeypatch, change):
    real = shutil.copytree
    calls = []

    def copytree(src, dst, **kw):
        out = real(src, dst, **kw)
        calls.append(dst)
        if len(calls) == 1:
            change()
        return out

    monkeypatch.setattr(pb.shutil, "copytree", copytree)
    return calls


def test_a_rename_during_the_copy_discards_it(tmp_path, monkeypatch):
    """A rename keeps every mtime, so only a before/after listing sees it."""
    p = _firefox_profile(tmp_path / "p")
    calls = _mutate_once(monkeypatch, lambda: (p / "prefs.js").rename(p / "prefs.js.bak"))
    dest = _backup(p)
    assert len(calls) == 2 and (dest / "prefs.js.bak").exists() and not (dest / "prefs.js").exists()


def test_a_delete_during_the_copy_discards_it(tmp_path, monkeypatch):
    p = _firefox_profile(tmp_path / "p")
    calls = _mutate_once(monkeypatch, lambda: (p / "prefs.js").unlink())
    dest = _backup(p)
    assert len(calls) == 2 and not (dest / "prefs.js").exists()


def test_a_file_vanishing_under_copytree_is_retried(tmp_path, monkeypatch):
    p = _firefox_profile(tmp_path / "p")
    real = shutil.copytree
    calls = []

    def copytree(src, dst, **kw):
        calls.append(dst)
        if len(calls) == 1:
            (p / "prefs.js").unlink()  # the browser removed it mid-copy
            raise shutil.Error([(str(src), str(dst), "No such file or directory")])
        return real(src, dst, **kw)

    monkeypatch.setattr(pb.shutil, "copytree", copytree)
    assert _backup(p) is not None and len(calls) == 2


def test_an_unreadable_file_is_reported_not_retried(tmp_path, monkeypatch, caplog):
    """A copy error with nothing changing is not a busy profile."""
    p = _firefox_profile(tmp_path / "p")
    calls = []

    def copytree(src, dst, **kw):
        calls.append(dst)
        raise shutil.Error([(str(src), str(dst), "Permission denied")])

    monkeypatch.setattr(pb.shutil, "copytree", copytree)
    assert _backup(p) is None
    assert len(calls) == 1 and "kept changing" not in caplog.text


def test_a_file_dated_in_the_future_does_not_block_the_copy(tmp_path):
    """After the clock steps back, a file can be newer than now; that is not a
    write during the copy."""
    p = _firefox_profile(tmp_path / "p")
    later = time.time() + 3600
    os.utime(p / "prefs.js", (later, later))
    assert _backup(p) is not None


def test_a_profile_that_keeps_changing_is_not_copied(tmp_path, monkeypatch):
    p = _firefox_profile(tmp_path / "p")
    _age_tree(p, 3600)
    real = shutil.copytree

    def copytree(src, dst, **kw):
        out = real(src, dst, **kw)
        (p / "cookies.sqlite").write_bytes(str(time.time()).encode())
        now = time.time()
        os.utime(p / "cookies.sqlite", (now, now))
        return out

    monkeypatch.setattr(pb.shutil, "copytree", copytree)
    assert _backup(p) is None
    assert _copies(p) == [] and not list(tmp_path.glob("*.tmp"))


def test_the_copy_is_stamped_when_it_started(tmp_path, monkeypatch):
    """A write landing after the copy started, but before it ended, is newer than
    the copy, so the next launch copies again."""
    p = _firefox_profile(tmp_path / "p")
    _age_tree(p, 3600)
    real = shutil.copytree

    def slow_copytree(src, dst, **kw):
        out = real(src, dst, **kw)
        time.sleep(0.05)
        return out

    monkeypatch.setattr(pb.shutil, "copytree", slow_copytree)
    before = time.time()
    dest = _backup(p)
    assert dest.stat().st_mtime < before + 0.05


def test_a_failing_copy_never_raises(tmp_path, monkeypatch):
    """The copy is advisory: a launch is never refused because a backup failed."""
    p = _firefox_profile(tmp_path / "p")

    def boom(*a, **k):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(pb.shutil, "copytree", boom)
    assert _backup(p) is None


def _age_tree(root: Path, seconds: float) -> None:
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            _age(Path(dirpath) / name, seconds)
    _age(root, seconds)


# ── per-browser entry points the launch paths call ───────────────────────────


def test_camoufox_entry_point_reads_both_versions(tmp_path):
    p = _firefox_profile(tmp_path / "camoufox-profile")
    binary = _engine(tmp_path / "engine")
    dest = pb.backup_camoufox_profile(p, binary)
    assert dest is not None and dest.name.startswith(f"camoufox-profile.pre-v{FF_LAST}-")


def test_camoufox_entry_point_no_op_on_the_same_engine(tmp_path):
    p = _firefox_profile(tmp_path / "camoufox-profile")
    binary = _engine(tmp_path / "engine", "135.0.1-beta.24", "20250315105650")
    assert pb.backup_camoufox_profile(p, binary) is None


def _chromium_profile(tmp_path, last="145.0.7632.6"):
    p = tmp_path / "browser-profile"
    (p / "Default").mkdir(parents=True)
    (p / "Default" / "Cookies").write_bytes(b"c")
    (p / "Last Version").write_text(last)
    return p


def _fake_package(tmp_path, monkeypatch, version="150.0.1.2"):
    """A package directory carrying browsers.json, importable as ``fakepw``."""
    pkg = tmp_path / "site" / "fakepw"
    (pkg / "driver" / "package").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "driver" / "package" / "browsers.json").write_text(
        json.dumps({"browsers": [{"name": "chromium", "browserVersion": version}]})
    )
    monkeypatch.syspath_prepend(str(tmp_path / "site"))
    return pkg


def test_chromium_entry_point(tmp_path, monkeypatch):
    p = _chromium_profile(tmp_path)
    _fake_package(tmp_path, monkeypatch)
    dest = pb.backup_chromium_profile(p, "fakepw")
    assert dest is not None and dest.name.startswith("browser-profile.pre-v145.0.7632.6-")
    assert (dest / "Default" / "Cookies").read_bytes() == b"c"


def test_chromium_entry_point_no_op_on_the_same_version(tmp_path, monkeypatch):
    p = _chromium_profile(tmp_path, last="150.0.1.2")
    _fake_package(tmp_path, monkeypatch)
    assert pb.backup_chromium_profile(p, "fakepw") is None


def test_chromium_package_dir_never_raises(monkeypatch):
    """A stubbed module with no spec, or no module at all, reads as unknown."""
    assert pb.chromium_package_dir("no_such_package_xyz") is None
    monkeypatch.setitem(sys.modules, "stubbed_pkg_xyz", object())
    assert pb.chromium_package_dir("stubbed_pkg_xyz") is None


# ── round 1 (#3199) ───────────────────────────────────────────────────────────


def test_an_untiered_disk_skips_the_copy(tmp_path, monkeypatch):
    p = _firefox_profile(tmp_path / "p")
    monkeypatch.setattr(pb, "tier_after_write", lambda _p, _n: None)
    assert _backup(p) is None and _copies(p) == []


def test_a_file_dated_in_the_future_does_not_force_a_copy_per_launch(tmp_path):
    """Freshness is the copied content, not the clock: a future-dated file must not
    make every launch copy the unchanged profile again."""
    p = _firefox_profile(tmp_path / "p")
    later = time.time() + 86400
    os.utime(p / "prefs.js", (later, later))
    assert _backup(p) is not None
    assert _backup(p) is None
    assert len(_copies(p)) == 1


def test_a_copy_is_judged_by_its_own_files(tmp_path):
    """No record is stored: a copy whose files match the profile's (path, size,
    mtime) is current, whenever and however it was made."""
    p = _firefox_profile(tmp_path / "p")
    old = tmp_path / f"p.pre-v{FF_LAST}-20260101T000000"
    shutil.copytree(p, old, ignore=pb._ignore_for(p))
    assert _backup(p) is None
    (old / "prefs.js").write_text("edited after the copy")
    assert _backup(p) is not None


def test_a_backup_lock_held_too_long_skips_the_copy(tmp_path, monkeypatch, caplog):
    """A stuck holder (a stopped session, a hung filesystem) must not hold every
    other session's launch: past the wait, the launch goes ahead without a copy."""
    import fcntl
    import logging

    p = _firefox_profile(tmp_path / "p")
    monkeypatch.setattr(pb, "_LOCK_WAIT_S", 0.3)
    with open(pb._lock_path(p), "w") as held, caplog.at_level(logging.WARNING):
        fcntl.flock(held, fcntl.LOCK_EX)
        assert _backup(p) is None
    assert _copies(p) == [] and "another launch" in caplog.text


def test_concurrent_launches_make_one_copy(tmp_path):
    """Each session's MCP server runs its own launch path; the first copies and the
    next, waiting on the same lock, finds that copy fresh."""
    import threading

    p = _firefox_profile(tmp_path / "p")
    results = []
    threads = [threading.Thread(target=lambda: results.append(_backup(p))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert len(_copies(p)) == 1
    assert sum(r is not None for r in results) == 1


def test_a_held_backup_lock_makes_the_launch_wait(tmp_path):
    import fcntl
    import threading

    p = _firefox_profile(tmp_path / "p")
    lock = pb._lock_path(p)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        t = threading.Thread(target=_backup, args=(p,))
        t.start()
        t.join(timeout=1.0)
        assert t.is_alive()  # waiting for the copy in progress
        assert _copies(p) == []
    t.join(timeout=60)
    assert not t.is_alive() and len(_copies(p)) == 1


def test_lock_names_below_the_profile_root_are_copied(tmp_path):
    """Only the browser's own launch locks sit at the profile root; a site or an
    extension may keep a file called "lock" deeper down."""
    p = _firefox_profile(tmp_path / "p")
    (p / "storage" / "default").mkdir(parents=True)
    (p / "storage" / "default" / "lock").write_text("site data")
    (p / ".parentlock").write_text("")
    dest = _backup(p)
    assert (dest / "storage" / "default" / "lock").read_text() == "site data"
    assert not (dest / ".parentlock").exists()


def test_a_change_below_the_root_under_a_lock_name_counts(tmp_path):
    p = _firefox_profile(tmp_path / "p")
    (p / "sub").mkdir()
    (p / "sub" / "lock").write_text("a")
    first = _backup(p)
    _age(first, 60)
    (p / "sub" / "lock").write_text("b")
    assert _backup(p) is not None


# ── round 3 (#3199) ───────────────────────────────────────────────────────────


def test_each_copy_attempt_is_admitted_for_what_it_copies(tmp_path, monkeypatch):
    """A profile that grows between the first scan and a retry is judged again: the
    admission is bound to the snapshot each attempt copies."""
    p = _firefox_profile(tmp_path / "p")
    seen = []
    monkeypatch.setattr(pb, "tier_after_write", lambda at, n: seen.append(n) or ("green",) * 2)
    real = pb.shutil.copytree
    grown = {"done": False}

    def copytree(src, dst, *a, **kw):
        out = real(src, dst, *a, **kw)
        if not grown["done"]:
            grown["done"] = True
            (p / "grew.sqlite").write_bytes(b"z" * 50_000)  # tears this attempt
        return out

    monkeypatch.setattr(pb.shutil, "copytree", copytree)
    assert _backup(p) is not None
    assert len(seen) == 2 and seen[1] > seen[0]


def test_a_retry_that_no_longer_fits_is_not_copied(tmp_path, monkeypatch):
    p = _firefox_profile(tmp_path / "p")
    calls = []

    def tiers(at, n):
        calls.append(n)
        return ("green", "green") if len(calls) == 1 else ("yellow", "orange")

    monkeypatch.setattr(pb, "tier_after_write", tiers)
    real = pb.shutil.copytree

    torn = {"done": False}

    def copytree(src, dst, *a, **kw):
        out = real(src, dst, *a, **kw)
        if not torn["done"]:
            torn["done"] = True
            (p / "grew.sqlite").write_bytes(b"z")  # tears the first attempt only
        return out

    monkeypatch.setattr(pb.shutil, "copytree", copytree)
    assert _backup(p) is None and _copies(p) == []


def test_both_profiles_beside_each_other_share_one_backup_lock(tmp_path):
    """Two engines first launched at once would each measure the same free space."""
    a = tmp_path / "camoufox-profile"
    b = tmp_path / "browser-profile"
    assert pb._lock_path(a) == pb._lock_path(b)
