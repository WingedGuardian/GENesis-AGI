"""Tests for scripts/disk_reclaim.py — regenerable-cache reclamation.

Focus: the safety allowlist (a bug here could delete real data), resilient
partial deletion, MEDIUM-tier gating, and the --fail-above exit contract that
the remediation registry relies on to escalate a stuck disk.
"""

import fcntl
import importlib.util
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Load the script as a module (it's not a package — use importlib). It must be
# registered in sys.modules BEFORE exec so its frozen @dataclass can resolve
# its own module namespace.
_SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "disk_reclaim.py"
_spec = importlib.util.spec_from_file_location("disk_reclaim", _SCRIPT_PATH)
_mod = importlib.util.module_from_spec(_spec)
sys.modules["disk_reclaim"] = _mod
_spec.loader.exec_module(_mod)

CacheTarget = _mod.CacheTarget


# ─── Safety allowlist ────────────────────────────────────────────────────


class TestIsSafeTarget:
    def test_rejects_home(self):
        assert _mod._is_safe_target(_mod.HOME) is False

    def test_rejects_repo(self):
        assert _mod._is_safe_target(_mod.HOME / "genesis") is False

    def test_rejects_venv_and_data(self):
        assert _mod._is_safe_target(_mod.HOME / "genesis" / ".venv") is False
        assert _mod._is_safe_target(_mod.HOME / "genesis" / "data") is False

    def test_rejects_filesystem_root(self):
        assert _mod._is_safe_target(Path("/")) is False

    def test_rejects_symlink(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        assert _mod._is_safe_target(link) is False

    def test_rejects_ancestor_of_protected(self, tmp_path):
        # A target that CONTAINS a protected path must be refused.
        protected = tmp_path / "cache" / "keepme"
        protected.mkdir(parents=True)
        with patch.object(_mod, "_PROTECTED", {protected}):
            assert _mod._is_safe_target(tmp_path / "cache") is False

    def test_accepts_plain_cache_dir(self, tmp_path):
        cache = tmp_path / "somecache"
        cache.mkdir()
        assert _mod._is_safe_target(cache) is True


# ─── Cache clearing ──────────────────────────────────────────────────────


def _make_cache(tmp_path: Path, name: str = "c", tier: str = "cheap") -> CacheTarget:
    d = tmp_path / name
    d.mkdir()
    (d / "a.bin").write_bytes(b"x" * 1000)
    (d / "sub").mkdir()
    (d / "sub" / "b.bin").write_bytes(b"y" * 2000)
    return CacheTarget(f"test {name}", d, tier)


class TestClearCache:
    def test_dry_run_reports_but_keeps(self, tmp_path):
        target = _make_cache(tmp_path)
        n = _mod._clear_cache(target, apply=False)
        assert n == 3000
        assert target.path.exists()  # untouched

    def test_apply_removes_and_reports(self, tmp_path):
        target = _make_cache(tmp_path)
        n = _mod._clear_cache(target, apply=True)
        assert n == 3000
        assert not target.path.exists()

    def test_missing_dir_is_noop(self, tmp_path):
        target = CacheTarget("missing", tmp_path / "nope", "cheap")
        assert _mod._clear_cache(target, apply=True) == 0

    def test_resilient_to_permission_error(self, tmp_path):
        # A read-only subdir blocks unlinking its file; clear must reclaim what
        # it can and report the rest as skipped rather than aborting.
        target = _make_cache(tmp_path, "locked")
        locked = target.path / "sub"
        locked.chmod(0o500)  # r-x: cannot remove b.bin inside
        try:
            reclaimed = _mod._clear_cache(target, apply=True)
            # The top-level a.bin (1000 B) is reclaimable; the locked file isn't.
            assert reclaimed >= 1000
            assert reclaimed < 3000
        finally:
            locked.chmod(0o700)  # restore so tmp cleanup works


# ─── main(): gating + exit contract ──────────────────────────────────────


class TestMain:
    def _run(self, argv, disk_pct):
        with (
            patch.object(_mod.sys, "argv", ["disk_reclaim.py", *argv]),
            patch.object(_mod, "_disk_pct", return_value=disk_pct),
        ):
            return _mod.main()

    def test_medium_held_below_threshold(self, tmp_path):
        cheap = _make_cache(tmp_path, "cheap1", "cheap")
        medium = _make_cache(tmp_path, "med1", "medium")
        with patch.object(_mod, "_CACHE_TARGETS", [cheap, medium]):
            self._run(["--apply", "--if-above", "90"], disk_pct=80.0)
        assert not cheap.path.exists()  # cheap always cleared
        assert medium.path.exists()  # medium held below 90

    def test_medium_cleared_above_threshold(self, tmp_path):
        cheap = _make_cache(tmp_path, "cheap2", "cheap")
        medium = _make_cache(tmp_path, "med2", "medium")
        with patch.object(_mod, "_CACHE_TARGETS", [cheap, medium]):
            self._run(["--apply", "--if-above", "90"], disk_pct=92.0)
        assert not cheap.path.exists()
        assert not medium.path.exists()  # medium cleared at/above 90

    def test_dry_run_deletes_nothing(self, tmp_path):
        cheap = _make_cache(tmp_path, "cheap3", "cheap")
        with patch.object(_mod, "_CACHE_TARGETS", [cheap]):
            self._run(["--dry-run", "--if-above", "0"], disk_pct=95.0)
        assert cheap.path.exists()

    def test_fail_above_returns_2_when_still_critical(self, tmp_path):
        with patch.object(_mod, "_CACHE_TARGETS", []):
            rc = self._run(["--apply", "--fail-above", "90"], disk_pct=93.0)
        assert rc == 2

    def test_fail_above_returns_0_when_recovered(self, tmp_path):
        with patch.object(_mod, "_CACHE_TARGETS", []):
            rc = self._run(["--apply", "--fail-above", "90"], disk_pct=70.0)
        assert rc == 0


class TestLastResortTier:
    """The index DBs must survive normal disk pressure — deleting one is what
    turns every later index into a full 0->100 rebuild that storms the box."""

    def _run(self, argv, disk_pct, home):
        with (
            patch.object(_mod.sys, "argv", ["disk_reclaim.py", *argv]),
            patch.object(_mod, "_disk_pct", return_value=disk_pct),
            patch.dict(_mod.os.environ, {"GENESIS_HOME": str(home)}),
        ):
            return _mod.main()

    def test_index_caches_are_last_resort_tier(self):
        tiers = {
            t.tier
            for t in _mod._CACHE_TARGETS
            if "index" in t.description or "gitnexus" in t.description.lower()
        }
        assert tiers == {"last_resort"}, tiers

    def test_held_below_last_resort_threshold(self, tmp_path):
        # Medium gate (90) is crossed but the last-resort threshold is not: DB survives.
        lr = _make_cache(tmp_path, "lr1", "last_resort")
        with patch.object(_mod, "_CACHE_TARGETS", [lr]):
            self._run(
                ["--apply", "--if-above", "90", "--last-resort-above", "95"],
                disk_pct=92.0,
                home=tmp_path / ".genesis",
            )
        assert lr.path.exists()  # NOT deleted at 92% — the whole point

    def test_default_never_clears_the_indexes(self, tmp_path):
        # #2567: with no --last-resort-above, no disk level clears an index DB.
        # Index deletion belongs to the disk guardian's RED pass, which asks for
        # it explicitly; any other caller (the remediation registry, a model
        # running the script by hand) must not get it by default.
        lr = _make_cache(tmp_path, "lr-default", "last_resort")
        home = tmp_path / ".genesis"
        with patch.object(_mod, "_CACHE_TARGETS", [lr]):
            rc = self._run(["--apply", "--if-above", "0"], disk_pct=99.9, home=home)
        assert rc == 0  # a held index is not a deferral, so no retry signal
        assert lr.path.exists()
        assert not (home / "index-requests").exists()

    def test_cleared_at_last_resort_threshold_drops_marker(self, tmp_path):
        lr = _make_cache(tmp_path, "lr2", "last_resort")
        home = tmp_path / ".genesis"
        with patch.object(_mod, "_CACHE_TARGETS", [lr]):
            self._run(["--apply", "--last-resort-above", "95"], disk_pct=96.0, home=home)
        assert not lr.path.exists()  # cleared at 96%
        with sqlite3.connect(home / "index-requests" / "queue.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM pending").fetchone()[0] == 1

    def test_empty_last_resort_cache_does_not_drop_rebuild_marker(self, tmp_path):
        empty = _mod.CacheTarget("empty index", tmp_path / "empty-index", "last_resort")
        empty.path.mkdir()
        home = tmp_path / ".genesis"

        with patch.object(_mod, "_CACHE_TARGETS", [empty]):
            rc = self._run(
                ["--apply", "--last-resort-above", "95"],
                disk_pct=96.0,
                home=home,
            )

        assert rc == 0
        assert empty.path.exists()
        assert not (home / "index-requests").exists()

    def test_busy_queue_spools_rebuild_before_last_resort_cache_clear(self, tmp_path):
        lr = _make_cache(tmp_path, "lr-busy", "last_resort")
        home = tmp_path / ".genesis"
        marker_dir = home / "index-requests"
        with patch.dict(_mod.os.environ, {"GENESIS_HOME": str(home)}):
            assert _mod._drop_index_marker()
        lock = sqlite3.connect(marker_dir / "queue.sqlite3", isolation_level=None)
        lock.execute("BEGIN IMMEDIATE")
        try:
            with patch.object(_mod, "_CACHE_TARGETS", [lr]):
                self._run(["--apply", "--last-resort-above", "95"], disk_pct=96.0, home=home)
            spools = list(marker_dir.glob(".spool-*.spool"))
            assert len(spools) == 1, "the rebuild request must be durable before deletion"
            assert not lr.path.exists()
        finally:
            lock.rollback()
            lock.close()
        with patch.dict(_mod.os.environ, {"GENESIS_HOME": str(home)}):
            import index_marker

            assert len(index_marker.list_markers()) == 1
        assert not spools[0].exists()

    def test_runner_cannot_consume_marker_during_last_resort_deletion(self, tmp_path):
        lr = _make_cache(tmp_path, "lr-race", "last_resort")
        home = tmp_path / ".genesis"
        runner_lock = home / "locks" / "code-intel-runner.lock"
        original_clear = _mod._clear_cache
        runner_acquired = []

        def racing_clear(target, *, apply):
            with runner_lock.open("a") as lock:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    runner_acquired.append(True)
                except BlockingIOError:
                    runner_acquired.append(False)
            return original_clear(target, apply=apply)

        with (
            patch.object(_mod, "_CACHE_TARGETS", [lr]),
            patch.object(_mod, "_clear_cache", side_effect=racing_clear),
        ):
            self._run(["--apply", "--last-resort-above", "95"], disk_pct=96.0, home=home)
        assert runner_acquired == [False]
        with sqlite3.connect(home / "index-requests" / "queue.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM pending").fetchone()[0] == 1

    def test_active_runner_at_critical_threshold_returns_failure_signal(self, tmp_path):
        lr = _make_cache(tmp_path, "lr-active", "last_resort")
        home = tmp_path / ".genesis"
        runner_lock = home / "locks" / "code-intel-runner.lock"
        runner_lock.parent.mkdir(parents=True)
        with runner_lock.open("a") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(_mod, "_CACHE_TARGETS", [lr]):
                rc = self._run(
                    ["--apply", "--last-resort-above", "95"],
                    disk_pct=96.0,
                    home=home,
                )

        assert rc == 2
        assert lr.path.exists()

    def test_dry_run_never_drops_marker(self, tmp_path):
        lr = _make_cache(tmp_path, "lr3", "last_resort")
        home = tmp_path / ".genesis"
        with patch.object(_mod, "_CACHE_TARGETS", [lr]):
            self._run(["--last-resort-above", "1"], disk_pct=96.0, home=home)
        assert lr.path.exists()  # dry-run keeps it
        assert not (home / "index-requests").exists()  # and drops no marker


_READ_MOUNTS = _mod._mount_points  # the real parser, fed a crafted table


class TestMountRefusal:
    """Review of #2570: rmtree follows into mounts, so a cache dir that is, or
    holds, a mount (a bind mount keeps its parent's device) is refused."""

    def _info(self, tmp_path, *mounts: str) -> Path:
        f = tmp_path / "mountinfo"
        f.write_text("".join(f"36 35 98:0 / {m} rw - btrfs /dev/x rw\n" for m in ("/", *mounts)))
        return f

    def test_refuses_a_cache_holding_a_mount(self, tmp_path):
        cache = tmp_path / "cache"
        (cache / "vol").mkdir(parents=True)
        info = self._info(tmp_path, str(cache / "vol"))
        with patch.object(_mod, "_mount_points", lambda: _READ_MOUNTS(info)):
            assert _mod._is_safe_target(cache) is False

    def test_refuses_a_mount_whose_name_is_escaped(self, tmp_path):
        cache = tmp_path / "a b"
        cache.mkdir()
        info = self._info(tmp_path, str(cache).replace(" ", "\\040"))
        with patch.object(_mod, "_mount_points", lambda: _READ_MOUNTS(info)):
            assert _mod._is_safe_target(cache) is False

    def test_allows_a_cache_with_no_mount(self, tmp_path):
        cache = tmp_path / "cache"
        cache.mkdir()
        info = self._info(tmp_path, str(tmp_path / "elsewhere"))
        with patch.object(_mod, "_mount_points", lambda: _READ_MOUNTS(info)):
            assert _mod._is_safe_target(cache) is True



def test_an_unreadable_mount_table_refuses_the_target(tmp_path):
    """Review of #2570: nothing else guards these rmtree targets, so a table
    that cannot be read must refuse, never read as "no mounts"."""
    cache = tmp_path / "cache"
    cache.mkdir()
    assert _READ_MOUNTS(tmp_path / "missing") is None
    with patch.object(_mod, "_mount_points", lambda: None):
        assert _mod._is_safe_target(cache) is False


# The same relation set as the shell guard's table-driven test. disk_reclaim's
# targets are FIXED cache paths, not candidates under a sweep root, so a mount
# ABOVE the target is not a refusal here: "/" is always one (#2570 premise
# check). Only a mount AT or BELOW the target is.
_PY_RELATIONS = [
    ("equal", "cache", False),
    ("below", "cache/vol", False),
    ("deep below", "cache/a/b/vol", False),
    ("above", "", True),
    ("sibling sharing a prefix", "cache2", True),
    ("disjoint", "other/vol", True),
]


@pytest.mark.parametrize(("relation", "mount", "allowed"), _PY_RELATIONS, ids=[r[0] for r in _PY_RELATIONS])
def test_is_safe_target_covers_every_relation(tmp_path, relation, mount, allowed):
    cache = tmp_path / "cache"
    cache.mkdir()
    info = tmp_path / "mountinfo"
    entries = ["/", str(tmp_path / mount) if mount else str(tmp_path)]
    info.write_text("".join(f"{i} 1 0:{i} / {m.replace(' ', chr(92) + '040')} rw - x x rw\n"
                            for i, m in enumerate(entries, 20)))
    with patch.object(_mod, "_mount_points", lambda: _READ_MOUNTS(info)):
        assert _mod._is_safe_target(cache) is allowed, relation


@pytest.mark.parametrize("text", ["", "\n", "36 35 98:0 / /home/x rw - btrfs /dev/x rw\n", "garbage\n"],
                         ids=["empty", "blank", "truncated-no-root", "malformed"])
def test_a_table_without_the_root_entry_is_unreadable(tmp_path, text):
    """Review of #2570 round 3: an empty or truncated read must refuse, like
    the shell guard, never read as "no mounts"."""
    info = tmp_path / "mountinfo"
    info.write_text(text)
    assert _READ_MOUNTS(info) is None


def test_mount_points_keep_raw_bytes_whatever_the_locale(tmp_path):
    """Review of #2570 round 4: a mount point is raw filename bytes. Decoding
    the table as locale text (latin-1 here, simulated because this box has no
    such locale) and re-encoding as UTF-8 turned b"caf\\xe9" into b"caf\\xc3\\xa9",
    so the real mount stopped matching and its cache could be cleared."""
    name = os.fsencode(tmp_path) + b"/caf\xe9"
    info = tmp_path / "mountinfo"
    info.write_bytes(b"20 1 0:20 / / rw - x x rw\n21 1 0:21 / " + name + b" rw - x x rw\n")
    with patch.object(Path, "read_text", lambda self, *a, **k: self.read_bytes().decode("latin-1")):
        mounts = _READ_MOUNTS(info)
    assert Path(os.fsdecode(name)) in mounts


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
