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
    monkeypatch.setitem(sys.modules, "camoufox", pkg)
    monkeypatch.setitem(sys.modules, "camoufox.pkgman", pkgman)


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
    pkg.browser_pin, pkg.multiversion = bp, mv
    for name, mod in (
        ("camoufox", pkg),
        ("camoufox.browser_pin", bp),
        ("camoufox.multiversion", mv),
    ):
        monkeypatch.setitem(sys.modules, name, mod)


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


def test_non_executable_pinned_engine_is_repaired_by_the_stack_script(cache, monkeypatch):
    """camoufox writes version.json, then runs chmod -R 755 (multiversion.py:450-454);
    a fetch killed in between leaves this state. The provisioning script finishes
    that chmod (provision._engine_step), so the hint names the script, never a fetch,
    which skips a directory that already has version.json."""
    _versions(monkeypatch, camoufox="0.5.7")
    broken = _installed(cache, executable=False)
    _fake_camoufox(monkeypatch, installed=[broken])
    detail = engine.resolve_camoufox_engine().detail
    assert "scripts/install_browser_stack.sh" in detail
    assert "camoufox fetch" not in detail


def test_corrupt_pinned_engine_is_repaired_by_the_stack_script(cache, monkeypatch):
    """The script removes a corrupt pinned directory and fetches it again, once."""
    _versions(monkeypatch, camoufox="0.5.7")
    broken = _installed(cache, ini=False)
    _fake_camoufox(monkeypatch, installed=[broken])
    detail = engine.resolve_camoufox_engine().detail
    assert "scripts/install_browser_stack.sh" in detail
    assert "camoufox fetch" not in detail


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
        OSError("permission denied"),
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
        monkeypatch.setitem(sys.modules, "camoufox", types.ModuleType("camoufox"))
        monkeypatch.setitem(sys.modules, "camoufox.browser_pin", None)
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


def test_legacy_engine_needs_no_fontconfig(cache, monkeypatch, tmp_path, linux):
    """0.4 sets FONTCONFIG_PATH without checking it exists (utils.py:71)."""
    binary = _kind_engine("legacy", cache, monkeypatch, tmp_path, fontconfig=())
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.READY and status.path == binary


def test_fontconfig_is_not_required_off_linux(cache, monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "_READS_FONTCONFIG", False)
    _kind_engine("versioned", cache, monkeypatch, tmp_path, fontconfig=())
    assert engine.resolve_camoufox_engine().kind == engine.READY


def test_corrupt_pinned_engine_missing_assets_names_the_stack_script(
    cache, monkeypatch, tmp_path, linux
):
    _kind_engine("versioned", cache, monkeypatch, tmp_path, properties=False)
    detail = engine.resolve_camoufox_engine().detail
    assert "scripts/install_browser_stack.sh" in detail and "camoufox fetch" not in detail


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
def test_without_a_file_list_the_version_decides_the_pin_cut(cache, monkeypatch, tmp_path, ver, refused):
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


def test_missing_pinned_engine_names_the_stack_script(cache, monkeypatch):
    """On camoufox 0.5.7+ the provisioning script is the one repair: it marks a
    pre-0.5 root engine with .0.5_FLAG before fetching (0.5's fetch deletes the
    whole cache without it, __main__.py:259-263), then fetches the pin."""
    _versions(monkeypatch, camoufox="0.5.7")
    _engine_dir(cache)  # a pre-0.5 root engine, no flag
    _fake_camoufox(monkeypatch, installed=[])
    status = engine.resolve_camoufox_engine()
    assert status.kind == engine.NOT_INSTALLED
    assert "tell the user" in status.detail
    assert "scripts/install_browser_stack.sh" in status.detail
    assert "camoufox fetch" not in status.detail


def test_stack_step_names_the_checkout_script_quoted(monkeypatch, tmp_path):
    root = tmp_path / "x; rm -rf ~"
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(root))
    step = engine.stack_step()
    assert step.startswith("tell the user")
    assert f"'{root}/scripts/install_browser_stack.sh'" in step


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
