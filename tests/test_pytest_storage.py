"""Real framework caches stay test-owned, persistent and observable (#4193)."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CAPTURE = '''
import json
@pytest.hookimpl(specname="pytest_terminal_summary", trylast=True)
def pytest_storage_capture(terminalreporter):
    from tests import _live_writes as audit
    state = audit._S
    Path(os.environ["STORAGE_SNAPSHOT"]).write_text(json.dumps({
        "installed": state.installed, "disarmed": state.disarmed,
        "faults": state.faults, "unreported": state.unreported,
        "workers": sorted(state.merged_workers), "rows": list(state.hits),
        "source": audit.__file__,
    }))
'''
EARLY = '''
from hypothesis.configuration import storage_directory
EARLY_STORAGE = storage_directory("examples", intent_to_write=False).path
'''
INNER = '''
import os
from pathlib import Path
from hypothesis import given, settings, strategies as st
from hypothesis.configuration import storage_directory
from hypothesis.database import DirectoryBasedExampleDatabase
from .conftest import EARLY_STORAGE

@settings(max_examples=3, deadline=None)
@given(st.integers(-2, 2))
def test_property(value):
    assert value * value >= 0

def test_storage(pytestconfig):
    expected = Path(os.environ["STORAGE_EXPECTED"])
    assert EARLY_STORAGE == expected / "examples"
    assert storage_directory("constants", intent_to_write=False).path == expected / "constants"
    previous = os.environ["STORAGE_ITERATION"] == "second"
    assert pytestconfig.cache.get("storage/present", False) == previous
    pytestconfig.cache.set("storage/present", True)
    assert settings.default.derandomize == bool(os.environ.get("CI"))
    assert (settings.default.database is None) == bool(os.environ.get("CI"))
    db = settings.default.database or DirectoryBasedExampleDatabase(expected / "examples")
    assert (b"value" in list(db.fetch(b"storage-probe"))) == previous
    db.save(b"storage-probe", b"value")
    assert b"value" in list(db.fetch(b"storage-probe"))
    directory = storage_directory("constants", intent_to_write=False)
    directory.create_if_missing()
    (directory.path / "probe").write_bytes(b"constant")

def test_observer_control():
    Path(os.environ["STORAGE_SENTINEL"]).write_bytes(b"deliberate observed write")
'''


def _project(tmp_path):
    project = tmp_path / "checkout with spaces"
    tests = project / "tests"
    tests.mkdir(parents=True)
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    # Copy the actual branch configuration and plugins, not a reconstructed
    # placement rule. The source symlink supplies unchanged autouse fixtures.
    (project / "src").symlink_to(ROOT / "src", target_is_directory=True)
    (project / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts/disk_reclaim.py", project / "scripts/disk_reclaim.py")
    for source in (ROOT / "tests").glob("_*.py"):
        shutil.copy2(source, tests / source.name)
    (tests / "conftest.py").write_text((ROOT / "tests/conftest.py").read_text() + CAPTURE)
    shutil.copy2(ROOT / "pyproject.toml", project / "pyproject.toml")
    early = tests / "testearly"
    early.mkdir()
    (early / "__init__.py").touch()
    (early / "conftest.py").write_text(EARLY)
    (early / "test_storage_inner.py").write_text(INNER)
    # Hypothesis detects many CI markers, not only CI. Own the child's
    # environment rather than duplicating that library's changing marker list.
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT")
           if key in os.environ}
    env.update(GENESIS_PYTEST_LOCK="0", GENESIS_BIG_TMP=str(temporary), TMPDIR=str(temporary),
               PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(project),
               GENESIS_ACTIVE_TEST_FILE=str(temporary / "active.txt"),
               GENESIS_LIVE_WRITES_REPORT=str(temporary / "rows.tsv"),
               STORAGE_SNAPSHOT=str(temporary / "snapshot.json"),
               STORAGE_SENTINEL=str(project / "sentinel"),
               STORAGE_EXPECTED=str(project / ".pytest_cache/hypothesis"))
    return project, temporary, env


def _run(project, temporary, env, *options, target="tests/testearly/test_storage_inner.py", expected_exit=0):
    snapshot = Path(env["STORAGE_SNAPSHOT"])
    snapshot.unlink(missing_ok=True)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", target, "--basetemp", str(temporary / "base"), *options],
        cwd=project, env=env, capture_output=True, text=True,
        # Existing suite ceiling is 600s per case; this is the generous outer
        # repository floor, not a new production timeout.
        timeout=7200,
    )
    (temporary / f"{env['STORAGE_ITERATION']}.log").write_text(result.stdout + result.stderr)
    assert result.returncode == expected_exit, result.stdout + result.stderr
    state = json.loads(snapshot.read_text())
    assert state["source"] == str(project / "tests/_live_writes.py")
    assert state["installed"] and not state["disarmed"]
    assert state["faults"] == 0 and state["unreported"] == []
    return state, result.stdout + result.stderr


@pytest.mark.parametrize("ci", [False, True])
@pytest.mark.parametrize("workers", [0, 2])
def test_native_default_storage_is_owned_and_persistent(tmp_path, ci, workers):
    project, temporary, env = _project(tmp_path)
    if ci:
        env["CI"] = "1"
    options = ["-n", str(workers)] if workers else []
    for iteration in ("first", "second"):
        env["STORAGE_ITERATION"] = iteration
        state, output = _run(project, temporary, env, *options)
        assert "3 passed" in output
        assert state["workers"] == (["gw0", "gw1"] if workers else [])
        assert [row[2:] for row in state["rows"]] == [["write", str(project / "sentinel")]]
    assert not (project / ".hypothesis").exists()
    assert (project / ".pytest_cache/pytest/v/storage/present").is_file()
    assert (project / ".pytest_cache/hypothesis/constants/probe").read_bytes() == b"constant"


@pytest.mark.parametrize("relative", [False, True])
def test_explicit_storage_overrides_are_preserved(tmp_path, relative):
    project, temporary, env = _project(tmp_path)
    hypothesis = temporary / "chosen hypothesis"
    # Pytest 9.2.0 fails during startup when cache_dir includes '..' (#15295).
    # Use a relative option within this owned checkout and keep the absolute
    # case outside it. The separate Hypothesis override still uses '../'.
    cache = (project / ".pytest_cache" if relative else temporary) / "chosen pytest"
    env["HYPOTHESIS_STORAGE_DIRECTORY"] = os.path.relpath(hypothesis, project) if relative else str(hypothesis)
    env["STORAGE_EXPECTED"] = env["HYPOTHESIS_STORAGE_DIRECTORY"]
    cache_option = os.path.relpath(cache, project) if relative else str(cache)
    for iteration in ("first", "second"):
        env["STORAGE_ITERATION"] = iteration
        state, output = _run(project, temporary, env, "-o", f"cache_dir={cache_option}")
        assert "3 passed" in output
        assert [row[2:] for row in state["rows"]] == [["write", str(project / "sentinel")]]
    assert not (project / ".pytest_cache/pytest").exists()
    assert not (project / ".pytest_cache/hypothesis").exists()
    assert (hypothesis / "constants/probe").is_file()
    assert (cache / "v/storage/present").is_file()


@pytest.mark.parametrize("empty", [False, True])
def test_hypothesis_storage_with_disabled_pytest_cache(tmp_path, empty):
    project, temporary, env = _project(tmp_path)
    env["STORAGE_ITERATION"] = "first"
    if empty:
        env["HYPOTHESIS_STORAGE_DIRECTORY"] = ""
        env["STORAGE_EXPECTED"] = str(project / ".hypothesis")
    test = project / "tests/testearly/test_empty.py"
    test.write_text('''
import os
from pathlib import Path
from .conftest import EARLY_STORAGE
def test_empty(pytestconfig):
    expected = Path(os.environ["STORAGE_EXPECTED"])
    if expected.name == ".hypothesis":
        assert os.environ["HYPOTHESIS_STORAGE_DIRECTORY"] == ""
    else:
        assert os.environ["HYPOTHESIS_STORAGE_DIRECTORY"] == str(expected)
    assert EARLY_STORAGE == expected / "examples"
    assert not hasattr(pytestconfig, "cache")
''')
    state, output = _run(project, temporary, env, "-p", "no:cacheprovider", target="tests/testearly/test_empty.py")
    assert "1 passed" in output
    if empty:
        assert not (project / ".pytest_cache").exists()
        assert all(".hypothesis" in row[3] for row in state["rows"])
    else:
        assert not (project / ".hypothesis").exists()
        assert state["rows"] == []


def test_last_failed_and_clear_keep_native_cache_behavior(tmp_path):
    project, temporary, env = _project(tmp_path)
    target = "tests/testearly/test_failure.py"
    (project / target).write_text('''
import os
def test_flaky():
    assert os.environ["STORAGE_ITERATION"] != "first", "deliberate first failure"
def test_stable():
    pass
''')
    env["STORAGE_ITERATION"] = "first"
    state, output = _run(project, temporary, env, target=target, expected_exit=1)
    assert "1 failed, 1 passed" in output and state["rows"] == []
    env["STORAGE_ITERATION"] = "second"
    state, output = _run(project, temporary, env, "--lf", target=target)
    assert "1 passed, 1 deselected" in output and state["rows"] == []
    marker = project / ".pytest_cache/hypothesis/keep"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_bytes(b"hypothesis sibling")
    env["STORAGE_ITERATION"] = "clear"
    state, output = _run(project, temporary, env, "--cache-clear", target=target)
    assert "2 passed" in output and state["rows"] == []
    assert marker.read_bytes() == b"hypothesis sibling"


def test_plain_pytest_cache_bootstrap_is_owned(tmp_path):
    project, temporary, env = _project(tmp_path)
    # Hypothesis can create .pytest_cache first and accidentally hide pytest's
    # cold bootstrap. Exercise that writer independently, with no property test.
    (project / "tests/testearly/conftest.py").write_text("")
    target = "tests/testearly/test_plain.py"
    (project / target).write_text('''
import os
import sys
from pathlib import Path
def test_plain(pytestconfig):
    assert "hypothesis" not in sys.modules
    assert not Path(".pytest_cache").exists()
    pytestconfig.cache.set("storage/plain", "saved")
    assert pytestconfig.cache.get("storage/plain", None) == "saved"
    Path(os.environ["STORAGE_SENTINEL"]).write_bytes(b"observed")
''')
    env["STORAGE_ITERATION"] = "first"
    state, output = _run(project, temporary, env, target=target)
    assert "1 passed" in output
    assert [row[2:] for row in state["rows"]] == [["write", str(project / "sentinel")]]
    assert (project / ".pytest_cache/pytest/v/storage/plain").is_file()
