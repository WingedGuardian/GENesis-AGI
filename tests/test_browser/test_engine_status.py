"""camoufox_engine_status: read-only engine readiness across both camoufox layouts.

The check exists because camoufox 0.5's own path lookup deletes a pre-0.5 install
directory and downloads from inside the caller. These tests pin that the status
never reports READY for a layout camoufox 0.5 would wipe, and never writes.
"""

from __future__ import annotations

import json
from pathlib import Path

from genesis.browser import engine
from genesis.browser.engine import camoufox_engine_status


def _pkg(tmp_path: Path, pin: dict | None) -> Path:
    pkg = tmp_path / "site" / "camoufox"
    pkg.mkdir(parents=True)
    if pin is not None:
        (pkg / "browser-pin.json").write_text(json.dumps(pin))
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
    "version": "156.0.1",
    "build": "beta.34",
}


def _snapshot(root: Path) -> list[tuple[str, float]]:
    return sorted((str(p), p.stat().st_mtime) for p in root.rglob("*"))


def test_no_package(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "_package_dir", lambda: None)
    status = camoufox_engine_status(install_dir=tmp_path / "cache")
    assert status.state == engine.NO_PACKAGE
    assert not status.ready


def test_pre05_package_with_root_engine_is_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "_playwright_build_floor", lambda: None)
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
    assert "install_browser_stack.sh" in status.detail
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
    monkeypatch.setattr(engine, "_playwright_build_floor", lambda: 30)
    pkg = _pkg(tmp_path, pin=None)
    root = _engine(tmp_path / "cache", "135.0.1", "beta.24")
    status = camoufox_engine_status(install_dir=root, package_dir=pkg)
    assert not status.ready
    assert "beta.30" in status.detail


def test_floor_from_installed_playwright(monkeypatch):
    monkeypatch.setattr(engine.importlib.metadata, "version", lambda name: "1.62.0")
    assert engine._playwright_build_floor() == 30
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
