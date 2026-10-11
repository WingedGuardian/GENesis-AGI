"""genesis.browser.provision: the unattended oneshot that installs the Camoufox engine
the installed package pairs with, patchright's Chromium, and their system libraries.

Every external effect goes through a recording fake runner and module seams; nothing
downloads, nothing runs sudo, and the engine cache is a tmp dir.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from genesis.browser import engine, provision

READY = engine.EngineStatus(
    engine.READY, "Camoufox official 156.0.1-beta.36", Path("/e/camoufox-bin")
)
MISSING = engine.EngineStatus(
    engine.NOT_INSTALLED, "Camoufox official 156.0.1-beta.36 is not installed"
)


class Runner:
    """Records every command; answers from a table keyed on the command's text."""

    def __init__(self, answers=None):
        self.calls: list[tuple[list[str], dict]] = []
        self.answers = answers or {}

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        text = " ".join(argv)
        for key, (rc, out) in self.answers.items():
            if key in text:
                return subprocess.CompletedProcess(argv, rc, out, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def ran(self, fragment: str) -> list[tuple[list[str], dict]]:
        return [(a, kw) for a, kw in self.calls if fragment in " ".join(a)]


@pytest.fixture(autouse=True)
def _no_real_commands(monkeypatch):
    """A fake that fails to take effect must not let a real fetch or sudo run."""

    def refuse(argv, *a, **kw):
        raise AssertionError(f"a test ran a real command: {argv}")

    monkeypatch.setattr(provision.subprocess, "run", refuse)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A cache under tmp_path, plenty of disk, camoufox 0.5.8 with a pin, no
    explicit `camoufox set` choice, patchright absent, no live Chromium."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("GENESIS_BIG_TMP", str(tmp_path / "bigtmp"))
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "gh"))
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("GENESIS_BROWSER_STACK_LOCK_FD", raising=False)
    monkeypatch.delenv("CAMOUFOX_EXECUTABLE_PATH", raising=False)
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    monkeypatch.delenv("GENESIS_BROWSER_MIN_FREE_GB", raising=False)
    monkeypatch.setattr(provision, "free_bytes", lambda path: 100 * 1024**3)
    monkeypatch.setattr(provision, "_pin_capable", lambda: True)
    monkeypatch.setattr(provision, "_explicit_choice", lambda: False)
    monkeypatch.setattr(provision, "_live_engine_in", lambda path: False)
    monkeypatch.setattr(provision, "_live_chromium", lambda: False)
    monkeypatch.setattr(provision, "_removed", [], raising=False)
    monkeypatch.setattr(
        provision, "_remove_version", lambda path: provision._removed.append(path) or True
    )
    monkeypatch.setattr(
        provision.packages,
        "installed_versions",
        lambda: {"camoufox": "0.5.8", "playwright": "1.62.0", "patchright": None},
    )
    return tmp_path


def _resolves(monkeypatch, *statuses):
    seq = list(statuses)

    def resolve():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(provision.engine, "resolve_camoufox_engine", resolve)


# ── free space ────────────────────────────────────────────────────────────────


def test_low_disk_with_nothing_to_install_succeeds(env, monkeypatch):
    """The stack is already ready, so a full disk costs nothing today."""
    monkeypatch.setattr(provision, "free_bytes", lambda path: 1 * 1024**3)
    _resolves(monkeypatch, READY)
    run = Runner()
    out = provision.provision(run)
    assert out.ok and run.calls == []
    assert "low disk" in out.line()


def test_low_disk_while_the_engine_is_missing_fails_visibly(env, monkeypatch):
    """After the browser packages moved to a new pin, a skipped download leaves
    the browser unusable; reporting that as success would hide it every day."""
    monkeypatch.setattr(provision, "free_bytes", lambda path: 1 * 1024**3)
    _resolves(monkeypatch, MISSING)
    run = Runner()
    out = provision.provision(run)
    assert not out.ok and run.calls == []
    assert "low disk" in out.line()


def test_low_disk_while_chromium_is_missing_fails_visibly(env, monkeypatch):
    monkeypatch.setattr(provision, "free_bytes", lambda path: 1 * 1024**3)
    monkeypatch.setattr(
        provision.packages,
        "installed_versions",
        lambda: {"camoufox": "0.5.8", "playwright": "1.62.0", "patchright": "1.62.3"},
    )
    monkeypatch.setattr(provision.chromium, "not_ready", lambda package, **kw: "missing")
    _resolves(monkeypatch, READY)
    assert not provision.provision(Runner()).ok


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "0", "abc", ""])
def test_bad_min_free_setting_falls_back_to_the_default(monkeypatch, value):
    monkeypatch.setenv("GENESIS_BROWSER_MIN_FREE_GB", value)
    assert provision.min_free_bytes() == 4 * 1024**3


def test_min_free_setting_is_honoured(monkeypatch):
    monkeypatch.setenv("GENESIS_BROWSER_MIN_FREE_GB", "2.5")
    assert provision.min_free_bytes() == int(2.5 * 1024**3)


def test_free_bytes_walks_up_to_an_existing_dir(tmp_path):
    assert provision.free_bytes(tmp_path / "not" / "yet") > 0


def test_a_killed_playwright_download_dir_is_cleared(env, monkeypatch):
    temp = env / "bigtmp" / "browser-stack"
    (temp / "playwright-download-abc").mkdir(parents=True)
    (temp / "playwright-download-abc" / "chrome.zip").write_bytes(b"z")
    (temp / "other-dir").mkdir()
    old = time.time() - 4 * 3600
    for d in (temp / "playwright-download-abc", temp / "other-dir"):
        os.utime(d, (old, old))
    _resolves(monkeypatch, READY)
    provision.provision(Runner())
    assert not (temp / "playwright-download-abc").exists() and (temp / "other-dir").exists()


def test_a_killed_runs_download_is_cleared_by_the_next_run(env, monkeypatch):
    """SIGKILL leaves the download's NamedTemporaryFile behind (MEASURED: 303 MB
    after a kill mid-fetch); ~/tmp itself is pruned only after 7 days."""
    temp = env / "bigtmp" / "browser-stack"
    temp.mkdir(parents=True)
    stale, fresh = temp / "tmpold", temp / "tmpnew"
    stale.write_bytes(b"x")
    fresh.write_bytes(b"y")
    old = time.time() - 4 * 3600
    os.utime(stale, (old, old))
    _resolves(monkeypatch, READY)
    provision.provision(Runner())
    assert not stale.exists() and fresh.exists()


# ── the legacy flag ───────────────────────────────────────────────────────────


def test_mark_legacy_flags_a_pre_0_5_root(tmp_path):
    """0.5's fetch deletes a cache that holds a pre-0.5 root and no flag
    (__main__.py:278 in 0.5.8)."""
    root = tmp_path / "camoufox"
    root.mkdir()
    (root / "version.json").write_text("{}")
    assert provision.mark_legacy(root) is True
    assert (root / ".0.5_FLAG").exists()
    assert provision.mark_legacy(root) is False  # already there: untouched


@pytest.mark.parametrize("make", ["none", "empty", "no_root_engine"])
def test_mark_legacy_leaves_other_caches_alone(tmp_path, make):
    root = tmp_path / "camoufox"
    if make != "none":
        root.mkdir()
    if make == "no_root_engine":
        (root / "browsers").mkdir()
    assert provision.mark_legacy(root) is False
    assert not (root / ".0.5_FLAG").exists()


def test_cli_mark_legacy(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    (tmp_path / "camoufox").mkdir()
    (tmp_path / "camoufox" / "version.json").write_text("{}")
    assert provision.main(["mark-legacy"]) == 0
    assert (tmp_path / "camoufox" / ".0.5_FLAG").exists()


# ── the engine ────────────────────────────────────────────────────────────────


def test_ready_engine_is_not_fetched(env, monkeypatch):
    _resolves(monkeypatch, READY)
    run = Runner()
    out = provision.provision(run)
    assert run.ran("camoufox fetch") == []
    assert "engine=Camoufox official 156.0.1-beta.36" in out.line()


def test_missing_engine_is_fetched_with_its_temp_file_off_cc_tmp(env, monkeypatch):
    """The 1.3 GB download is a NamedTemporaryFile in TMPDIR (MEASURED on 0.5.8: it
    landed in TMPDIR); the process's own TMPDIR is never changed."""
    _resolves(monkeypatch, MISSING, READY)
    before = os.environ.get("TMPDIR")
    run = Runner()
    out = provision.provision(run)
    ((argv, kw),) = run.ran("camoufox fetch")
    assert argv[:3] == [sys.executable, "-m", "camoufox"]
    assert kw["env"]["TMPDIR"] == str(env / "bigtmp" / "browser-stack")
    assert kw["stdin"] == subprocess.DEVNULL
    assert os.environ.get("TMPDIR") == before
    assert out.ok and "engine=Camoufox" in out.line()


def test_fetch_is_judged_by_re_resolving_not_its_exit_code(env, monkeypatch):
    """fetch can exit 0 having installed nothing (#2952)."""
    _resolves(monkeypatch, MISSING, MISSING)
    out = provision.provision(Runner())
    assert not out.ok and "engine=missing" in out.line()


def test_corrupt_pinned_dir_is_removed_then_fetched(env, monkeypatch):
    bad = env / "xdg" / "camoufox" / "browsers" / "official" / "156.0.1-beta.36-abc"
    invalid = engine.EngineStatus(engine.INVALID, "corrupt", bad / "camoufox-bin")
    _resolves(monkeypatch, invalid, READY)
    run = Runner()
    out = provision.provision(run)
    assert provision._removed == [bad]
    assert len(run.ran("camoufox fetch")) == 1 and out.ok


@pytest.mark.parametrize(("age_days", "replaced"), [(1, False), (8, True)])
def test_an_engine_fetched_this_week_is_not_downloaded_again(env, monkeypatch, age_days, replaced):
    """The daily timer would otherwise repeat one run's delete-and-fetch every day
    for an engine that installs invalid by Genesis's checks: 1.3 GB a day."""
    bad = env / "xdg" / "camoufox" / "browsers" / "official" / "156.0.1-beta.36-abc"
    bad.mkdir(parents=True)
    (bad / "version.json").write_text("{}")
    then = time.time() - age_days * 86400
    os.utime(bad / "version.json", (then, then))
    invalid = engine.EngineStatus(engine.INVALID, "corrupt", bad / "camoufox-bin")
    _resolves(monkeypatch, invalid, READY)
    run = Runner()
    out = provision.provision(run)
    assert (provision._removed == [bad]) is replaced
    assert len(run.ran("camoufox fetch")) == (1 if replaced else 0)
    if not replaced:
        assert not out.ok and any("remove it by hand" in n for n in out.notes)


def test_a_fresh_fetch_that_stays_invalid_is_not_deleted_again(env, monkeypatch):
    """A validator stricter than a fresh install would otherwise turn every run
    into another delete and 1.3 GB download."""
    bad = env / "xdg" / "camoufox" / "browsers" / "official" / "156.0.1-beta.36-abc"
    invalid = engine.EngineStatus(engine.INVALID, "corrupt", bad / "camoufox-bin")
    _resolves(monkeypatch, invalid, invalid, invalid)
    run = Runner()
    out = provision.provision(run)
    assert len(run.ran("camoufox fetch")) == 1 and provision._removed == [bad]
    assert not out.ok and "engine=invalid (after fetch)" in out.line()


def test_a_killed_fetchs_missing_chmod_is_finished_not_redownloaded(env, monkeypatch):
    """install_versioned writes version.json, then chmods; a kill between leaves
    "not executable", which the skipped chmod repairs."""
    bad = env / "xdg" / "camoufox" / "browsers" / "official" / "156.0.1-beta.36-abc"
    (bad / "sub").mkdir(parents=True)
    (bad / "camoufox-bin").write_bytes(b"x")
    (bad / "camoufox-bin").chmod(0o644)
    status = engine.EngineStatus(engine.INVALID, f"{bad}/camoufox-bin is not executable", bad / "camoufox-bin")
    _resolves(monkeypatch, status, READY)
    run = Runner()
    out = provision.provision(run)
    assert out.ok and run.ran("camoufox fetch") == [] and provision._removed == []
    assert os.access(bad / "camoufox-bin", os.X_OK)


def test_an_override_under_browsers_is_never_removed(env, monkeypatch):
    """CAMOUFOX_EXECUTABLE_PATH names the operator's own engine: theirs to fix."""
    bad = env / "xdg" / "camoufox" / "browsers" / "official" / "150.0-user.choice"
    monkeypatch.setenv("CAMOUFOX_EXECUTABLE_PATH", str(bad / "camoufox-bin"))
    _resolves(monkeypatch, engine.EngineStatus(engine.INVALID, "bad", bad / "camoufox-bin"))
    run = Runner()
    out = provision.provision(run)
    assert provision._removed == [] and run.ran("camoufox fetch") == []
    assert not out.ok


def test_a_dir_with_a_live_engine_is_never_removed(env, monkeypatch):
    bad = env / "xdg" / "camoufox" / "browsers" / "official" / "156.0.1-beta.36-abc"
    invalid = engine.EngineStatus(engine.INVALID, "corrupt", bad / "camoufox-bin")
    _resolves(monkeypatch, invalid)
    monkeypatch.setattr(provision, "_live_engine_in", lambda path: True)
    run = Runner()
    out = provision.provision(run)
    assert provision._removed == [] and run.ran("camoufox fetch") == []
    assert not out.ok


@pytest.mark.parametrize(
    "path",
    [
        None,  # a package fault: reinstalling the package is the repair
        Path("/opt/custom/camoufox-bin"),  # CAMOUFOX_EXECUTABLE_PATH
        Path("/x/xdg/camoufox/camoufox-bin"),  # the legacy root
    ],
)
def test_other_invalid_states_are_never_fetched(env, monkeypatch, path):
    _resolves(monkeypatch, engine.EngineStatus(engine.INVALID, "bad", path))
    run = Runner()
    out = provision.provision(run)
    assert run.ran("camoufox fetch") == [] and provision._removed == []
    assert not out.ok and "engine=invalid" in out.line()


def test_camoufox_older_than_the_pin_is_never_fetched(env, monkeypatch):
    """camoufox 0.4's fetch empties the whole cache before installing
    (pkgman.cleanup, MEASURED in 0.4.11), and 0.5.3-0.5.6 have no pin to fetch."""
    monkeypatch.setattr(provision, "_pin_capable", lambda: False)
    _resolves(monkeypatch, MISSING)
    run = Runner()
    out = provision.provision(run)
    assert run.ran("camoufox fetch") == []
    assert out.ok and "engine=skipped" in out.line()


def test_an_explicit_camoufox_set_choice_is_not_fetched_over(env, monkeypatch):
    """A bare fetch would install the user's choice, never the pin."""
    monkeypatch.setattr(provision, "_explicit_choice", lambda: True)
    _resolves(monkeypatch, MISSING)
    run = Runner()
    out = provision.provision(run)
    assert run.ran("camoufox fetch") == []
    assert not out.ok and "engine=user-selected" in out.line()
    assert "camoufox set --release" in "\n".join(out.notes)


@pytest.mark.parametrize("kind", [engine.NO_PACKAGE, engine.PLAYWRIGHT_MISSING])
def test_no_browser_package_is_reported(env, monkeypatch, kind):
    _resolves(monkeypatch, engine.EngineStatus(kind, "no package"))
    run = Runner()
    out = provision.provision(run)
    assert run.calls == [] and not out.ok
    assert "engine=no_package" in out.line()


def test_the_flag_is_set_before_any_fetch(env, monkeypatch):
    root = env / "xdg" / "camoufox"
    root.mkdir(parents=True)
    (root / "version.json").write_text("{}")
    seen = []
    _resolves(monkeypatch, MISSING, READY)
    run = Runner()
    real = run.__call__

    def spy(argv, **kw):
        seen.append(((root / ".0.5_FLAG").exists(), " ".join(argv)))
        return real(argv, **kw)

    provision.provision(spy)
    assert all(flag for flag, cmd in seen if "camoufox fetch" in cmd)


# ── Chromium ──────────────────────────────────────────────────────────────────


def _patchright(env, monkeypatch, *, installed=True):
    pkg = env / "site" / "patchright"
    (pkg / "driver" / "package").mkdir(parents=True)
    (pkg / "driver" / "package" / "browsers.json").write_text(
        json.dumps(
            {
                "browsers": [
                    {"name": "chromium", "revision": "1194"},
                    {"name": "chromium-headless-shell", "revision": "1194"},
                    {"name": "firefox", "revision": "1490"},
                ]
            }
        )
    )
    monkeypatch.setattr(
        provision.chromium, "package_dir", lambda name: pkg if name == "patchright" else None
    )
    monkeypatch.setattr(
        provision.packages,
        "installed_versions",
        lambda: {
            "camoufox": "0.5.8",
            "playwright": "1.62.0",
            "patchright": "1.62.3" if installed else None,
        },
    )
    return env / "xdg" / "ms-playwright"


def _complete(browsers: Path, *names):
    for name in names:
        (browsers / name).mkdir(parents=True, exist_ok=True)
        (browsers / name / "INSTALLATION_COMPLETE").write_text("")


def test_chromium_is_skipped_without_patchright(env, monkeypatch):
    _resolves(monkeypatch, READY)
    run = Runner()
    out = provision.provision(run)
    assert run.ran("patchright") == []
    assert "chromium=skipped" in out.line() and out.ok


def test_chromium_is_installed_and_confirmed(env, monkeypatch):
    browsers = _patchright(env, monkeypatch)
    _resolves(monkeypatch, READY)
    run = Runner()

    def install(argv, **kw):
        if "patchright install chromium" in " ".join(argv):
            _complete(browsers, "chromium-1194", "chromium_headless_shell-1194")
        return Runner.__call__(run, argv, **kw)

    out = provision.provision(install)
    ((argv, kw),) = run.ran("patchright install chromium")
    assert argv[:3] == [sys.executable, "-m", "patchright"]
    assert kw["env"]["TMPDIR"] == str(env / "bigtmp" / "browser-stack")
    assert "PLAYWRIGHT_SKIP_BROWSER_GC" not in kw["env"]
    assert "chromium=1194" in out.line() and out.ok


def test_a_live_chromium_turns_off_browser_gc(env, monkeypatch):
    """Playwright's install deletes revisions it considers unused; one a live browser
    runs from must stay."""
    browsers = _patchright(env, monkeypatch)
    _complete(browsers, "chromium-1194", "chromium_headless_shell-1194")
    monkeypatch.setattr(provision, "_live_chromium", lambda: True)
    _resolves(monkeypatch, READY)
    run = Runner()
    provision.provision(run)
    ((_, kw),) = run.ran("patchright install chromium")
    assert kw["env"]["PLAYWRIGHT_SKIP_BROWSER_GC"] == "1"


def test_an_unfinished_chromium_install_is_missing(env, monkeypatch):
    browsers = _patchright(env, monkeypatch)
    _complete(browsers, "chromium-1194")  # the headless shell never finished
    _resolves(monkeypatch, READY)
    out = provision.provision(Runner())
    assert "chromium=missing" in out.line() and not out.ok


# ── system libraries ──────────────────────────────────────────────────────────


def _engine_on_disk(env):
    d = env / "engine"
    d.mkdir()
    for name in ("camoufox-bin", "libxul.so"):
        (d / name).write_bytes(b"\x7fELF")
    return engine.EngineStatus(engine.READY, "Camoufox", d / "camoufox-bin")


def test_libraries_are_checked_on_the_engine_and_libxul(env, monkeypatch):
    """camoufox-bin links only libc-family libraries; libxul.so needs GTK and X11
    (MEASURED with ldd), so both are checked."""
    status = _engine_on_disk(env)
    _resolves(monkeypatch, status)
    run = Runner()
    out = provision.provision(run)
    checked = [a[-1] for a, _ in run.ran("ldd")]
    assert str(status.path) in checked and str(status.path.parent / "libxul.so") in checked
    assert "libs=ok" in out.line()


def test_mark_legacy_flags_a_cache_with_engines_but_no_flag(tmp_path):
    """camoufox's installer touches the flag last; a run killed before that leaves
    engines and no flag, and the next 0.5 fetch would delete them all."""
    root = tmp_path / "camoufox"
    (root / "browsers" / "official" / "156.0.1-beta.36-abc").mkdir(parents=True)
    assert provision.mark_legacy(root) is True and (root / ".0.5_FLAG").exists()


def test_a_symlinked_version_dir_is_never_changed(env, monkeypatch, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    browsers = env / "xdg" / "camoufox" / "browsers" / "official"
    browsers.mkdir(parents=True)
    (browsers / "156.0.1-beta.36-abc").symlink_to(outside)
    bad = browsers / "156.0.1-beta.36-abc"
    status = engine.EngineStatus(engine.INVALID, f"{bad}/camoufox-bin is not executable", bad / "camoufox-bin")
    _resolves(monkeypatch, status)
    run = Runner()
    provision.provision(run)
    assert provision._removed == [] and run.ran("camoufox fetch") == []


def test_chmod_that_cannot_help_stops(env, monkeypatch):
    bad = env / "xdg" / "camoufox" / "browsers" / "official" / "156.0.1-beta.36-abc"
    bad.mkdir(parents=True)
    status = engine.EngineStatus(engine.INVALID, f"{bad}/camoufox-bin is not executable", bad / "camoufox-bin")
    _resolves(monkeypatch, status, status, status)
    run = Runner()
    out = provision.provision(run)
    assert run.ran("camoufox fetch") == [] and provision._removed == []
    assert any("noexec" in n for n in out.notes)


def test_a_failing_extras_step_keeps_the_engine_result(env, monkeypatch):
    _resolves(monkeypatch, READY)

    def run(argv, **kw):
        if "maybe_download_addons" in " ".join(argv):
            raise subprocess.TimeoutExpired(argv, 1)
        return subprocess.CompletedProcess(argv, 0, "", "")

    out = provision.provision(run)
    assert "engine=Camoufox official 156.0.1-beta.36" in out.line()


def test_a_too_old_glibc_is_not_sent_to_install_deps(env, monkeypatch):
    status = _engine_on_disk(env)
    _resolves(monkeypatch, status)
    run = Runner({"ldd": (0, "libxul.so: /lib/x86_64-linux-gnu/libc.so.6: version `GLIBC_2.38' not found (required by libxul.so)\n")})
    out = provision.provision(run)
    assert "libs=glibc-too-old:" in out.line() and run.ran("install-deps") == []


def test_a_ready_engine_gets_camoufoxs_own_addon_and_model_installers(env, monkeypatch):
    """fetch runs them; a launch otherwise downloads them inside the tool call."""
    _resolves(monkeypatch, READY)
    run = Runner()
    provision.provision(run)
    ((argv, kw),) = run.ran("maybe_download_addons")
    assert "ensure_fpgen_model" in argv[-1] and kw["env"]["TMPDIR"].endswith("browser-stack")


def test_no_binary_to_check_is_unchecked_not_ok(env, monkeypatch):
    monkeypatch.setattr(provision, "_pin_capable", lambda: False)
    _resolves(monkeypatch, MISSING)
    out = provision.provision(Runner())
    assert "libs=unchecked" in out.line()


def test_no_sudo_binary_prints_the_command(env, monkeypatch):
    status = _engine_on_disk(env)
    _resolves(monkeypatch, status)

    def run(argv, **kw):
        if argv[0] == "sudo":
            raise FileNotFoundError("sudo")
        out = "\tlibgtk-3.so.0 => not found\n" if argv[0] == "ldd" else ""
        return subprocess.CompletedProcess(argv, 0, out, "")

    out = provision.provision(run)
    assert "libs=missing:libgtk-3.so.0" in out.line()
    assert any("install-deps" in n for n in out.notes)


def test_a_failing_step_still_prints_the_outcome(env, monkeypatch):
    def boom():
        raise OSError("disk went away")

    monkeypatch.setattr(provision.engine, "resolve_camoufox_engine", boom)
    out = provision.provision(Runner())
    assert "engine=error:OSError" in out.line() and not out.ok


def test_ldd_sees_the_libraries_a_browser_bundles(env, monkeypatch):
    """Firefox ships libmozgtk.so, libmozsandbox.so and others beside libxul.so and
    its launcher puts that directory on the library path; a bare ldd reports them
    not found (MEASURED on the real 156.0.1-beta.36 engine: 6 false misses, 0 with
    the engine dir on LD_LIBRARY_PATH)."""
    status = _engine_on_disk(env)
    _resolves(monkeypatch, status)
    run = Runner()
    provision.provision(run)
    for argv, kw in run.ran("ldd"):
        assert kw["env"]["LD_LIBRARY_PATH"].split(":")[0] == str(Path(argv[-1]).parent)


def test_an_unreadable_camoufox_config_is_treated_as_a_choice(env, monkeypatch):
    """Unknown whether the user ran `camoufox set`: a fetch could install their
    choice instead of the pin, so it is not run."""
    monkeypatch.setattr(provision, "_explicit_choice", lambda: None)
    _resolves(monkeypatch, MISSING)
    run = Runner()
    out = provision.provision(run)
    assert run.ran("camoufox fetch") == [] and "engine=user-selected" in out.line()


def test_missing_libraries_are_installed_through_noninteractive_sudo(env, monkeypatch):
    status = _engine_on_disk(env)
    _resolves(monkeypatch, status)
    state = {"installed": False}
    run = Runner()

    def fake(argv, **kw):
        text = " ".join(argv)
        run.calls.append((list(argv), kw))
        if text.startswith("ldd") and not state["installed"]:
            return subprocess.CompletedProcess(argv, 0, "\tlibgtk-3.so.0 => not found\n", "")
        if "install-deps" in text:
            state["installed"] = True
        return subprocess.CompletedProcess(argv, 0, "", "")

    out = provision.provision(fake)
    ((argv, _),) = run.ran("install-deps")
    assert argv[:2] == ["sudo", "-n"]
    assert argv[2:] == [sys.executable, "-m", "playwright", "install-deps", "chromium", "firefox"]
    assert "libs=ok" in out.line() and out.ok


def test_without_sudo_the_exact_command_is_printed(env, monkeypatch):
    status = _engine_on_disk(env)
    _resolves(monkeypatch, status)
    run = Runner(
        {
            "ldd": (0, "\tlibgtk-3.so.0 => not found\n\tlibX11-xcb.so.1 => not found\n"),
            "sudo -n true": (1, ""),
        }
    )
    out = provision.provision(run)
    assert run.ran("install-deps") == []
    assert "libs=missing:libX11-xcb.so.1,libgtk-3.so.0" in out.line()
    assert any(
        f"sudo {sys.executable} -m playwright install-deps chromium firefox" in n for n in out.notes
    )
    assert not out.ok


# ── the outcome ───────────────────────────────────────────────────────────────


def test_main_prints_one_outcome_line_and_exits_by_it(env, monkeypatch, capsys):
    _resolves(monkeypatch, READY)
    monkeypatch.setattr(provision.subprocess, "run", Runner())
    assert provision.main(["run"]) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("browser-stack:")]
    assert (
        len(lines) == 1
        and "engine=" in lines[0]
        and "chromium=" in lines[0]
        and "libs=" in lines[0]
    )


def test_check_packages_passes_with_the_whole_extra(env, monkeypatch, capsys):
    monkeypatch.setattr(
        provision.packages,
        "installed_versions",
        lambda: {"camoufox": "0.5.8", "playwright": "1.62.0", "patchright": "1.62.3"},
    )
    assert provision.main(["check-packages"]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("versions", "pin_capable", "expected"),
    [
        ({"camoufox": None, "playwright": "1.62.0", "patchright": "1.62.3"}, False, "camoufox"),
        ({"camoufox": "0.4.11", "playwright": "1.62.0", "patchright": "1.62.3"}, False, "0.5.7"),
        ({"camoufox": "0.5.8", "playwright": None, "patchright": "1.62.3"}, True, "playwright"),
        ({"camoufox": "0.5.8", "playwright": "1.62.0", "patchright": None}, True, "patchright"),
    ],
)
def test_check_packages_names_what_the_browser_extra_left_out(
    env, monkeypatch, capsys, versions, pin_capable, expected
):
    """pip's exit status is masked in venv_setup.sh, and a failed resolution leaves
    the old packages importable; bootstrap and install.sh read this instead."""
    monkeypatch.setattr(provision.packages, "installed_versions", lambda: versions)
    monkeypatch.setattr(provision, "_pin_capable", lambda: pin_capable)
    monkeypatch.setenv("CLAUDECODE", "1")  # read-only: allowed inside a session
    assert provision.main(["check-packages"]) == 1
    out = capsys.readouterr().out
    assert expected in out and out.startswith("browser-stack:")


_REQUIRES = [
    "aiosqlite",
    'tomli>=2; python_version < "3.13"',
    'camoufox<0.6,>=0.5.7; extra == "browser"',
    'playwright<1.63,>=1.62; extra == "browser"',
    'patchright<1.63,>=1.62.1; extra == "browser"',
    'openai>=2; extra == "voice"',
]


def _metadata(monkeypatch, requires=_REQUIRES):
    monkeypatch.setattr(
        provision.metadata, "packages_distributions", lambda: {"genesis": ["genesis-v3"]}
    )
    monkeypatch.setattr(
        provision.metadata, "requires", lambda dist: requires if dist == "genesis-v3" else None
    )


def test_check_packages_reads_the_extra_from_the_installed_metadata(env, monkeypatch, capsys):
    """A failed or offline pip run can leave a playwright outside the pinned minor,
    which breaks the one-Chromium-build pairing the pins exist for."""
    _metadata(monkeypatch)
    monkeypatch.setattr(
        provision.packages,
        "installed_versions",
        lambda: {"camoufox": "0.5.8", "playwright": "1.58.0", "patchright": "1.62.3"},
    )
    assert provision.main(["check-packages"]) == 1
    out = capsys.readouterr().out
    assert "playwright 1.58.0" in out and "1.63" in out
    assert "camoufox" not in out and "patchright" not in out


def test_browser_requirements_are_exactly_the_extra(monkeypatch):
    """A core requirement whose marker holds anyway (python_version) is not one."""
    _metadata(monkeypatch)
    names = sorted(r.name for r in provision._browser_requirements())
    assert names == ["camoufox", "patchright", "playwright"]


def test_check_packages_ignores_other_groups_and_core_markers(env, monkeypatch, capsys):
    _metadata(monkeypatch)
    monkeypatch.setattr(
        provision.packages,
        "installed_versions",
        lambda: {"camoufox": "0.5.8", "playwright": "1.62.0", "patchright": "1.62.3"},
    )
    assert provision.main(["check-packages"]) == 0
    assert capsys.readouterr().out == ""


def test_main_refuses_inside_a_claude_code_session(env, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    assert provision.main(["run"]) == 2


def test_main_rejects_unknown_arguments(env):
    with pytest.raises(SystemExit):
        provision.main(["run", "--dry-run"])


def test_main_exits_0_while_another_run_holds_the_lock(env, monkeypatch, capsys):
    import fcntl

    lock = env / "gh" / "locks" / "browser-stack.lock"
    lock.parent.mkdir(parents=True)
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert provision.main(["run"]) == 0
    assert "another run is in progress" in capsys.readouterr().out


def test_an_inherited_lock_descriptor_is_verified_not_trusted(env, monkeypatch):
    import fcntl

    lock = env / "gh" / "locks" / "browser-stack.lock"
    lock.parent.mkdir(parents=True)
    other = env / "unrelated"
    other.write_text("")
    with open(lock, "w") as held, open(other) as wrong:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monkeypatch.setenv("GENESIS_BROWSER_STACK_LOCK_FD", str(held.fileno()))
        assert provision._hold_lock() is True  # the script's own descriptor
        monkeypatch.setenv("GENESIS_BROWSER_STACK_LOCK_FD", str(wrong.fileno()))
        assert provision._hold_lock() is False  # a descriptor for another file proves nothing


def test_main_exits_nonzero_when_not_ok(env, monkeypatch):
    _resolves(monkeypatch, MISSING, MISSING)
    monkeypatch.setattr(provision.subprocess, "run", Runner())
    assert provision.main(["run"]) == 1


def test_check_packages_without_packaging_checks_presence_and_notes_it(env, monkeypatch, capsys):
    """Not being able to read the version ranges is not a range violation: a venv
    without `packaging` must not report a correctly installed extra as incomplete."""
    monkeypatch.setattr(
        provision.packages,
        "installed_versions",
        lambda: {"camoufox": "0.5.8", "playwright": "1.62.0", "patchright": "1.62.3"},
    )

    def no_packaging():
        raise ModuleNotFoundError("No module named 'packaging'")

    monkeypatch.setattr(provision, "_browser_requirements", no_packaging)
    assert provision.main(["check-packages"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "" and "version ranges" in captured.err
