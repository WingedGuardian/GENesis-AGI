"""genesis.browser.engine: which Camoufox binary a launch may use, decided without
reaching camoufox's own resolution (which deletes a pre-0.5 engine and downloads a
new one when ``executable_path`` is None).

CI installs neither camoufox nor playwright, so every cell runs through seams:
``_dist_version`` / ``_dist_files`` for package metadata, ``XDG_CACHE_HOME`` for the
engine cache, and fake ``camoufox`` modules that carry upstream 0.5.7's real names
(``BrowserPin``, ``load_pin``, ``matches``, ``list_installed``, ``InstalledVersion``).
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import types
from dataclasses import dataclass
from pathlib import Path

import pytest

from genesis.browser import engine

# ── seams ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def cache(tmp_path, monkeypatch):
    """An empty camoufox cache root under a private XDG_CACHE_HOME."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("CAMOUFOX_EXECUTABLE_PATH", raising=False)
    root = engine.camoufox_install_dir()
    assert root == tmp_path / "xdg" / "camoufox"  # guard: the seam really moved it
    return root


def _versions(
    monkeypatch, *, camoufox="0.4.11", playwright="1.58.0", files=None, legacy_supported=True
):
    found = {"camoufox": camoufox, "playwright": playwright}
    monkeypatch.setattr(engine, "_dist_version", lambda name: found.get(name))
    monkeypatch.setattr(engine, "_dist_files", lambda name: files)
    if camoufox and camoufox.startswith("0.4"):
        _fake_legacy_pkgman(monkeypatch, supported=legacy_supported)
    elif camoufox:
        # The resolver imports camoufox on these paths; CI has no browser extra, so
        # a test must never lean on a camoufox the box happens to have.
        _fake_camoufox(monkeypatch)


def _fake_legacy_pkgman(monkeypatch, *, supported=True):
    """camoufox 0.4's ``pkgman.Version``: ``from_path`` reads the root version.json,
    ``is_supported`` is the package's own range check (pkgman.py:106-129)."""

    class Version:
        def __init__(self, data):
            self.data = data

        @staticmethod
        def from_path(path=None):
            return Version(json.loads((Path(path) / "version.json").read_text()))

        def is_supported(self):
            return supported

    pkg = types.ModuleType("camoufox")
    pkgman = types.ModuleType("camoufox.pkgman")
    pkgman.Version = Version
    pkg.pkgman = pkgman
    utils = types.ModuleType("camoufox.utils")
    utils._load_properties = _load_properties_0_4  # 0.4.11 utils.py:77-88
    pkg.utils = utils
    monkeypatch.setitem(sys.modules, "camoufox", pkg)
    monkeypatch.setitem(sys.modules, "camoufox.pkgman", pkgman)
    monkeypatch.setitem(sys.modules, "camoufox.utils", utils)


def _read_properties(path):
    data = json.loads((Path(path).parent / "properties.json").read_bytes())
    return data


def _load_properties_0_4(path=None):
    return {prop["property"]: prop["type"] for prop in _read_properties(path)}


def _load_properties_0_5_8(path=None):
    return {prop["property"]: prop for prop in _read_properties(path)}


ALL_OS = ("linux", "macos", "windows")


def _engine_dir(
    d: Path,
    *,
    version="135.0.1",
    version_json=True,
    ini=True,
    executable=True,
    properties=True,
    fontconfig=ALL_OS,
    fontconfig_dir="fontconfig",
):
    d.mkdir(parents=True, exist_ok=True)
    binary = d / "camoufox-bin"
    binary.write_bytes(b"\x7fELF")
    binary.chmod(0o755 if executable else 0o644)
    if version_json:
        (d / "version.json").write_text(json.dumps({"version": version, "release": "beta.24"}))
    if ini:
        (d / "application.ini").write_text(f"[App]\nName=Camoufox\nVersion={version}\n")
    if properties:
        (d / "properties.json").write_text("[]")
    for os_dir in fontconfig:
        (d / fontconfig_dir / os_dir).mkdir(parents=True, exist_ok=True)
        (d / fontconfig_dir / os_dir / "fonts.conf").write_text("<fontconfig/>")
    return binary


# Fakes of camoufox 0.5.7's read-only selection API (browser_pin.py, multiversion.py).


@dataclass(frozen=True)
class _BrowserPin:
    tag: str
    repo: str
    repo_name: str
    version: str
    build: str

    @property
    def spec(self) -> str:
        return f"{self.version}-{self.build}"


@dataclass
class _Version:
    build: str
    version: str | None = None


@dataclass
class _InstalledVersion:
    repo_name: str
    version: _Version
    path: Path


PIN = _BrowserPin("v156.0.1-beta.34", "daijro/camoufox", "official", "156.0.1", "beta.34")


def _fake_camoufox(monkeypatch, *, pin=PIN, installed=(), list_raises=None, pin_raises=None):
    def matches(p, repo_name, version, build):
        return (repo_name.lower(), version, build) == (p.repo_name, p.version, p.build)

    def list_installed():
        if list_raises is not None:
            raise list_raises
        return list(installed)

    pkg = types.ModuleType("camoufox")
    bp = types.ModuleType("camoufox.browser_pin")

    def load_pin(path=None):
        if pin_raises is not None:
            raise pin_raises
        return pin

    bp.BrowserPin, bp.load_pin, bp.matches = _BrowserPin, load_pin, matches
    mv = types.ModuleType("camoufox.multiversion")
    mv.list_installed = list_installed
    pkgman = types.ModuleType("camoufox.pkgman")
    pkgman.Version = _UpstreamVersion
    utils = types.ModuleType("camoufox.utils")
    utils._load_properties = _load_properties_0_5_8  # 0.5.8 utils.py:334-353
    pkg.browser_pin, pkg.multiversion, pkg.pkgman, pkg.utils = bp, mv, pkgman, utils
    for name, mod in (
        ("camoufox", pkg),
        ("camoufox.browser_pin", bp),
        ("camoufox.multiversion", mv),
        ("camoufox.pkgman", pkgman),
        ("camoufox.utils", utils),
    ):
        monkeypatch.setitem(sys.modules, name, mod)


class _UpstreamVersion:
    """camoufox 0.5.8 ``pkgman.Version.from_path`` (pkgman.py:378-397): orjson-parses
    version.json, takes ``release`` or ``tag`` as the build, raises KeyError with no
    build and IndexError on an empty one (``Version.__post_init__``); either aborts
    ``list_installed``."""

    @staticmethod
    def from_path(path):
        raw = (Path(path) / "version.json").read_text()
        try:
            data = json.loads(raw)
        except ValueError as exc:  # upstream raises orjson's decode error
            raise engine._listing_skips()[1]("bad json", raw, 0) from exc
        build = data.get("release", data.get("tag", data.get("build")))
        if build is None:
            raise KeyError("build")
        if not build:
            raise IndexError("string index out of range")
        return _UpstreamVersion()


def _installed(cache, version="156.0.1", build="beta.34", repo="official", suffix="ab12cd34", **kw):
    d = cache / "browsers" / repo / f"{version}-{build}-{suffix}"
    _engine_dir(d, version=version, **kw)
    return _InstalledVersion(repo, _Version(build, version), d)


# ── packages ───────────────────────────────────────────────────────────────────


def test_no_camoufox_package(cache, monkeypatch):
    _versions(monkeypatch, camoufox=None)
    assert engine.resolve_camoufox_engine().kind == engine.NO_PACKAGE


def test_no_playwright_package(cache, monkeypatch):
    _versions(monkeypatch, playwright=None)
    assert engine.resolve_camoufox_engine().kind == engine.PLAYWRIGHT_MISSING


def test_unparseable_camoufox_version_is_invalid(cache, monkeypatch):
    _versions(monkeypatch, camoufox="dev")
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID and "dev" in status.detail


# ── pre-0.5: the legacy root engine, the binary launched today ─────────────────


def test_legacy_root_engine_is_ready(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.4.11")
    binary = _engine_dir(cache)
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.READY
    assert status.path == binary


def test_legacy_missing_engine_is_not_installed(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.4.11")
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.NOT_INSTALLED
    assert "camoufox fetch" in status.detail


def test_legacy_engine_without_exec_bit_is_invalid_and_says_retry(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.4.11")
    _engine_dir(cache, executable=False)
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID
    assert "fetch was interrupted" in status.detail


def test_engine_without_version_line_is_invalid(cache, monkeypatch):
    """Without application.ini's Version= camoufox falls back to get_active_path(),
    which writes config.json (utils.resolve_verstr)."""
    _versions(monkeypatch, camoufox="0.4.11")
    _engine_dir(cache, ini=False)
    assert engine.resolve_camoufox_engine().kind == engine.INVALID


def test_engine_without_version_json_is_invalid(cache, monkeypatch):
    """camoufox writes version.json last-but-one on install (multiversion.py:450)."""
    _versions(monkeypatch, camoufox="0.4.11")
    _engine_dir(cache, version_json=False)
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID and "version.json" in status.detail


def test_engine_with_empty_version_line_is_invalid(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.4.11")
    _engine_dir(cache)
    (cache / "application.ini").write_text("[App]\nVersion=\n")
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID and "Version" in status.detail


# ── 0.5.7+: the engine the package pin names ──────────────────────────────────


def test_pinned_engine_is_ready(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.5.7")
    iv = _installed(cache)
    _fake_camoufox(monkeypatch, installed=[iv])
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.READY
    assert status.path == iv.path / "camoufox-bin"


def test_prerelease_package_version_parses(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.5.8b1")
    iv = _installed(cache)
    _fake_camoufox(monkeypatch, installed=[iv])
    assert engine.resolve_camoufox_engine().kind == engine.READY


def test_unpinned_build_is_not_selected(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.5.7")
    other = _installed(cache, version="150.0.2", build="beta.25")
    _fake_camoufox(monkeypatch, installed=[other])
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.NOT_INSTALLED
    assert PIN.spec in status.detail


def test_first_valid_duplicate_wins(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.5.7")
    broken = _installed(cache, suffix="00000000", executable=False)
    good = _installed(cache, suffix="ffffffff")
    _fake_camoufox(monkeypatch, installed=[broken, good])
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.READY and status.path == good.path / "camoufox-bin"


def test_only_broken_pinned_engine_is_invalid(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.5.7")
    broken = _installed(cache, executable=False)
    _fake_camoufox(monkeypatch, installed=[broken])
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID and "fetch was interrupted" in status.detail


def test_package_without_pin_is_invalid(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.5.7")
    _fake_camoufox(monkeypatch, pin=None)
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID and "pin" in status.detail
    assert "tell the user" in status.detail


# ── each INVALID cause names the step that repairs it ─────────────────────────


def test_non_executable_engine_is_repaired_by_chmod_not_fetch(cache, monkeypatch):
    """camoufox writes version.json, then runs chmod -R 755 (multiversion.py:450-454);
    a fetch killed in between leaves this state, and fetch skips a directory that
    already has version.json, so fetching again never repairs it."""
    _versions(monkeypatch, camoufox="0.5.7")
    broken = _installed(cache, executable=False)
    _fake_camoufox(monkeypatch, installed=[broken])
    detail = engine.resolve_camoufox_engine().detail
    assert f"chmod -R 755 {broken.path}" in detail
    assert "camoufox fetch" not in detail


def test_corrupt_pinned_engine_is_removed_then_fetched(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.5.7")
    broken = _installed(cache, ini=False)
    _fake_camoufox(monkeypatch, installed=[broken])
    detail = engine.resolve_camoufox_engine().detail
    assert f"remove `{broken.path}`" in detail
    assert detail.index("remove") < detail.index("camoufox fetch")


def test_legacy_engine_without_exec_bit_is_repaired_by_chmod(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.4.11")
    _engine_dir(cache, executable=False)
    assert f"chmod -R 755 {cache}" in engine.resolve_camoufox_engine().detail


def test_invalid_override_names_the_override_as_the_fix(cache, monkeypatch, tmp_path):
    _versions(monkeypatch, camoufox="0.5.7")
    monkeypatch.setenv("CAMOUFOX_EXECUTABLE_PATH", str(tmp_path / "nope" / "camoufox-bin"))
    detail = engine.resolve_camoufox_engine().detail
    assert "unset CAMOUFOX_EXECUTABLE_PATH" in detail
    assert "camoufox fetch" not in detail


def test_0_5_without_browser_pin_module_is_invalid(cache, monkeypatch):
    """0.5.3-0.5.6 ship multiversion.py but no browser_pin.py (MEASURED, 0.5.6 wheel)."""
    _versions(
        monkeypatch,
        camoufox="0.5.6",
        files=["camoufox/__init__.py", "camoufox/multiversion.py", "camoufox/pkgman.py"],
    )
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID
    assert "0.5.7" in status.detail


@pytest.mark.parametrize(
    "exc",
    [
        KeyError("build"),
        ValueError("x"),
        # upstream Version() indexes the first character of each build part, so
        # any browsers/ entry whose build is "" raises this (MEASURED, 0.5.7)
        IndexError("string index out of range"),
        RuntimeError("anything else"),
    ],
)
def test_upstream_lookup_failure_is_invalid_never_raises(cache, monkeypatch, exc):
    _versions(monkeypatch, camoufox="0.5.7")
    _fake_camoufox(monkeypatch, list_raises=exc)
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID
    assert "lookup failed" in status.detail
    assert "tell the user" in status.detail


@pytest.mark.parametrize(
    "exc", [PermissionError(13, "Permission denied"), OSError(5, "Input/output error")]
)
def test_an_unreadable_cache_gets_the_permissions_step(cache, monkeypatch, exc):
    """list_installed walks browsers/ unguarded, so a directory it cannot read
    raises from it; naming engine directories or config.json would not help."""
    _versions(monkeypatch, camoufox="0.5.7")
    _fake_camoufox(monkeypatch, list_raises=exc)
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID
    assert "check the permissions under" in status.detail
    assert "config.json" not in status.detail


def test_engine_dirs_does_not_hide_an_unreadable_directory(cache, monkeypatch):
    repo = engine.camoufox_install_dir() / "browsers" / "official"
    repo.mkdir(parents=True)
    real = Path.iterdir

    def iterdir(self):
        if self == repo:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    with pytest.raises(PermissionError):
        engine._engine_dirs()


def test_engine_dirs_without_a_browsers_directory_is_empty(cache):
    assert engine._engine_dirs() == []


def test_import_failure_is_invalid_never_raises(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.5.7")
    monkeypatch.setitem(sys.modules, "camoufox", None)  # import raises ImportError
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID


@pytest.mark.parametrize("broken", ["unimportable", "missing_submodule"])
def test_import_failure_names_the_package_not_the_cache(cache, monkeypatch, broken):
    """A package that cannot be imported is repaired by reinstalling it; telling the
    user to remove an unreadable engine sends them to the wrong place."""
    _versions(monkeypatch, camoufox="0.5.7")
    if broken == "unimportable":
        monkeypatch.setitem(sys.modules, "camoufox", None)
    else:
        empty = types.ModuleType("camoufox")
        empty.__path__ = []  # a package whose submodules are gone: importing one fails
        monkeypatch.setitem(sys.modules, "camoufox", empty)
        for sub in ("browser_pin", "multiversion", "pkgman", "utils"):
            monkeypatch.delitem(sys.modules, f"camoufox.{sub}", raising=False)
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID
    assert "cannot be imported" in status.detail
    assert engine.PACKAGE_HINT in status.detail
    assert "lookup failed" not in status.detail and "remove the unreadable" not in status.detail


# ── CAMOUFOX_EXECUTABLE_PATH: upstream's own override, the only one honoured ──


def test_override_is_used(cache, monkeypatch, tmp_path):
    _versions(monkeypatch, camoufox="0.5.7")
    binary = _engine_dir(tmp_path / "custom", version_json=False)
    monkeypatch.setenv("CAMOUFOX_EXECUTABLE_PATH", str(binary))
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.READY and status.path == binary


def test_invalid_override_never_falls_through(cache, monkeypatch, tmp_path):
    _versions(monkeypatch, camoufox="0.5.7")
    _fake_camoufox(monkeypatch, installed=[_installed(cache)])  # a perfectly good pinned engine
    monkeypatch.setenv("CAMOUFOX_EXECUTABLE_PATH", str(tmp_path / "nope" / "camoufox-bin"))
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID
    assert "CAMOUFOX_EXECUTABLE_PATH" in status.detail


def test_override_is_ignored_below_0_5(cache, monkeypatch, tmp_path):
    """camoufox 0.4 has no CAMOUFOX_EXECUTABLE_PATH, and its launch_options reads the
    root version.json whatever executable_path is (installed_verstr ->
    Version.from_path(), pkgman.py:409, utils.py:501), so the root engine is the one
    to check and launch."""
    _versions(monkeypatch, camoufox="0.4.11")
    root_binary = _engine_dir(cache)
    monkeypatch.setenv("CAMOUFOX_EXECUTABLE_PATH", str(_engine_dir(tmp_path / "custom")))
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.READY and status.path == root_binary


def test_override_does_not_stand_in_for_a_missing_root_below_0_5(cache, monkeypatch, tmp_path):
    _versions(monkeypatch, camoufox="0.4.11")
    monkeypatch.setenv("CAMOUFOX_EXECUTABLE_PATH", str(_engine_dir(tmp_path / "custom")))
    assert engine.resolve_camoufox_engine().kind == engine.NOT_INSTALLED


# ── files a launch reads beside the binary ────────────────────────────────────
# camoufox opens properties.json beside an explicit executable_path on every launch
# (utils._load_properties), and from 0.5 on Linux reads fontconfig/<os>/fonts.conf
# (or the older fontconfigs/<os>/) beside it, raising when absent (utils.py:313-327).
# Genesis passes no os=, so <os> is any of linux, macos, windows.


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(engine, "_READS_FONTCONFIG", True)


def _kind_engine(kind, cache, monkeypatch, tmp_path, **kw):
    """An engine of ``kind`` built with ``kw``, and the binary the resolver checks."""
    if kind == "legacy":
        _versions(monkeypatch, camoufox="0.4.11")
        return _engine_dir(cache, **kw)
    _versions(monkeypatch, camoufox="0.5.7")
    if kind == "versioned":
        iv = _installed(cache, **kw)
        _fake_camoufox(monkeypatch, installed=[iv])
        return iv.path / "camoufox-bin"
    binary = _engine_dir(tmp_path / "custom", **kw)
    monkeypatch.setenv("CAMOUFOX_EXECUTABLE_PATH", str(binary))
    return binary


@pytest.mark.parametrize("kind", ["legacy", "versioned", "override"])
def test_engine_without_properties_json_is_invalid(kind, cache, monkeypatch, tmp_path, linux):
    _kind_engine(kind, cache, monkeypatch, tmp_path, properties=False)
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID and "properties.json" in status.detail


@pytest.mark.parametrize("kind", ["versioned", "override"])
@pytest.mark.parametrize("missing", ALL_OS)
def test_0_5_engine_missing_one_os_fontconfig_is_invalid(
    kind, missing, cache, monkeypatch, tmp_path, linux
):
    have = tuple(o for o in ALL_OS if o != missing)
    _kind_engine(kind, cache, monkeypatch, tmp_path, fontconfig=have)
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID
    assert f"{missing}/fonts.conf" in status.detail


def test_older_fontconfigs_spelling_is_accepted(cache, monkeypatch, tmp_path, linux):
    binary = _kind_engine("versioned", cache, monkeypatch, tmp_path, fontconfig_dir="fontconfigs")
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.READY and status.path == binary


@pytest.mark.parametrize("kind", ["versioned", "override"])
@pytest.mark.parametrize("bad", ALL_OS)
def test_undecodable_fonts_conf_is_invalid(kind, bad, cache, monkeypatch, tmp_path, linux):
    """camoufox reads fonts.conf as text (utils._generate_fontconfig, open(.., 'r')).
    MEASURED on a real 0.5.8 engine: invalid UTF-8 there fails the launch with
    UnicodeDecodeError."""
    binary = _kind_engine(kind, cache, monkeypatch, tmp_path)
    (binary.parent / "fontconfig" / bad / "fonts.conf").write_bytes(b"<fontconfig>\xff\xfe")
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID
    assert f"{bad}/fonts.conf" in status.detail


def test_fonts_conf_read_is_the_one_upstream_picks(cache, monkeypatch, tmp_path, linux):
    """utils.py:318-320 takes fontconfig/<os>/ when it holds a fonts.conf, and only
    otherwise the older fontconfigs/<os>/; a good older copy does not rescue it."""
    binary = _kind_engine("versioned", cache, monkeypatch, tmp_path)
    for os_dir in ALL_OS:
        older = binary.parent / "fontconfigs" / os_dir
        older.mkdir(parents=True)
        (older / "fonts.conf").write_text("<fontconfig/>")
    (binary.parent / "fontconfig" / "linux" / "fonts.conf").write_bytes(b"\xff")
    assert engine.resolve_camoufox_engine().kind == engine.INVALID


def test_undecodable_application_ini_is_still_ready(cache, monkeypatch, tmp_path, linux):
    """camoufox decodes application.ini with errors='replace' (0.5.7 utils.py:809,
    0.5.8 utils.py:816) and Firefox does not read that copy. MEASURED: a real 0.5.8
    engine launches with invalid UTF-8 in it."""
    binary = _kind_engine("versioned", cache, monkeypatch, tmp_path)
    ini = binary.parent / "application.ini"
    ini.write_bytes(b"; \xff\xfe\n" + ini.read_bytes())
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.READY and status.path == binary


def test_legacy_engine_needs_no_fontconfig(cache, monkeypatch, tmp_path, linux):
    """0.4 sets FONTCONFIG_PATH without checking it exists (utils.py:71)."""
    binary = _kind_engine("legacy", cache, monkeypatch, tmp_path, fontconfig=())
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.READY and status.path == binary


def test_fontconfig_is_not_required_off_linux(cache, monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "_READS_FONTCONFIG", False)
    _kind_engine("versioned", cache, monkeypatch, tmp_path, fontconfig=())
    assert engine.resolve_camoufox_engine().kind == engine.READY


def test_corrupt_pinned_engine_missing_assets_is_removed_then_fetched(
    cache, monkeypatch, tmp_path, linux
):
    _kind_engine("versioned", cache, monkeypatch, tmp_path, properties=False)
    detail = engine.resolve_camoufox_engine().detail
    assert "remove `" in detail and detail.index("remove") < detail.index("camoufox fetch")


def test_blank_override_is_unset(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.4.11")
    binary = _engine_dir(cache)
    monkeypatch.setenv("CAMOUFOX_EXECUTABLE_PATH", "   ")
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.READY and status.path == binary


def test_override_does_not_mask_missing_playwright(cache, monkeypatch, tmp_path):
    _versions(monkeypatch, playwright=None)
    binary = _engine_dir(tmp_path / "custom")
    monkeypatch.setenv("CAMOUFOX_EXECUTABLE_PATH", str(binary))
    assert engine.resolve_camoufox_engine().kind == engine.PLAYWRIGHT_MISSING


@pytest.mark.parametrize("ver", ["0.5.3", "0.5.6"])
def test_override_is_not_honoured_before_0_5_7(cache, monkeypatch, tmp_path, ver):
    """CAMOUFOX_EXECUTABLE_PATH first appears in 0.5.7, the release that also ships
    browser_pin.py (MEASURED: 0 references in the 0.5.3-0.5.6 wheels)."""
    _versions(monkeypatch, camoufox=ver, files=["camoufox/__init__.py", "camoufox/utils.py"])
    monkeypatch.setenv("CAMOUFOX_EXECUTABLE_PATH", str(_engine_dir(tmp_path / "custom")))
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID and "0.5.7" in status.detail


@pytest.mark.parametrize("ver,refused", [("0.5.6", True), ("0.5.7", False), ("0.5.8b1", False)])
def test_without_a_file_list_the_version_decides_the_pin_cut(
    cache, monkeypatch, tmp_path, ver, refused
):
    """Some installers write no RECORD, so the file check cannot run."""
    _versions(monkeypatch, camoufox=ver, files=None)
    monkeypatch.setenv("CAMOUFOX_EXECUTABLE_PATH", str(_engine_dir(tmp_path / "custom")))
    status = engine.resolve_camoufox_engine()
    assert (status.kind == engine.INVALID and "0.5.7" in status.detail) is refused


# ── camoufox 0.4 still resolves its root engine itself on every launch ────────
# Even with an explicit path, 0.4's launch calls get_path() for fonts and add-ons,
# which goes through camoufox_path(): a root engine outside the package's supported
# range is deleted with the whole cache and downloaded again (pkgman.py:412-432).


def test_legacy_root_outside_the_supported_range_is_invalid(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.4.11", legacy_supported=False)
    _engine_dir(cache)
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID
    assert "supports" in status.detail and "camoufox fetch" in status.detail


def test_legacy_root_with_unreadable_version_json_is_invalid(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.4.11")
    _engine_dir(cache)
    (cache / "version.json").write_text("{not json")
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID and "version.json" in status.detail
    assert status.detail.index("rm -f") < status.detail.index("camoufox fetch")


def test_legacy_camoufox_that_cannot_be_imported_names_the_package(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.4.11")
    _engine_dir(cache)
    monkeypatch.setitem(sys.modules, "camoufox.pkgman", None)
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID
    assert "cannot be imported" in status.detail and engine.PACKAGE_HINT in status.detail


def test_legacy_file_problem_drops_the_version_record_before_fetch(cache, monkeypatch, linux):
    """0.4's fetch is a no-op when the root already holds the latest release
    (CamoufoxUpdate.is_updated_needed, __main__.py:41-56), and both its remove and
    fetch crash on an unreadable version.json (the constructor catches only
    FileNotFoundError). Without version.json, fetch reinstalls."""
    _versions(monkeypatch, camoufox="0.4.11")
    _engine_dir(cache, properties=False)
    detail = engine.resolve_camoufox_engine().detail
    assert f"rm -f {cache / 'version.json'}" in detail
    assert detail.index("rm -f") < detail.index("camoufox fetch")
    assert "camoufox remove" not in detail


def test_legacy_missing_engine_is_fetched_without_dropping_anything(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.4.11")
    assert "rm -f" not in engine.resolve_camoufox_engine().detail


@pytest.mark.parametrize(
    "content", ["", "[{", "\x00garbage", '{"a": 1}', "[1]", '[{"type": "str"}]']
)
def test_unreadable_properties_json_is_invalid(cache, monkeypatch, tmp_path, linux, content):
    """camoufox json-loads it on every launch (utils._load_properties)."""
    binary = _kind_engine("versioned", cache, monkeypatch, tmp_path)
    (binary.parent / "properties.json").write_text(content)
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID and "properties.json" in status.detail


@pytest.mark.parametrize("exc", [KeyError("repo"), TypeError("x")])
def test_unreadable_package_pin_names_the_package(cache, monkeypatch, exc):
    """load_pin catches only a missing or non-JSON file (browser_pin.py:44-58); a pin
    missing a key raises, and that is the package's fault, not the cache's."""
    _versions(monkeypatch, camoufox="0.5.7")
    _fake_camoufox(monkeypatch, pin_raises=exc)
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID
    assert engine.PACKAGE_HINT in status.detail and "remove the unreadable" not in status.detail


# ── the operator hint ─────────────────────────────────────────────────────────


def test_hint_marks_legacy_engine_before_fetch(cache, monkeypatch):
    """0.5's fetch deletes the whole cache when .0.5_FLAG is absent
    (__main__.py:259-263), so the flag step must come first."""
    _versions(monkeypatch, camoufox="0.5.7")
    _engine_dir(cache)  # a pre-0.5 root engine, no flag
    _fake_camoufox(monkeypatch, installed=[])
    detail = engine.resolve_camoufox_engine().detail
    assert ".0.5_FLAG" in detail
    assert detail.index(".0.5_FLAG") < detail.index("camoufox fetch")


def test_hint_has_no_flag_step_when_flag_present(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.5.7")
    _engine_dir(cache)
    (cache / ".0.5_FLAG").touch()
    _fake_camoufox(monkeypatch, installed=[])
    assert ".0.5_FLAG" not in engine.resolve_camoufox_engine().detail


def test_legacy_hint_warns_that_0_4_fetch_replaces_the_whole_cache(cache, monkeypatch):
    """camoufox 0.4's fetch runs cleanup() = shutil.rmtree(INSTALL_DIR) before it
    installs (pkgman.py:296-322), taking any 0.5 engines under browsers/ with it."""
    _versions(monkeypatch, camoufox="0.4.11")
    (cache / "browsers" / "official").mkdir(parents=True)
    detail = engine.resolve_camoufox_engine().detail
    assert "browsers" in detail and "deletes" in detail
    assert detail.index("deletes") < detail.index("camoufox fetch")


def test_legacy_hint_has_no_cache_warning_without_browsers(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.4.11")
    assert "deletes" not in engine.resolve_camoufox_engine().detail


def test_legacy_repair_step_has_no_0_5_flag_step(cache, monkeypatch, linux):
    """The flag guards 0.5's fetch; 0.4's fetch deletes the whole cache anyway."""
    _versions(monkeypatch, camoufox="0.4.11")
    _engine_dir(cache, properties=False)
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID and "camoufox fetch" in status.detail
    assert ".0.5_FLAG" not in status.detail


def test_hint_keeps_the_download_off_cc_tmp(cache, monkeypatch):
    """The 1.3 GB download is a TMPDIR temp file; a session's TMPDIR is a small
    quota, and a missing TMPDIR falls back to /tmp."""
    _versions(monkeypatch, camoufox="0.4.11")
    detail = engine.resolve_camoufox_engine().detail
    assert "mkdir -p ~/tmp && TMPDIR=~/tmp" in detail


def test_hint_names_this_interpreter(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.4.11")
    assert f"{sys.executable} -m camoufox fetch" in engine.resolve_camoufox_engine().detail


def test_hint_is_an_operator_step_and_names_no_script(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.4.11")
    detail = engine.resolve_camoufox_engine().detail
    assert "tell the user" in detail
    assert "scripts/" not in detail


# ── module shape ──────────────────────────────────────────────────────────────


def test_module_never_imports_camoufox_at_top_level():
    """Importing any camoufox submodule imports playwright; genesis-server asks
    through a subprocess so it never does."""
    tree = ast.parse(Path(engine.__file__).read_text())
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert not any(a.name.split(".")[0] == "camoufox" for a in node.names)
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] != "camoufox"


def test_never_calls_camoufox_own_resolution():
    source = Path(engine.__file__).read_text()
    for name in ("camoufox_path", "get_path", "get_active_path", "launch_path", "set_active"):
        assert f"{name}(" not in source


def test_status_json_round_trip(tmp_path):
    status = engine.EngineStatus(engine.READY, "ok", tmp_path / "camoufox-bin")
    assert engine.EngineStatus.from_json(status.to_json()) == status
    blank = engine.EngineStatus(engine.NO_PACKAGE, "none")
    assert engine.EngineStatus.from_json(blank.to_json()) == blank


def test_cli_prints_one_json_line(tmp_path):
    env = {"XDG_CACHE_HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    src = str(Path(engine.__file__).resolve().parents[2])
    env["PYTHONPATH"] = src
    out = subprocess.run(
        [sys.executable, "-m", "genesis.browser.engine", "--json"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=True,
    )
    lines = out.stdout.strip().splitlines()
    status = engine.EngineStatus.from_json(json.loads(lines[-1]))
    assert status.kind in {
        engine.READY,
        engine.NOT_INSTALLED,
        engine.INVALID,
        engine.NO_PACKAGE,
        engine.PLAYWRIGHT_MISSING,
    }


def test_operator_steps_quote_their_paths(tmp_path, monkeypatch):
    """A cache path with shell metacharacters stays one quoted word in the step."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "x; rm -rf ~"))
    monkeypatch.delenv("CAMOUFOX_EXECUTABLE_PATH", raising=False)
    _versions(monkeypatch, camoufox="0.4.11")
    root = engine.camoufox_install_dir()
    _engine_dir(root, executable=False)
    detail = engine.resolve_camoufox_engine().detail
    assert f"chmod -R 755 '{root}'" in detail


# ── round 2: what upstream's own readers reject ───────────────────────────────


@pytest.mark.parametrize("kind", ["versioned", "legacy"])
def test_a_property_without_its_type_is_invalid(kind, cache, monkeypatch, tmp_path, linux):
    """camoufox 0.4 and 0.5.7 index both `property` and `type` of every entry when
    they load properties.json (utils._load_properties), 0.5.8 when a launch sets
    that property (validate_config). MEASURED: every entry of a real 0.4 and 0.5.8
    engine has both, the 50 removed ones of 0.5.8's beta.36 included."""
    binary = _kind_engine(kind, cache, monkeypatch, tmp_path)
    (binary.parent / "properties.json").write_text('[{"property": "navigator.userAgent"}]')
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID and "properties.json" in status.detail


def test_a_pinned_directory_list_installed_skips_is_invalid_and_named(cache, monkeypatch):
    """list_installed skips a directory whose version.json is unreadable without a
    word (multiversion.py:312), and install_versioned refuses to replace a directory
    that has one (:404), so a bare fetch can never repair it."""
    _versions(monkeypatch, camoufox="0.5.7")
    broken = _installed(cache)
    (broken.path / "version.json").write_text("{not json")
    _fake_camoufox(monkeypatch, installed=[])
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID
    assert status.path == broken.path / "camoufox-bin"
    assert str(broken.path) in status.detail and "remove" in status.detail


def test_an_unreadable_directory_of_another_build_does_not_block_the_pin(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.5.7")
    good = _installed(cache)
    other = _installed(cache, build="beta.30")
    (other.path / "version.json").write_text("{not json")
    _fake_camoufox(monkeypatch, installed=[good])
    assert engine.resolve_camoufox_engine().kind == engine.READY


def test_a_failed_listing_names_the_directory_upstream_cannot_read(cache, monkeypatch):
    """`camoufox list` runs the same list_installed, so it fails the same way; the
    step names the directory upstream's own Version.from_path rejects instead."""
    _versions(monkeypatch, camoufox="0.5.7")
    good = _installed(cache)
    bad = _installed(cache, build="beta.30")
    (bad.path / "version.json").write_text(json.dumps({"version": "156.0.1", "release": ""}))
    _fake_camoufox(monkeypatch, installed=[good], list_raises=IndexError("list index out of range"))
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID
    assert str(bad.path) in status.detail and str(good.path) not in status.detail
    assert "camoufox list" not in status.detail


@pytest.mark.parametrize(
    "exc", [UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"), OSError(5, "EIO")]
)
def test_unreadable_package_metadata_is_a_broken_package(cache, monkeypatch, exc):
    """importlib.metadata raises these for a damaged .dist-info; the resolver
    promises never to raise."""

    def version(name):
        raise exc

    monkeypatch.setattr(engine.metadata, "version", version)
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID
    assert "metadata" in status.detail and engine.PACKAGE_HINT in status.detail


def test_a_skipped_directory_of_a_longer_build_is_not_the_pin(cache, monkeypatch):
    """`beta.3`'s folder is `156.0.1-beta.3[-sha8]`; `156.0.1-beta.34-…` shares the
    prefix and is another build."""
    _versions(monkeypatch, camoufox="0.5.7")
    other = _installed(cache, build="beta.34")
    (other.path / "version.json").write_text("{not json")
    pin = _BrowserPin("v156.0.1-beta.3", "daijro/camoufox", "official", "156.0.1", "beta.3")
    _fake_camoufox(monkeypatch, pin=pin, installed=[])
    assert engine.resolve_camoufox_engine().kind == engine.NOT_INSTALLED


def test_a_hidden_repo_directory_is_not_named(cache, monkeypatch):
    """list_installed never reads a repo directory whose name starts with a dot."""
    _versions(monkeypatch, camoufox="0.5.7")
    hidden = _installed(cache, repo=".partial", build="beta.30")
    bad = _installed(cache, build="beta.31")
    for d in (hidden.path, bad.path):
        (d / "version.json").write_text(json.dumps({"version": "156.0.1", "release": ""}))
    _fake_camoufox(monkeypatch, installed=[], list_raises=IndexError("list index out of range"))
    detail = engine.resolve_camoufox_engine().detail
    assert str(bad.path) in detail and str(hidden.path) not in detail


def test_a_failed_listing_with_no_culprit_directory_names_the_config(cache, monkeypatch):
    """A config.json holding valid JSON of the wrong shape breaks every listing; no
    engine directory is to blame, so none is named and no path is set."""
    _versions(monkeypatch, camoufox="0.5.7")
    good = _installed(cache)
    _fake_camoufox(monkeypatch, installed=[good], list_raises=AttributeError("'list' ..."))
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID and status.path is None
    assert "config.json" in status.detail and "remove them" not in status.detail


def test_a_quietly_skipped_directory_is_not_blamed_for_a_failed_listing(cache, monkeypatch):
    """list_installed skips an unparseable version.json (multiversion.py:312); only
    what it raises on can have broken the listing."""
    _versions(monkeypatch, camoufox="0.5.7")
    skipped = _installed(cache, build="beta.29")
    (skipped.path / "version.json").write_text("{bad")
    culprit = _installed(cache, build="beta.30")
    (culprit.path / "version.json").write_text(json.dumps({"version": "156.0.1", "release": ""}))
    _fake_camoufox(monkeypatch, installed=[], list_raises=IndexError("string index out of range"))
    status = engine.resolve_camoufox_engine()
    assert str(culprit.path) in status.detail and str(skipped.path) not in status.detail
    assert status.path == culprit.path / "camoufox-bin"


def test_two_culprits_are_both_named_and_neither_is_a_path(cache, monkeypatch):
    """A caller may remove the path it is given; with two causes, removing one
    would leave the listing broken."""
    _versions(monkeypatch, camoufox="0.5.7")
    bad = [_installed(cache, build=b) for b in ("beta.30", "beta.31")]
    for iv in bad:
        (iv.path / "version.json").write_text(json.dumps({"version": "156.0.1", "release": ""}))
    _fake_camoufox(monkeypatch, installed=[], list_raises=IndexError("string index out of range"))
    status = engine.resolve_camoufox_engine()
    assert all(str(iv.path) in status.detail for iv in bad) and status.path is None


def test_a_pinned_folder_holding_another_build_says_so(cache, monkeypatch):
    _versions(monkeypatch, camoufox="0.5.7")
    other = _installed(cache, build="beta.34")
    wrong = other.path.parent / "156.0.1-beta.34-0123abcd"
    other.path.rename(wrong)
    listed = _InstalledVersion("official", _Version("beta.35", "156.0.1"), wrong)
    _fake_camoufox(monkeypatch, installed=[listed])
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID and "another build" in status.detail
    assert status.path == wrong / "camoufox-bin"


def test_a_dashed_build_is_not_the_pins_folder(cache, monkeypatch):
    """`<spec>-<sha8>` is the only suffix install_versioned writes."""
    _versions(monkeypatch, camoufox="0.5.7")
    hotfix = _installed(cache, build="beta.34", suffix="hotfix")
    (hotfix.path / "version.json").write_text("{bad")
    _fake_camoufox(monkeypatch, installed=[])
    assert engine.resolve_camoufox_engine().kind == engine.NOT_INSTALLED


@pytest.mark.parametrize("value", ["15\ufffd6.0.1-beta.36", "beta.36", "  "])
def test_a_version_whose_major_is_not_a_number_is_invalid(
    value, cache, monkeypatch, tmp_path, linux
):
    """camoufox puts ``Version.split('.', 1)[0]`` into the browser's fingerprint
    (0.5.8 utils.py:1082): a garbled major launches with a garbage User-Agent."""
    binary = _kind_engine("versioned", cache, monkeypatch, tmp_path)
    (binary.parent / "application.ini").write_text(f"[App]\nVersion={value}\nVersion=156.0.1\n")
    status = engine.resolve_camoufox_engine()
    if value.strip():
        assert status.kind == engine.INVALID and "Version" in status.detail
    else:  # upstream skips an empty value and takes the next Version= line
        assert status.kind == engine.READY


@pytest.mark.parametrize(
    ("content", "ready"),
    [
        (None, True),  # absent: upstream leaves an unpackaged build alone
        ('{"version": "156.0.1", "release": "beta.36"}', True),
        ("not json", True),  # ValueError: upstream passes
        ('{"version": "156.0.1"}', True),  # KeyError-shaped: upstream passes
        ('{"version": "156.0.1", "build": ""}', False),  # IndexError: the launch raises
    ],
)
def test_override_version_json_is_read_as_upstream_reads_it(
    content, ready, cache, monkeypatch, tmp_path, linux
):
    """With an explicit path camoufox parses version.json beside it and tolerates
    only FileNotFoundError, KeyError and ValueError (0.5.8 utils.py:212-215)."""
    binary = _kind_engine("override", cache, monkeypatch, tmp_path, version_json=False)
    _fake_camoufox(monkeypatch)
    if content is not None:
        (binary.parent / "version.json").write_text(content)
    status = engine.resolve_camoufox_engine()
    assert status.ready is ready, status.detail
    if not ready:
        assert "version.json" in status.detail


def test_main_rejects_an_unknown_flag(capsys):
    with pytest.raises(SystemExit):
        engine.main(["--jsno"])


def test_a_broken_package_under_an_override_is_the_package_not_the_file(
    cache, monkeypatch, tmp_path, linux
):
    """camoufox.pkgman failing to import is a damaged package: its repair is the
    package reinstall, never "fix CAMOUFOX_EXECUTABLE_PATH"."""
    _kind_engine("override", cache, monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "camoufox.pkgman", None)
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID
    assert engine.PACKAGE_HINT in status.detail and "version.json" not in status.detail


@pytest.mark.parametrize(
    ("content", "ready"),
    [
        (None, True),
        (b'{"readBy": {"lin": ["L", "LM"], "mac": ["M"], "win": ["W"]}}', True),
        (b"\xff\xfe not json", True),  # ValueError: upstream falls back to fonts/<os>
        (b'{"readBy": {"lin": [', True),  # truncated: ValueError again
        (b"[]", False),  # AttributeError at .get: the launch raises
        (b"null", False),
        (b'{"readBy": []}', False),
        (b'{"readBy": {"lin": [1]}}', False),  # TypeError in os.path.join
    ],
)
def test_fonts_groups_json_is_read_as_upstream_reads_it(
    content, ready, cache, monkeypatch, tmp_path, linux
):
    """0.5.7/0.5.8 _generate_fontconfig reads fonts/groups.json beside the binary
    and tolerates only OSError and ValueError (utils.py:147-159)."""
    binary = _kind_engine("versioned", cache, monkeypatch, tmp_path)
    if content is not None:
        (binary.parent / "fonts").mkdir()
        (binary.parent / "fonts" / "groups.json").write_bytes(content)
    status = engine.resolve_camoufox_engine()
    assert status.ready is ready, status.detail
    if not ready:
        assert "groups.json" in status.detail


def test_fonts_conf_is_decoded_as_utf_8_whatever_the_locale(tmp_path):
    """The real 0.5.8 fonts.conf has non-ASCII lines (16 in linux/). Read in an
    ASCII locale, every healthy engine would look broken, and a caller that
    replaces broken engines would download one forever. Run in a real C locale
    with UTF-8 mode off, the way such a process would."""
    d = tmp_path / "e"
    (d / "fontconfig" / "linux").mkdir(parents=True)
    (d / "fontconfig" / "linux" / "fonts.conf").write_text(
        "<fontconfig><!-- \u00e9 --></fontconfig>", encoding="utf-8"
    )
    code = (
        "import sys, pathlib; from genesis.browser import engine; "
        "print(engine._fonts_conf_problem(pathlib.Path(sys.argv[1]) / 'camoufox-bin', 'linux'))"
    )
    src = str(Path(engine.__file__).resolve().parents[2])
    env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONPATH": src}
    out = subprocess.run(
        [sys.executable, "-c", code, str(d)], capture_output=True, text=True, env=env, check=True
    )
    assert out.stdout.strip() == "None", out.stdout + out.stderr


def test_an_unsearchable_directory_is_reported_not_raised(cache, monkeypatch, tmp_path, linux):
    """pathlib's exists() raises on a directory this user cannot search; the
    resolver never raises."""
    binary = _kind_engine("versioned", cache, monkeypatch, tmp_path)
    fc = binary.parent / "fontconfig"
    fc.chmod(0)
    try:
        status = engine.resolve_camoufox_engine()
    finally:
        fc.chmod(0o755)
    assert status.kind == engine.INVALID


def test_the_legacy_root_needs_a_version_line_but_not_a_numeric_major(cache, monkeypatch, tmp_path):
    """camoufox 0.4 takes the version from the root version.json, never application.ini."""
    binary = _kind_engine("legacy", cache, monkeypatch, tmp_path)
    (binary.parent / "application.ini").write_text("[App]\nVersion=beta.24\n")
    assert engine.resolve_camoufox_engine().kind == engine.READY


def test_a_filesystem_error_anywhere_is_a_status(cache, monkeypatch):
    def denied():
        raise PermissionError(13, "denied")

    monkeypatch.setattr(engine, "_resolve", denied)
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID and status.path is None
    assert "permissions" in status.detail


@pytest.mark.parametrize(
    "content",
    [
        b'[{"property": [], "type": "str"}]',  # unhashable key: the launch's dict raises
        b'[{"property": {"a": 1}, "type": "str"}]',
        b'[{"type": "str"}]',
        b'[{"property": "a"}]',
        b'{"property": "a", "type": "str"}',
    ],
)
@pytest.mark.parametrize("loader", ["upstream-0.5.8"])
def test_properties_json_is_loaded_the_way_camoufox_loads_it(
    content, loader, cache, monkeypatch, tmp_path, linux
):
    """Every camoufox release keys a dict by each entry's ``property``
    (utils._load_properties), and the launch reads each entry's ``type``. Run
    against a 0.5.8-shaped loader (keys whole entries, never reads ``type`` while
    loading)."""
    binary = _kind_engine("versioned", cache, monkeypatch, tmp_path)
    if loader == "upstream-0.5.8":
        utils = types.ModuleType("camoufox.utils")

        def _load_properties(path=None):
            data = json.loads((Path(path).parent / "properties.json").read_bytes())
            return {prop["property"]: prop for prop in data}

        utils._load_properties = _load_properties
        monkeypatch.setitem(sys.modules, "camoufox.utils", utils)
    (binary.parent / "properties.json").write_bytes(content)
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.INVALID and "properties.json" in status.detail


def test_upstreams_own_properties_loader_is_used_when_it_imports(
    cache, monkeypatch, tmp_path, linux
):
    """camoufox's loader is the authority; the resolver's copy is only the fallback."""
    binary = _kind_engine("versioned", cache, monkeypatch, tmp_path)
    utils = types.ModuleType("camoufox.utils")
    seen = []

    def _load_properties(path=None):
        seen.append(path)
        raise TypeError("unhashable type: 'list'")

    utils._load_properties = _load_properties
    monkeypatch.setitem(sys.modules, "camoufox.utils", utils)
    status = engine.resolve_camoufox_engine()
    assert seen == [binary] and status.kind == engine.INVALID
    assert "unhashable" in status.detail


def test_a_removed_property_needs_no_type(cache, monkeypatch, tmp_path, linux):
    """0.5.8's validate_config skips an entry marked "removed" before it reads its
    type; a launch with one is fine."""
    binary = _kind_engine("versioned", cache, monkeypatch, tmp_path)
    utils = types.ModuleType("camoufox.utils")
    utils._load_properties = lambda path=None: {
        prop["property"]: prop
        for prop in json.loads((Path(path).parent / "properties.json").read_bytes())
    }
    monkeypatch.setitem(sys.modules, "camoufox.utils", utils)
    (binary.parent / "properties.json").write_text(
        '[{"property": "a", "type": "str"}, {"property": "b", "removed": "140"}]'
    )
    assert engine.resolve_camoufox_engine().kind == engine.READY


def test_a_versionless_package_is_unreadable_not_absent(monkeypatch):
    """A dist-info with no Version answers None on Python 3.12 (MEASURED): a damaged
    install, whose step is a reinstall, never "not installed"."""
    monkeypatch.setattr(engine.metadata, "version", lambda name: None)
    with pytest.raises(engine._UnreadableMetadata):
        engine._dist_version("camoufox")
