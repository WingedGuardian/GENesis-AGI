"""genesis.browser.provision: the unattended oneshot that installs the Camoufox engine
the installed package pairs with, the Chromium the fallback launches, and their system libraries.

Every external effect goes through a recording fake runner and module seams; nothing
downloads, nothing runs sudo, and the engine cache is a tmp dir.
"""

from __future__ import annotations

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
    monkeypatch.setattr(provision, "_device_free", lambda path: 100 * 1024**3)
    monkeypatch.setattr(provision, "_pin_capable", lambda: True)
    monkeypatch.setattr(provision, "_explicit_choice", lambda: False)
    monkeypatch.setattr(provision, "_live_engine_in", lambda path: False)
    monkeypatch.setattr(provision, "_live_under", lambda directory: False)
    # The fallback's Chromium, complete, where the (faked) dry-run says it is.
    _uses(monkeypatch, tmp_path, package="playwright")
    _layout(tmp_path)
    monkeypatch.setattr(provision, "_removed", [], raising=False)
    monkeypatch.setattr(
        provision, "_remove_version", lambda path: provision._removed.append(path) or True
    )
    return tmp_path


def _resolves(monkeypatch, *statuses):
    seq = list(statuses)

    def resolve():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr(provision.engine, "resolve_camoufox_engine", resolve)


# ── free space ────────────────────────────────────────────────────────────────


def test_low_disk_skips_everything_and_succeeds(env, monkeypatch):
    """The timer retries tomorrow; a full disk is not a failed unit."""
    monkeypatch.setattr(provision, "free_bytes", lambda path: 1 * 1024**3)
    _resolves(monkeypatch, MISSING)
    run = Runner()
    out = provision.provision(run)
    assert out.ok and run.calls == []
    assert "low disk" in out.line()


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "0", "abc", ""])
def test_bad_min_free_setting_falls_back_to_the_default(monkeypatch, value):
    monkeypatch.setenv("GENESIS_BROWSER_MIN_FREE_GB", value)
    assert provision.min_free_bytes() == 4 * 1024**3


def test_min_free_setting_is_honoured(monkeypatch):
    monkeypatch.setenv("GENESIS_BROWSER_MIN_FREE_GB", "2.5")
    assert provision.min_free_bytes() == int(2.5 * 1024**3)


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
    status = engine.EngineStatus(
        engine.INVALID, f"{bad}/camoufox-bin is not executable", bad / "camoufox-bin"
    )
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
    # Nothing camoufox-side runs; the Chromium fallback does not need camoufox.
    assert run.ran("camoufox") == [] and not out.ok
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


def _layout(env, *, chrome=True, shell=True, marked=("chromium", "chromium-headless-shell")):
    """Chromium as `install chromium` leaves it, at the locations the package's
    dry-run names; ``chrome``/``shell`` say whether each executable is there."""
    root = env / "pw"
    dirs = {
        "chromium": root / "chromium-1234",
        "chromium-headless-shell": root / "chromium_headless_shell-1234",
    }
    exes = {
        "chromium": dirs["chromium"] / "chrome-linux64" / "chrome",
        "chromium-headless-shell": dirs["chromium-headless-shell"]
        / "chrome-headless-shell-linux64"
        / "chrome-headless-shell",
    }
    for build, present in (("chromium", chrome), ("chromium-headless-shell", shell)):
        exes[build].parent.mkdir(parents=True, exist_ok=True)
        if present:
            exes[build].write_bytes(b"\x7fELF")
            exes[build].chmod(0o755)
        else:
            exes[build].unlink(missing_ok=True)
        if build in marked:
            (dirs[build] / "INSTALLATION_COMPLETE").write_text("")
    return dirs, exes


class _Installing(Runner):
    """A Runner whose `<package> install chromium` completes the Chromium layout."""

    def __init__(self, env, answers=None):
        super().__init__(answers)
        self.env = env

    def __call__(self, argv, **kw):
        if argv[2:5] == ["install", "chromium"] or argv[3:5] == ["install", "chromium"]:
            _layout(self.env)
        return super().__call__(argv, **kw)


def _uses(monkeypatch, env, package="patchright"):
    """The installed packages, and the locations that package's dry-run names."""
    monkeypatch.setattr(
        provision.chromium.packages,
        "installed_versions",
        lambda: {
            "camoufox": "0.5.8",
            "playwright": "1.62.0",
            "patchright": "1.62.3" if package == "patchright" else None,
        },
    )
    asked = []

    def locations(pkg, *, run=None):
        asked.append(pkg)
        return {
            "chromium": ("1234", env / "pw" / "chromium-1234"),
            "chromium-headless-shell": ("1234", env / "pw" / "chromium_headless_shell-1234"),
        }

    monkeypatch.setattr(provision.chromium, "locations", locations)
    return asked


def test_chromium_is_installed_for_the_package_the_fallback_launches(env, monkeypatch):
    """Without patchright the fallback launches playwright's Chromium, so that is the
    one the stack installs (round-1 P1: it used to skip and report success)."""
    asked = _uses(monkeypatch, env, package="playwright")
    _layout(env, chrome=False)  # due: the headed build lost its executable
    _resolves(monkeypatch, READY)
    run = Runner()

    def install(argv, **kw):
        if argv[1:4] == ["-m", "playwright", "install"]:
            _layout(env)
        return run(argv, **kw)

    out = provision.provision(install)
    assert asked == ["playwright"]
    ((argv, kw),) = run.ran("-m playwright install chromium")
    assert argv == [sys.executable, "-m", "playwright", "install", "chromium"]
    assert kw["env"]["TMPDIR"] == str(env / "bigtmp" / "browser-stack")
    assert "chromium=1234" in out.line() and out.ok


def test_patchright_is_used_whenever_it_is_installed(env, monkeypatch):
    asked = _uses(monkeypatch, env, package="patchright")
    _layout(env, chrome=False)
    _resolves(monkeypatch, READY)
    run = _Installing(env)
    out = provision.provision(run)
    assert asked == ["patchright"] and run.ran("-m patchright install chromium")
    assert out.ok


def test_no_playwright_family_package_skips_chromium(env, monkeypatch):
    monkeypatch.setattr(provision.chromium, "launch_package", lambda: None)
    _resolves(monkeypatch, READY)
    run = Runner()
    out = provision.provision(run)
    assert run.ran("install chromium") == []
    assert "chromium=skipped (no package)" in out.line() and out.ok


def test_both_builds_must_have_their_executable(env, monkeypatch):
    _uses(monkeypatch, env)
    _layout(env, shell=False)  # marked complete, the headless shell's binary gone
    _resolves(monkeypatch, READY)
    out = provision.provision(Runner())
    assert "chromium=missing" in out.line() and not out.ok
    assert any("no executable" in n for n in out.notes)


def test_a_marked_build_without_its_executable_is_force_reinstalled(env, monkeypatch):
    """A plain install skips a directory that has its marker; upstream's --force
    re-downloads it under the installer's own lock."""
    _uses(monkeypatch, env)
    _layout(env, chrome=False)
    _resolves(monkeypatch, READY)
    run = Runner()

    def install(argv, **kw):
        if "--force" in argv:
            _layout(env)
        return run(argv, **kw)

    out = provision.provision(install)
    forced = run.ran("--force")
    assert [a[3:] for a, _ in forced] == [["install", "--force", "--no-shell", "chromium"]]
    assert out.ok


def test_a_broken_headless_shell_is_forced_on_its_own(env, monkeypatch):
    _uses(monkeypatch, env)
    _layout(env, shell=False)
    _resolves(monkeypatch, READY)
    run = Runner()
    provision.provision(run)
    assert [a[3:] for a, _ in run.ran("--force")] == [
        ["install", "--force", "chromium-headless-shell"]
    ]


def test_a_build_a_live_browser_runs_from_is_not_forced(env, monkeypatch):
    dirs, _ = _layout(env, chrome=False)
    _uses(monkeypatch, env)
    monkeypatch.setattr(provision, "_live_under", lambda d: d == dirs["chromium"])
    _resolves(monkeypatch, READY)
    run = Runner()
    out = provision.provision(run)
    assert run.ran("--force") == [] and not out.ok
    assert any("running" in n for n in out.notes)


def test_a_live_chromium_turns_off_browser_gc(env, monkeypatch):
    """Playwright's install deletes revisions it considers unused; one a live browser
    runs from must stay."""
    _uses(monkeypatch, env)
    _layout(env, chrome=False)
    monkeypatch.setattr(provision, "_live_under", lambda d: d == env / "pw")
    _resolves(monkeypatch, READY)
    run = _Installing(env)
    provision.provision(run)
    ((_, kw),) = run.ran("install chromium")
    assert kw["env"]["PLAYWRIGHT_SKIP_BROWSER_GC"] == "1"


def test_no_live_chromium_leaves_browser_gc_on(env, monkeypatch):
    _uses(monkeypatch, env)
    _layout(env, chrome=False)
    _resolves(monkeypatch, READY)
    run = _Installing(env)
    provision.provision(run)
    ((_, kw),) = run.ran("install chromium")
    assert "PLAYWRIGHT_SKIP_BROWSER_GC" not in kw["env"]


def test_an_unknown_chromium_layout_is_not_ok(env, monkeypatch):
    def locations(pkg, *, run=None):
        raise provision.chromium.ChromiumLayoutUnknown("dry-run named no build")

    monkeypatch.setattr(provision.chromium, "locations", locations)
    _resolves(monkeypatch, READY)
    run = Runner()
    out = provision.provision(run)
    assert "chromium=unknown" in out.line() and not out.ok
    assert run.ran("install chromium") == []
    assert any("no build" in n for n in out.notes)


def test_a_corrupt_chromium_executable_is_force_reinstalled_once(env, monkeypatch):
    """ldd cannot read it (a truncated file): marker and file are there, so only
    --force replaces it."""
    _uses(monkeypatch, env)
    _, exes = _layout(env)
    _resolves(monkeypatch, READY)
    run = Runner({f"ldd {exes['chromium']}": (1, "\tnot a dynamic executable\n")})
    out = provision.provision(run)
    assert [a[3:] for a, _ in run.ran("--force")] == [
        ["install", "--force", "--no-shell", "chromium"]
    ]
    assert "libs=error" in out.line() and not out.ok


# ── which processes run from a directory ──────────────────────────────────────


def _proc(tmp_path, monkeypatch, entries):
    """A fake /proc: {pid: (uid, exe or PermissionError, argv0 or OSError)}."""
    root = tmp_path / "proc"
    links = {}
    for pid, (uid, exe, argv0) in entries.items():
        d = root / str(pid)
        d.mkdir(parents=True)
        links[str(d / "exe")] = exe
        if isinstance(argv0, str):
            (d / "cmdline").write_bytes(argv0.encode() + b"\0--flag\0")
        else:  # a cmdline this user may not read
            (d / "cmdline").write_bytes(b"")
            (d / "cmdline").chmod(0)
        os.chmod(d, 0o755)
        monkeypatch.setitem(_UIDS, str(d), uid)

    real_readlink = os.readlink

    def readlink(path, *a, **kw):
        value = links.get(str(path))
        if value is None:
            return real_readlink(path, *a, **kw)
        if isinstance(value, type) and issubclass(value, BaseException):
            raise value(13, "denied")
        return value

    monkeypatch.setattr(provision, "_PROC", root)
    monkeypatch.setattr(provision.os, "readlink", readlink)
    monkeypatch.setattr(provision, "_owner", lambda d: _UIDS[str(d)])
    return root


_UIDS: dict[str, int] = {}


def test_live_under_reads_each_processes_executable(tmp_path, monkeypatch):
    me = os.getuid()
    target = tmp_path / "pw" / "chromium-1234"
    _proc(tmp_path, monkeypatch, {10: (me, str(target / "chrome-linux64" / "chrome"), "chrome")})
    assert provision._live_under(target) is True
    assert provision._live_under(tmp_path / "pw" / "chromium-1100") is False


def test_a_deleted_executable_still_counts(tmp_path, monkeypatch):
    target = tmp_path / "pw" / "chromium-1234"
    exe = f"{target}/chrome-linux64/chrome (deleted)"
    _proc(tmp_path, monkeypatch, {10: (os.getuid(), exe, "x")})
    assert provision._live_under(target) is True


def test_an_unreadable_executable_falls_back_to_its_command_line(tmp_path, monkeypatch):
    """MEASURED: 12 of 108 of this user's processes (systemd, gpg-agent, tmux) deny
    the exe link and allow cmdline; counting them as live would make every check
    true for good."""
    target = tmp_path / "pw" / "chromium-1234"
    me = os.getuid()
    _proc(
        tmp_path,
        monkeypatch,
        {
            10: (me, PermissionError, "/usr/lib/systemd/systemd"),
            11: (me, PermissionError, "tmux"),
        },
    )
    assert provision._live_under(target) is False


def test_a_command_line_under_the_directory_counts(tmp_path, monkeypatch):
    target = tmp_path / "pw" / "chromium-1234"
    _proc(
        tmp_path,
        monkeypatch,
        {10: (os.getuid(), PermissionError, f"{target}/chrome-linux64/chrome")},
    )
    assert provision._live_under(target) is True


def test_a_process_that_reads_as_nothing_counts_as_live(tmp_path, monkeypatch):
    target = tmp_path / "pw" / "chromium-1234"
    _proc(tmp_path, monkeypatch, {10: (os.getuid(), PermissionError, PermissionError)})
    assert provision._live_under(target) is True


def test_other_users_processes_are_not_counted(tmp_path, monkeypatch):
    target = tmp_path / "pw" / "chromium-1234"
    _proc(tmp_path, monkeypatch, {10: (os.getuid() + 1, str(target / "x" / "chrome"), "chrome")})
    assert provision._live_under(target) is False


# ── free space on every destination ───────────────────────────────────────────


def test_every_destination_must_have_room(env, monkeypatch):
    """The engine lands in the camoufox cache and its download in the temp dir;
    either filesystem may be the short one."""
    temp = env / "bigtmp" / "browser-stack"
    monkeypatch.setattr(provision, "disk_device", lambda path: str(path))  # separate filesystems
    monkeypatch.setattr(
        provision, "free_bytes", lambda path: 1 * 1024**3 if Path(path) == temp else 100 * 1024**3
    )
    _resolves(monkeypatch, MISSING)
    run = Runner()
    out = provision.provision(run)
    assert out.ok and run.calls == []
    assert "low disk" in out.line() and str(temp) in out.line()


def test_chromiums_destination_must_have_room(env, monkeypatch):
    _uses(monkeypatch, env)
    _layout(env, chrome=False, marked=("chromium-headless-shell",))  # chromium is due
    monkeypatch.setattr(provision, "disk_device", lambda path: str(path))
    monkeypatch.setattr(
        provision,
        "free_bytes",
        lambda path: 1 * 1024**3 if Path(path) == env / "pw" else 100 * 1024**3,
    )
    _resolves(monkeypatch, READY)
    run = Runner()
    out = provision.provision(run)
    assert run.ran("install chromium") == []
    assert "chromium=skipped (low disk" in out.line()


def test_a_destination_that_cannot_be_measured_skips(env, monkeypatch):
    monkeypatch.setattr(provision, "free_bytes", lambda path: None)
    _resolves(monkeypatch, MISSING)
    run = Runner()
    out = provision.provision(run)
    assert run.calls == [] and "cannot measure" in out.line()


def test_free_bytes_is_the_effective_headroom(monkeypatch, tmp_path):
    monkeypatch.setattr(provision, "effective_free_bytes", lambda path: 42)
    assert provision.free_bytes(tmp_path) == 42


# ── one time budget for the whole run ─────────────────────────────────────────


def test_each_step_gets_what_is_left_of_the_units_budget(env, monkeypatch):
    """TimeoutStartSec covers the whole run, so a slow fetch shortens what the later
    steps may take instead of letting systemd kill the run mid-install."""
    _uses(monkeypatch, env)
    _layout(env, chrome=False)
    clock = iter([0.0, 0.0] + [25000.0] * 6)
    monkeypatch.setattr(provision.time, "monotonic", lambda: next(clock, 25000.0))
    _resolves(monkeypatch, MISSING, READY)
    run = _Installing(env)
    provision.provision(run)
    ((_, fetch),) = run.ran("camoufox fetch")
    ((_, install),) = run.ran("install chromium")
    assert fetch["timeout"] == provision.STEP_TIMEOUT_S
    assert install["timeout"] == provision.RUN_BUDGET_S - 25000


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
    status = engine.EngineStatus(
        engine.INVALID, f"{bad}/camoufox-bin is not executable", bad / "camoufox-bin"
    )
    _resolves(monkeypatch, status)
    run = Runner()
    provision.provision(run)
    assert provision._removed == [] and run.ran("camoufox fetch") == []


def test_chmod_that_cannot_help_stops(env, monkeypatch):
    bad = env / "xdg" / "camoufox" / "browsers" / "official" / "156.0.1-beta.36-abc"
    bad.mkdir(parents=True)
    status = engine.EngineStatus(
        engine.INVALID, f"{bad}/camoufox-bin is not executable", bad / "camoufox-bin"
    )
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
    run = Runner(
        {
            "ldd": (
                0,
                "libxul.so: /lib/x86_64-linux-gnu/libc.so.6: version `GLIBC_2.38' not found (required by libxul.so)\n",
            )
        }
    )
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
    monkeypatch.setattr(provision.chromium, "launch_package", lambda: None)
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


# ── review fixes ──────────────────────────────────────────────────────────────


def test_a_forced_reinstall_that_fails_is_not_reported_ready(env, monkeypatch):
    """Upstream's --force removes the build before it downloads; a failed download
    leaves no build at all (coreBundle.js), and that is not ready."""
    import shutil as _shutil

    _uses(monkeypatch, env)
    dirs, exes = _layout(env)
    _resolves(monkeypatch, READY)
    run = Runner({f"ldd {exes['chromium-headless-shell']}": (1, "\tnot a dynamic executable\n")})

    def install(argv, **kw):
        if "--force" in argv:
            _shutil.rmtree(dirs["chromium-headless-shell"])
            return subprocess.CompletedProcess(argv, 1, "", "download failed")
        return run(argv, **kw)

    out = provision.provision(install)
    assert "chromium=missing" in out.line() and not out.ok


def test_a_forced_reinstall_keeps_browser_gc_off_while_a_browser_runs(env, monkeypatch):
    """Every install, --force included, deletes revisions Playwright thinks unused
    unless told not to."""
    _uses(monkeypatch, env)
    _layout(env, chrome=False)
    monkeypatch.setattr(provision, "_live_under", lambda d: d == env / "pw")
    _resolves(monkeypatch, READY)
    run = Runner()
    provision.provision(run)
    for _, kw in run.ran("install"):
        assert kw["env"].get("PLAYWRIGHT_SKIP_BROWSER_GC") == "1"


def test_a_fetch_needs_room_for_itself_above_the_floor(env, monkeypatch):
    """The engine (about 1.3 GB unpacked) and its download (about as much) land on
    top of the floor; the floor alone would let the fetch take the disk below it."""
    free = 5 * 1024**3  # above the 4 GB floor, short of floor + engine + archive
    monkeypatch.setattr(provision, "free_bytes", lambda path: free)
    _resolves(monkeypatch, MISSING)
    run = Runner()
    out = provision.provision(run)
    assert run.ran("camoufox fetch") == []
    assert "engine=skipped (low disk" in out.line() and out.ok


def test_a_ready_engine_needs_no_room_for_a_fetch(env, monkeypatch):
    monkeypatch.setattr(provision, "free_bytes", lambda path: 5 * 1024**3)
    _resolves(monkeypatch, READY)
    out = provision.provision(Runner())
    assert "engine=Camoufox" in out.line()


def test_ldd_missing_does_not_count_against_chromium(env, monkeypatch):
    _uses(monkeypatch, env)
    _layout(env)
    _resolves(monkeypatch, READY)

    def run(argv, **kw):
        if argv[0] == "ldd":
            raise FileNotFoundError("ldd")
        return subprocess.CompletedProcess(argv, 0, "", "")

    out = provision.provision(run)
    assert "chromium=1234" in out.line()
    assert "libs=error" in out.line() and not out.ok


def test_writes_to_one_filesystem_are_summed(env, monkeypatch):
    """The engine and its archive on the same disk need room for both at once: the
    device's own free space must hold both writes above the floor."""
    monkeypatch.setattr(provision, "disk_device", lambda path: "one-disk")
    floor = provision.min_free_bytes()
    just_one = floor + provision._ENGINE_BYTES + 1
    monkeypatch.setattr(provision, "free_bytes", lambda path: just_one)
    monkeypatch.setattr(provision, "_device_free", lambda path: just_one)
    assert "low disk" in provision._room_to_fetch()
    monkeypatch.setattr(provision, "disk_device", lambda path: str(path))
    assert provision._room_to_fetch() is None


def test_an_installed_chromium_needs_no_room_for_an_install(env, monkeypatch):
    """A re-run with everything in place downloads nothing, so it must not be
    skipped for lack of room a download would need."""
    _uses(monkeypatch, env)
    _layout(env)
    monkeypatch.setattr(provision, "free_bytes", lambda path: provision.min_free_bytes() + 1)
    _resolves(monkeypatch, READY)
    out = provision.provision(Runner())
    assert "chromium=1234" in out.line() and out.ok


# ── review round 2 (#3203) ────────────────────────────────────────────────────


def test_a_binary_ldd_cannot_read_after_install_deps_is_not_ok(env, monkeypatch):
    """An interrupted install-deps can leave the loader unable to inspect a binary;
    the recheck must report that, never libs=ok."""
    status = _engine_on_disk(env)
    _resolves(monkeypatch, status)
    state = {"installed": False}

    def fake(argv, **kw):
        text = " ".join(argv)
        if text.startswith("ldd"):
            if not state["installed"]:
                return subprocess.CompletedProcess(argv, 0, "\tlibgtk-3.so.0 => not found\n", "")
            return subprocess.CompletedProcess(argv, 1, "", "error while loading")
        if "install-deps" in text:
            state["installed"] = True
        return subprocess.CompletedProcess(argv, 0, "", "")

    out = provision.provision(fake)
    assert "libs=error:ldd" in out.line() and not out.ok


def test_a_stale_download_is_cleared_before_the_space_check(env, monkeypatch):
    """A killed run's archive can be what takes the disk below the floor; checking
    first would skip every later run and never reclaim it."""
    temp = env / "bigtmp" / "browser-stack"
    temp.mkdir(parents=True)
    stale = temp / "tmpold"
    stale.write_bytes(b"x")
    old = time.time() - 4 * 3600
    os.utime(stale, (old, old))
    monkeypatch.setattr(
        provision, "free_bytes", lambda path: 1 if stale.exists() else 100 * 1024**3
    )
    _resolves(monkeypatch, READY)
    out = provision.provision(Runner())
    assert not stale.exists()
    assert "low disk" not in out.line()


def test_each_destination_on_one_filesystem_is_measured(env, monkeypatch):
    """btrfs subvolumes share a filesystem but can each carry their own quota: the
    tight one must be measured, not only the first destination on that device."""
    monkeypatch.setattr(provision, "disk_device", lambda path: "one-pool")
    engine_dir = provision.engine.camoufox_install_dir()
    temp = provision._temp_root()
    plenty = 100 * 1024**3
    monkeypatch.setattr(
        provision, "free_bytes", lambda path: 1024**3 if Path(path) == temp else plenty
    )
    short = provision._short_of_space([engine_dir, temp])
    assert short and str(temp) in short


def test_an_unlistable_cache_still_prints_the_outcome(env, monkeypatch, capsys):
    """mark_legacy lists browsers/; a directory that can be statted but not listed
    must not end the run with a traceback before the engine step reports it."""

    def unlistable(root):
        raise PermissionError(13, "Permission denied", str(root / "browsers"))

    monkeypatch.setattr(provision, "mark_legacy", unlistable)
    _resolves(monkeypatch, READY)
    out = provision.provision(Runner())
    assert any("browsers" in n and "Permission denied" in n for n in out.notes)
    assert "engine=" in out.line()


def test_a_destination_the_run_cannot_write_is_reported(env, monkeypatch):
    """The unit allows writes under $HOME only; a configured destination outside it
    (or any read-only one) is named with the fix, not left to fail mid-install."""
    readonly = env / "readonly"
    readonly.mkdir()
    monkeypatch.setattr(provision, "_writable", lambda path: Path(path) != readonly)
    monkeypatch.setattr(provision.engine, "camoufox_install_dir", lambda: readonly)
    _resolves(monkeypatch, MISSING)
    run = Runner()
    out = provision.provision(run)
    assert run.ran("camoufox fetch") == []
    assert "engine=not writable" in out.line()
    assert any(str(readonly) in n for n in out.notes)
    assert not out.ok


def test_a_ready_stack_on_unwritable_destinations_is_still_ok(env, monkeypatch):
    """Writability matters only where something is due: a stack with nothing to
    install reads ready whatever the destinations' modes (found by review)."""
    monkeypatch.setattr(provision, "_writable", lambda path: False)
    status = _engine_on_disk(env)
    _resolves(monkeypatch, status)
    out = provision.provision(Runner())
    assert out.ok, out.line()
    assert "not writable" not in out.line()


def test_a_due_chromium_install_into_an_unwritable_root_is_reported(env, monkeypatch):
    _layout(env, chrome=False)  # the fallback's Chromium lost its executable
    monkeypatch.setattr(provision, "_writable", lambda path: Path(path) != env / "pw")
    _resolves(monkeypatch, READY)
    run = Runner()
    out = provision.provision(run)
    assert run.ran("install") == []
    assert "chromium=not writable" in out.line() and not out.ok


def test_a_temp_dir_that_cannot_be_made_still_prints_the_outcome(env, monkeypatch):
    monkeypatch.setenv("GENESIS_BIG_TMP", str(env / "file-not-dir" / "tmp"))
    (env / "file-not-dir").write_text("a file where a directory should be")
    _resolves(monkeypatch, READY)
    out = provision.provision(Runner())
    assert out.line().startswith("browser-stack: engine=")


def test_a_cache_root_that_cannot_be_made_is_an_outcome_not_a_traceback(env, monkeypatch):
    root = env / "dangling"
    root.symlink_to(env / "nowhere")
    monkeypatch.setattr(provision.engine, "camoufox_install_dir", lambda: root)
    _resolves(monkeypatch, MISSING)
    out = provision.provision(Runner())
    assert not out.ok and any(str(root) in n for n in out.notes)


def test_a_low_disk_skip_keeps_its_notes(env, monkeypatch):
    """The stale-download cleanup can fail in exactly the case the low-disk skip
    then reports; its note must survive the early return."""

    def no_clear():
        raise PermissionError(13, "Permission denied", "stale")

    monkeypatch.setattr(provision, "_clear_stale_temp", no_clear)
    monkeypatch.setattr(provision, "free_bytes", lambda path: 1)
    _resolves(monkeypatch, READY)
    out = provision.provision(Runner())
    assert "low disk" in out.line()
    assert any("could not clear stale downloads" in n for n in out.notes)


# ── review round 3 (#3203) ────────────────────────────────────────────────────


def test_an_engine_under_a_symlinked_repo_dir_is_never_removed(env, monkeypatch):
    """Only the version directory was checked for a link: a symlinked
    browsers/<repo> would send the recursive delete outside the cache."""
    outside = env / "outside" / "156.0.1-beta.36-abc"
    outside.mkdir(parents=True)
    (outside / "keep").write_text("x")
    browsers = env / "xdg" / "camoufox" / "browsers"
    browsers.mkdir(parents=True)
    (browsers / "official").symlink_to(env / "outside")
    bad = browsers / "official" / "156.0.1-beta.36-abc"
    _resolves(monkeypatch, engine.EngineStatus(engine.INVALID, "corrupt", bad / "camoufox-bin"))
    run = Runner()
    out = provision.provision(run)
    assert provision._removed == [] and run.ran("camoufox fetch") == []
    assert (outside / "keep").exists() and not out.ok
    assert any("through a link" in n for n in out.notes)


def test_a_ready_engine_missing_libxul_is_repaired(env, monkeypatch):
    """libxul.so ships inside the engine; install-deps cannot restore it."""
    bad = env / "xdg" / "camoufox" / "browsers" / "official" / "156.0.1-beta.36-abc"
    bad.mkdir(parents=True)
    (bad / "camoufox-bin").write_bytes(b"\x7fELF")
    ready = engine.EngineStatus(engine.READY, "Camoufox", bad / "camoufox-bin")
    _resolves(monkeypatch, ready, READY)
    run = Runner()
    out = provision.provision(run)
    assert provision._removed == [bad] and len(run.ran("camoufox fetch")) == 1
    assert run.ran("install-deps") == []
    assert out.ok


def test_writes_sharing_a_quota_are_summed_against_it(env, monkeypatch):
    """The common btrfs layout: one subvolume, one quota, both writes inside it. The
    quota-aware figure must hold both; the pool's own free space says nothing."""
    monkeypatch.setattr(provision, "disk_device", lambda path: "one-subvolume")
    floor, w = provision.min_free_bytes(), 1024**3
    monkeypatch.setattr(provision, "free_bytes", lambda path: floor + w + 1)
    monkeypatch.setattr(provision, "_device_free", lambda path: 100 * 1024**3)
    assert "low disk" in provision._short_of_space([], [(env / "a", w), (env / "b", w)])


def test_the_device_itself_must_hold_every_write_on_it(env, monkeypatch):
    """Each quota can have room while the pool under them does not."""
    monkeypatch.setattr(provision, "disk_device", lambda path: "one-device")
    floor, w = provision.min_free_bytes(), 1024**3
    monkeypatch.setattr(provision, "free_bytes", lambda path: 100 * 1024**3)
    monkeypatch.setattr(provision, "_device_free", lambda path: floor + w + 1)
    assert "low disk" in provision._short_of_space([], [(env / "a", w), (env / "b", w)])


def test_one_glibc_shortfall_on_two_binaries_is_not_unreadable(env, monkeypatch):
    status = _engine_on_disk(env)
    _resolves(monkeypatch, status)
    glibc = "./x: /lib/libc.so.6: version `GLIBC_2.38' not found (required by ./x)\n"
    out = provision.provision(Runner({"ldd": (1, glibc)}))
    assert "glibc-too-old" in out.line(), out.line()


def test_a_complete_chromium_runs_no_installer(env, monkeypatch):
    """A run with nothing due writes nothing: the installer creates its temp dir."""
    _resolves(monkeypatch, READY)
    run = Runner()
    out = provision.provision(run)
    assert run.ran("install chromium") == [] and out.ok


def test_stale_entries_that_cannot_be_removed_are_named(env, monkeypatch):
    temp = env / "bigtmp" / "browser-stack"
    temp.mkdir(parents=True)
    stale = temp / "tmpold"
    stale.write_bytes(b"x")
    old = time.time() - 4 * 3600
    os.utime(stale, (old, old))
    real = Path.unlink

    def unlink(self, *a, **kw):
        if self == stale:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", unlink)
    _resolves(monkeypatch, READY)
    out = provision.provision(Runner())
    assert any(str(stale) in n for n in out.notes)


def test_two_writes_to_one_path_are_both_counted(env, monkeypatch):
    """PLAYWRIGHT_BROWSERS_PATH can be the download dir itself: the install and its
    archive then land on one path, and both must fit."""
    floor, w = provision.min_free_bytes(), 1024**3
    monkeypatch.setattr(provision, "free_bytes", lambda path: floor + w + 1)
    assert "low disk" in provision._short_of_space([], [(env / "a", w), (env / "a", w)])
    assert provision._short_of_space([], [(env / "a", w)]) is None


def test_an_absurd_free_space_floor_skips_without_a_traceback(env, monkeypatch):
    monkeypatch.setenv("GENESIS_BROWSER_MIN_FREE_GB", "1e300")
    out = provision.provision(Runner())
    assert "low disk" in out.line()


def test_a_failed_addon_or_model_install_is_not_ready(env, monkeypatch):
    """camoufox's launch ensures the fingerprint model before it imports it, so a
    failed install here fails the same way at every launch."""
    _resolves(monkeypatch, READY, READY)
    assert provision.provision(Runner()).ok
    out = provision.provision(Runner({"maybe_download_addons": (1, "")}))
    assert not out.ok and "engine=Camoufox official 156.0.1-beta.36" in out.line()


def test_printed_commands_quote_a_path_with_spaces(env, monkeypatch):
    monkeypatch.setattr(sys, "executable", "/opt/my venv/bin/python")
    monkeypatch.setattr(provision, "_explicit_choice", lambda: True)
    _resolves(monkeypatch, MISSING)
    out = provision.provision(Runner())
    assert "'/opt/my venv/bin/python' -m camoufox set --release" in "\n".join(out.notes)
    monkeypatch.setattr(provision, "_explicit_choice", lambda: False)
    status = _engine_on_disk(env)
    _resolves(monkeypatch, status)
    run = Runner({"ldd": (0, "\tlibgtk-3.so.0 => not found\n"), "sudo -n true": (1, "")})
    out = provision.provision(run)
    assert any(
        "sudo '/opt/my venv/bin/python' -m playwright install-deps chromium firefox" in n
        for n in out.notes
    )
