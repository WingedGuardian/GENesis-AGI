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


def _write_cache(home: Path, prs, *, age_hours=0, repo="o/r", capped=False) -> None:
    computed = (datetime.now(UTC) - timedelta(hours=age_hours)).isoformat()
    cache = _cache_path(home)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps(
            {"version": 1, "computed_at": computed, "repo": repo, "prs": prs, "limit_hit": capped}
        )
    )


def _run(
    home: Path,
    *,
    disabled=False,
    disabled_raw: str | None = None,
    genesis_home: Path | None = None,
    cc_session=False,
) -> str:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(_SRC)
    env.pop("GENESIS_REPO_ROOT", None)
    env.pop("GENESIS_HOME", None)
    if genesis_home is not None:
        env["GENESIS_HOME"] = str(genesis_home)
    if disabled_raw is not None:
        env["GENESIS_REPO_PULSE_DISABLED"] = disabled_raw
    elif disabled:
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


def _write_local_overlay(home: Path, text: str) -> None:
    cfg = home / ".genesis" / "config" / "repo_pulse.local.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(text)


def test_ttl_scales_with_large_debounce(tmp_path):
    """When min_interval_minutes >= 1440 the worker can only refresh at most
    daily, so a fixed 1-day TTL would suppress the surface for the whole debounce
    window. The TTL derives from the debounce (2x), so a 30h-old cache under a
    2-day debounce is STILL surfaced (old fixed-86400 TTL would suppress it)."""
    home = tmp_path / "home"
    _write_local_overlay(home, "min_interval_minutes: 2880\n")  # 2 days
    _write_cache(home, [_openpr(1379, 12)], age_hours=30)  # >1 day, < 2*2day TTL
    assert "#1379 (12d)" in _run(home)


def test_ttl_default_debounce_still_caps_at_one_day(tmp_path):
    """With the default 30-min debounce the 1-day floor still governs: a 48h-old
    cache stays suppressed (the derived TTL never drops BELOW the 1-day floor)."""
    home = tmp_path / "home"
    _write_local_overlay(home, "min_interval_minutes: 30\n")
    _write_cache(home, [_openpr(1379, 12)], age_hours=48)
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


def test_env_kill_switch_only_exact_one(tmp_path):
    """The kill switch honors ONLY the exact "1" — the value the worker and
    genesis_session_context honor and the yaml documents. A looser truthy set
    here would silence THIS surface while the worker kept running: a partial,
    misleading kill switch. So `=true`/`=yes` must NOT suppress."""
    home = tmp_path / "home"
    _write_cache(home, [_openpr(1379, 12)])
    for raw in ("true", "yes", "TRUE", "on", "0", "2"):
        assert "[Open PRs]" in _run(home, disabled_raw=raw), f"{raw!r} wrongly suppressed"
    # the documented value still silences it
    assert _run(home, disabled_raw="1").strip() == ""


def test_honors_genesis_home(tmp_path):
    """A relocated install (GENESIS_HOME set) reads the cache from under it, not
    $HOME/.genesis — the worker writes there too, so the surface must follow."""
    home = tmp_path / "home"  # deliberately EMPTY (no cache here)
    ghome = tmp_path / "relocated"
    cache = ghome / "repo_pulse" / "open_prs.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    computed = datetime.now(UTC).isoformat()
    cache.write_text(
        json.dumps(
            {
                "version": 1,
                "computed_at": computed,
                "repo": "o/r",
                "prs": [_openpr(1379, 12)],
                "limit_hit": False,
            }
        )
    )
    out = _run(home, genesis_home=ghome)
    assert "#1379 (12d)" in out
    # and the seen-map is written under GENESIS_HOME too (not $HOME)
    assert (ghome / "repo_pulse" / "open_prs_seen.json").exists()


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


def test_capped_cache_shows_floor_count(tmp_path):
    home = tmp_path / "home"
    _write_cache(home, [_openpr(1379, 12), _openpr(1223, 9)], capped=True)
    out = _run(home)
    assert out.startswith("[Open PRs] ≥2 open PRs idle")  # ≥2, a floor


def test_seen_map_namespaced_by_repo(tmp_path):
    home = tmp_path / "home"
    _write_cache(home, [_openpr(1379, 12)], repo="owner/therepo")
    _run(home)
    surfaced = json.loads(_seen_path(home).read_text())["surfaced"]
    assert "owner/therepo#1379" in surfaced  # keyed by repo slug + number


def test_seen_map_pruned_when_pr_no_longer_stalled(tmp_path):
    home = tmp_path / "home"
    # 1st run: stale → surfaced + recorded.
    _write_cache(home, [_openpr(1379, 12)])
    assert "#1379" in _run(home)
    assert "o/r#1379" in json.loads(_seen_path(home).read_text())["surfaced"]
    # 2nd run: same PR now recent (not stalled) → its entry is PRUNED, so a later
    # re-stale is a fresh episode rather than a wrongly-aged-out suppression.
    _write_cache(home, [_openpr(1379, 2)])  # 2d < 7d threshold
    _run(home)
    assert "o/r#1379" not in json.loads(_seen_path(home).read_text())["surfaced"]
