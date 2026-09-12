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


def test_a_failure_is_announced_IN_BAND_not_only_on_stderr(capsys, monkeypatch):
    """A hook that fails silently is indistinguishable from one with nothing to
    say -- and that is precisely the outage this handler exists to expose.

    The first version wrote only a traceback to stderr, reasoning that stderr
    "is not model-facing and so costs the session nothing". Which is exactly why
    it achieved nothing: Claude Code DISCARDS an exit-0 hook's stderr. This is a
    SessionStart hook, so stdout is the injected channel (the same contract
    genesis_session_context.py cites when it routes its own mis-wire alert
    in-band, and git_discard_guard.py when it uses additionalContext).

    So a genesis/src version skew -- this block sits downstream of module
    attribute reads -- read to the model as "no PRs to report". Silence that
    means "broken" wearing the costume of silence that means "nothing".

    The line is fixed-format on purpose: its only variable part is an exception
    CLASS NAME, sliced, so it cannot approach the hook-output cap that this
    hook's gate exemption depends on.
    """
    # This test drives main() IN-PROCESS, so unlike the subprocess `_run` helper
    # it inherits the AMBIENT environment -- including the two kill switches
    # main() checks BEFORE its try block. `_run` pops both for exactly this
    # reason. MEASURED: without these deletions the test returns early, captures
    # nothing, and fails -- and GENESIS_CC_SESSION=1 is what a Genesis-dispatched
    # background session sets, so it would be green in CI and red precisely when
    # an autonomous session ran the suite.
    monkeypatch.delenv("GENESIS_CC_SESSION", raising=False)
    monkeypatch.delenv("GENESIS_REPO_PULSE_DISABLED", raising=False)
    # main() prepends to sys.path; let monkeypatch own that so it is undone.
    monkeypatch.syspath_prepend(str(_SRC))

    import importlib.util

    spec = importlib.util.spec_from_file_location("surface_open_prs", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    import builtins

    real_import = builtins.__import__

    def _skew(name, *a, **k):
        if "session_awareness" in name:
            raise AttributeError("simulated genesis/src version skew")
        return real_import(name, *a, **k)

    builtins.__import__ = _skew
    try:
        mod.main()  # fail-open: must not raise
    finally:
        builtins.__import__ = real_import

    out = capsys.readouterr()
    assert "open-PR surfacing FAILED" in out.out, (
        "the failure never reached the model-facing channel"
    )
    assert "no open PRs" in out.out, (
        "the line must say the absence is UNKNOWN, not zero"
    )
    assert "Traceback" in out.err, "the trace should still reach the debug log"
    assert len(out.out) < 400, "the failure line must stay structurally bounded"
