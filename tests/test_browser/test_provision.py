"""genesis.browser.provision: the one-process upgrade transaction behind
scripts/install_browser_stack.sh.

The hazards it exists for (measured on camoufox 0.5.7):
  * camoufox 0.5's own fetch deletes a pre-0.5 engine directory BEFORE
    downloading, so the new engine is staged next to the old one and swapped in
    by rename only once it checks out;
  * the new packages cannot drive the old engine, so a failure before the swap
    reinstalls the package versions that were there before and removes any the
    run added;
  * a Firefox/Chromium profile opened by a newer version is refused by an older
    one, so it is copied once before the newer version first opens it;
  * a step is usually a wrapper (bash running pip): an interrupt must stop the
    step's whole process tree before the rollback starts a second pip.
No test downloads or launches anything: subprocess steps are stubbed.
"""

from __future__ import annotations

import contextlib
import importlib.metadata
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

from genesis.browser import chromium, engine, provision

_REAL_BROWSERS_RUNNING = provision.browsers_running
_REAL_RUN_GROUP = provision._run_group
_REAL_INSTALLED_VERSIONS = provision.installed_versions
PIN = engine.CamoufoxPin("156.0.1", "beta.34", "official")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Every path the transaction can write is inside tmp_path."""
    home = tmp_path / "home"
    monkeypatch.setattr(provision, "PROFILE_DIR", home / ".genesis" / "camoufox-profile")
    monkeypatch.setattr(provision, "CHROMIUM_PROFILE_DIR", home / ".genesis" / "browser-profile")
    monkeypatch.setattr(chromium, "PROFILE_DIR", home / ".genesis" / "browser-profile")
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
    monkeypatch.setattr(
        provision,
        "_run_group",
        lambda cmd, on_survived=None, **kw: subprocess.run(cmd, **kw),
    )
    # The supported engine range must not depend on this machine's playwright.
    monkeypatch.setattr(engine, "_playwright_build_floor", lambda *_: None)


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    (tmp_path / "cache").mkdir()
    return tmp_path / "cache" / "camoufox"


@pytest.fixture
def stack(cache, tmp_path, monkeypatch):
    """camoufox 0.5.7 installed (a fake package dir: its pin and multiversion.py),
    engine status computed by the real engine module, preflight passing."""
    pkg = tmp_path / "pkg" / "camoufox"
    pkg.mkdir(parents=True)
    (pkg / "browser-pin.json").write_text(
        json.dumps(
            {
                "tag": f"v{PIN.version}-{PIN.build}",
                "repo_name": "Official",
                "version": PIN.version,
                "build": PIN.build,
            }
        )
    )
    (pkg / "multiversion.py").write_text("")

    def status(*, install_dir=None, package_dir=None):
        return engine.camoufox_engine_status(install_dir=install_dir, package_dir=pkg)

    monkeypatch.setattr(provision, "camoufox_engine_status", status)
    monkeypatch.setattr(provision, "camoufox_pin", lambda: PIN)
    monkeypatch.setattr(provision, "unmet_browser_requirements", lambda _pyproject: [])
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
    d = install_dir / "browsers" / "official" / f"{PIN.version}-{PIN.build}"
    d.mkdir(parents=True)
    (d / "version.json").write_text(json.dumps({"version": PIN.version, "release": PIN.build}))
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
    assert (
        stack / "browsers" / "official" / f"{PIN.version}-{PIN.build}" / "version.json"
    ).is_file()
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


def test_stale_staging_from_a_killed_run_is_removed(stack, tmp_path, monkeypatch):
    stale = stack.parent / ".camoufox-staging-99999"
    (stale / "camoufox").mkdir(parents=True)
    _install_pinned(stack)
    monkeypatch.setattr(subprocess, "run", FakeRun())
    _tx(tmp_path).run()
    assert not stale.exists()


def test_stale_staging_is_cleared_before_the_free_space_check(stack, tmp_path, monkeypatch):
    stale = stack.parent / ".camoufox-staging-99999"
    stale.mkdir()
    seen = []
    monkeypatch.setattr(provision, "preflight", lambda: seen.append(stale.exists()) or "low disk")
    _tx(tmp_path).run()
    assert seen == [False]


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


def test_a_failed_run_puts_the_camoufox_set_choice_back(stack, tmp_path, monkeypatch):
    """Devin + Codex (#2952, #2953): `set --release` forgets channel, pinned,
    pinned_sha and active_version; a rollback before the swap restored only
    the packages, so the operator's choice was lost."""
    stack.mkdir()
    (stack / ".0.5_FLAG").touch()
    choice = '{"channel": "official/prerelease", "pinned": "157.0-alpha.1"}'
    (stack / "config.json").write_text(choice)
    _versions(monkeypatch, {**NEW, "camoufox": "0.5.6"}, NEW)
    fake = FakeRun()  # its fetch installs nothing: "not ready after fetch"

    def run(cmd, **kw):
        if cmd[1:5] == ["-m", "camoufox", "set", "--release"]:
            (stack / "config.json").write_text("{}")
        return fake(cmd, **kw)

    monkeypatch.setattr(subprocess, "run", run)
    outcome = _tx(tmp_path).run()
    assert "engine=False" in outcome
    assert (stack / "config.json").read_text() == choice


def test_an_in_place_rollback_leaves_the_old_engine_selected(stack, tmp_path, monkeypatch):
    """Review finding on this slice: the in-place fetch records the new engine
    as active_version (multiversion.set_active) and leaves its directory, so a
    rolled-back unpinned package went on selecting the new engine."""
    stack.mkdir()
    (stack / ".0.5_FLAG").touch()
    old = stack / "browsers" / "official" / "150.0.2-beta.25"
    old.mkdir(parents=True)
    (old / "version.json").write_text(json.dumps({"version": "150.0.2", "release": "beta.25"}))
    before = '{"active_version": "browsers/official/150.0.2-beta.25"}'
    (stack / "config.json").write_text(before)
    _versions(monkeypatch, {**NEW, "camoufox": "0.5.6"}, NEW)

    def fetch(install_dir, env):
        _install_pinned(install_dir)
        (install_dir / "config.json").write_text(
            json.dumps({"active_version": f"browsers/official/{PIN.version}-{PIN.build}"})
        )

    smoke_fails = [lambda cmd: cmd[1:2] == ["-c"] and "AsyncCamoufox" in cmd[2]]
    monkeypatch.setattr(subprocess, "run", FakeRun(fetch=fetch, fail=smoke_fails))
    outcome = _tx(tmp_path).run()
    assert "launch=False" in outcome
    assert (stack / "config.json").read_text() == before
    assert sorted(p.name for p in (stack / "browsers" / "official").iterdir()) == [
        "150.0.2-beta.25"
    ]


def test_a_failed_root_restore_still_restores_the_packages(stack, tmp_path, monkeypatch, capsys):
    """Review finding on this slice: a failed config write (ENOSPC after a
    2.4 GB fetch) stopped the package restore that follows it."""
    stack.mkdir()
    (stack / ".0.5_FLAG").touch()
    (stack / "config.json").write_text('{"channel": "official/prerelease"}')
    _versions(monkeypatch, {**NEW, "camoufox": "0.5.6"}, NEW)

    def no_space(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(provision, "atomic_write_text", no_space)
    run = FakeRun()
    monkeypatch.setattr(subprocess, "run", run)
    _tx(tmp_path).run()
    assert [c[3] for c in run.pip_calls()] == ["install"]
    assert "official/prerelease" in capsys.readouterr().out, "the choice is printed to re-apply"


def test_a_truncated_pinned_engine_is_replaced_not_skipped(stack, tmp_path, monkeypatch):
    """Codex (#2953): camoufox's fetch returns early when the version directory
    and its version.json exist, so a truncated pinned engine was never repaired."""
    stack.mkdir()
    (stack / ".0.5_FLAG").touch()
    truncated = stack / "browsers" / "official" / f"{PIN.version}-{PIN.build}-09effb44"
    truncated.mkdir(parents=True)
    (truncated / "version.json").write_text(
        json.dumps({"version": PIN.version, "release": PIN.build})
    )
    # Another engine with no executable is not the pin's, so it stays.
    other = stack / "browsers" / "official" / "150.0.2-beta.25"
    other.mkdir(parents=True)
    (other / "version.json").write_text(json.dumps({"version": "150.0.2", "release": "beta.25"}))

    def fetch(install_dir, env):
        # multiversion.install_versioned without replace: skip what is there.
        if any((install_dir / "browsers" / "official").glob(f"{PIN.version}-{PIN.build}*")):
            return
        _install_pinned(install_dir)

    monkeypatch.setattr(subprocess, "run", FakeRun(fetch=fetch))
    outcome = _tx(tmp_path).run()
    assert "engine=True" in outcome, outcome
    assert not truncated.exists()
    assert other.is_dir()


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


def test_smoke_never_launches_without_a_ready_engine(monkeypatch, tmp_path):
    monkeypatch.setattr(provision, "camoufox_engine_status", lambda: NOT_READY)
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a))
    assert _tx(tmp_path).smoke() is False
    assert ran == []


def test_a_signal_during_the_fetch_leaves_no_stage_and_restores(
    stack, tmp_path, monkeypatch, capsys
):
    _legacy_engine(stack)
    _versions(monkeypatch, OLD, NEW)

    def interrupted_fetch(install_dir, env):
        install_dir.mkdir(parents=True)
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(1)  # the handler raises before this returns

    run = FakeRun(fetch=interrupted_fetch)
    monkeypatch.setattr(subprocess, "run", run)
    outcome = _tx(tmp_path).run()
    assert "interrupted by signal SIGTERM" in capsys.readouterr().out
    assert (stack / "camoufox-bin").read_text() == "binary"
    assert not list(stack.parent.glob(".camoufox-staging-*"))
    assert [c[3] for c in run.pip_calls()] == ["install", "uninstall"]
    assert "engine=False" in outcome


# ── review findings on this slice ─────────────────────────────────────────


def test_a_signal_swallowed_by_a_status_read_still_stops_the_run(
    stack, tmp_path, monkeypatch, capsys
):
    """camoufox_engine_status never raises, so the handler's one ProvisionError
    can die inside it; the next step must still see the interrupt."""
    _legacy_engine(stack)
    _versions(monkeypatch, OLD, NEW)
    real_status = engine._status
    calls = []

    def status_hit_by_sigterm(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(1)  # the handler raises here, inside the catch-all
        return real_status(*a, **k)

    monkeypatch.setattr(engine, "_status", status_hit_by_sigterm)
    run = FakeRun(fetch=_install_pinned)
    monkeypatch.setattr(subprocess, "run", run)
    outcome = _tx(tmp_path).run()
    assert not [c for c, _ in run.calls if c[1:3] == ["-m", "camoufox"]], "never fetched"
    assert (stack / "camoufox-bin").read_text() == "binary"
    assert [c[3] for c in run.pip_calls()] == ["install", "uninstall"]
    assert "interrupted" in capsys.readouterr().out
    assert "engine=False" in outcome


def test_an_in_place_engine_that_does_not_launch_rolls_the_packages_back(
    stack, tmp_path, monkeypatch
):
    stack.mkdir()
    (stack / ".0.5_FLAG").touch()
    _versions(monkeypatch, {**NEW, "camoufox": "0.5.6"}, NEW)
    _write_caps({"browser_automation": {"status": "active", "description": "d"}})
    run = FakeRun(
        fetch=_install_pinned, fail=[lambda cmd: cmd[1:2] == ["-c"] and "AsyncCamoufox" in cmd[2]]
    )
    monkeypatch.setattr(subprocess, "run", run)
    outcome = _tx(tmp_path).run()
    assert [c[3] for c in run.pip_calls()] == ["install"]
    assert "camoufox==0.5.6" in run.pip_calls()[0]
    assert "engine=True" in outcome and "launch=False" in outcome
    # The root goes back to how it was (no engine): the old unpinned package
    # must not go on to select the engine the failed run fetched.
    assert not (stack / "browsers" / "official" / f"{PIN.version}-{PIN.build}").exists()
    caps = json.loads(provision.CAPABILITIES_FILE.read_text())
    assert caps["browser_automation"]["status"] == "degraded"


def test_a_swapped_engine_that_then_fails_to_launch_is_reported_installed(
    stack, tmp_path, monkeypatch
):
    """After the swap the new packages stay (they match the engine in place), so
    a failed final launch reports the engine installed but not launching."""
    _legacy_engine(stack)
    _versions(monkeypatch, OLD, NEW)
    fake = FakeRun(fetch=_install_pinned)

    def run(cmd, env=None, **kw):
        staged = env is not None and ".camoufox-staging-" in env.get("XDG_CACHE_HOME", "")
        if cmd[1:2] == ["-c"] and "AsyncCamoufox" in cmd[2] and not staged:
            fake.calls.append((cmd, env))
            return SimpleNamespace(returncode=1, stdout="", stderr="no display")
        return fake(cmd, env=env, **kw)

    monkeypatch.setattr(subprocess, "run", run)
    outcome = _tx(tmp_path).run()
    assert "launch=False" in outcome and "installed but did not launch" in outcome
    assert fake.pip_calls() == []


def test_a_signal_during_the_swap_waits_and_keeps_the_new_packages(stack, tmp_path, monkeypatch):
    """The renames run with signals held; the signal then lands after the swap,
    when the new packages are the ones that match the engine in place."""
    _legacy_engine(stack)
    _versions(monkeypatch, OLD, NEW)
    real_rename = Path.rename
    sent = []

    def rename(self, target):
        if self == stack and not sent:
            sent.append(1)
            # Aimed at this thread, which holds signals (another thread of the
            # test process could otherwise take a process-directed signal).
            signal.pthread_kill(threading.get_ident(), signal.SIGTERM)
            time.sleep(0.2)
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    run = FakeRun(fetch=_install_pinned)
    monkeypatch.setattr(subprocess, "run", run)
    _tx(tmp_path).run()
    assert sent
    assert (stack / ".0.5_FLAG").exists(), "the new engine is in place"
    assert (stack.with_name(f"camoufox.pre-0.5-{provision._stamp()}") / "camoufox-bin").exists()
    assert run.pip_calls() == [], "never rolled back over the swapped engine"


def test_a_launch_that_hangs_is_a_failed_launch(stack, tmp_path, monkeypatch, capsys):
    _install_pinned(stack)

    def run(cmd, **kw):
        if cmd[1:2] == ["-c"]:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    assert _tx(tmp_path).smoke() is False
    assert "did not launch within" in capsys.readouterr().out


# ── a ready engine ────────────────────────────────────────────────────────


def test_ready_engine_completes_the_run(stack, tmp_path, monkeypatch):
    _install_pinned(stack)
    run = FakeRun()
    monkeypatch.setattr(subprocess, "run", run)
    outcome = _tx(tmp_path, install=True).run()
    assert "ready (engine=True, chromium=True, launch=True)" in outcome
    assert run.pip_calls() == []
    assert not [c for c, _ in run.calls if c[1:3] == ["-m", "camoufox"]], "never fetched"


# ── failure: the packages go back ─────────────────────────────────────────


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
            "--no-deps",
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
        "patchright==1.58.0",
        "playwright==1.58.0",
    ]


def test_failure_with_no_previous_camoufox_still_restores_the_rest(stack, tmp_path, monkeypatch):
    """Review finding (#2951, #2952, #2953, #2956): with no camoufox before the
    run, the restore returned early, leaving an upgraded playwright and an added
    patchright (with no Chromium) behind a failed transaction."""
    _versions(monkeypatch, {"playwright": "1.58.0"}, NEW)
    run = FakeRun()
    monkeypatch.setattr(subprocess, "run", run)
    outcome = _tx(tmp_path, install=True).run()
    assert run.pip_calls() == [
        [sys.executable, "-m", "pip", "install", "--quiet", "--no-deps", "playwright==1.58.0"],
        [sys.executable, "-m", "pip", "uninstall", "--yes", "--quiet", "camoufox", "patchright"],
    ]
    assert "engine=False" in outcome


def test_restore_covers_the_dependency_closure(stack, tmp_path, monkeypatch):
    """Codex P1 (#2951): the extra moves transitive dependencies too; restoring
    only the three top-level pins left them upgraded or added."""
    _legacy_engine(stack)
    _versions(
        monkeypatch,
        {**OLD, "greenlet": "3.0.3", "pyee": "11.0.0"},
        {**NEW, "greenlet": "3.1.1", "browserforge": "1.2.3"},
    )
    run = FakeRun()
    monkeypatch.setattr(subprocess, "run", run)
    _tx(tmp_path, install=True).run()
    install, uninstall = run.pip_calls()
    assert install[-4:] == [
        "camoufox==0.4.11",
        "greenlet==3.0.3",
        "playwright==1.58.0",
        "pyee==11.0.0",
    ]
    assert "--no-deps" in install
    assert uninstall[-2:] == ["browserforge", "patchright"]


def test_a_name_kept_out_of_the_restore_is_never_uninstalled(stack, tmp_path, monkeypatch):
    """Review finding on this slice: an editable or unreadable dist is recorded
    without a version; if it turns up readable after the run it is NOT 'added'."""
    _legacy_engine(stack)
    _versions(monkeypatch, {**OLD, "genesis-v3": None}, {**OLD, "genesis-v3": "3.0.0b18"})
    run = FakeRun()
    monkeypatch.setattr(subprocess, "run", run)
    _tx(tmp_path, install=True).run()
    assert not [arg for c in run.pip_calls() for arg in c if arg.startswith("genesis-v3")]


def test_a_rollback_reinstalls_genesis_when_it_stopped_importing(stack, tmp_path, monkeypatch):
    """Review finding on this slice: pip reinstalls the editable Genesis on every
    run, so an interrupted install can leave it unimportable; the restore checks
    it and puts it back from the checkout."""
    _legacy_engine(stack)
    _versions(monkeypatch, OLD, NEW)
    run = FakeRun(fail=[lambda cmd: cmd[1:2] == ["-c"] and "genesis.runtime" in cmd[2]])
    monkeypatch.setattr(subprocess, "run", run)
    _tx(tmp_path, install=True).run()
    installs = [c for c, _ in run.calls if c[0] == "bash"]
    assert [c[-1] for c in installs] == ["browser", ""], "the extra, then Genesis alone"
    assert "editable_install_guarded" in installs[1][2]


def test_installed_versions_is_the_whole_venv_but_not_editables():
    """The snapshot is every index-installed distribution; an editable or URL
    install (Genesis itself) cannot be put back by name==version, so it is
    recorded without a version rather than 'restored' from an index."""
    found = _REAL_INSTALLED_VERSIONS()
    assert "pytest" in found and found["pytest"] == importlib.metadata.version("pytest")
    for dist in importlib.metadata.distributions():
        if dist.read_text("direct_url.json"):
            name = provision._canonical(dist.metadata["Name"])
            assert name in found and found[name] is None


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


# ── signals ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT, signal.SIGHUP])
def test_a_signal_during_the_install_restores_packages_and_handlers(
    stack, tmp_path, monkeypatch, capsys, sig
):
    _legacy_engine(stack)
    _versions(monkeypatch, OLD, NEW)
    before = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    fake = FakeRun()

    def run(cmd, **kw):
        if cmd[0] == "bash":
            os.kill(os.getpid(), sig)
            time.sleep(1)  # the handler raises before this returns
        return fake(cmd, **kw)

    monkeypatch.setattr(subprocess, "run", run)
    outcome = _tx(tmp_path, install=True).run()

    assert f"interrupted by signal {sig.name}" in capsys.readouterr().out
    assert (stack / "camoufox-bin").read_text() == "binary"
    assert [c[3] for c in fake.pip_calls()] == ["install", "uninstall"]
    assert "engine=False" in outcome
    assert {s: signal.getsignal(s) for s in before} == before


def test_signals_are_held_off_inside_the_guard():
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
    _install_pinned(stack)
    monkeypatch.setattr(subprocess, "run", FakeRun())
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


def test_another_users_chrome_does_not_block(monkeypatch):
    """readlink of another user's /proc/<pid>/exe raises PermissionError; that
    process cannot hold our profile, so it must not read as "unknown"."""
    monkeypatch.undo()
    monkeypatch.setattr(subprocess, "run", _fake_pgrep({"chrome": (0, "9\n")}))

    def denied(path):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(provision.os, "readlink", denied)
    assert provision.chromium_running() is False


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


def test_chromium_running_sees_a_legacy_headless_shell(monkeypatch):
    """Codex P1 (#2951): older Playwright names its shell `headless_shell`,
    which the lock module already treats as a browser; the preflight did not."""
    monkeypatch.undo()
    monkeypatch.setattr(subprocess, "run", _fake_pgrep({"headless_shell": (0, "55\n")}))
    monkeypatch.setattr(
        provision.os,
        "readlink",
        lambda path: "/home/u/.cache/ms-playwright/chromium-1100/chrome-linux/headless_shell",
    )
    assert provision.chromium_running() is True


def _driver_exe() -> str:
    return "/venv/lib/python3.12/site-packages/playwright/driver/node"


@pytest.mark.parametrize(
    "exe, expected",
    [(_driver_exe(), True), ("/usr/bin/node", False)],
)
def test_a_live_playwright_driver_blocks_the_upgrade(monkeypatch, exe, expected):
    """Codex P1 (#2951): a remote-CDP or TinyFish session holds no stack lock
    but runs this venv's Playwright Node driver, which the install replaces."""
    monkeypatch.undo()
    monkeypatch.setattr(subprocess, "run", _fake_pgrep({"node": (0, "66\n")}))
    monkeypatch.setattr(provision.os, "readlink", lambda path: exe)
    monkeypatch.setattr(
        provision,
        "_driver_dirs",
        lambda: ["/venv/lib/python3.12/site-packages/playwright/driver/"],
    )
    assert _REAL_BROWSERS_RUNNING() is expected


def test_driver_dirs_are_the_packages_own(tmp_path, monkeypatch):
    pkg = tmp_path / "site-packages" / "playwright"
    pkg.mkdir(parents=True)
    specs = {"playwright": SimpleNamespace(submodule_search_locations=[str(pkg)])}
    monkeypatch.setattr(provision.importlib.util, "find_spec", lambda name: specs.get(name))
    assert provision._driver_dirs() == [str(pkg.resolve() / "driver") + os.sep]


def test_a_signal_while_a_step_starts_still_stops_it(tmp_path, monkeypatch):
    """Review finding on this slice: an interrupt raised inside Popen's own
    construction escaped before the step was held, so nothing stopped it and
    the rollback could start a second pip beside it."""
    pidfile = tmp_path / "pid"
    real_popen = subprocess.Popen

    def popen_hit_by_sigterm(*a, **k):
        proc = real_popen(*a, **k)
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(0.5)  # the handler runs here, before Popen has returned
        return proc

    monkeypatch.setattr(subprocess, "Popen", popen_hit_by_sigterm)

    def handler(signum, _frame):
        # The transaction's handler; before the fix it raised unconditionally.
        raise_interrupt = getattr(provision, "_raise_interrupt", None)
        if raise_interrupt is None:
            raise provision.ProvisionError("interrupted by signal SIGTERM")
        raise_interrupt(signum)

    previous = signal.signal(signal.SIGTERM, handler)
    try:
        with pytest.raises(provision.ProvisionError, match="SIGTERM"):
            _REAL_RUN_GROUP(["bash", "-c", _TREE, "_", str(pidfile)])
    finally:
        signal.signal(signal.SIGTERM, previous)
    deadline = time.monotonic() + 5
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(int(pidfile.read_text()))


def test_a_disk_probe_error_is_a_reported_skip(cache, tmp_path, monkeypatch):
    """Devin (#2951): an OSError from the free-space probe escaped before
    _finish, so the capability was never refreshed and no outcome line printed."""

    def unreadable(path):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(provision.shutil, "disk_usage", unreadable)
    outcome = _tx(tmp_path, install=True).run()
    assert outcome.startswith("browser stack: SKIPPED")
    assert "Permission denied" in outcome


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
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a))
    outcome = _tx(tmp_path, install=True).run()
    assert outcome.startswith("browser stack: SKIPPED")
    assert (cache / "camoufox-bin").exists()
    assert ran == []


def test_steps_run_with_a_temp_dir_outside_the_default_temp(stack, tmp_path, monkeypatch):
    """Inside a CC session TMPDIR is the shared cc-tmp volume; the browser
    downloads must never land there."""
    _install_pinned(stack)
    run = FakeRun()
    monkeypatch.setattr(subprocess, "run", run)
    _tx(tmp_path, install=True).run()
    staging_parent = tmp_path / "home" / "tmp"
    tmpdirs = [env["TMPDIR"] for _, env in run.calls]
    assert tmpdirs and all(t.startswith(str(staging_parent)) for t in tmpdirs)
    assert not any(staging_parent.iterdir()), "the temp dir is removed afterwards"


# ── the lock ──────────────────────────────────────────────────────────────


def test_a_second_concurrent_run_skips(tmp_path):
    import fcntl

    provision.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(provision.LOCK_FILE, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        outcome = _tx(tmp_path).run()
    assert "another provisioning run holds the lock" in outcome


def test_a_browser_holding_the_lock_skips_with_its_own_reason(stack, tmp_path):
    import fcntl

    provision.LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(provision.LOCK_FILE, "w") as browser:
        fcntl.flock(browser, fcntl.LOCK_SH)
        outcome = _tx(tmp_path).run()
    assert "has a browser open" in outcome


def test_every_step_carries_the_lock(stack, tmp_path, monkeypatch):
    _install_pinned(stack)
    tx = _tx(tmp_path, install=True)
    seen = []

    def run(cmd, **kw):
        seen.append((kw.get("pass_fds"), tx.lock_fd))
        return FakeRun()(cmd, **kw)

    monkeypatch.setattr(subprocess, "run", run)
    tx.run()
    assert seen and all(fds == (lock_fd,) and lock_fd is not None for fds, lock_fd in seen)


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
        assert provision._kill_group(SimpleNamespace(pid=pid, poll=lambda: None)) is False
    assert sent == []


def test_kill_group_reports_a_member_that_outlives_sigkill(monkeypatch):
    """A member in uninterruptible I/O (pip writing site-packages) ignores
    SIGKILL until the I/O returns; the group is NOT gone."""
    sent = []
    monkeypatch.setattr(provision, "_GROUP_GRACE_S", 0.2)
    monkeypatch.setattr(provision.os, "killpg", lambda pgid, sig: sent.append(sig))
    assert provision._kill_group(SimpleNamespace(pid=424242, poll=lambda: None)) is False
    assert signal.SIGTERM in sent and signal.SIGKILL in sent


def test_run_group_says_when_the_group_survived(tmp_path, monkeypatch):
    """The interrupt is replaced by StepSurvived, so the transaction knows the
    step may still be writing."""
    pidfile = tmp_path / "pid"

    def survived(proc):
        # Stop the real tree (so the test leaves nothing behind) but report what
        # _kill_group reports when a member outlives SIGKILL.
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        return False

    monkeypatch.setattr(provision, "_kill_group", survived)
    with pytest.raises(provision.StepSurvived) as caught:
        _REAL_RUN_GROUP(["bash", "-c", _TREE, "_", str(pidfile)], timeout=0.5)
    assert isinstance(caught.value.__cause__, subprocess.TimeoutExpired)


def test_run_group_does_not_wait_for_a_leader_that_survived(tmp_path, monkeypatch):
    """Devin + Codex (#2951, #2952, #2953): `with Popen` waits for the leader
    with no timeout on the way out, so a leader that outlived SIGKILL hung the
    provisioner while it held the lock. The leader here is left alive on
    purpose (the stub reports it survived without killing it)."""
    leaders = []

    def survived(proc):
        leaders.append(proc.pid)
        return False

    monkeypatch.setattr(provision, "_kill_group", survived)
    started = time.monotonic()
    try:
        with pytest.raises(provision.StepSurvived):
            _REAL_RUN_GROUP(["sleep", "8"], timeout=0.5, capture_output=True, text=True)
        assert time.monotonic() - started < 4, "it waited for the surviving leader"
    finally:
        for pid in leaders:
            assert pid > 1 and pid != os.getpgrp()
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pid, signal.SIGKILL)


def test_no_rollback_while_a_step_survives(stack, tmp_path, monkeypatch, capsys):
    """Codex finding on #2899: the rollback started a second pip beside a pip
    that outlived SIGKILL. Now the restore is refused and says why."""
    _legacy_engine(stack)
    _versions(monkeypatch, OLD, NEW)
    fake = FakeRun()

    def run(cmd, **kw):
        if cmd[0] == "bash":
            raise provision.StepSurvived("`bash -c ...` still has processes after SIGKILL")
        return fake(cmd, **kw)

    monkeypatch.setattr(provision, "_run_group", run)
    outcome = _tx(tmp_path, install=True).run()
    out = capsys.readouterr().out
    assert fake.pip_calls() == []
    assert "were NOT restored" in out
    assert "engine=False" in outcome


def test_a_second_signal_while_the_step_is_stopped_keeps_the_survivor_known(
    stack, tmp_path, monkeypatch
):
    """Review finding on this slice: the operator's second Ctrl-C, delivered when
    the kill wait unmasks signals, used to raise before StepSurvived did, so the
    rollback ran a second pip beside the survivor anyway."""
    _legacy_engine(stack)
    _versions(monkeypatch, OLD, NEW)
    monkeypatch.setattr(provision, "_run_group", _REAL_RUN_GROUP)
    pids = []

    def survived(proc):
        pids.append(proc.pid)
        os.killpg(proc.pid, signal.SIGKILL)  # leave nothing behind in the test
        proc.wait()
        # The second Ctrl-C, held until unmask. Aimed at this thread: a
        # process-directed signal can be taken by another thread of the test
        # process that does not block it (the provisioner itself is one thread).
        signal.pthread_kill(threading.get_ident(), signal.SIGINT)
        return False

    monkeypatch.setattr(provision, "_kill_group", survived)
    tx = _tx(tmp_path, install=True)
    monkeypatch.setattr(provision, "STEP_TIMEOUT_S", 0.5)
    lib = tmp_path / "venv_setup.sh"
    lib.write_text("editable_install_guarded() { sleep 60; }\n")
    tx.lib = lib
    restores = []
    monkeypatch.setattr(tx, "restore_packages", lambda: restores.append(tx.step_survived))
    tx.run()
    assert pids, "the step was stopped"
    assert tx.step_survived is True
    assert restores == [True], "the restore saw the survivor"


def test_unsatisfied_extra_after_the_install_rolls_back(stack, tmp_path, monkeypatch):
    """The guarded installer masks pip's exit status: an install that left
    camoufox 0.5.6 behind still imports and has a pin; only the version check
    fails it, and the run then restores the previous packages."""
    _legacy_engine(stack)
    _versions(monkeypatch, OLD, {**OLD, "camoufox": "0.5.6"})
    monkeypatch.setattr(
        provision,
        "unmet_browser_requirements",
        lambda _p: ["camoufox<0.6,>=0.5.7 (installed 0.5.6)"],
    )
    run = FakeRun()
    monkeypatch.setattr(subprocess, "run", run)
    tx = _tx(tmp_path, install=True)
    with pytest.raises(provision.ProvisionError, match="not satisfied"):
        tx.install_extras()
    outcome = tx.run()
    assert run.pip_calls() and "camoufox==0.4.11" in run.pip_calls()[0]
    assert "engine=False" in outcome


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
    run = FakeRun()
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(sys, "stdout", _DeadPipe())
    monkeypatch.setattr(sys, "stderr", _DeadPipe())
    outcome = _tx(tmp_path).run()
    assert [c[3] for c in run.pip_calls()] == ["install", "uninstall"]
    assert "engine=False" in outcome
    assert provision._output_broken is True
    # _finish still ran: the seeded "active" was replaced by the stub's engine
    # state (its package dir is 0.5, so the legacy root reads LEGACY_LAYOUT).
    caps = json.loads(provision.CAPABILITIES_FILE.read_text())
    assert caps["browser_automation"]["status"] == "degraded"


def test_a_second_signal_during_the_rollback_is_recorded_not_raised(stack, tmp_path, monkeypatch):
    _legacy_engine(stack)
    _versions(monkeypatch, OLD, NEW)
    before = signal.getsignal(signal.SIGTERM)
    fake = FakeRun()

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
    _install_pinned(stack)
    monkeypatch.setattr(subprocess, "run", FakeRun())
    _versions(monkeypatch, {**NEW, "camoufox": "0.5.6"}, NEW)
    _tx(tmp_path).run()
    assert "restart running Claude Code sessions" in capsys.readouterr().out


def test_unchanged_packages_say_nothing_about_restarting(stack, tmp_path, monkeypatch, capsys):
    _install_pinned(stack)
    monkeypatch.setattr(subprocess, "run", FakeRun())
    _versions(monkeypatch, NEW, NEW)
    _tx(tmp_path).run()
    assert "restart" not in capsys.readouterr().out


# ── stateless profile backups ─────────────────────────────────────────────


def _firefox_profile(profile: Path, last_version: str = "135.0.1-beta.24_x/x") -> Path:
    profile.mkdir(parents=True)
    (profile / "compatibility.ini").write_text(f"[Compatibility]\nLastVersion={last_version}\n")
    (profile / "cookies.sqlite").write_text("c")
    return profile


def test_a_same_major_build_change_backs_up_the_profile(stack, tmp_path, monkeypatch):
    """Codex (#2953): Firefox's downgrade check compares the whole version, so a
    newer 156 build can make the profile unusable to the older 156 build; a
    major-only comparison skipped the copy."""
    _legacy_engine(stack)
    _firefox_profile(provision.PROFILE_DIR, f"{PIN.version}-beta.33_20260101/20260101")
    monkeypatch.setattr(subprocess, "run", FakeRun(fetch=_install_pinned))
    _tx(tmp_path).run()
    assert len(list(provision.PROFILE_DIR.parent.glob("camoufox-profile.pre-v156*"))) == 1


_OPENED = f"{PIN.version}-{PIN.build}_20260101/20260101"


@pytest.mark.parametrize(
    "last_version, opened_days_ago, engine_ready, expected",
    [
        (_OPENED, 20, True, "ready"),
        # The 14 days run from the migration, not from the copy (Devin, #2953).
        (_OPENED, 2, True, "opened_recently"),
        (f"{PIN.version}-beta.33_20260101/20260101", 20, True, "not_opened_by_the_new_engine"),
        ("135.0.1-beta.24_x/x", 20, True, "not_opened_by_the_new_engine"),
        ("", 20, True, "unknown"),  # a profile with no readable record
        (None, 0, True, "ready"),  # no profile at all: nothing of it to protect
        (_OPENED, 20, False, engine.PIN_NOT_INSTALLED),
    ],
)
def test_camoufox_backups_are_prunable_only_once_the_engine_opened_the_profile(
    stack, last_version, opened_days_ago, engine_ready, expected
):
    """Devin red (#2953) + Codex (#2953, #2956): engine files being ready said
    nothing about the profile; its rollback copy could be pruned before the new
    engine ever opened it, or after an upgrade whose launch never worked."""
    if engine_ready:
        _install_pinned(stack)
    if last_version is not None:
        _firefox_profile(provision.PROFILE_DIR, last_version)
        if not last_version:
            (provision.PROFILE_DIR / "compatibility.ini").unlink()
        else:
            t = time.time() - opened_days_ago * 86400
            os.utime(provision.PROFILE_DIR / "compatibility.ini", (t, t))
    assert provision.camoufox_backup_state() == expected


def test_a_rollback_run_keeps_the_copy_the_rollback_needs(tmp_path):
    """Review finding on this slice: copies were kept newest-per-MAJOR, so going
    back from beta.35 to beta.34 replaced the beta.34 copy with the beta.35
    profile, which beta.34 refuses."""
    profile = _firefox_profile(tmp_path / "camoufox-profile", "156.0.1-beta.34_1/1")
    (profile / "cookies.sqlite").write_text("opened by beta.34")
    provision.backup_if_upgrading(profile, "156.0.1-beta.34", "156.0.1-beta.35", "Camoufox")
    (profile / "compatibility.ini").write_text("[Compatibility]\nLastVersion=156.0.1-beta.35_2/2\n")
    (profile / "cookies.sqlite").write_text("opened by beta.35")
    provision.backup_if_upgrading(profile, "156.0.1-beta.35", "156.0.1-beta.34", "Camoufox")
    contents = sorted(
        (p / "cookies.sqlite").read_text() for p in tmp_path.glob("camoufox-profile.pre-*")
    )
    assert contents == ["opened by beta.34", "opened by beta.35"]


@pytest.mark.parametrize("last", ["../../etc", "1" * 300, "156.0.1/../x"])
def test_an_odd_recorded_version_gets_a_safe_label(tmp_path, last):
    profile = _firefox_profile(tmp_path / "camoufox-profile")
    provision.backup_if_upgrading(profile, last, "156.0.1-beta.34", "Camoufox")
    assert [p.name.split("-2")[0] for p in tmp_path.glob("camoufox-profile.pre-*")] == [
        "camoufox-profile.pre-vunknown"
    ]


def test_an_engine_upgrade_backs_up_the_profile_first(stack, tmp_path, monkeypatch):
    _legacy_engine(stack)
    _firefox_profile(provision.PROFILE_DIR)
    order = []

    def fetch(install_dir, env):
        order.append(
            ("fetch", len(list(provision.PROFILE_DIR.parent.glob("camoufox-profile.pre-v135*"))))
        )
        _install_pinned(install_dir)

    monkeypatch.setattr(subprocess, "run", FakeRun(fetch=fetch))
    outcome = _tx(tmp_path).run()
    assert order == [("fetch", 1)], "the copy exists before the new engine is fetched"
    assert "engine=True" in outcome


def test_profile_copy_failure_restores_packages_and_leaves_the_engine(stack, tmp_path, monkeypatch):
    """ENOSPC while copying the profile happens before any engine change."""
    _legacy_engine(stack)
    _firefox_profile(provision.PROFILE_DIR)
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


def test_backup_only_when_another_build_will_open_the_profile(tmp_path):
    profile = _firefox_profile(tmp_path / "camoufox-profile")
    last = provision.firefox_profile_version(profile)
    assert last == "135.0.1-beta.24"
    provision.backup_if_upgrading(profile, last, "156.0.1-beta.34", "Camoufox")
    assert len(list(tmp_path.glob("camoufox-profile.pre-v135*"))) == 1
    provision.backup_if_upgrading(profile, last, "156.0.1-beta.34", "Camoufox")
    assert len(list(tmp_path.glob("camoufox-profile.pre-v135*"))) == 1
    provision.backup_if_upgrading(profile, "156.0.1-beta.34", "156.0.1-beta.34", "Camoufox")
    assert len(list(tmp_path.glob("camoufox-profile.pre-*"))) == 1


def test_each_profile_checks_only_its_own_browser(tmp_path, monkeypatch):
    """A running desktop-automation Chromium must not block the Camoufox copy, and
    a running Camoufox must not block the Chromium copy."""
    camoufox_profile = tmp_path / "camoufox-profile"
    camoufox_profile.mkdir()
    (camoufox_profile / "prefs.js").write_text("x")
    monkeypatch.setattr(provision, "camoufox_running", lambda: False)
    monkeypatch.setattr(provision, "chromium_running", lambda: True)
    provision.backup_if_upgrading(camoufox_profile, "135.0.1", "156.0.1", "Camoufox")
    assert len(list(tmp_path.glob("camoufox-profile.pre-v135*"))) == 1

    chromium_profile = tmp_path / "browser-profile"
    chromium_profile.mkdir()
    (chromium_profile / "Cookies").write_text("c")
    monkeypatch.setattr(provision, "camoufox_running", lambda: True)
    monkeypatch.setattr(provision, "chromium_running", lambda: False)
    provision.backup_if_upgrading(chromium_profile, "145.0.1", "151.0.1", "Chromium")
    assert len(list(tmp_path.glob("browser-profile.pre-v145*"))) == 1

    monkeypatch.setattr(provision, "chromium_running", lambda: None)
    other = tmp_path / "other" / "browser-profile"
    other.mkdir(parents=True)
    (other / "Cookies").write_text("c")
    with pytest.raises(provision.ProvisionError, match="may be using the Chromium"):
        provision.backup_if_upgrading(other, "145.0.1", "151.0.1", "Chromium")


def test_interrupted_copy_is_not_a_backup(tmp_path):
    src = tmp_path / "camoufox-profile"
    src.mkdir()
    (src / "cookies.sqlite").write_text("c")
    leftover = tmp_path / "camoufox-profile.pre-v135-20200101.tmp"
    leftover.mkdir()
    made = provision.copy_aside(src, "v135")
    assert made is not None and (made / "cookies.sqlite").read_text() == "c"
    assert leftover.is_dir(), "a killed run's temp is disk_hygiene's to remove, not a backup"


def test_a_backup_ages_from_today_not_from_the_profile(tmp_path):
    """copytree keeps the profile's mtime (its last session); a backup of a
    profile unused for a month must not be pruned on its first day."""
    src = tmp_path / "camoufox-profile"
    src.mkdir()
    (src / "prefs.js").write_text("x")
    month_ago = time.time() - 30 * 86400
    os.utime(src, (month_ago, month_ago))
    made = provision.copy_aside(src, "v135")
    assert abs(made.stat().st_mtime - time.time()) < 60


def test_an_unreadable_pin_version_still_backs_up(stack, tmp_path, monkeypatch):
    _install_pinned(stack)
    _firefox_profile(provision.PROFILE_DIR)
    monkeypatch.setattr(
        provision, "camoufox_pin", lambda: engine.CamoufoxPin("dev", "x", "official")
    )
    monkeypatch.setattr(subprocess, "run", FakeRun())
    _tx(tmp_path).run()
    assert len(list(provision.PROFILE_DIR.parent.glob("camoufox-profile.pre-v135*"))) == 1


def test_firefox_profile_version(tmp_path):
    (tmp_path / "compatibility.ini").write_text(
        "[Compatibility]\nLastVersion=135.0.1-beta.24_20250315105650/20250315105650\n"
    )
    assert provision.firefox_profile_version(tmp_path) == "135.0.1-beta.24"
    assert provision.firefox_profile_version(tmp_path / "missing") is None


def test_unknown_version_still_backs_up(tmp_path):
    profile = tmp_path / "camoufox-profile"
    profile.mkdir()
    (profile / "prefs.js").write_text("x")
    provision.backup_if_upgrading(profile, None, "156.0.1", "Camoufox")
    assert len(list(tmp_path.glob("camoufox-profile.pre-vunknown-*"))) == 1


def test_backup_refused_while_its_browser_runs(tmp_path, monkeypatch):
    profile = tmp_path / "camoufox-profile"
    profile.mkdir()
    (profile / "prefs.js").write_text("x")
    monkeypatch.setattr(provision, "camoufox_running", lambda: True)
    with pytest.raises(provision.ProvisionError, match="may be using"):
        provision.backup_if_upgrading(profile, "135.0.1", "156.0.1", "Camoufox")


def test_a_retry_refreshes_the_profile_backup(tmp_path):
    """After a failed upgrade the user kept using the old browser; the backup a
    later rollback needs is the newest one, not the first attempt's."""
    profile = tmp_path / "camoufox-profile"
    profile.mkdir()
    (profile / "cookies.sqlite").write_text("first")
    provision.backup_if_upgrading(profile, "135.0.1", "156.0.1", "Camoufox")
    (profile / "cookies.sqlite").write_text("after the failed attempt")
    provision.backup_if_upgrading(profile, "135.0.1", "156.0.1", "Camoufox")
    backups = list(tmp_path.glob("camoufox-profile.pre-v135*"))
    assert len(backups) == 1
    assert (backups[0] / "cookies.sqlite").read_text() == "after the failed attempt"


def test_failed_copy_leaves_no_temp_and_keeps_the_old_backup(tmp_path, monkeypatch):
    profile = tmp_path / "camoufox-profile"
    profile.mkdir()
    (profile / "prefs.js").write_text("x")
    first = provision.copy_aside(profile, "v135")

    def no_space(src, dst, **kw):
        Path(dst).mkdir()
        (Path(dst) / "half").write_text("x")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(provision.shutil, "copytree", no_space)
    with pytest.raises(OSError):
        provision.copy_aside(profile, "v135")
    assert not list(tmp_path.glob("*.tmp"))
    assert first.is_dir()
    assert len(list(tmp_path.glob("camoufox-profile.pre-v135*"))) == 1


# ── Chromium (non-fatal) ──────────────────────────────────────────────────


def _chromium_profile(last_version: str = "145.0.7632.6") -> Path:
    profile = provision.CHROMIUM_PROFILE_DIR
    profile.mkdir(parents=True)
    (profile / "Last Version").write_text(last_version)
    (profile / "Cookies").write_text("c")
    return profile


def _aged(path: Path, days: float) -> None:
    t = time.time() - days * 86400
    os.utime(path, (t, t))


@pytest.mark.parametrize(
    "last, new, opened_days_ago, expected",
    [
        ("151.0.7700.1", "151.0.7700.1", 20, "ready"),
        # "Last Version" is rewritten on every launch, so its age dates nothing;
        # the backup's own mtime (taken just before the check opens the
        # profile) is what disk_hygiene ages.
        ("151.0.7700.1", "151.0.7700.1", 0, "ready"),
        # Devin red (#2954): after a downgrade the profile records a NEWER
        # Chromium than the installed one, which cannot open it.
        ("151.0.7700.1", "145.0.7632.6", 20, "not_opened_by_the_new_chromium"),
        ("151.0.7700.1", "151.0.7700.9", 20, "not_opened_by_the_new_chromium"),
        ("151.0.7700.1", None, 20, "unknown"),
    ],
)
def test_chromium_backups_are_prunable_only_once_its_build_opened_the_profile(
    monkeypatch, last, new, opened_days_ago, expected
):
    _chromium_profile(last)
    _aged(provision.CHROMIUM_PROFILE_DIR / "Last Version", opened_days_ago)
    monkeypatch.setattr(chromium, "patchright_version", lambda: new)
    assert chromium.backup_state() == expected


def test_chromium_profile_version(tmp_path):
    (tmp_path / "Last Version").write_text("145.0.7632.6\n")
    assert chromium.profile_version(tmp_path) == "145.0.7632.6"
    assert chromium.profile_version(tmp_path / "missing") is None


def test_a_same_major_chromium_build_change_backs_up_the_profile(stack, tmp_path, monkeypatch):
    _install_pinned(stack)
    _chromium_profile("151.0.7700.1")
    monkeypatch.setattr(chromium, "patchright_version", lambda: "151.0.7700.9")
    monkeypatch.setattr(subprocess, "run", FakeRun())
    _tx(tmp_path).run()
    assert len(list(provision.CHROMIUM_PROFILE_DIR.parent.glob("browser-profile.pre-v151*"))) == 1


def test_the_chromium_launch_check_is_the_fallback_launch(stack, tmp_path, monkeypatch):
    """Devin (#2954, #2956): a headless launch on a fresh profile passed where the
    fallback's headed launch of the persistent profile on the VNC display could
    not start; the check now runs the fallback's own launch."""
    from genesis.mcp.health import browser

    assert "launch_persistent_context" in chromium.SMOKE and "headless=False" in chromium.SMOKE
    assert chromium.DISPLAY == browser._VNC_DISPLAY
    assert chromium.PROFILE_DIR.name == browser._CHROMIUM_PROFILE_DIR.name
    _install_pinned(stack)
    run = FakeRun()
    monkeypatch.setattr(subprocess, "run", run)
    _tx(tmp_path).run()
    smokes = [c for c, _ in run.calls if c[1:2] == ["-c"] and "patchright" in c[2]]
    assert smokes and smokes[0][3:] == [str(chromium.PROFILE_DIR)]


def test_chromium_upgrade_backs_up_its_profile(stack, tmp_path, monkeypatch):
    _install_pinned(stack)
    _chromium_profile()
    monkeypatch.setattr(chromium, "patchright_version", lambda: "151.0.7700.1")
    monkeypatch.setattr(subprocess, "run", FakeRun())
    outcome = _tx(tmp_path).run()
    assert len(list(provision.CHROMIUM_PROFILE_DIR.parent.glob("browser-profile.pre-v145*"))) == 1
    assert "chromium=True" in outcome


def test_chromium_backup_refusal_is_non_fatal(stack, tmp_path, monkeypatch, capsys):
    _install_pinned(stack)
    _chromium_profile()
    monkeypatch.setattr(chromium, "patchright_version", lambda: "151.0.7700.1")
    monkeypatch.setattr(provision, "chromium_running", lambda: True)
    monkeypatch.setattr(subprocess, "run", FakeRun())
    outcome = _tx(tmp_path).run()
    assert "FAILED: Chromium profile backup" in capsys.readouterr().out
    assert "engine=True, chromium=False, launch=True" in outcome


def test_chromium_backup_oserror_is_non_fatal(tmp_path, monkeypatch):
    _chromium_profile()
    monkeypatch.setattr(chromium, "patchright_version", lambda: "151.0.7700.1")

    def no_space(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(provision, "copy_aside", no_space)
    monkeypatch.setattr(subprocess, "run", FakeRun())
    assert _tx(tmp_path).chromium() is False


def test_chromium_install_failure_is_non_fatal(stack, tmp_path, monkeypatch, capsys):
    _install_pinned(stack)
    run = FakeRun(fail=[lambda cmd: cmd[1:4] == ["-m", "patchright", "install"]])
    monkeypatch.setattr(subprocess, "run", run)
    outcome = _tx(tmp_path).run()
    assert "patchright install chromium exited 1" in capsys.readouterr().out
    assert "DEGRADED (engine=True, chromium=False, launch=True)" in outcome
    assert run.pip_calls() == [], "a Chromium failure never rolls the packages back"


_NO_LIBS = "chrome: error while loading shared libraries: libnss3.so: cannot open shared object"


def test_chromium_that_does_not_launch_gets_its_system_libraries(stack, tmp_path, monkeypatch):
    _install_pinned(stack)
    launches = iter([False, True])
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[1:2] == ["-c"] and "patchright" in cmd[2]:
            ok = next(launches)
            return SimpleNamespace(returncode=0 if ok else 1, stdout="", stderr=_NO_LIBS)
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
            return SimpleNamespace(returncode=1, stdout="", stderr=_NO_LIBS)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    outcome = _tx(tmp_path).run()
    assert "install-deps chromium" in capsys.readouterr().out
    assert "chromium=False" in outcome


def test_a_signal_during_the_chromium_step_is_reported_not_raised(stack, tmp_path, monkeypatch):
    _install_pinned(stack)
    fake = FakeRun()

    def run(cmd, **kw):
        if cmd[1:4] == ["-m", "patchright", "install"]:
            raise provision.ProvisionError("interrupted by signal SIGTERM")
        return fake(cmd, **kw)

    monkeypatch.setattr(subprocess, "run", run)
    outcome = _tx(tmp_path).run()
    assert "DEGRADED (engine=True, chromium=False, launch=True)" in outcome
    assert fake.pip_calls() == []


def test_no_chromium_step_after_a_camoufox_failure(stack, tmp_path, monkeypatch):
    _install_pinned(stack)
    run = FakeRun(fail=[lambda cmd: cmd[1:2] == ["-c"] and "AsyncCamoufox" in cmd[2]])
    monkeypatch.setattr(subprocess, "run", run)
    outcome = _tx(tmp_path).run()
    assert not [c for c, _ in run.calls if "patchright" in " ".join(c)]
    assert "chromium=False" in outcome


def test_chromium_still_down_after_install_deps_names_the_command(
    stack, tmp_path, monkeypatch, capsys
):
    _install_pinned(stack)

    def run(cmd, **kw):
        if cmd[1:2] == ["-c"] and "patchright" in cmd[2]:
            return SimpleNamespace(returncode=1, stdout="", stderr=_NO_LIBS)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    outcome = _tx(tmp_path).run()
    out = capsys.readouterr().out
    assert "installing its system libraries" in out
    assert "run: sudo" in out and "install-deps chromium" in out
    assert "chromium=False" in outcome


def test_a_missing_display_names_the_display_and_installs_nothing(
    stack, tmp_path, monkeypatch, capsys
):
    """Review finding on this slice: every failed launch ran a root apt
    install-deps, including a missing X display (bootstrap treats a VNC setup
    failure as non-fatal), which no library can fix."""
    _install_pinned(stack)
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[1:2] == ["-c"] and "patchright" in cmd[2]:
            return SimpleNamespace(returncode=1, stdout="", stderr="Missing X server or $DISPLAY")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    outcome = _tx(tmp_path).run()
    assert not [c for c in calls if c[:1] == ["sudo"]]
    assert "X display :99" in capsys.readouterr().out
    assert "chromium=False" in outcome


def test_a_profile_from_a_newer_chromium_is_not_opened(stack, tmp_path, monkeypatch, capsys):
    """Review finding on this slice: after a downgrade the check would open a
    profile a newer Chromium wrote; it is backed up and left alone instead."""
    _install_pinned(stack)
    _chromium_profile("151.0.7700.1")
    monkeypatch.setattr(chromium, "patchright_version", lambda: "145.0.7632.6")
    run = FakeRun()
    monkeypatch.setattr(subprocess, "run", run)
    outcome = _tx(tmp_path).run()
    assert not [c for c, _ in run.calls if c[1:2] == ["-c"] and "patchright" in c[2]]
    assert len(list(provision.CHROMIUM_PROFILE_DIR.parent.glob("browser-profile.pre-v151*"))) == 1
    assert "newer Chromium" in capsys.readouterr().out
    assert "chromium=False" in outcome
