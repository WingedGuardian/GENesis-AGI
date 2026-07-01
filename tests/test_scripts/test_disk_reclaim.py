"""Tests for scripts/disk_reclaim.py — regenerable-cache reclamation.

Focus: the safety allowlist (a bug here could delete real data), resilient
partial deletion, MEDIUM-tier gating, and the --fail-above exit contract that
the remediation registry relies on to escalate a stuck disk.
"""

import importlib.util
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
        with patch.object(_mod.sys, "argv", ["disk_reclaim.py", *argv]), \
             patch.object(_mod, "_disk_pct", return_value=disk_pct):
            return _mod.main()

    def test_medium_held_below_threshold(self, tmp_path):
        cheap = _make_cache(tmp_path, "cheap1", "cheap")
        medium = _make_cache(tmp_path, "med1", "medium")
        with patch.object(_mod, "_CACHE_TARGETS", [cheap, medium]):
            self._run(["--apply", "--if-above", "90"], disk_pct=80.0)
        assert not cheap.path.exists()     # cheap always cleared
        assert medium.path.exists()        # medium held below 90

    def test_medium_cleared_above_threshold(self, tmp_path):
        cheap = _make_cache(tmp_path, "cheap2", "cheap")
        medium = _make_cache(tmp_path, "med2", "medium")
        with patch.object(_mod, "_CACHE_TARGETS", [cheap, medium]):
            self._run(["--apply", "--if-above", "90"], disk_pct=92.0)
        assert not cheap.path.exists()
        assert not medium.path.exists()    # medium cleared at/above 90

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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
