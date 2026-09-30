"""scripts/lib/venv_matches_pyproject.py: the code-only deploy's dependency gate.

It runs under the test interpreter (the venv it would answer for) with a fake
installed project on PYTHONPATH, so every case is about metadata this test
wrote. Exit codes: 0 match, 1 differs (a reinstall clears it), 2 cannot tell,
3 the interpreter is older than the incoming requires-python.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_scripts._deploy_station import PYPROJECT_OK, REPO
from tests.test_scripts._deploy_station import install_fixture as _install_fixture

GATE = REPO / "scripts" / "lib" / "venv_matches_pyproject.py"
_OPT = "[project.optional-dependencies]\n"


def _gate(root: Path, site: Path, pyproject: str) -> subprocess.CompletedProcess:
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
