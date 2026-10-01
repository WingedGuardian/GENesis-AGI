"""scripts/lib/venv_matches_pyproject.py: the code-only deploy's dependency gate.

It runs under the test interpreter (the venv it would answer for) with a fake
installed project on PYTHONPATH, so every case is about metadata this test
wrote. Exit codes: 0 match, 1 differs (a reinstall clears it), 2 cannot tell,
3 the interpreter is older than the incoming requires-python.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from tests.test_scripts._deploy_station import BUILD_CONFIG, PYPROJECT_OK, REPO
from tests.test_scripts._deploy_station import install_fixture as _install_fixture

GATE = REPO / "scripts" / "lib" / "venv_matches_pyproject.py"
_OPT = "[project.optional-dependencies]\n"


def _gate(
    root: Path, site: Path, pyproject: str, *, raw: bool = False
) -> subprocess.CompletedProcess:
    """Run the gate on *pyproject*. Unless *raw*, the build configuration the
    fixture's install records (BUILD_CONFIG) is prepended when the text carries
    none, so a case about requirements is not refused for its layout."""
    if not raw and "[build-system]" not in pyproject:
        pyproject = BUILD_CONFIG + pyproject
    return subprocess.run(
        [sys.executable, str(GATE), str(root)],
        input=pyproject,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "PYTHONPATH": str(site)},
    )


@pytest.mark.parametrize(
    ("pyproject", "installed", "rc"),
    [
        # Equal: the install describes this pyproject.
        ('[project]\nname = "fixture"\ndependencies = ["packaging>=20"]\n', ("packaging>=20",), 0),
        # A changed specifier, an added and a removed requirement all differ.
        ('[project]\nname = "fixture"\ndependencies = ["packaging>=21"]\n', ("packaging>=20",), 1),
        ('[project]\nname = "fixture"\ndependencies = ["packaging", "httpx"]\n', ("packaging",), 1),
        ('[project]\nname = "fixture"\ndependencies = []\n', ("packaging",), 1),
        # Optional groups are recorded with the extra folded into the marker.
        (
            '[project]\nname = "fixture"\ndependencies = []\n' + _OPT + 'voice = ["packaging"]\n',
            ('packaging; extra == "voice"',),
            0,
        ),
        (
            '[project]\nname = "fixture"\ndependencies = []\n'
            + _OPT
            + 'voice = ["packaging; python_version >= \\"3\\""]\n',
            ('packaging; python_version >= "3" and extra == "voice"',),
            0,
        ),
        # A group's requirement added upstream is a difference even though no
        # installed package of that group exists (the old checker skipped it).
        (
            '[project]\nname = "fixture"\ndependencies = []\n' + _OPT + 'voice = ["zz-absent"]\n',
            (),
            1,
        ),
        # A direct-URL requirement compares like any other: equal passes. It is in
        # an optional group, whose packages are never checked for presence.
        (
            '[project]\nname = "fixture"\ndependencies = []\n'
            + _OPT
            + 'vcs = ["foo @ https://example.invalid/foo.whl"]\n',
            ('foo @ https://example.invalid/foo.whl ; extra == "vcs"',),
            0,
        ),
        # Order and spelling do not matter; the parsed requirement does.
        (
            '[project]\nname = "fixture"\ndependencies = ["Packaging >= 20", "pytest"]\n',
            ("pytest", "packaging>=20"),
            0,
        ),
        # Cannot tell: unreadable input, no name, the project not installed.
        ("not toml [[[", (), 2),
        ("[project]\ndependencies = []\n", (), 2),
        ('[project]\nname = "not-installed-here"\ndependencies = []\n', (), 2),
    ],
)
def test_the_gate_compares_the_install_with_the_pyproject(tmp_path, pyproject, installed, rc):
    site = tmp_path / "site"
    _install_fixture(site, tmp_path, installed)
    r = _gate(tmp_path, site, pyproject)
    assert r.returncode == rc, (r.stdout, r.stderr)


@pytest.mark.parametrize(
    ("dependency", "needle"),
    [
        ("zz-absent-package", "missing: zz-absent-package"),
        ("packaging>=9999", "unsatisfied: packaging"),
    ],
    ids=["removed-after-install", "downgraded-after-install"],
)
def test_equal_metadata_with_a_base_requirement_not_present_is_refused(
    tmp_path, dependency, needle
):
    """Equal metadata proves the install DESCRIBES this pyproject, not that what
    it installed is still there (a package removed or downgraded since)."""
    site = tmp_path / "site"
    _install_fixture(site, tmp_path, (dependency,))
    r = _gate(tmp_path, site, f'[project]\nname = "fixture"\ndependencies = ["{dependency}"]\n')
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert needle in r.stdout, r.stdout
    assert "not installed:" not in r.stdout, "the metadata is equal; only presence fails"


def test_a_base_requirement_whose_marker_does_not_apply_is_not_checked(tmp_path):
    dep = 'zz-absent-package; python_version < "3"'
    site = tmp_path / "site"
    _install_fixture(site, tmp_path, (dep,))
    r = _gate(tmp_path, site, f"[project]\nname = \"fixture\"\ndependencies = ['{dep}']\n")
    assert r.returncode == 0, (r.stdout, r.stderr)


def test_an_interpreter_older_than_requires_python_exits_3(tmp_path):
    """A reinstall into this venv cannot fix it, so the caller must not send it
    to update.sh."""
    site = tmp_path / "site"
    _install_fixture(site, tmp_path, requires_python=">=3.99")
    r = _gate(
        tmp_path,
        site,
        '[project]\nname = "fixture"\nrequires-python = ">=3.99"\ndependencies = ["packaging"]\n',
    )
    assert r.returncode == 3, (r.stdout, r.stderr)
    assert "requires-python" in r.stdout and ">=3.99" in r.stdout, r.stdout


@pytest.mark.parametrize(
    ("kwargs", "source", "needle"),
    [
        ({}, "other", "is installed from"),
        ({"editable": False}, "root", "not an editable install"),
        ({"requires_python": ">=3.12"}, "root", "requires-python"),
    ],
    ids=["another-checkout", "not-editable", "requires-python"],
)
def test_the_gate_checks_where_and_how_it_was_installed(tmp_path, kwargs, source, needle):
    """The restarted server imports whatever the install points at: a worktree's
    src/, or a wheel's copy, is not this checkout's code."""
    root = tmp_path / "root"
    root.mkdir()
    site = tmp_path / "site"
    _install_fixture(site, root if source == "root" else tmp_path / "other", **kwargs)
    r = _gate(root, site, PYPROJECT_OK)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert needle in r.stdout, r.stdout


@pytest.mark.parametrize("direct_url", ["[]", '{"url": "file:///x", "dir_info": []}'])
def test_the_gate_cannot_tell_from_a_malformed_direct_url(tmp_path, direct_url):
    site = tmp_path / "site"
    _install_fixture(site, tmp_path)
    (site / "fixture-0.0.0.dist-info" / "direct_url.json").write_text(direct_url)
    r = _gate(tmp_path, site, PYPROJECT_OK)
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "Traceback" not in r.stderr


def test_an_unparsable_requires_python_cannot_tell(tmp_path):
    site = tmp_path / "site"
    _install_fixture(site, tmp_path)
    r = _gate(tmp_path, site, '[project]\nname = "fixture"\nrequires-python = "not a spec"\n')
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "Traceback" not in r.stderr


# Entry points (Devin, #2557): a console command or plugin declared in the
# pyproject gets its shim or its entry_points.txt line only from a reinstall, so
# a code-only deploy of a change to one must be refused like a requirements change.
_SCRIPTS = '\n[project.scripts]\ngenesis-x = "genesis.cli:main"\n'
_PLUGIN = '\n[project.entry-points."genesis.plugins"]\nx = "genesis.x:Plugin"\n'


@pytest.mark.parametrize(
    ("pyproject", "installed", "rc", "needle"),
    [
        (PYPROJECT_OK + _SCRIPTS, None, 1, "genesis-x"),
        (PYPROJECT_OK + _SCRIPTS, {"console_scripts": {"genesis-x": "genesis.cli:main"}}, 0, ""),
        (PYPROJECT_OK, {"console_scripts": {"genesis-x": "genesis.cli:main"}}, 1, "genesis-x"),
        (
            PYPROJECT_OK + _SCRIPTS,
            {"console_scripts": {"genesis-x": "genesis.old:main"}},
            1,
            "genesis-x",
        ),
        (PYPROJECT_OK + _PLUGIN, None, 1, "genesis.plugins"),
        (PYPROJECT_OK + _PLUGIN, {"genesis.plugins": {"x": "genesis.x:Plugin"}}, 0, ""),
    ],
    ids=[
        "a-new-command",
        "the-same-command",
        "a-dropped-command",
        "a-retargeted-command",
        "a-new-plugin-group",
        "the-same-plugin",
    ],
)
def test_the_gate_compares_entry_points(tmp_path, pyproject, installed, rc, needle):
    site = tmp_path / "site"
    _install_fixture(site, tmp_path, entry_points=installed)
    r = _gate(tmp_path, site, pyproject)
    assert r.returncode == rc, (r.stdout, r.stderr)
    assert needle in r.stdout, r.stdout


def test_a_malformed_entry_point_table_cannot_tell(tmp_path):
    site = tmp_path / "site"
    _install_fixture(site, tmp_path)
    r = _gate(tmp_path, site, PYPROJECT_OK + 'scripts = "not a table"\n')
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "Traceback" not in r.stderr
    assert "cannot read the incoming pyproject.toml" in r.stderr, r.stderr
    assert "build configuration" not in r.stderr, "refused for the entry points, not the layout"


# Package layout (#2600): the editable install is a .pth naming the package root
# the pyproject had at install time, and only a reinstall rewrites it. A pyproject
# that moves that root must be refused, or the restarted server imports the old
# location. Measured with setuptools 84 (the version the live install records):
# `packages.find` with one `where` writes that directory as the .pth's one line.
_PROJECT = '[project]\nname = "fixture"\ndependencies = ["packaging"]\n'
_BACKEND = (
    '[build-system]\nrequires = ["setuptools>=64"]\nbuild-backend = "setuptools.build_meta"\n'
)


def _layout(find: str, backend: str = _BACKEND) -> str:
    return backend + "\n[tool.setuptools.packages.find]\n" + find + "\n" + _PROJECT


@pytest.mark.parametrize(
    ("installed_root", "pyproject", "rc", "needle"),
    [
        # The control: the install's .pth names the incoming root.
        ("src", _layout('where = ["src"]'), 0, ""),
        # The same root, spelled differently, is the same directory.
        ("src", _layout('where = ["./src"]'), 0, ""),
        # include/exclude/namespaces select packages under the root; the .pth
        # stays the root (measured), so they do not move it.
        ("src", _layout('where = ["src"]\nexclude = ["tests*"]\nnamespaces = false'), 0, ""),
        # The issue's case: only `where` changed.
        ("src", _layout('where = ["lib"]'), 1, "package root"),
        # And the reverse: installed when the root was lib/, the tree says src/.
        ("lib", _layout('where = ["src"]'), 1, "package root"),
    ],
    ids=["same-root", "same-root-spelled-differently", "selection-only", "moved", "moved-back"],
)
def test_the_gate_compares_the_package_root(tmp_path, installed_root, pyproject, rc, needle):
    site = tmp_path / "site"
    _install_fixture(site, tmp_path, pth=installed_root)
    r = _gate(tmp_path, site, pyproject, raw=True)
    assert r.returncode == rc, (r.stdout, r.stderr)
    assert needle in r.stdout, r.stdout
    if rc:
        assert str(tmp_path.resolve() / installed_root) in r.stdout, "names what is installed"


@pytest.mark.parametrize(
    ("pth", "needle"),
    [
        (
            "import __editable___fixture_0_0_0_finder; __editable___fixture_0_0_0_finder.install()",
            # Not bare "finder": the finder module's own name carries that word, so
            # a .pth misread as a path would match it too.
            "loads a finder, not a package root",
        ),
        (None, "no editable .pth"),
    ],
    ids=["finder-install", "no-pth"],
)
def test_an_install_the_gate_cannot_read_a_root_from_is_refused(tmp_path, pth, needle):
    """A finder maps packages, not a root, and an install that records no .pth
    names no root at all: neither shows the incoming root is what imports. A
    reinstall of a one-`where` layout writes the plain .pth (measured)."""
    site = tmp_path / "site"
    _install_fixture(site, tmp_path, pth=pth)
    r = _gate(tmp_path, site, PYPROJECT_OK)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert needle in r.stdout, r.stdout


@pytest.mark.parametrize(
    "pyproject",
    [
        # No build configuration at all: setuptools auto-discovers the layout.
        _PROJECT,
        # A backend other than setuptools lays the install out its own way.
        _layout('where = ["src"]', backend='[build-system]\nbuild-backend = "hatchling.build"\n'),
        _layout('where = ["src"]', backend="[build-system]\nrequires = []\n"),
        # Two roots: setuptools switches to a finder (measured).
        _layout('where = ["src", "lib"]'),
        _layout("where = []"),
        # The checkout itself: setuptools installs that through a finder (measured).
        _layout('where = ["."]'),
        _layout('where = ["./"]'),
        # package-dir, or any other [tool.setuptools] key, can move a package.
        _BACKEND
        + '\n[tool.setuptools.packages.find]\nwhere = ["src"]\n'
        + '\n[tool.setuptools.package-dir]\ngenesis = "lib/genesis"\n\n'
        + _PROJECT,
        _BACKEND + '\n[tool.setuptools]\npackages = ["genesis"]\n\n' + _PROJECT,
        _layout('where = ["src"]\nunknown-key = 1'),
    ],
    ids=[
        "no-build-config",
        "another-backend",
        "no-backend",
        "two-roots",
        "no-root",
        "root-itself",
        "root-itself-slash",
        "package-dir",
        "explicit-packages",
        "unknown-find-key",
    ],
)
def test_a_layout_the_gate_cannot_certify_cannot_tell(tmp_path, pyproject):
    """Outside the one shape it can compare, the gate refuses rather than model
    setuptools: a code-only deploy of such a tree needs update.sh."""
    site = tmp_path / "site"
    _install_fixture(site, tmp_path)
    r = _gate(tmp_path, site, pyproject, raw=True)
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "build configuration" in r.stderr, r.stderr
    assert "Traceback" not in r.stderr


def test_the_interpreter_check_still_comes_before_the_layout(tmp_path):
    """Exit 3 must survive an uncertifiable layout: a reinstall cannot fix it."""
    site = tmp_path / "site"
    _install_fixture(site, tmp_path, requires_python=">=3.99")
    r = _gate(
        tmp_path,
        site,
        _PROJECT.replace("dependencies", 'requires-python = ">=3.99"\ndependencies'),
        raw=True,
    )
    assert r.returncode == 3, (r.stdout, r.stderr)


def test_a_checkout_reached_through_a_symlink_is_the_same_root(tmp_path):
    """setuptools records the resolved path in the .pth; the deploy passes the checkout
    as it is spelled. The same directory must compare equal either way."""
    real = tmp_path / "real"
    (real / "src").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    site = tmp_path / "site"
    _install_fixture(site, real)
    r = _gate(link, site, PYPROJECT_OK)
    assert r.returncode == 0, (r.stdout, r.stderr)


def test_a_pth_naming_the_checkout_by_its_symlinked_path_is_the_same_root(tmp_path):
    """The other spelling: the .pth names the checkout through the symlink the deploy
    was given. Read lexically, it is rebased onto the resolved checkout root, so the
    same directory still compares equal."""
    real = tmp_path / "real"
    (real / "src").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    site = tmp_path / "site"
    _install_fixture(site, real)
    (site / "__editable__.fixture-0.0.0.pth").write_text(str(link / "src") + "\n")
    r = _gate(link, site, PYPROJECT_OK)
    assert r.returncode == 0, (r.stdout, r.stderr)


def test_a_symlink_in_the_old_tree_does_not_stand_in_for_the_incoming_root(tmp_path):
    """Round-1 review (Devin, Codex): the deploy asks before it merges, so a symlink
    in the checkout belongs to the OLD tree. Here the old tree has `alias -> src`,
    the install's .pth names src, and the incoming pyproject sets where = alias
    (the incoming commit replaces the symlink with a package directory). Resolving
    `alias` against the old tree gives src and would pass; after the merge the
    server would still import src. The root is compared by name, so it is refused."""
    (tmp_path / "src").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "src", target_is_directory=True)
    site = tmp_path / "site"
    _install_fixture(site, tmp_path, pth="src")
    r = _gate(tmp_path, site, _layout('where = ["alias"]'), raw=True)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "package root" in r.stdout, r.stdout
    assert str(tmp_path.resolve() / "alias") in r.stdout, r.stdout


def test_an_uncertifiable_layout_still_prints_the_other_differences(tmp_path):
    """Exit 2 for the layout, and the dependency difference is still reported:
    the refusal is an inventory, not the first reason found."""
    site = tmp_path / "site"
    _install_fixture(site, tmp_path)
    r = _gate(tmp_path, site, _PROJECT.replace('"packaging"', '"packaging>=9999"'), raw=True)
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "build configuration" in r.stderr, r.stderr
    assert "not installed: packaging >=9999" in r.stdout, r.stdout


def test_a_pth_line_is_read_as_site_py_reads_it(tmp_path):
    """site.py strips nothing from the left: a line with leading blanks is a
    directory of that name under site-packages, not the root it looks like."""
    site = tmp_path / "site"
    _install_fixture(site, tmp_path)
    pth = site / "__editable__.fixture-0.0.0.pth"
    pth.write_text("  " + pth.read_text())
    r = _gate(tmp_path, site, PYPROJECT_OK)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "package root" in r.stdout, r.stdout


def test_an_undecodable_pth_cannot_tell(tmp_path):
    site = tmp_path / "site"
    _install_fixture(site, tmp_path)
    (site / "__editable__.fixture-0.0.0.pth").write_bytes(b"\xff\xfe\xfa\n")
    r = _gate(tmp_path, site, PYPROJECT_OK)
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "Traceback" not in r.stderr, r.stderr


def test_this_repositorys_pyproject_is_a_layout_the_gate_can_certify():
    """A [tool.setuptools] change here that leaves the one shape the gate reads
    makes every code-only deploy refuse, on every install, and only a deploy
    would show it. Fail here instead, where the change is made."""
    spec = importlib.util.spec_from_file_location("_venv_gate_under_test", GATE)
    assert spec is not None and spec.loader is not None
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    doc = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert gate._incoming_root(doc, REPO) == (REPO / "src").resolve(), (
        "pyproject.toml's build configuration left the shape "
        "scripts/lib/venv_matches_pyproject.py can certify: teach the gate the new shape"
    )
