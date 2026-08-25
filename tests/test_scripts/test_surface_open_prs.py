"""E2E: the surface_open_prs SessionStart hook script.

Drives the real script as a subprocess with a temp HOME (the open-PR cache +
seen-map are Path.home()-anchored, matching the worker's _pulse_root). No
network, no live services — the worker's fetch cache is synthesized on disk.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "surface_open_prs.py"
_SRC = Path(__file__).resolve().parents[2] / "src"


def _openpr(number, days_idle, *, login="human", is_bot=False):
    updated = (datetime.now(UTC) - timedelta(days=days_idle)).isoformat()
    return {
        "number": number,
        "title": "t",
        "url": f"https://x/pull/{number}",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "updatedAt": updated,
        "author": {"login": login, "is_bot": is_bot},
    }


def _cache_path(home: Path) -> Path:
    return home / ".genesis" / "repo_pulse" / "open_prs.json"


def _seen_path(home: Path) -> Path:
    return home / ".genesis" / "repo_pulse" / "open_prs_seen.json"


def _write_cache(home: Path, prs, *, age_hours=0) -> None:
    computed = (datetime.now(UTC) - timedelta(hours=age_hours)).isoformat()
    cache = _cache_path(home)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(
            {"version": 1, "computed_at": computed, "repo": "o/r", "prs": prs, "limit_hit": False}
        )
    )


def _run(home: Path, *, disabled=False, cc_session=False) -> str:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(_SRC)
    env.pop("GENESIS_REPO_ROOT", None)
    env.pop("GENESIS_HOME", None)
    if disabled:
        env["GENESIS_REPO_PULSE_DISABLED"] = "1"
    else:
        env.pop("GENESIS_REPO_PULSE_DISABLED", None)
    if cc_session:
        env["GENESIS_CC_SESSION"] = "1"
    else:
        env.pop("GENESIS_CC_SESSION", None)
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT)], env=env, capture_output=True, text=True, timeout=60
    )
    return proc.stdout


def test_surfaces_stale_open_prs(tmp_path):
    home = tmp_path / "home"
    _write_cache(home, [_openpr(1379, 12), _openpr(1223, 9), _openpr(1406, 2)])
    out = _run(home)
    assert "[Open PRs]" in out
    assert "#1379 (12d)" in out and "#1223 (9d)" in out
    assert "#1406" not in out  # 2d idle < 7d threshold
    assert "ready to merge" not in out.lower()
    assert _seen_path(home).exists()


def test_bot_tag_in_clause(tmp_path):
    home = tmp_path / "home"
    _write_cache(home, [_openpr(1223, 10, login="dependabot[bot]", is_bot=True)])
    out = _run(home)
    assert "#1223 (10d, dependabot)" in out


def test_stale_cache_not_surfaced(tmp_path):
    home = tmp_path / "home"
    _write_cache(home, [_openpr(1379, 12)], age_hours=48)  # snapshot >1 day old
    assert _run(home).strip() == ""


def test_no_stale_prs_silent(tmp_path):
    home = tmp_path / "home"
    _write_cache(home, [_openpr(1, 2), _openpr(2, 3)])  # all recent
    assert _run(home).strip() == ""


def test_missing_cache_fail_open(tmp_path):
    assert _run(tmp_path / "home").strip() == ""  # no cache written


def test_env_kill_switch_silences(tmp_path):
    home = tmp_path / "home"
    _write_cache(home, [_openpr(1379, 12)])
    assert _run(home, disabled=True).strip() == ""


def test_dispatched_session_silent_and_leaves_seen_untouched(tmp_path):
    home = tmp_path / "home"
    _write_cache(home, [_openpr(1379, 12)])
    out = _run(home, cc_session=True)
    assert out.strip() == ""
    assert not _seen_path(home).exists()  # never touched


def test_resurfaces_on_second_run(tmp_path):
    home = tmp_path / "home"
    _write_cache(home, [_openpr(1379, 12)])
    first = _run(home)
    second = _run(home)  # within resurface window
    assert "[Open PRs]" in first and "[Open PRs]" in second
