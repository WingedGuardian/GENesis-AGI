"""camoufox_engine_status: read-only engine readiness across both camoufox layouts.

The check exists because camoufox 0.5's own path lookup deletes a pre-0.5 install
directory and downloads from inside the caller. These tests pin that the status
never reports READY for a layout camoufox 0.5 would wipe, and never writes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from genesis.browser import engine
from genesis.browser.engine import camoufox_engine_status


def _pkg(tmp_path: Path, pin: dict | None, *, multiversion: bool | None = None) -> Path:
    """A camoufox package directory. Every 0.5 release ships multiversion.py and
    no 0.4 release does; ``multiversion`` defaults to "a pin was given", the
    0.4 / 0.5.7+ shapes. Pass it explicitly for a pinless 0.5 (0.5.3 to 0.5.6)."""
    pkg = tmp_path / "site" / "camoufox"
    pkg.mkdir(parents=True)
    if pin is not None:
        (pkg / "browser-pin.json").write_text(json.dumps(pin))
    if multiversion if multiversion is not None else pin is not None:
        (pkg / "multiversion.py").write_text("")
    return pkg


def _engine(dirpath: Path, version: str, build: str) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    (dirpath / "version.json").write_text(json.dumps({"version": version, "release": build}))
    exe = dirpath / "camoufox-bin"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return dirpath


PIN = {
    "tag": "v156.0.1-beta.34",
    "repo": "daijro/camoufox",
    "repo_name": "Official",
    "version": "156.0.1",
    "build": "beta.34",
}


@pytest.fixture(autouse=True)
def _no_playwright_floor(monkeypatch):
    """The supported range must not depend on whichever playwright this machine
    has; tests that exercise the floor set it themselves."""
    monkeypatch.setattr(engine, "_playwright_build_floor", lambda *_: None)


def _flagged_root(tmp_path: Path) -> Path:
    root = tmp_path / "cache"
    root.mkdir()
    (root / ".0.5_FLAG").touch()
    return root


def _snapshot(root: Path) -> list[tuple[str, float]]:
    return sorted((str(p), p.stat().st_mtime) for p in root.rglob("*"))


def test_no_package(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "_package_dir", lambda: None)
    status = camoufox_engine_status(install_dir=tmp_path / "cache")
    assert status.state == engine.NO_PACKAGE
    assert not status.ready


def test_pre05_package_with_root_engine_is_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "_playwright_build_floor", lambda *_: None)
    pkg = _pkg(tmp_path, pin=None)
    root = _engine(tmp_path / "cache", "135.0.1", "beta.24")
    status = camoufox_engine_status(install_dir=root, package_dir=pkg)
    assert status.ready
    assert status.path == root


def test_pre05_package_without_engine(tmp_path):
    pkg = _pkg(tmp_path, pin=None)
    status = camoufox_engine_status(install_dir=tmp_path / "cache", package_dir=pkg)
    assert status.state == engine.NOT_INSTALLED


def test_05_package_over_legacy_layout_is_not_ready(tmp_path):
    """The hazard: camoufox 0.5 would rmtree this directory on the next launch."""
    pkg = _pkg(tmp_path, pin=PIN)
    root = _engine(tmp_path / "cache", "135.0.1", "beta.24")
    before = _snapshot(root)
    status = camoufox_engine_status(install_dir=root, package_dir=pkg)
    assert status.state == engine.LEGACY_LAYOUT
    assert not status.ready
    assert engine.PROVISION_HINT in status.detail
    assert _snapshot(root) == before, "the status check must not modify the install"


def test_05_package_with_pinned_engine_is_ready(tmp_path):
    pkg = _pkg(tmp_path, pin=PIN)
    root = tmp_path / "cache"
    root.mkdir()
    (root / ".0.5_FLAG").touch()
    want = _engine(
        root / "browsers" / "official" / "156.0.1-beta.34-abcd1234", "156.0.1", "beta.34"
    )
    status = camoufox_engine_status(install_dir=root, package_dir=pkg)
    assert status.ready
    assert status.path == want


def test_05_package_with_only_other_builds(tmp_path):
    pkg = _pkg(tmp_path, pin=PIN)
    root = tmp_path / "cache"
    root.mkdir()
    (root / ".0.5_FLAG").touch()
    _engine(root / "browsers" / "official" / "152.0.4-beta.27", "152.0.4", "beta.27")
    status = camoufox_engine_status(install_dir=root, package_dir=pkg)
    assert status.state == engine.PIN_NOT_INSTALLED
    assert "156.0.1-beta.34" in status.detail and "152.0.4-beta.27" in status.detail


def test_05_package_empty_install_dir(tmp_path):
    pkg = _pkg(tmp_path, pin=PIN)
    status = camoufox_engine_status(install_dir=tmp_path / "missing", package_dir=pkg)
    assert status.state == engine.PIN_NOT_INSTALLED
    assert not status.ready


def test_unpinned_dev_copy_uses_any_engine(tmp_path):
    pkg = _pkg(tmp_path, pin={})
    root = tmp_path / "cache"
    root.mkdir()
    (root / ".0.5_FLAG").touch()
    _engine(root / "browsers" / "official" / "156.0.1-beta.33", "156.0.1", "beta.33")
    assert camoufox_engine_status(install_dir=root, package_dir=pkg).ready


def test_unreadable_install_never_raises(tmp_path, monkeypatch):
    pkg = _pkg(tmp_path, pin=PIN)

    def boom(*_a, **_k):
        raise PermissionError("denied")

    monkeypatch.setattr(engine, "_installed_engines", boom)
    root = tmp_path / "cache"
    root.mkdir()
    (root / ".0.5_FLAG").touch()
    status = camoufox_engine_status(install_dir=root, package_dir=pkg)
    assert not status.ready
    assert "denied" in status.detail


def test_install_dir_honours_xdg_cache_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert engine.camoufox_install_dir() == tmp_path / "camoufox"
    # platformdirs (camoufox's resolver) uses a relative value as given; so must we.
    monkeypatch.setenv("XDG_CACHE_HOME", "relative/path")
    assert engine.camoufox_install_dir() == Path("relative/path") / "camoufox"
    monkeypatch.setenv("XDG_CACHE_HOME", "  ")
    assert engine.camoufox_install_dir() == Path.home() / ".cache" / "camoufox"


def test_install_dir_matches_camoufox_without_platformdirs(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_platformdirs(name, *a, **k):
        if name == "platformdirs":
            raise ImportError(name)
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_platformdirs)
    monkeypatch.setenv("XDG_CACHE_HOME", "relative/path")
    assert engine.camoufox_install_dir() == Path("relative/path") / "camoufox"
    monkeypatch.delenv("XDG_CACHE_HOME")
    assert engine.camoufox_install_dir() == Path.home() / ".cache" / "camoufox"


def test_engine_without_its_binary_is_not_installed(tmp_path, monkeypatch):
    """A truncated extraction can leave version.json without camoufox-bin."""
    pkg = _pkg(tmp_path, pin=PIN)
    monkeypatch.setattr(engine, "_package_dir", lambda: pkg)
    root = tmp_path / "cache"
    root.mkdir()
    (root / ".0.5_FLAG").touch()
    d = _engine(root / "browsers" / "official" / "156.0.1-beta.34", "156.0.1", "beta.34")
    (d / "camoufox-bin").unlink()
    assert camoufox_engine_status(install_dir=root).state == engine.PIN_NOT_INSTALLED


def test_set_override_is_not_ready(tmp_path, monkeypatch):
    """`camoufox set` makes camoufox launch and fetch another build."""
    pkg = _pkg(tmp_path, pin=PIN)
    monkeypatch.setattr(engine, "_package_dir", lambda: pkg)
    root = tmp_path / "cache"
    root.mkdir()
    (root / ".0.5_FLAG").touch()
    _engine(root / "browsers" / "official" / "156.0.1-beta.34", "156.0.1", "beta.34")
    (root / "config.json").write_text(json.dumps({"channel": "official/prerelease"}))
    status = camoufox_engine_status(install_dir=root)
    assert status.state == engine.OVERRIDDEN and "official/prerelease" in status.detail


def test_pre05_engine_below_playwright_floor_is_not_ready(tmp_path, monkeypatch):
    """playwright can reach 1.62 while camoufox 0.4 and its beta.24 engine remain
    (a hand install, or a failed run whose package restore also failed);
    camoufox's own floor table says playwright >= 1.61 needs beta.30+, so that
    engine cannot launch."""
    monkeypatch.setattr(engine, "_playwright_build_floor", lambda *_: "beta.30")
    pkg = _pkg(tmp_path, pin=None)
    root = _engine(tmp_path / "cache", "135.0.1", "beta.24")
    status = camoufox_engine_status(install_dir=root, package_dir=pkg)
    assert not status.ready
    assert "beta.30" in status.detail


def test_floor_from_installed_playwright(monkeypatch):
    monkeypatch.undo()  # this test reads the real function
    monkeypatch.setattr(engine.importlib.metadata, "version", lambda name: "1.62.0")
    assert engine._playwright_build_floor() == "beta.30"
    monkeypatch.setattr(engine.importlib.metadata, "version", lambda name: "1.58.0")
    assert engine._playwright_build_floor() is None


def test_interrupted_fetch_residue_is_not_legacy(tmp_path):
    """A failed first 0.5 fetch leaves files but no .0.5_FLAG and no root
    version.json. That must read as not-installed (fetch again), never as a
    legacy engine to protect, or provisioning wedges for good."""
    pkg = _pkg(tmp_path, pin=PIN)
    root = tmp_path / "cache"
    root.mkdir()
    (root / "repo_cache.json").write_text("{}")
    (root / "browsers" / "official").mkdir(parents=True)
    status = camoufox_engine_status(install_dir=root, package_dir=pkg)
    assert status.state == engine.NOT_INSTALLED
    assert "interrupted" in status.detail


def test_active_version_alone_is_not_an_override(tmp_path, monkeypatch):
    """A plain paired fetch records active_version itself (measured live); only
    camoufox's is_explicit_choice keys (channel, pinned) mean the user chose."""
    pkg = _pkg(tmp_path, pin=PIN)
    monkeypatch.setattr(engine, "_package_dir", lambda: pkg)
    root = tmp_path / "cache"
    root.mkdir()
    (root / ".0.5_FLAG").touch()
    _engine(root / "browsers" / "official" / "156.0.1-beta.34", "156.0.1", "beta.34")
    (root / "config.json").write_text(
        json.dumps({"active_version": "browsers/official/156.0.1-beta.34-09effb44"})
    )
    assert camoufox_engine_status(install_dir=root).ready


# ── One row per camoufox release family ──────────────────────────────────────
# From the wheels of every release 0.4.1 to 0.5.8b1: no 0.4 package has
# multiversion.py, every 0.5 package has it (its camoufox_path imports
# COMPAT_FLAG from it), and browser-pin.json exists only from 0.5.7b1. So
# 0.5.3 to 0.5.6 use the side-by-side layout with no pin.
_FAMILIES = {
    "0.4.1-0.4.11": {"pin": None, "multiversion": False},
    "0.5.3-0.5.6": {"pin": None, "multiversion": True},
    "0.5.7b1-0.5.8b1": {"pin": PIN, "multiversion": True},
    "0.5 dev checkout": {"pin": {}, "multiversion": True},
}


@pytest.mark.parametrize(
    ("family", "layout", "expected"),
    [
        ("0.4.1-0.4.11", "root", engine.READY),
        ("0.4.1-0.4.11", "side_by_side", engine.NOT_INSTALLED),
        ("0.5.3-0.5.6", "side_by_side", engine.READY),
        ("0.5.3-0.5.6", "root", engine.LEGACY_LAYOUT),
        ("0.5.7b1-0.5.8b1", "side_by_side", engine.READY),
        ("0.5.7b1-0.5.8b1", "root", engine.LEGACY_LAYOUT),
        ("0.5 dev checkout", "side_by_side", engine.READY),
        ("0.5 dev checkout", "root", engine.LEGACY_LAYOUT),
    ],
)
def test_status_per_release_family(tmp_path, family, layout, expected):
    """The layout follows the package generation, never the pin: a pinless 0.5.6
    over a side-by-side engine launches it, and over a root engine it deletes it."""
    pkg = _pkg(tmp_path, **_FAMILIES[family])
    if layout == "root":
        root = want = _engine(tmp_path / "cache", "135.0.1", "beta.24")
    else:
        root = _flagged_root(tmp_path)
        want = _engine(
            root / "browsers" / "official" / "156.0.1-beta.34-abcd1234", "156.0.1", "beta.34"
        )
    status = camoufox_engine_status(install_dir=root, package_dir=pkg)
    assert status.state == expected, status.detail
    assert status.path == (want if expected == engine.READY else None)


def test_engine_without_a_firefox_version_is_still_an_engine(tmp_path):
    """camoufox reads ``version`` with .get in every release, so list_installed
    keeps such an engine and get_active_path can select it; so must this."""
    pkg = _pkg(tmp_path, **_FAMILIES["0.5.3-0.5.6"])
    root = _flagged_root(tmp_path)
    want = root / "browsers" / "official" / "beta.34"
    want.mkdir(parents=True)
    (want / "version.json").write_text(json.dumps({"release": "beta.34"}))
    (want / "camoufox-bin").write_text("#!/bin/sh\n")
    (want / "camoufox-bin").chmod(0o755)
    status = camoufox_engine_status(install_dir=root, package_dir=pkg)
    assert status.state == engine.READY, status.detail
    assert status.path == want


# ── Mirroring camoufox's own engine selection ────────────────────────────────
# READY must mean camoufox would select an installed engine and launch it. Each
# case below is one place camoufox's resolution (multiversion.get_active_path,
# pkgman.camoufox_path, browser_pin.matches) would instead fetch.


def test_pin_match_includes_the_repository(tmp_path):
    """browser_pin.matches compares (repo_name, version, build): the same build in
    another repository is not the paired engine, and camoufox would fetch."""
    pkg = _pkg(tmp_path, pin=PIN)
    root = _flagged_root(tmp_path)
    _engine(root / "browsers" / "custom" / "156.0.1-beta.34", "156.0.1", "beta.34")
    status = camoufox_engine_status(install_dir=root, package_dir=pkg)
    assert status.state == engine.PIN_NOT_INSTALLED
    assert "official/156.0.1-beta.34" in status.detail


def test_pin_repo_name_is_case_insensitive(tmp_path):
    """load_pin lowercases the pin's repo_name; matches lowercases the directory."""
    pkg = _pkg(tmp_path, pin=PIN)
    root = _flagged_root(tmp_path)
    want = _engine(root / "browsers" / "OFFICIAL" / "156.0.1-beta.34", "156.0.1", "beta.34")
    status = camoufox_engine_status(install_dir=root, package_dir=pkg)
    assert status.ready and status.path == want


def test_pinned_engine_below_playwright_floor_is_not_ready(tmp_path, monkeypatch):
    """camoufox_path launches the paired engine only when it is supported; below
    the playwright floor it fetches."""
    monkeypatch.setattr(engine, "_playwright_build_floor", lambda *_: "beta.35")
    pkg = _pkg(tmp_path, pin=PIN)
    root = _flagged_root(tmp_path)
    _engine(root / "browsers" / "official" / "156.0.1-beta.34", "156.0.1", "beta.34")
    status = camoufox_engine_status(install_dir=root, package_dir=pkg)
    assert not status.ready
    assert "beta.35" in status.detail


def test_pin_without_a_tag_is_no_pin(tmp_path):
    """load_pin treats a pin with no tag as unpinned; so must this, or a pin it
    would never enforce decides readiness."""
    pin = {k: v for k, v in PIN.items() if k != "tag"}
    assert engine.camoufox_pin(_pkg(tmp_path, pin=pin)) is None


def test_pin_keeps_version_first(tmp_path):
    pin = engine.camoufox_pin(_pkg(tmp_path, pin=PIN))
    assert pin == ("156.0.1", "beta.34", "official")
    assert pin[0] == "156.0.1"


def test_unpinned_resolves_the_active_engine(tmp_path):
    """get_active_path without a pin: config.json's active engine wins over the
    newest installed one."""
    pkg = _pkg(tmp_path, pin={})
    root = _flagged_root(tmp_path)
    _engine(root / "browsers" / "official" / "156.0.1-beta.33", "156.0.1", "beta.33")
    active = _engine(root / "browsers" / "official" / "156.0.1-beta.40", "156.0.1", "beta.40")
    (root / "config.json").write_text(
        json.dumps({"active_version": "browsers/official/156.0.1-beta.40"})
    )
    status = camoufox_engine_status(install_dir=root, package_dir=pkg)
    assert status.ready and status.path == active


def test_unpinned_active_engine_below_floor_is_not_ready(tmp_path, monkeypatch):
    """An old active build plus a newer installed one: camoufox launches the
    active one, finds it unsupported, and fetches."""
    monkeypatch.setattr(engine, "_playwright_build_floor", lambda *_: "beta.30")
    pkg = _pkg(tmp_path, pin={})
    root = _flagged_root(tmp_path)
    _engine(root / "browsers" / "official" / "156.0.1-beta.34", "156.0.1", "beta.34")
    _engine(root / "browsers" / "official" / "135.0.1-beta.24", "135.0.1", "beta.24")
    (root / "config.json").write_text(
        json.dumps({"active_version": "browsers/official/135.0.1-beta.24"})
    )
    status = camoufox_engine_status(install_dir=root, package_dir=pkg)
    assert not status.ready
    assert "beta.30" in status.detail


def test_unpinned_without_active_takes_the_newest(tmp_path):
    pkg = _pkg(tmp_path, pin={})
    root = _flagged_root(tmp_path)
    _engine(root / "browsers" / "official" / "156.0.1-beta.1", "156.0.1", "beta.1")
    _engine(root / "browsers" / "official" / "156.0.1-beta.9", "156.0.1", "beta.9")
    newest = _engine(root / "browsers" / "official" / "156.0.1-beta.10", "156.0.1", "beta.10")
    status = camoufox_engine_status(install_dir=root, package_dir=pkg)
    assert status.ready and status.path == newest  # numeric order, not name order


def test_unpinned_channel_choice_without_active_is_not_ready(tmp_path):
    """With a channel chosen and nothing active, get_active_path returns None and
    camoufox fetches that channel's build."""
    pkg = _pkg(tmp_path, pin={})
    root = _flagged_root(tmp_path)
    _engine(root / "browsers" / "official" / "156.0.1-beta.33", "156.0.1", "beta.33")
    (root / "config.json").write_text(json.dumps({"channel": "official/prerelease"}))
    assert not camoufox_engine_status(install_dir=root, package_dir=pkg).ready


def test_version_json_tag_key_is_the_build(tmp_path):
    """pkgman.Version.from_path reads the build from release, else tag."""
    pkg = _pkg(tmp_path, pin=PIN)
    root = _flagged_root(tmp_path)
    d = _engine(root / "browsers" / "official" / "156.0.1-beta.34", "156.0.1", "beta.34")
    (d / "version.json").write_text(json.dumps({"version": "156.0.1", "tag": "beta.34"}))
    assert camoufox_engine_status(install_dir=root, package_dir=pkg).ready


def test_build_key_mirrors_camoufox_ordering():
    key = engine._build_key
    assert key("alpha.1") < key("beta.9") < key("beta.30") < key("beta.34") < key("1")
    assert key("beta.30") == key("beta.30.0")


def test_recovery_hints_name_only_scripts_that_exist():
    """Every repo script a browser message or doc points an operator at must
    exist in this tree: a hint naming a script that is not shipped is a dead end."""
    repo = Path(__file__).resolve().parents[2]
    surfaces = [
        "src/genesis/browser/engine.py",
        "src/genesis/mcp/health/browser.py",
        "src/genesis/runtime/_capabilities.py",
        "src/genesis/runtime/_init_delegates.py",
        "src/genesis/skills/browser-automation/SKILL.md",
    ]
    named = {
        (surface, m)
        for surface in surfaces
        for m in re.findall(r"scripts/[\w./-]+\.(?:sh|py)", (repo / surface).read_text())
    }
    missing = sorted(f"{s}: {m}" for s, m in named if not (repo / m).exists())
    assert not missing, missing


# ── The installed package's own supported range ─────────────────────────────


def _version_py(pkg: Path, body: str) -> None:
    (pkg / "__version__.py").write_text(body)


_V04 = """
class CONSTRAINTS:
    MIN_VERSION = 'beta.19'
    MAX_VERSION = '1'
"""


def test_pre05_engine_below_the_packages_minimum_is_not_ready(tmp_path):
    """camoufox 0.4.11's camoufox_path fetches when the root engine is below its
    own MIN_VERSION ('beta.19'), which camoufox main's 'alpha.1' would pass."""
    pkg = _pkg(tmp_path, pin=None)
    _version_py(pkg, _V04)
    root = _engine(tmp_path / "cache", "128.0", "beta.18")
    status = camoufox_engine_status(install_dir=root, package_dir=pkg)
    assert not status.ready
    assert "beta.19" in status.detail


def test_pre05_engine_inside_the_packages_range_is_ready(tmp_path):
    pkg = _pkg(tmp_path, pin=None)
    _version_py(pkg, _V04)
    root = _engine(tmp_path / "cache", "135.0.1", "beta.24")
    status = camoufox_engine_status(install_dir=root, package_dir=pkg)
    assert status.ready
    assert "copy" not in status.detail  # the package's own limits were read


def test_05_package_minimum_raises_above_the_pinned_engine(tmp_path):
    """A release that raises MIN_VERSION past an installed engine: camoufox
    fetches, whatever Genesis's mirrored constants say."""
    pkg = _pkg(tmp_path, pin=PIN)
    _version_py(pkg, "class CONSTRAINTS:\n    MIN_VERSION = 'beta.35'\n    MAX_VERSION = '1'\n")
    root = _flagged_root(tmp_path)
    _engine(root / "browsers" / "official" / "156.0.1-beta.34", "156.0.1", "beta.34")
    status = camoufox_engine_status(install_dir=root, package_dir=pkg)
    assert not status.ready
    assert "beta.35" in status.detail


def test_package_floor_table_is_used(tmp_path, monkeypatch):
    monkeypatch.undo()  # the real floor function, fed the package's table
    monkeypatch.setattr(engine.importlib.metadata, "version", lambda name: "1.70.0")
    pkg = _pkg(tmp_path, pin=PIN)
    _version_py(
        pkg,
        "class CONSTRAINTS:\n    MIN_VERSION = 'alpha.1'\n    MAX_VERSION = '1'\n"
        "    PLAYWRIGHT_BROWSER_FLOORS = (((1, 61), 'beta.30'), ((1, 70), 'beta.36'))\n",
    )
    root = _flagged_root(tmp_path)
    _engine(root / "browsers" / "official" / "156.0.1-beta.34", "156.0.1", "beta.34")
    status = camoufox_engine_status(install_dir=root, package_dir=pkg)
    assert not status.ready
    assert "beta.36" in status.detail


def test_unreadable_package_limits_fall_back_and_say_so(tmp_path):
    pkg = _pkg(tmp_path, pin=PIN)
    _version_py(pkg, "this is not python (")
    root = _flagged_root(tmp_path)
    _engine(root / "browsers" / "official" / "156.0.1-beta.34", "156.0.1", "beta.34")
    status = camoufox_engine_status(install_dir=root, package_dir=pkg)
    assert status.ready
    assert "unreadable" in status.detail


def test_status_never_imports_camoufox_at_runtime(tmp_path):
    """The AST check above covers the module's own imports; this covers what a
    status call pulls in, in a fresh interpreter."""
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[2]
    code = (
        f"import sys; sys.path.insert(0, {str(repo / 'src')!r})\n"
        "from genesis.browser.engine import camoufox_engine_status\n"
        "camoufox_engine_status()\n"
        "print('camoufox' in sys.modules)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "False"
