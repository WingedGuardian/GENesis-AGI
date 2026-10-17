"""scripts/prune_browser_engines.py: which Camoufox engines retention removes.

The script only decides and prints; scripts/disk_hygiene.sh deletes what it prints
through its mount-aware deleter. Every case builds a cache under tmp_path; nothing
here deletes anything.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from genesis.browser import engine, provision

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "prune_browser_engines.py"
DAY = 86400
NOW = 2_000_000_000.0


@pytest.fixture
def prune():
    spec = importlib.util.spec_from_file_location("prune_browser_engines", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _version(root: Path, name: str, *, age_days: float, repo: str = "official") -> Path:
    d = root / "browsers" / repo / name
    d.mkdir(parents=True)
    (d / "camoufox-bin").write_bytes(b"\x7fELF")
    (d / "version.json").write_text(json.dumps({"version": "156.0.1", "release": "beta.36"}))
    t = NOW - age_days * DAY
    os.utime(d / "version.json", (t, t))
    return d


def _legacy(root: Path) -> None:
    """The live 0.4 root's shape: Firefox's files, camoufox's additions, and the
    0.5-owned names that sit beside them."""
    root.mkdir(parents=True, exist_ok=True)
    for name in (
        "camoufox-bin",
        "libxul.so",
        "omni.ja",
        "application.ini",
        "version.json",
        "properties.json",
        "camoucfg.jvv",
        "chrome.css",
        "removed-files",
    ):
        (root / name).write_bytes(b"x")
    for name in ("browser", "defaults", "fonts", "fontconfigs", "distribution", "gmp-clearkey"):
        (root / name).mkdir()
    (root / "precomplete").write_text(
        'remove "libxul.so"\nremove "camoufox-bin"\nremove "browser/omni.ja"\n'
        'remove "omni.ja"\nremove "precomplete"\nrmdir "browser/"\nrmdir "defaults/"\n'
    )
    for keep in ("addons", "geoip", "fontconfig"):
        (root / keep).mkdir()
    for keep in ("config.json", "repo_cache.json", ".0.5_FLAG"):
        (root / keep).write_text("{}")


def _ready(pinned: Path) -> engine.EngineStatus:
    return engine.EngineStatus(
        engine.READY, "Camoufox official 156.0.1-beta.36", pinned / "camoufox-bin"
    )


def _plan(prune, root, status, installed=(), exes=()):
    return prune.plan(root, status, list(installed), list(exes), NOW)


# ── when nothing is removed ───────────────────────────────────────────────────


def test_nothing_while_the_pinned_engine_is_not_ready(prune, tmp_path):
    root = tmp_path / "camoufox"
    old = _version(root, "156.0.1-beta.30-aa", age_days=90)
    _legacy(root)
    status = engine.EngineStatus(engine.NOT_INSTALLED, "missing")
    doomed, notes = _plan(prune, root, status, [old])
    assert doomed == [] and notes


def test_nothing_while_the_engine_in_use_is_not_a_pinned_one(prune, tmp_path):
    """A legacy root engine or a CAMOUFOX_EXECUTABLE_PATH override in use."""
    root = tmp_path / "camoufox"
    _legacy(root)
    old = _version(root, "156.0.1-beta.30-aa", age_days=90)
    for path in (root / "camoufox-bin", tmp_path / "elsewhere" / "camoufox-bin"):
        status = engine.EngineStatus(engine.READY, "x", path)
        assert _plan(prune, root, status, [old])[0] == []


def test_nothing_within_two_weeks_of_the_pinned_engine(prune, tmp_path):
    """The grace is the window for going back to the previous build."""
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=13)
    old = _version(root, "156.0.1-beta.30-aa", age_days=90)
    _legacy(root)
    doomed, notes = _plan(prune, root, _ready(pinned), [pinned, old])
    assert doomed == [] and any("14 days" in n for n in notes)


def test_nothing_when_the_process_table_is_unreadable(prune, tmp_path):
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=30)
    old = _version(root, "156.0.1-beta.30-aa", age_days=90)
    doomed, _ = prune.plan(root, _ready(pinned), [pinned, old], None, NOW)
    assert doomed == []


# ── old pinned-layout versions ────────────────────────────────────────────────


def test_old_versions_go_and_the_pinned_one_stays(prune, tmp_path):
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=30)
    old = _version(root, "156.0.1-beta.30-aa", age_days=90)
    other = _version(root, "135.0-beta.1-cc", age_days=90, repo="custom")
    doomed, _ = _plan(prune, root, _ready(pinned), [pinned, old, other])
    assert sorted(doomed) == sorted([old, other])


def test_a_version_a_running_browser_uses_stays(prune, tmp_path):
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=30)
    old = _version(root, "156.0.1-beta.30-aa", age_days=90)
    doomed, notes = _plan(prune, root, _ready(pinned), [pinned, old], [old / "camoufox-bin"])
    assert doomed == [] and any(str(old) in n for n in notes)


def test_a_listed_directory_outside_the_cache_is_never_printed(prune, tmp_path):
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=30)
    stray = _version(tmp_path / "elsewhere", "156.0.1-beta.30-aa", age_days=90)
    assert _plan(prune, root, _ready(pinned), [pinned, stray])[0] == []


# ── the pre-0.5 root engine ───────────────────────────────────────────────────


def test_the_legacy_root_goes_version_json_first_manifest_last(prune, tmp_path):
    """version.json first: alone it retires the root for camoufox. precomplete last:
    a run killed midway still finds the manifest that names the rest."""
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=30)
    _legacy(root)
    doomed, _ = _plan(prune, root, _ready(pinned), [pinned])
    assert doomed[0] == root / "version.json" and doomed[-1] == root / "precomplete"
    names = {p.name for p in doomed}
    assert {
        "camoufox-bin",
        "libxul.so",
        "browser",
        "defaults",
        "fontconfigs",
        "properties.json",
        "camoucfg.jvv",
        "chrome.css",
        "distribution",
    } <= names
    for keep in (
        "browsers",
        "addons",
        "geoip",
        "fontconfig",
        "config.json",
        "repo_cache.json",
        ".0.5_FLAG",
    ):
        assert keep not in names


def test_an_unknown_root_entry_stays_and_is_named(prune, tmp_path):
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=30)
    _legacy(root)
    (root / "something-new").write_text("x")
    doomed, notes = _plan(prune, root, _ready(pinned), [pinned])
    assert root / "something-new" not in doomed
    assert any("something-new" in n for n in notes)


def test_a_legacy_engine_still_running_keeps_the_root(prune, tmp_path):
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=30)
    _legacy(root)
    doomed, _ = _plan(prune, root, _ready(pinned), [pinned], [root / "camoufox-bin"])
    assert doomed == []


def test_the_pinned_engine_running_does_not_keep_the_root(prune, tmp_path):
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=30)
    _legacy(root)
    doomed, _ = _plan(prune, root, _ready(pinned), [pinned], [pinned / "camoufox-bin"])
    assert root / "camoufox-bin" in doomed


def test_a_run_killed_after_version_json_finishes_next_time(prune, tmp_path):
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=30)
    _legacy(root)
    (root / "version.json").unlink()
    doomed, _ = _plan(prune, root, _ready(pinned), [pinned])
    assert root / "libxul.so" in doomed and doomed[-1] == root / "precomplete"


def test_without_a_manifest_the_measured_names_are_used(prune, tmp_path):
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=30)
    _legacy(root)
    (root / "precomplete").unlink()
    doomed, _ = _plan(prune, root, _ready(pinned), [pinned])
    assert {"libxul.so", "camoufox-bin", "browser", "version.json"} <= {p.name for p in doomed}


# ── the command line ──────────────────────────────────────────────────────────


def _run(root: Path, env_extra=None):
    src = str(Path(__file__).resolve().parents[2] / "src")
    env = {"PATH": os.environ["PATH"], "HOME": str(root.parent), "PYTHONPATH": src}
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(_SCRIPT), str(root)],
        env=env,
        capture_output=True,
        timeout=120,
        check=False,
    )


def test_a_root_other_than_camoufoxs_own_prints_nothing(tmp_path):
    """The shell passes the cache it will delete in; if camoufox resolves another,
    nothing is printed."""
    (tmp_path / "xdg").mkdir()
    r = _run(tmp_path / "not-the-cache", {"XDG_CACHE_HOME": str(tmp_path / "xdg")})
    assert r.returncode == 0 and r.stdout == b""


def test_main_prints_nul_separated_paths(prune, tmp_path, monkeypatch, capsysbinary):
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=30)
    old = _version(root, "156.0.1-beta.30-aa", age_days=90)
    monkeypatch.setattr(prune.engine, "camoufox_install_dir", lambda: root)
    monkeypatch.setattr(prune, "_inputs", lambda root: (_ready(pinned), [pinned, old], [], {}))
    monkeypatch.setattr(prune.time, "time", lambda: NOW)
    assert prune.main([str(root)]) == 0
    assert capsysbinary.readouterr().out == str(old).encode() + b"\0"


def test_the_manifest_never_overrides_what_camoufox_0_5_keeps(prune, tmp_path):
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=30)
    _legacy(root)
    (root / "precomplete").write_text(
        'remove "addons/UBO/x.xpi"\nrmdir "geoip/"\nremove "config.json"\nremove "libxul.so"\n'
    )
    names = {p.name for p in _plan(prune, root, _ready(pinned), [pinned])[0]}
    assert "libxul.so" in names
    assert not names & {"addons", "geoip", "config.json", "browsers"}


def test_a_name_only_the_manifest_lists_goes_too(prune, tmp_path):
    """Another 0.4 release's Firefox may ship a file the measured list lacks."""
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=30)
    _legacy(root)
    (root / "libnewer.so").write_bytes(b"x")
    (root / "precomplete").write_text('remove "libnewer.so"\nremove "precomplete"\n')
    assert root / "libnewer.so" in _plan(prune, root, _ready(pinned), [pinned])[0]


def test_main_prints_nothing_for_another_root(prune, tmp_path, monkeypatch, capsysbinary):
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=30)
    old = _version(root, "156.0.1-beta.30-aa", age_days=90)
    monkeypatch.setattr(prune.engine, "camoufox_install_dir", lambda: tmp_path / "other")
    monkeypatch.setattr(prune, "_inputs", lambda root: (_ready(pinned), [pinned, old], [], {}))
    monkeypatch.setattr(prune.time, "time", lambda: NOW)
    assert prune.main([str(root)]) == 0
    assert capsysbinary.readouterr().out == b""


# ── round 2: whose engine it is, and since when ───────────────────────────────


def _recent(path: Path, days: float = 1) -> None:
    t = NOW - days * DAY
    os.utime(path, (t, path.stat().st_mtime))


def test_an_operator_override_keeps_every_engine(prune, tmp_path):
    """The override may name any directory under browsers/, and the disk-hygiene
    unit may not see the variable the browser tools see."""
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=30)
    old = _version(root, "146.0.1-beta.25-aa", age_days=90)
    doomed, notes = prune.plan(root, _ready(pinned), [pinned, old], [], NOW, override=True)
    assert doomed == [] and any("CAMOUFOX_EXECUTABLE_PATH" in n for n in notes)


@pytest.mark.parametrize("choice", [True, None])
def test_a_camoufox_set_choice_or_an_unreadable_config_keeps_every_engine(prune, tmp_path, choice):
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=30)
    old = _version(root, "146.0.1-beta.25-aa", age_days=90)
    doomed, _ = prune.plan(root, _ready(pinned), [pinned, old], [], NOW, explicit_choice=choice)
    assert doomed == []


def test_a_recent_package_install_restarts_the_grace(prune, tmp_path):
    """A downgrade back to a pin whose engine was fetched long ago is a new pairing."""
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=60)
    old = _version(root, "157.0.1-beta.40-aa", age_days=20)
    doomed, _ = prune.plan(root, _ready(pinned), [pinned, old], [], NOW, paired_at=NOW - 3 * DAY)
    assert doomed == []


def test_an_engine_launched_recently_stays(prune, tmp_path):
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=30)
    old = _version(root, "146.0.1-beta.25-aa", age_days=90)
    _recent(old / "camoufox-bin")
    _legacy(root)
    _recent(root / "camoufox-bin")
    doomed, notes = _plan(prune, root, _ready(pinned), [pinned, old])
    assert old not in doomed and root / "libxul.so" not in doomed
    assert sum("launched in the last" in n for n in notes) == 2


def test_what_an_interrupted_run_left_retiring_is_finished(prune, tmp_path):
    """camoufox's listing skips dot directories, so only this finds them again."""
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=30)
    left = root / "browsers" / ".retiring" / "official.146.0.1-beta.25-aa.40417"
    left.mkdir(parents=True)
    busy = root / "browsers" / ".retiring" / "official.145.0-beta.1-cc.456"
    busy.mkdir()
    doomed, _ = _plan(prune, root, _ready(pinned), [pinned], [busy / "camoufox-bin"])
    assert left in doomed and busy not in doomed


def test_a_leftover_launched_recently_stays(prune, tmp_path):
    """A browser that started in the moment before its version was set aside runs
    from .retiring; once it exits, only its launch time says it was in use."""
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=30)
    left = root / "browsers" / ".retiring" / "official.146.0.1-beta.25-aa.40417"
    left.mkdir(parents=True)
    (left / "camoufox-bin").write_bytes(b"\x7fELF")
    _recent(left / "camoufox-bin")
    doomed, notes = _plan(prune, root, _ready(pinned), [pinned])
    assert left not in doomed
    assert any("launched in the last" in n for n in notes)


def test_a_root_without_version_json_or_manifest_is_not_a_legacy_engine(prune, tmp_path):
    """A later camoufox might keep a root name the 0.4 allowlist also has."""
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=30)
    root.joinpath("fonts").mkdir()
    root.joinpath("distribution").mkdir()
    assert _plan(prune, root, _ready(pinned), [pinned])[0] == []


def test_an_undecodable_name_is_printed_as_its_bytes(prune, tmp_path, monkeypatch, capsysbinary):
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=30)
    odd = _version(root, "146.0.1-beta.25-\udcff", age_days=90)
    monkeypatch.setattr(prune.engine, "camoufox_install_dir", lambda: root)
    monkeypatch.setattr(prune, "_inputs", lambda root: (_ready(pinned), [pinned, odd], [], {}))
    monkeypatch.setattr(prune.time, "time", lambda: NOW)
    assert prune.main([str(root)]) == 0
    assert capsysbinary.readouterr().out == os.fsencode(odd) + b"\0"


def test_a_listed_directory_in_a_dot_repo_is_never_printed(prune, tmp_path):
    """camoufox never lists one; .retiring is this step's own and handled apart."""
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=30)
    hidden = _version(root, "146.0.1-beta.25-aa", age_days=90, repo=".partial")
    assert _plan(prune, root, _ready(pinned), [pinned, hidden])[0] == []


def test_without_a_manifest_version_json_goes_last(prune, tmp_path):
    """The marker the next run finds the root by is whichever exists; printed last,
    it survives any earlier entry that stays."""
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=30)
    _legacy(root)
    (root / "precomplete").unlink()
    doomed, _ = _plan(prune, root, _ready(pinned), [pinned])
    names = [p.name for p in doomed if p.parent == root]
    assert names[-1] == "version.json" and "libxul.so" in names


def test_with_a_manifest_version_json_goes_first_and_the_manifest_last(prune, tmp_path):
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=30)
    _legacy(root)
    doomed, _ = _plan(prune, root, _ready(pinned), [pinned])
    names = [p.name for p in doomed if p.parent == root]
    assert names[0] == "version.json" and names[-1] == "precomplete"


def _fake_camoufox(monkeypatch, config_file: Path) -> None:
    """camoufox 0.5.8's own shapes: load_config() turns a decode error into {}
    (multiversion.py), and a choice is a channel or a pinned build (browser_pin.py)."""
    import types

    multiversion = types.ModuleType("camoufox.multiversion")
    multiversion.CONFIG_FILE = config_file
    multiversion.load_config = lambda: {}
    browser_pin = types.ModuleType("camoufox.browser_pin")
    browser_pin.is_explicit_choice = lambda c: bool(c.get("channel") or c.get("pinned"))
    package = types.ModuleType("camoufox")
    package.multiversion = multiversion
    package.browser_pin = browser_pin
    monkeypatch.setitem(sys.modules, "camoufox", package)
    monkeypatch.setitem(sys.modules, "camoufox.multiversion", multiversion)
    monkeypatch.setitem(sys.modules, "camoufox.browser_pin", browser_pin)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b'{"pinned": ', None),  # an interrupted write
        (b"[1]", None),  # JSON, but not camoufox's shape
        (b"\xff\xfe", None),
        (b"{}", False),
        (b'{"channel": "official/beta"}', True),
        (b'{"pinned": "official/156.0.1-beta.36"}', True),
    ],
)
def test_an_unreadable_camoufox_config_counts_as_unknown(tmp_path, monkeypatch, content, expected):
    """Unknown keeps every engine; camoufox's own loader would read a corrupt file
    as no choice at all."""
    config = tmp_path / "config.json"
    config.write_bytes(content)
    _fake_camoufox(monkeypatch, config)
    assert provision._explicit_choice() is expected


def test_a_dangling_camoufox_config_link_counts_as_unknown(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.symlink_to(tmp_path / "gone")
    _fake_camoufox(monkeypatch, config)
    assert provision._explicit_choice() is None


def test_the_decider_takes_the_provisioners_reading(prune, tmp_path, monkeypatch):
    """One reader for both: the provisioner refuses a fetch on the same answer."""
    monkeypatch.setattr(prune.provision, "_explicit_choice", lambda: None)
    monkeypatch.setattr(
        prune.engine,
        "resolve_camoufox_engine",
        lambda: engine.EngineStatus(engine.NOT_INSTALLED, "x"),
    )
    monkeypatch.setattr(prune.provision, "live_camoufox_exes", lambda: [])
    assert prune._inputs(tmp_path)[3]["explicit_choice"] is None


def test_no_camoufox_config_is_no_choice(tmp_path, monkeypatch):
    _fake_camoufox(monkeypatch, tmp_path / "config.json")
    assert provision._explicit_choice() is False


# ── what the browser itself records (round 4) ─────────────────────────────────


def test_the_engine_the_profile_last_ran_is_kept(prune, tmp_path):
    """An override set only in a session's environment is invisible here; the
    profile's own record of the engine that last opened it is not."""
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=60)
    old = _version(root, "146.0.1-beta.25-aa", age_days=90)
    doomed, notes = prune.plan(root, _ready(pinned), [pinned, old], [], NOW, last_used=old)
    assert old not in doomed
    assert any("last ran" in n for n in notes)


def test_a_legacy_root_the_profile_last_ran_is_kept(prune, tmp_path):
    root = tmp_path / "camoufox"
    _legacy(root)
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=60)
    doomed, _ = prune.plan(root, _ready(pinned), [pinned], [], NOW, last_used=root)
    assert not any(p.parent == root and p.name != "browsers" for p in doomed)


def test_a_leftover_of_the_engine_the_profile_last_ran_is_kept(prune, tmp_path):
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=60)
    left = root / "browsers" / ".retiring" / "official.146.0.1-beta.25-aa.40417"
    left.mkdir(parents=True)
    last = root / "browsers" / "official" / "146.0.1-beta.25-aa"
    doomed, _ = prune.plan(root, _ready(pinned), [pinned], [], NOW, last_used=last)
    assert left not in doomed


def test_an_unknown_profile_record_keeps_every_engine(prune, tmp_path):
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=60)
    old = _version(root, "146.0.1-beta.25-aa", age_days=90)
    doomed, notes = prune.plan(
        root, _ready(pinned), [pinned, old], [], NOW, last_used=prune.UNKNOWN
    )
    assert doomed == []
    assert any("profile" in n for n in notes)


def test_a_profile_record_outside_the_cache_is_reported(prune, tmp_path):
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=60)
    old = _version(root, "146.0.1-beta.25-aa", age_days=90)
    elsewhere = tmp_path / "other" / "camoufox"
    doomed, notes = prune.plan(root, _ready(pinned), [pinned, old], [], NOW, last_used=elsewhere)
    assert old in doomed
    assert any(str(elsewhere) in n for n in notes)


def test_without_access_times_every_engine_is_kept(prune, tmp_path):
    """Under noatime the recent-launch check could only ever say no."""
    root = tmp_path / "camoufox"
    pinned = _version(root, "156.0.1-beta.36-bb", age_days=60)
    old = _version(root, "146.0.1-beta.25-aa", age_days=90)
    doomed, notes = prune.plan(root, _ready(pinned), [pinned, old], [], NOW, atime_kept=False)
    assert doomed == []
    assert any("access time" in n for n in notes)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (None, None),  # never opened: no constraint
        ("[Compatibility]\nLastPlatformDir=/x/camoufox/browsers/official/1-a\n", "path"),
        ("[Compatibility]\nLastVersion=1\n", "unknown"),
        ("[Compatibility]\nLastPlatformDir=\n", "unknown"),
        ("not an ini", "unknown"),
    ],
)
def test_the_profile_record_reader(prune, tmp_path, content, expected):
    if content is not None:
        (tmp_path / "compatibility.ini").write_text(content)
    got = prune._last_used(tmp_path)
    if expected is None:
        assert got is None
    elif expected == "unknown":
        assert got is prune.UNKNOWN
    else:
        assert got == Path("/x/camoufox/browsers/official/1-a")


def test_an_undecodable_profile_record_is_unknown(prune, tmp_path):
    (tmp_path / "compatibility.ini").write_bytes(b"\xff\xfe[Compat")
    assert prune._last_used(tmp_path) is prune.UNKNOWN


@pytest.mark.parametrize(
    ("env", "line", "expected"),
    [
        ("", None, False),
        ("/opt/cf/bin", None, True),
        ("", "export CAMOUFOX_EXECUTABLE_PATH=/opt/cf/bin\n", True),  # dotenv reads it
        ("", "CAMOUFOX_EXECUTABLE_PATH = /opt/cf/bin\n", True),
        ("", "OTHER=1\n", False),
    ],
)
def test_the_override_is_read_from_the_env_and_the_secrets_file(
    prune, tmp_path, monkeypatch, env, line, expected
):
    secrets = tmp_path / "secrets.env"
    if line is not None:
        secrets.write_text(line)
    monkeypatch.setenv("SECRETS_PATH", str(secrets))
    monkeypatch.setenv("CAMOUFOX_EXECUTABLE_PATH", env)
    assert prune._override_set() is expected


def test_an_unreadable_secrets_file_counts_as_an_override(prune, tmp_path, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    secrets = tmp_path / "secrets.env"
    secrets.write_text("CAMOUFOX_EXECUTABLE_PATH=/opt/cf/bin\n")
    secrets.chmod(0)  # there, but unreadable: unknown
    monkeypatch.setenv("SECRETS_PATH", str(secrets))
    monkeypatch.delenv("CAMOUFOX_EXECUTABLE_PATH", raising=False)
    assert prune._override_set() is True


def test_a_noatime_mount_is_detected(prune, tmp_path, monkeypatch):
    class Fake:
        f_flag = prune.os.ST_NOATIME

    monkeypatch.setattr(prune.os, "statvfs", lambda p: Fake())
    assert prune._atime_kept(tmp_path) is False
    Fake.f_flag = 0
    assert prune._atime_kept(tmp_path) is True


def test_the_decider_reads_the_profile_record_and_the_mount(prune, tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "compatibility.ini").write_text(
        "[Compatibility]\nLastPlatformDir=/x/camoufox/browsers/official/1-a\n"
    )
    monkeypatch.setattr(prune, "CAMOUFOX_PROFILE_DIR", profile)
    monkeypatch.setattr(
        prune.engine,
        "resolve_camoufox_engine",
        lambda: engine.EngineStatus(engine.NOT_INSTALLED, "x"),
    )
    monkeypatch.setattr(prune.provision, "live_camoufox_exes", lambda: [])
    monkeypatch.setattr(prune, "_override_set", lambda: True)
    monkeypatch.setattr(prune, "_atime_kept", lambda root: root == tmp_path)
    gates = prune._inputs(tmp_path)[3]
    assert gates["last_used"] == Path("/x/camoufox/browsers/official/1-a")
    assert gates["override"] is True and gates["atime_kept"] is True
