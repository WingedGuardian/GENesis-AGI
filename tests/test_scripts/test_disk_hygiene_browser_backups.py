"""prune_browser_backups in scripts/disk_hygiene.sh.

Rollback material from a browser-stack upgrade (the moved-aside pre-0.5 engine,
profile copies) is pruned after 14 days, but only while the engine is ready: a
broken install keeps its way back. Interrupted copies (*.tmp) go after a day
regardless. Ages are set with os.utime, so the tests do not depend on the clock.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

_HYGIENE = Path(__file__).resolve().parents[2] / "scripts" / "disk_hygiene.sh"


def _aged_dir(path: Path, days: float) -> Path:
    path.mkdir(parents=True)
    (path / "f").write_text("x")
    t = time.time() - days * 86400
    os.utime(path, (t, t))
    return path


def _prune(home: Path, state: str, chromium: str = "unknown") -> subprocess.CompletedProcess:
    # Paths travel as positional parameters, never interpolated into the script.
    env = {**os.environ, "XDG_CACHE_HOME": str(home / ".cache")}
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; prune_browser_backups "$2" "$3" "$4"',
            "_",
            str(_HYGIENE),
            str(home),
            state,
            chromium,
        ],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )


def _layout(home: Path) -> dict[str, Path]:
    g = home / ".genesis"
    c = home / ".cache"
    return {
        "old_engine": _aged_dir(c / "camoufox.pre-0.5-20260901", 20),
        "new_engine_backup": _aged_dir(c / "camoufox.pre-0.5-20261003", 1),
        "old_profile": _aged_dir(g / "camoufox-profile.pre-v135-20260901T000000", 20),
        "stale_tmp": _aged_dir(g / "camoufox-profile.pre-v135-20261001.tmp", 3),
        # provision.copy_aside's TemporaryDirectory left by a run killed outright.
        "killed_copy": _aged_dir(g / "camoufox-profile.pre-v135-20261001T010203.k2j9x1.tmp", 3),
        "fresh_copy": _aged_dir(g / "camoufox-profile.pre-v135-20261005T010203.a8b7c6.tmp", 0.1),
        "old_chromium": _aged_dir(g / "browser-profile.pre-v145-20260901T000000", 20),
        "killed_chromium_copy": _aged_dir(g / "browser-profile.pre-v145-20261001T010203.q1w2e3.tmp", 3),
        "live_chromium_profile": _aged_dir(g / "browser-profile", 400),
        "live_profile": _aged_dir(g / "camoufox-profile", 400),
        "live_engine": _aged_dir(c / "camoufox", 400),
    }


def test_ready_engine_prunes_old_backups_only(tmp_path):
    paths = _layout(tmp_path)
    _prune(tmp_path, "ready")
    for gone in ("old_engine", "old_profile", "stale_tmp", "killed_copy"):
        assert not paths[gone].exists(), gone
    for kept in ("new_engine_backup", "live_profile", "live_engine", "fresh_copy"):
        assert paths[kept].exists(), kept


def test_unready_engine_keeps_rollback_material(tmp_path):
    paths = _layout(tmp_path)
    result = _prune(tmp_path, "legacy_layout")
    assert "kept" in result.stdout
    for kept in ("old_engine", "old_profile", "live_profile", "live_engine"):
        assert paths[kept].exists(), kept
    assert not paths["stale_tmp"].exists(), "an interrupted copy is never a backup"
    assert not paths["killed_copy"].exists()
    assert paths["fresh_copy"].exists(), "a copy that may still be in progress is left alone"


def test_chromium_backups_follow_the_chromium_state_not_the_engine(tmp_path):
    paths = _layout(tmp_path)
    _prune(tmp_path, "ready", "not_opened_by_the_new_chromium")
    assert paths["old_chromium"].exists(), "the Camoufox engine says nothing about Chromium"
    assert not paths["killed_chromium_copy"].exists()
    assert paths["live_chromium_profile"].exists()
    _prune(tmp_path, "legacy_layout", "ready")
    assert not paths["old_chromium"].exists()
    assert paths["live_chromium_profile"].exists()
