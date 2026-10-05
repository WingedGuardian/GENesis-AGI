"""genesis.browser.provision: the one-process upgrade transaction behind
scripts/install_browser_stack.sh.

The hazards it exists for (measured on camoufox 0.5.7):
  * camoufox 0.5's own fetch deletes a pre-0.5 engine directory BEFORE
    downloading, so the new engine is staged next to the old one and swapped in
    by rename only once it checks out;
  * the new packages cannot drive the old engine, so a failure before the swap
    reinstalls the package versions that were there before;
  * a Firefox/Chromium profile opened by a newer version is refused by an older
    one, so it is copied once before the newer version first opens it.
No test downloads or launches anything: subprocess steps are stubbed.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from genesis.browser import engine, provision

_REAL_BROWSERS_RUNNING = provision.browsers_running
_REAL_RUN_GROUP = provision._run_group
PIN = ("156.0.1", "beta.34")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Every path the transaction can write is inside tmp_path: an earlier version
    of these tests copied the REAL ~/.genesis profile into the real home."""
    home = tmp_path / "home"
    monkeypatch.setattr(provision, "PROFILE_DIR", home / ".genesis" / "camoufox-profile")
    monkeypatch.setattr(provision, "CHROMIUM_PROFILE_DIR", home / ".genesis" / "browser-profile")
    monkeypatch.setattr(provision, "LOCK_FILE", home / ".genesis" / "locks" / "provision.lock")
    monkeypatch.setattr(provision, "STAGING_PARENT", home / "tmp")
    monkeypatch.setattr(provision, "CAPABILITIES_FILE", home / ".genesis" / "capabilities.json")
    monkeypatch.setattr(provision, "browsers_running", lambda: False)
    monkeypatch.setattr(provision, "camoufox_running", lambda: False)
    monkeypatch.setattr(provision, "chromium_running", lambda: False)
    # No real package metadata: by default nothing was installed before the run,
    # so nothing is ever reinstalled unless a test says otherwise.
    monkeypatch.setattr(provision, "installed_versions", lambda: {})
    # Steps run through _run_group; route it through subprocess.run (looked up at
    # call time) so each test's FakeRun stands in for the step. _run_group itself
    # is tested against real processes below.
    monkeypatch.setattr(provision, "_run_group", lambda cmd, **kw: subprocess.run(cmd, **kw))


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir()
    return tmp_path / "cache" / "camoufox"


@pytest.fixture
def stack(cache, tmp_path, monkeypatch):
    """camoufox 0.5.7 installed (a fake package dir with its pin), engine status
    computed by the real engine module, preflight passing."""
    pkg = tmp_path / "pkg" / "camoufox"
    pkg.mkdir(parents=True)
    (pkg / "browser-pin.json").write_text(json.dumps({"version": PIN[0], "build": PIN[1]}))

    def status(*, install_dir=None, package_dir=None):
        return engine.camoufox_engine_status(install_dir=install_dir, package_dir=pkg)

    monkeypatch.setattr(provision, "camoufox_engine_status", status)
    monkeypatch.setattr(provision, "camoufox_pin", lambda: PIN)
    monkeypatch.setattr(provision, "preflight", lambda: None)
    return cache


class FakeRun:
    """Stands in for subprocess.run; ``fetch(install_dir, env)`` plays camoufox fetch."""

    def __init__(self, fetch=None, fail=()):
        self.calls: list[tuple[list[str], dict | None]] = []
        self.fetch = fetch
        self.fail = fail  # predicates on cmd that return rc=1

    def __call__(self, cmd, env=None, **kw):
        self.calls.append((cmd, env))
        if any(pred(cmd) for pred in self.fail):
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        if cmd[1:3] == ["-m", "camoufox"] and self.fetch:
            self.fetch(Path(env["XDG_CACHE_HOME"]) / "camoufox", env)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def pip_calls(self):
        return [cmd for cmd, _ in self.calls if cmd[1:3] == ["-m", "pip"]]


def _install_pinned(install_dir: Path, *_):
    """What a successful 0.5 fetch leaves: the flag and the pinned engine."""
    install_dir.mkdir(parents=True, exist_ok=True)
    (install_dir / ".0.5_FLAG").touch()
    d = install_dir / "browsers" / "official" / f"{PIN[0]}-{PIN[1]}"
    d.mkdir(parents=True)
    (d / "version.json").write_text(json.dumps({"version": PIN[0], "release": PIN[1]}))
    (d / "camoufox-bin").write_text("#!/bin/sh\n")
    (d / "camoufox-bin").chmod(0o755)


def _legacy_engine(root: Path, age_days: float = 90) -> None:
    root.mkdir(parents=True)
    (root / "version.json").write_text(json.dumps({"version": "135.0.1", "release": "beta.24"}))
    (root / "camoufox-bin").write_text("binary")
    (root / "camoufox-bin").chmod(0o755)
    t = time.time() - age_days * 86400
    os.utime(root, (t, t))


def _tx(tmp_path, install: bool = False) -> provision.Transaction:
    return provision.Transaction(tmp_path / "repo", tmp_path / "venv_setup.sh", install=install)


NOT_READY = engine.EngineStatus(engine.PIN_NOT_INSTALLED, "needs 156.0.1-beta.34")
READY = engine.EngineStatus(engine.READY, "Camoufox 156.0.1-beta.34", Path("/e"))
OLD = {"camoufox": "0.4.11", "playwright": "1.58.0"}
NEW = {"camoufox": "0.5.7", "playwright": "1.62.0", "patchright": "1.62.1"}


def _versions(monkeypatch, first: dict, then: dict) -> None:
    calls = iter([first])
    monkeypatch.setattr(provision, "installed_versions", lambda: next(calls, then))


# ── the engine: stage, then swap ──────────────────────────────────────────


def test_stage_then_swap_keeps_the_legacy_engine_until_the_new_one_checks_out(
    stack, tmp_path, monkeypatch
):
    _legacy_engine(stack, age_days=90)
    seen = {}

    def fetch(install_dir, env):
        # The old engine is still in place, untouched, for the whole download.
        seen["legacy_during_fetch"] = (stack / "camoufox-bin").read_text()
        seen["staging"] = install_dir.parent
        _install_pinned(install_dir)

    run = FakeRun(fetch=fetch)
    monkeypatch.setattr(subprocess, "run", run)
    outcome = _tx(tmp_path).run()

    assert seen["legacy_during_fetch"] == "binary"
    # Staged on the same filesystem (next to the root), and removed afterwards.
    assert seen["staging"].parent == stack.parent
    assert seen["staging"].name.startswith(".camoufox-staging-")
    assert not list(stack.parent.glob(".camoufox-staging-*"))
    # The root is now the new engine...
    assert (stack / ".0.5_FLAG").exists()
    assert (stack / "browsers" / "official" / f"{PIN[0]}-{PIN[1]}" / "version.json").is_file()
    # ...and the legacy engine was renamed aside, aged from today.
    aside = stack.with_name(f"camoufox.pre-0.5-{provision._stamp()}")
    assert (aside / "camoufox-bin").read_text() == "binary"
    assert time.time() - aside.stat().st_mtime < 3600
    assert "ready (engine=True, chromium=True, launch=True)" in outcome
    assert run.pip_calls() == []


def test_05_layout_is_fetched_in_place_and_never_renamed(stack, tmp_path, monkeypatch):
    """camoufox 0.5 keeps versions side by side and deletes nothing in its own
    layout, so a 0.5 root just gets the new version added."""
    stack.mkdir()
    (stack / ".0.5_FLAG").touch()
    old = stack / "browsers" / "official" / "150.0.2-beta.25"
    old.mkdir(parents=True)
    (old / "version.json").write_text(json.dumps({"version": "150.0.2", "release": "beta.25"}))
    inode = stack.stat().st_ino
    envs = []

    def fetch(install_dir, env):
        envs.append(env["XDG_CACHE_HOME"])
        _install_pinned(install_dir)

    monkeypatch.setattr(subprocess, "run", FakeRun(fetch=fetch))
    outcome = _tx(tmp_path).run()
    assert envs == [str(stack.parent)], "fetched into the real install root"
    assert stack.stat().st_ino == inode
    assert (old / "version.json").is_file()
    assert not list(stack.parent.glob("camoufox.pre-*"))
    assert "engine=True" in outcome


def test_residue_root_is_replaced_without_a_backup(stack, tmp_path, monkeypatch):
    stack.mkdir()
    (stack / "partial.zip").write_text("x")  # an interrupted download: no engine, no flag
    monkeypatch.setattr(subprocess, "run", FakeRun(fetch=_install_pinned))
    outcome = _tx(tmp_path).run()
    assert not (stack / "partial.zip").exists() and (stack / ".0.5_FLAG").exists()
    assert not list(stack.parent.glob("camoufox.pre-*"))
    assert "engine=True" in outcome


def test_no_root_at_all_is_staged_and_moved_in(stack, tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", FakeRun(fetch=_install_pinned))
    outcome = _tx(tmp_path).run()
    assert (stack / ".0.5_FLAG").exists() and "engine=True" in outcome


def test_ready_engine_is_not_fetched(stack, tmp_path, monkeypatch):
    _install_pinned(stack)
    run = FakeRun()
    monkeypatch.setattr(subprocess, "run", run)
    _tx(tmp_path).run()
    assert not [c for c, _ in run.calls if c[1:3] == ["-m", "camoufox"]]


def test_stale_staging_from_a_killed_run_is_removed(stack, tmp_path, monkeypatch):
    stale = stack.parent / ".camoufox-staging-99999"
    (stale / "camoufox").mkdir(parents=True)
    _install_pinned(stack)
    monkeypatch.setattr(subprocess, "run", FakeRun())
    _tx(tmp_path).run()
    assert not stale.exists()


def test_failed_rename_into_place_puts_the_legacy_engine_back(stack, tmp_path, monkeypatch):
    _legacy_engine(stack)
    tx = _tx(tmp_path)
    staged = tmp_path / "staged-elsewhere" / "camoufox"  # rename target that cannot exist
    real_rename = Path.rename

    def rename(self, target):
        if self == staged:
            raise OSError(18, "Invalid cross-device link")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(OSError):
        tx._swap_in(staged)
    assert (stack / "camoufox-bin").read_text() == "binary"
    assert tx.swapped is False


# ── failure: the root is untouched, the packages go back ──────────────────


def test_fetch_failure_leaves_the_legacy_root_untouched_and_restores_packages(
    stack, tmp_path, monkeypatch
):
    """Measured: `camoufox fetch` with no network exits 0 and installs nothing."""
    _legacy_engine(stack)
    mtime = stack.stat().st_mtime
    _versions(monkeypatch, OLD, NEW)
    run = FakeRun(fetch=lambda install_dir, env: install_dir.mkdir(parents=True))
    monkeypatch.setattr(subprocess, "run", run)
    outcome = _tx(tmp_path, install=True).run()

    assert (stack / "camoufox-bin").read_text() == "binary"
    assert stack.stat().st_mtime == mtime
    assert not list(stack.parent.glob("camoufox.pre-*"))
    assert not list(stack.parent.glob(".camoufox-staging-*"))
    assert run.pip_calls() == [
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--quiet",
            "camoufox==0.4.11",
            "playwright==1.58.0",
        ],
        # patchright was not there before the run, so it goes too.
        [sys.executable, "-m", "pip", "uninstall", "--yes", "--quiet", "patchright"],
    ]
    assert "DEGRADED" in outcome and "engine=False" in outcome


def test_restore_removes_only_what_the_run_added(stack, tmp_path, monkeypatch):
    """Versions unchanged, patchright added: no reinstall, just the removal."""
    _legacy_engine(stack)
    _versions(monkeypatch, OLD, {**OLD, "patchright": "1.62.3"})
    run = FakeRun(fail=[lambda cmd: cmd[0] == "bash"])
    monkeypatch.setattr(subprocess, "run", run)
    _tx(tmp_path, install=True).run()
    assert run.pip_calls() == [
        [sys.executable, "-m", "pip", "uninstall", "--yes", "--quiet", "patchright"]
    ]


def test_restore_includes_patchright_when_it_was_installed(stack, tmp_path, monkeypatch):
    _legacy_engine(stack)
    _versions(monkeypatch, {**OLD, "patchright": "1.58.0"}, NEW)
    run = FakeRun()
    monkeypatch.setattr(subprocess, "run", run)
    _tx(tmp_path).run()
    assert run.pip_calls()[0][-3:] == [
        "camoufox==0.4.11",
        "playwright==1.58.0",
        "patchright==1.58.0",
    ]


def test_failure_with_no_previous_camoufox_does_not_call_pip(stack, tmp_path, monkeypatch):
    _versions(monkeypatch, {}, NEW)
    run = FakeRun()  # fetch installs nothing
    monkeypatch.setattr(subprocess, "run", run)
    outcome = _tx(tmp_path, install=True).run()
    assert run.pip_calls() == []
    assert "engine=False" in outcome


def test_unchanged_packages_are_not_reinstalled(stack, tmp_path, monkeypatch):
    _legacy_engine(stack)
    _versions(monkeypatch, OLD, OLD)
    run = FakeRun()
    monkeypatch.setattr(subprocess, "run", run)
    _tx(tmp_path).run()
    assert run.pip_calls() == []


def test_failed_package_install_restores_packages(stack, tmp_path, monkeypatch):
    """pip can move some packages before it fails."""
    _legacy_engine(stack)
    _versions(monkeypatch, OLD, {"camoufox": "0.4.11", "playwright": "1.62.0"})
    run = FakeRun(fail=[lambda cmd: cmd[0] == "bash"])
    monkeypatch.setattr(subprocess, "run", run)
    outcome = _tx(tmp_path, install=True).run()
    assert (stack / "camoufox-bin").exists()
    assert len(run.pip_calls()) == 1 and "playwright==1.58.0" in run.pip_calls()[0]
    assert "engine=False" in outcome


def test_profile_copy_failure_restores_packages_and_leaves_the_engine(stack, tmp_path, monkeypatch):
    """ENOSPC while copying the profile happens before any engine change."""
    _legacy_engine(stack)
    profile = provision.PROFILE_DIR
    profile.mkdir(parents=True)
    (profile / "compatibility.ini").write_text("[Compatibility]\nLastVersion=135.0.1-beta.24_x/x\n")
    _versions(monkeypatch, OLD, NEW)

    def no_space(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(provision, "copy_aside", no_space)
    run = FakeRun(fetch=_install_pinned)
    monkeypatch.setattr(subprocess, "run", run)
    outcome = _tx(tmp_path).run()
    assert (stack / "camoufox-bin").exists()
    assert not [c for c, _ in run.calls if c[1:3] == ["-m", "camoufox"]], "never fetched"
    assert [c[3] for c in run.pip_calls()] == ["install", "uninstall"]
    assert "DEGRADED" in outcome


def test_failed_restore_is_reported_not_raised(stack, tmp_path, monkeypatch, capsys):
    _legacy_engine(stack)
    _versions(monkeypatch, OLD, NEW)
    monkeypatch.setattr(subprocess, "run", FakeRun(fail=[lambda cmd: cmd[1:3] == ["-m", "pip"]]))
    outcome = _tx(tmp_path).run()
    assert "FAILED to restore the previous browser packages" in capsys.readouterr().out
    assert "engine=False" in outcome


def test_extras_that_do_not_import_fail_the_install(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[0] == "bash":
            return SimpleNamespace(returncode=0)
        return SimpleNamespace(returncode=1, stderr="No module named 'patchright'")

    monkeypatch.setattr(subprocess, "run", fake_run)
    tx = provision.Transaction(tmp_path / "repo", tmp_path / "lib.sh", install=True)
    with pytest.raises(provision.ProvisionError, match="patchright"):
        tx.install_extras()
    assert calls[0][0] == "bash" and "editable_install_guarded" in calls[0][2]


def test_install_that_left_old_camoufox_is_a_failure(tmp_path, monkeypatch):
    """The import check passes against camoufox 0.4 too; no pin file means the
    extra did not upgrade it."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stderr=""))
    monkeypatch.setattr(provision, "camoufox_pin", lambda: None)
    tx = provision.Transaction(tmp_path / "repo", tmp_path / "lib.sh", install=True)
    with pytest.raises(provision.ProvisionError, match="did not install camoufox 0.5"):
        tx.install_extras()


def test_fetch_exit_zero_without_an_engine_is_a_failure(stack, tmp_path, monkeypatch):
    """Measured: `camoufox fetch` with no network exits 0 and installs nothing."""
    monkeypatch.setattr(subprocess, "run", FakeRun())
    with pytest.raises(provision.ProvisionError, match="not ready after fetch"):
        _tx(tmp_path).engine()


def test_in_place_fetch_exit_zero_without_an_engine_is_a_failure(stack, tmp_path, monkeypatch):
    stack.mkdir()
    (stack / ".0.5_FLAG").touch()
    monkeypatch.setattr(subprocess, "run", FakeRun())
    with pytest.raises(provision.ProvisionError, match="not ready after fetch"):
        _tx(tmp_path).engine()


# ── signals ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_a_signal_during_the_fetch_restores_packages_and_handlers(
    stack, tmp_path, monkeypatch, capsys, sig
):
    _legacy_engine(stack)
    _versions(monkeypatch, OLD, NEW)
    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}

    def interrupted_fetch(install_dir, env):
        install_dir.mkdir(parents=True)
        os.kill(os.getpid(), sig)
        time.sleep(1)  # the handler raises before this returns

    run = FakeRun(fetch=interrupted_fetch)
    monkeypatch.setattr(subprocess, "run", run)
    outcome = _tx(tmp_path).run()

    assert f"interrupted by signal {sig.name}" in capsys.readouterr().out
    assert (stack / "camoufox-bin").read_text() == "binary"
    assert not list(stack.parent.glob(".camoufox-staging-*"))
    assert [c[3] for c in run.pip_calls()] == ["install", "uninstall"]
    assert "engine=False" in outcome
    assert {s: signal.getsignal(s) for s in before} == before


def test_signals_are_held_off_during_the_swap():
    got = []
    previous = signal.signal(signal.SIGHUP, lambda *a: got.append("hup"))
    try:
        with provision._signals_held():
            # Aimed at this thread: a process-directed signal could be taken by
            # another pytest thread that does not block it.
            signal.pthread_kill(threading.get_ident(), signal.SIGHUP)
            time.sleep(0.05)
            assert got == []
        time.sleep(0.05)
        assert got == ["hup"]
    finally:
        signal.signal(signal.SIGHUP, previous)


# ── Chromium (non-fatal) ──────────────────────────────────────────────────


def _chromium_profile(last_version: str = "145.0.7632.6") -> Path:
    profile = provision.CHROMIUM_PROFILE_DIR
    profile.mkdir(parents=True)
    (profile / "Last Version").write_text(last_version)
    (profile / "Cookies").write_text("c")
    return profile


def test_chromium_backup_refusal_is_non_fatal(stack, tmp_path, monkeypatch, capsys):
    _install_pinned(stack)
    _chromium_profile()
    monkeypatch.setattr(provision, "patchright_chromium_major", lambda: 151)
    monkeypatch.setattr(provision, "chromium_running", lambda: True)
    monkeypatch.setattr(subprocess, "run", FakeRun())
    outcome = _tx(tmp_path).run()
    assert "FAILED: Chromium profile backup" in capsys.readouterr().out
    assert "engine=True, chromium=False, launch=True" in outcome


def test_chromium_backup_oserror_is_non_fatal(tmp_path, monkeypatch):
    _chromium_profile()
    monkeypatch.setattr(provision, "patchright_chromium_major", lambda: 151)

    def no_space(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(provision, "copy_aside", no_space)
    monkeypatch.setattr(subprocess, "run", FakeRun())
    assert _tx(tmp_path).chromium() is False


def test_each_profile_checks_only_its_own_browser(tmp_path, monkeypatch):
    """A running desktop-automation Chromium must not block the Camoufox copy, and
    a running Camoufox must not block the Chromium copy."""
    camoufox_profile = tmp_path / "camoufox-profile"
    camoufox_profile.mkdir()
    (camoufox_profile / "prefs.js").write_text("x")
    monkeypatch.setattr(provision, "camoufox_running", lambda: False)
    monkeypatch.setattr(provision, "chromium_running", lambda: True)
    provision.backup_if_upgrading(camoufox_profile, 135, 156, "Camoufox")
    assert len(list(tmp_path.glob("camoufox-profile.pre-v135-*"))) == 1

    chromium_profile = tmp_path / "browser-profile"
    chromium_profile.mkdir()
    (chromium_profile / "Cookies").write_text("c")
    monkeypatch.setattr(provision, "camoufox_running", lambda: True)
    monkeypatch.setattr(provision, "chromium_running", lambda: False)
    provision.backup_if_upgrading(chromium_profile, 145, 151, "Chromium")
    assert len(list(tmp_path.glob("browser-profile.pre-v145-*"))) == 1

    monkeypatch.setattr(provision, "chromium_running", lambda: None)
    other = tmp_path / "other" / "browser-profile"
    other.mkdir(parents=True)
    (other / "Cookies").write_text("c")
    with pytest.raises(provision.ProvisionError, match="may be using the Chromium"):
        provision.backup_if_upgrading(other, 145, 151, "Chromium")


# ── capabilities.json ─────────────────────────────────────────────────────


def _write_caps(data: dict) -> Path:
    path = provision.CAPABILITIES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def test_capability_refreshed_to_active_keeping_the_rest(tmp_path):
    path = _write_caps(
        {
            "browser_automation": {"status": "degraded", "description": "the browser"},
            "secrets": {"status": "active", "description": "keys"},
        }
    )
    provision.refresh_capability(READY)
    data = json.loads(path.read_text())
    assert data["browser_automation"] == {"status": "active", "description": "the browser"}
    assert data["secrets"] == {"status": "active", "description": "keys"}
    assert not list(path.parent.glob("*.tmp")), "temp file replaced into place"


def test_capability_refreshed_to_degraded(tmp_path):
    path = _write_caps({"browser_automation": {"status": "active", "description": "d"}})
    provision.refresh_capability(NOT_READY)
    assert json.loads(path.read_text())["browser_automation"]["status"] == "degraded"


def test_capability_entry_missing_gets_the_runtime_description(tmp_path):
    from genesis.runtime._capabilities import _CAPABILITY_DESCRIPTIONS

    path = _write_caps({"secrets": {"status": "active", "description": "keys"}})
    provision.refresh_capability(READY)
    entry = json.loads(path.read_text())["browser_automation"]
    assert entry == {
        "status": "active",
        "description": _CAPABILITY_DESCRIPTIONS["browser_automation"],
    }


def test_capability_file_absent_is_left_absent(tmp_path):
    provision.refresh_capability(READY)
    assert not provision.CAPABILITIES_FILE.exists()


def test_capability_file_unreadable_never_raises(tmp_path):
    path = provision.CAPABILITIES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    provision.refresh_capability(READY)
    assert path.read_text() == "{not json"


def test_a_run_refreshes_the_capability(stack, tmp_path, monkeypatch):
    path = _write_caps({"browser_automation": {"status": "degraded", "description": "d"}})
    monkeypatch.setattr(subprocess, "run", FakeRun(fetch=_install_pinned))
    _tx(tmp_path).run()
    assert json.loads(path.read_text())["browser_automation"]["status"] == "active"


# ── preflight ─────────────────────────────────────────────────────────────


def _fake_pgrep(results):
    def run(cmd, **kw):
        rc, out = results.get(cmd[-1], (1, ""))
        return SimpleNamespace(returncode=rc, stdout=out, stderr="")

    return run


@pytest.mark.parametrize(
    "camoufox, chrome, exes, expected",
    [
        ((1, ""), (1, ""), {}, False),
        ((0, "4242\n"), (1, ""), {}, True),
        (
            (1, ""),
            (0, "77\n"),
            {77: "/home/u/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome"},
            True,
        ),
        ((1, ""), (0, "78\n"), {78: "/opt/google/chrome/chrome"}, False),
        ((2, ""), (1, ""), {}, None),
        ((1, ""), (3, ""), {}, None),
    ],
)
def test_browsers_running_matches_names_not_argv(monkeypatch, camoufox, chrome, exes, expected):
    monkeypatch.undo()  # the real per-browser checks, not the autouse stubs
    monkeypatch.setattr(
        subprocess, "run", _fake_pgrep({"camoufox-bin": camoufox, "chrome": chrome})
    )
    monkeypatch.setattr(provision.os, "readlink", lambda path: exes[int(path.split("/")[2])])
    assert _REAL_BROWSERS_RUNNING() is expected


def test_chromium_under_a_custom_browsers_path_counts(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/srv/pw")
    monkeypatch.setattr(subprocess, "run", _fake_pgrep({"chrome": (0, "9\n")}))
    monkeypatch.setattr(provision.os, "readlink", lambda path: "/srv/pw/chromium-1234/chrome")
    assert provision.chromium_running() is True


def test_running_check_uses_exact_names(monkeypatch):
    """`pgrep -f` matched shells whose arguments mention camoufox-bin; -x does not."""
    monkeypatch.undo()
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    _REAL_BROWSERS_RUNNING()
    assert seen and all(cmd[:2] == ["pgrep", "-x"] for cmd in seen)


def test_browsers_running_without_pgrep_is_unknown(monkeypatch):
    monkeypatch.undo()

    def missing(*a, **k):
        raise FileNotFoundError("pgrep")

    monkeypatch.setattr(subprocess, "run", missing)
    assert _REAL_BROWSERS_RUNNING() is None


def test_preflight_skips_when_running_is_unknown(monkeypatch):
    monkeypatch.setattr(provision, "MIN_FREE_BYTES", 0)
    monkeypatch.setattr(provision, "browsers_running", lambda: None)
    assert "could not tell" in provision.preflight()


def test_preflight_skips_on_short_disk(monkeypatch):
    monkeypatch.setattr(provision, "MIN_FREE_BYTES", 1 << 62)
    assert "GB free" in provision.preflight()


def test_preflight_proceeds(monkeypatch):
    monkeypatch.setattr(provision, "MIN_FREE_BYTES", 0)
    monkeypatch.setattr(provision, "browsers_running", lambda: False)
    assert provision.preflight() is None


def test_skipped_run_changes_nothing(cache, tmp_path, monkeypatch):
    _legacy_engine(cache)
    monkeypatch.setattr(provision, "preflight", lambda: "a browser is running")
    outcome = _tx(tmp_path).run()
    assert outcome.startswith("browser stack: SKIPPED")
    assert (cache / "camoufox-bin").exists()


# ── stateless profile backups ─────────────────────────────────────────────


def test_backup_only_when_a_newer_major_will_open_the_profile(tmp_path):
    profile = tmp_path / "browser-profile"
    profile.mkdir()
    (profile / "Last Version").write_text("145.0.7632.6")
    last = provision.chromium_profile_major(profile)
    assert last == 145
    provision.backup_if_upgrading(profile, last, 153, "Chromium")
    backups = list(tmp_path.glob("browser-profile.pre-v145-*"))
    assert len(backups) == 1
    provision.backup_if_upgrading(profile, last, 153, "Chromium")
    assert len(list(tmp_path.glob("browser-profile.pre-v145-*"))) == 1
    provision.backup_if_upgrading(profile, 153, 153, "Chromium")
    assert len(list(tmp_path.glob("browser-profile.pre-*"))) == 1


def test_interrupted_copy_is_not_a_backup(tmp_path):
    src = tmp_path / "camoufox-profile"
    src.mkdir()
    (src / "cookies.sqlite").write_text("c")
    (tmp_path / "camoufox-profile.pre-v135-20200101.tmp").mkdir()
    made = provision.copy_aside(src, "v135")
    assert made is not None and (made / "cookies.sqlite").read_text() == "c"


def test_firefox_profile_major(tmp_path):
    (tmp_path / "compatibility.ini").write_text(
        "[Compatibility]\nLastVersion=135.0.1-beta.24_20250315105650/20250315105650\n"
    )
    assert provision.firefox_profile_major(tmp_path) == 135
    assert provision.firefox_profile_major(tmp_path / "missing") is None


def test_smoke_never_launches_without_a_ready_engine(monkeypatch, tmp_path):
    monkeypatch.setattr(provision, "camoufox_engine_status", lambda: NOT_READY)
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a))
    assert _tx(tmp_path).smoke() is False
    assert ran == []


def test_unknown_version_still_backs_up(tmp_path):
    profile = tmp_path / "camoufox-profile"
    profile.mkdir()
    (profile / "prefs.js").write_text("x")
    provision.backup_if_upgrading(profile, None, 156, "Camoufox")
    assert len(list(tmp_path.glob("camoufox-profile.pre-vunknown-*"))) == 1


def test_backup_refused_while_its_browser_runs(tmp_path, monkeypatch):
    profile = tmp_path / "camoufox-profile"
    profile.mkdir()
    (profile / "prefs.js").write_text("x")
    monkeypatch.setattr(provision, "camoufox_running", lambda: True)
    with pytest.raises(provision.ProvisionError, match="may be using"):
        provision.backup_if_upgrading(profile, 135, 156, "Camoufox")


def test_downloads_are_staged_outside_the_default_temp(stack, tmp_path, monkeypatch):
    """Inside a CC session TMPDIR is the shared cc-tmp volume; the 1.3 GB engine
    download must never land there."""
    _legacy_engine(stack)
    run = FakeRun(fetch=_install_pinned)
    monkeypatch.setattr(subprocess, "run", run)
    _tx(tmp_path).run()
    staging_parent = tmp_path / "home" / "tmp"
    tmpdirs = [env["TMPDIR"] for _, env in run.calls]
    assert tmpdirs and all(t.startswith(str(staging_parent)) for t in tmpdirs)
    assert not any(staging_parent.iterdir()), "the staging dir is removed afterwards"


def test_a_second_concurrent_run_skips(tmp_path):
    import fcntl

    provision.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(provision.LOCK_FILE, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        outcome = _tx(tmp_path).run()
    assert "another provisioning run holds the lock" in outcome


# ── a step's whole process tree stops with it ─────────────────────────────


def _alive(pid: int) -> bool:
    """Running (a zombie waiting for its reaper does not count)."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False


# A wrapper whose grandchild does the work: the shape of `bash -c ... pip install`.
_TREE = 'sleep 60 & echo $! > "$1"; wait'


def test_run_group_returns_like_subprocess_run():
    proc = _REAL_RUN_GROUP(
        [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
        capture_output=True,
        text=True,
    )
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "out\n", "err\n")


def test_run_group_timeout_stops_the_grandchild(tmp_path):
    pidfile = tmp_path / "pid"
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        _REAL_RUN_GROUP(["bash", "-c", _TREE, "_", str(pidfile)], timeout=1)
    # The unreaped leader once kept the group visible, so every stop waited out
    # both grace periods (measured 21 s).
    assert time.monotonic() - started < provision._GROUP_GRACE_S
    assert not _alive(int(pidfile.read_text()))


def test_run_group_passes_the_lock_to_the_step(tmp_path):
    """A step that outlives its parent keeps the provisioning lock held."""
    held = open(tmp_path / "lock", "w")  # noqa: SIM115 - closed below
    try:
        proc = _REAL_RUN_GROUP(
            [sys.executable, "-c", f"import os; os.fstat({held.fileno()})"],
            pass_fds=(held.fileno(),),
        )
    finally:
        held.close()
    assert proc.returncode == 0


def test_run_group_interrupt_stops_the_grandchild_before_the_rollback(tmp_path):
    """The transaction's signal handler raises inside the wait. subprocess.run
    would kill only bash and leave the grandchild (pip) writing site-packages
    while the rollback started a second pip on the same files."""
    pidfile = tmp_path / "pid"

    def interrupted(signum, _frame):
        raise provision.ProvisionError("interrupted by signal SIGALRM")

    previous = signal.signal(signal.SIGALRM, interrupted)
    try:
        signal.setitimer(signal.ITIMER_REAL, 1.0)
        with pytest.raises(provision.ProvisionError):
            _REAL_RUN_GROUP(["bash", "-c", _TREE, "_", str(pidfile)])
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    assert not _alive(int(pidfile.read_text()))


def test_kill_group_never_signals_init_or_its_own_group(monkeypatch):
    sent = []
    monkeypatch.setattr(provision.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))
    for pid in (1, 0, os.getpgrp()):
        provision._kill_group(SimpleNamespace(pid=pid, poll=lambda: None))
    assert sent == []


def test_every_step_carries_the_lock(stack, tmp_path, monkeypatch):
    _legacy_engine(stack)
    seen = []

    def run(cmd, **kw):
        seen.append(kw.get("pass_fds"))
        return FakeRun(fetch=_install_pinned)(cmd, **kw)

    monkeypatch.setattr(subprocess, "run", run)
    _tx(tmp_path).run()
    assert seen and all(fds and len(fds) == 1 for fds in seen)


class _DeadPipe:
    """stdout after `| sed` died with the Ctrl-C."""

    def write(self, _text):
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        raise BrokenPipeError(32, "Broken pipe")


def test_a_dead_output_pipe_does_not_skip_the_rollback(stack, tmp_path, monkeypatch):
    """Measured: the BrokenPipeError from printing FAILED inside the failure
    handler skipped restore_packages, and the capability was never refreshed."""
    _legacy_engine(stack)
    _versions(monkeypatch, OLD, NEW)
    monkeypatch.setattr(provision, "_output_broken", False)
    provision.CAPABILITIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    provision.CAPABILITIES_FILE.write_text(
        json.dumps({"browser_automation": {"status": "active", "description": "x"}})
    )
    run = FakeRun(fetch=lambda install_dir, env: install_dir.mkdir(parents=True))
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(sys, "stdout", _DeadPipe())
    monkeypatch.setattr(sys, "stderr", _DeadPipe())
    outcome = _tx(tmp_path).run()
    assert [c[3] for c in run.pip_calls()] == ["install", "uninstall"]
    assert "engine=False" in outcome
    assert provision._output_broken is True
    # _finish still ran: the seeded "active" was replaced by the stub's engine
    # state (its package dir stays 0.5, so the legacy root reads LEGACY_LAYOUT).
    caps = json.loads(provision.CAPABILITIES_FILE.read_text())
    assert caps["browser_automation"]["status"] == "degraded"


def test_a_second_signal_during_the_rollback_is_recorded_not_raised(stack, tmp_path, monkeypatch):
    _legacy_engine(stack)
    _versions(monkeypatch, OLD, NEW)
    before = signal.getsignal(signal.SIGTERM)
    fake = FakeRun(fetch=lambda install_dir, env: install_dir.mkdir(parents=True))

    def run(cmd, **kw):
        if cmd[1:4] == ["-m", "pip", "install"]:
            os.kill(os.getpid(), signal.SIGTERM)  # the operator's second Ctrl-C
        return fake(cmd, **kw)

    monkeypatch.setattr(subprocess, "run", run)
    outcome = _tx(tmp_path).run()
    assert [c[3] for c in fake.pip_calls()] == ["install", "uninstall"]
    assert "engine=False" in outcome
    assert signal.getsignal(signal.SIGTERM) == before


# ── sessions that predate the upgrade ─────────────────────────────────────


def test_changed_packages_tell_the_operator_to_restart_sessions(
    stack, tmp_path, monkeypatch, capsys
):
    _legacy_engine(stack)
    monkeypatch.setattr(subprocess, "run", FakeRun(fetch=_install_pinned))
    _versions(monkeypatch, OLD, NEW)
    _tx(tmp_path).run()
    assert "restart running Claude Code sessions" in capsys.readouterr().out


def test_unchanged_packages_say_nothing_about_restarting(stack, tmp_path, monkeypatch, capsys):
    _install_pinned(stack)
    monkeypatch.setattr(subprocess, "run", FakeRun())
    _versions(monkeypatch, NEW, NEW)
    _tx(tmp_path).run()
    assert "restart" not in capsys.readouterr().out


# ── round-1 review findings ───────────────────────────────────────────────


def test_a_retry_refreshes_the_profile_backup(tmp_path):
    """After a failed upgrade the user kept using the old browser; the backup a
    later rollback needs is the newest one, not the first attempt's."""
    profile = tmp_path / "camoufox-profile"
    profile.mkdir()
    (profile / "cookies.sqlite").write_text("first")
    provision.backup_if_upgrading(profile, 135, 156, "Camoufox")
    (profile / "cookies.sqlite").write_text("after the failed attempt")
    provision.backup_if_upgrading(profile, 135, 156, "Camoufox")
    backups = list(tmp_path.glob("camoufox-profile.pre-v135-*"))
    assert len(backups) == 1
    assert (backups[0] / "cookies.sqlite").read_text() == "after the failed attempt"


def test_failed_copy_leaves_no_temp_and_keeps_the_old_backup(tmp_path, monkeypatch):
    profile = tmp_path / "camoufox-profile"
    profile.mkdir()
    (profile / "prefs.js").write_text("x")
    first = provision.copy_aside(profile, "v135")

    def no_space(src, dst, **kw):
        Path(dst).mkdir()
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(provision.shutil, "copytree", no_space)
    with pytest.raises(OSError):
        provision.copy_aside(profile, "v135")
    assert not list(tmp_path.glob("*.tmp"))
    assert first.is_dir()


def test_stale_staging_is_cleared_before_the_free_space_check(stack, tmp_path, monkeypatch):
    stale = stack.parent / ".camoufox-staging-99999"
    stale.mkdir()
    seen = []
    monkeypatch.setattr(provision, "preflight", lambda: seen.append(stale.exists()) or "low disk")
    _tx(tmp_path).run()
    assert seen == [False]


def test_staged_engine_must_launch_before_the_swap(stack, tmp_path, monkeypatch):
    """Files present is not usable: a staged engine that does not launch never
    replaces the working one, and the packages go back."""
    _legacy_engine(stack)
    _versions(monkeypatch, OLD, NEW)
    run = FakeRun(fetch=_install_pinned, fail=[lambda cmd: cmd[1:2] == ["-c"]])
    monkeypatch.setattr(subprocess, "run", run)
    outcome = _tx(tmp_path).run()
    assert (stack / "camoufox-bin").read_text() == "binary"  # legacy still in place
    assert not list(stack.parent.glob("camoufox.pre-0.5-*"))
    assert [c[3] for c in run.pip_calls()] == ["install", "uninstall"]
    smoke_envs = [env for cmd, env in run.calls if cmd[1:2] == ["-c"]]
    assert smoke_envs and ".camoufox-staging-" in smoke_envs[0]["XDG_CACHE_HOME"]
    assert "engine=False" in outcome


def test_a_set_override_is_reset_to_the_paired_build(stack, tmp_path, monkeypatch):
    _install_pinned(stack)
    (stack / "config.json").write_text(json.dumps({"channel": "official/prerelease"}))

    def run(cmd, **kw):
        if cmd[1:5] == ["-m", "camoufox", "set", "--release"]:
            (stack / "config.json").write_text("{}")
        return FakeRun()(cmd, **kw)

    monkeypatch.setattr(subprocess, "run", run)
    outcome = _tx(tmp_path).run()
    assert json.loads((stack / "config.json").read_text()) == {}
    assert "engine=True" in outcome


def test_unmet_extra_after_install_fails_the_step(tmp_path, monkeypatch):
    """The guarded installer masks pip's exit status; an older 0.5-era stack
    still imports and has a pin, so only the versions tell."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "x"\n[project.optional-dependencies]\n'
        'browser = ["camoufox>=0.5.7,<0.6", "playwright>=1.62,<1.63"]\n'
    )
    have = {"camoufox": "0.5.6", "playwright": "1.62.0"}
    monkeypatch.setattr(provision.importlib.metadata, "version", lambda n: have[n])
    assert provision.unmet_browser_requirements(pyproject) == [
        "camoufox<0.6,>=0.5.7 (installed 0.5.6)"
    ]
    have["camoufox"] = "0.5.7"
    assert provision.unmet_browser_requirements(pyproject) == []


def test_a_browser_holding_the_lock_skips_with_its_own_reason(stack, tmp_path):
    import fcntl

    provision.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(provision.LOCK_FILE, "w") as browser:
        fcntl.flock(browser, fcntl.LOCK_SH)
        outcome = _tx(tmp_path).run()
    assert "has a browser open" in outcome


def test_chromium_that_does_not_launch_gets_its_system_libraries(stack, tmp_path, monkeypatch):
    _install_pinned(stack)
    launches = iter([False, True])
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[1:2] == ["-c"] and "patchright" in cmd[2]:
            return SimpleNamespace(returncode=0 if next(launches) else 1, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    outcome = _tx(tmp_path).run()
    assert ["sudo", "-n", "true"] in calls
    assert any(c[:2] == ["sudo", "-n"] and "install-deps" in c for c in calls)
    assert "chromium=True" in outcome


def test_chromium_without_sudo_names_the_command(stack, tmp_path, monkeypatch, capsys):
    _install_pinned(stack)

    def run(cmd, **kw):
        if cmd[:2] == ["sudo", "-n"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if cmd[1:2] == ["-c"] and "patchright" in cmd[2]:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    outcome = _tx(tmp_path).run()
    assert "install-deps chromium" in capsys.readouterr().out
    assert "chromium=False" in outcome


def test_requirement_check_works_without_packaging(tmp_path, monkeypatch):
    """packaging is not a declared dependency; a minimal venv lacked it (measured
    in the live harness), so pip's vendored copy stands in."""
    import builtins

    real_import = builtins.__import__

    def no_packaging(name, *a, **k):
        if name == "packaging.requirements" or name == "packaging":
            raise ImportError(name)
        return real_import(name, *a, **k)

    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "x"\n[project.optional-dependencies]\nbrowser = ["camoufox>=0.5.7"]\n'
    )
    monkeypatch.setattr(builtins, "__import__", no_packaging)
    monkeypatch.setattr(provision.importlib.metadata, "version", lambda n: "0.5.7")
    assert provision.unmet_browser_requirements(pyproject) == []
