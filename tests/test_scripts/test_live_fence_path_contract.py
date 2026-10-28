"""Path controls use the guard and backup's contained, read-only startup prefix."""

import subprocess
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[2] / "scripts/lib/live_system_guard.sh"
BACKUP = LIB.parent.parent / "backup.sh"
REFUSED = "REFUSED (test fence)"
BACKUP_BOUNDARY = "\ndr_lock_open\n"
BACKUP_STOP = 'echo "test fixture: backup before lock" >&2\nexit 125\n'


def guard(tmp_path, *, acting=None, home=None, state=None, roots=None, resolver=None):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home or tmp_path / "private-home")}
    if state is not None:
        env["GENESIS_HOME"] = str(state)
    if roots is not None:
        env["GENESIS_TEST_FENCE_LIVE_ROOTS"] = str(roots)
    if resolver is not None:
        bindir = tmp_path / "bin"
        bindir.mkdir()
        stub = bindir / "realpath"
        stub.write_text("#!/bin/sh\n" + resolver + "\n")
        stub.chmod(0o755)
        env["PATH"] = str(bindir) + ":/usr/bin:/bin"
    return subprocess.run(
        ["/bin/bash", "-c", '. "$1"; refuse_if_test_fenced_live "$2"',
         "path-contract", str(LIB), str(acting or tmp_path / "private-code")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=5,
    )


def assert_refused(result):
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert REFUSED in result.stderr


@pytest.mark.parametrize("kind", ["acting", "home", "state"])
@pytest.mark.parametrize("suffix", ["", "[1]*", "\ninside", "\n", "\n\n"])
@pytest.mark.parametrize("descendant", [False, True])
@pytest.mark.parametrize("existing", [False, True])
def test_protected_path_bytes_are_preserved(tmp_path, kind, suffix, descendant, existing):
    root = tmp_path / ("protected" + suffix)
    candidate = root / "child" if descendant else root
    if existing:
        candidate.mkdir(parents=True)
    assert_refused(guard(tmp_path, roots=root, **{kind: candidate}))


@pytest.mark.parametrize("explicit", [False, True])
def test_default_state_is_protected_independently_of_parent(tmp_path, explicit):
    home = tmp_path / "private-home"
    assert_refused(guard(
        tmp_path, home=home, roots=home / ".genesis",
        state=tmp_path / "private-override" if explicit else None,
    ))


def test_home_is_protected_independently_of_default_state(tmp_path):
    home = tmp_path / "protected-home"
    home.mkdir()
    outside = tmp_path / "outside-state"
    outside.mkdir()
    (home / ".genesis").symlink_to(outside, target_is_directory=True)
    assert_refused(guard(tmp_path, home=home, roots=home))


@pytest.mark.parametrize("kind", ["one-lf", "two-lf", "lexical-parent"])
@pytest.mark.parametrize("relative", [False, True])
@pytest.mark.parametrize("armed", [False, True])
def test_backup_checks_its_actual_startup_state_before_lock(tmp_path, kind, relative, armed):
    fixture = tmp_path / "fixture" / "scripts"
    fixture.mkdir(parents=True)
    (fixture / "lib").symlink_to(LIB.parent, target_is_directory=True)
    source = BACKUP.read_text()
    assert source.count(BACKUP_BOUNDARY) == 1
    prefix = source.partition(BACKUP_BOUNDARY)[0] + "\n"
    script = fixture / "backup.sh"
    script.write_text(prefix + BACKUP_STOP)
    protected = tmp_path / "protected-state"
    if kind == "lexical-parent":
        protected.mkdir()
        physical = tmp_path / "physical" / "child"
        physical.mkdir(parents=True)
        (protected / "link").symlink_to(physical, target_is_directory=True)
        state = str(protected / "link" / "..")
    else:
        state = str(protected) + ("\n" if kind == "one-lf" else "\n\n")
    if relative:
        state = str(Path(state).relative_to(tmp_path))
    home = tmp_path / "private-home"
    home.mkdir()
    log = tmp_path / "refusals.log"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "GENESIS_HOME": state,
           "GENESIS_DIR": str(tmp_path / "private-code"), "GENESIS_TEST_FENCE_LOG": str(log)}
    if armed:
        env["GENESIS_TEST_FENCE_LIVE_ROOTS"] = str(protected)
    # Independently require the exact current prefix and an unconditional stop:
    # no missing guard can turn this child into a lock, backup or service action.
    assert script.read_text() == BACKUP.read_text().partition(BACKUP_BOUNDARY)[0] + "\n" + BACKUP_STOP
    assert "\ndr_lock_open\n" not in script.read_text()
    result = subprocess.run(["/bin/bash", str(script)], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=5)
    if armed:
        assert result.returncode == 97, (result.stdout, result.stderr)
        assert REFUSED in result.stderr and REFUSED in log.read_text()
    else:
        assert result.returncode == 125 and "test fixture: backup before lock" in result.stderr
        assert not log.exists()
    actual_state = Path(state) if Path(state).is_absolute() else tmp_path / state
    assert not (actual_state / "locks").exists()
    assert not (protected / "backup_status.json").exists()


@pytest.mark.parametrize("kind", ["acting", "home", "state"])
@pytest.mark.parametrize("symlink", [False, True])
@pytest.mark.parametrize("armed", [False, True])
def test_canonical_colon_paths_refuse_only_when_armed(tmp_path, kind, symlink, armed):
    target = tmp_path / "private:state"
    target.mkdir()
    candidate = target
    if symlink:
        candidate = tmp_path / "alias"
        candidate.symlink_to(target, target_is_directory=True)
    result = guard(tmp_path, roots=tmp_path / "unrelated" if armed else None,
                   **{kind: candidate})
    if armed:
        assert_refused(result)
    else:
        assert result.returncode == 0 and result.stderr == ""


@pytest.mark.parametrize("kind", ["acting", "home", "state"])
@pytest.mark.parametrize("name", ["-candidate", "--bad-option"])
def test_relative_dash_operand_is_a_path(tmp_path, kind, name):
    assert_refused(guard(tmp_path, roots=tmp_path / name, **{kind: name}))


@pytest.mark.parametrize("state", ["~synthetic-user/state", "~nobody", "~literal"])
@pytest.mark.parametrize("armed", [False, True])
def test_a_tilde_user_state_is_unsupported_only_when_armed(tmp_path, state, armed):
    result = guard(tmp_path, state=state, roots=tmp_path / "unrelated" if armed else None)
    if armed:
        assert_refused(result)
        assert "unsupported tilde-form GENESIS_HOME" in result.stderr
    else:
        assert result.returncode == 0 and result.stderr == ""


@pytest.mark.parametrize("state", ["~", "~/state"])
def test_a_home_tilde_state_is_judged_in_both_expansions(tmp_path, state):
    """Backup reads "~/x" as HOME/x and the lock path reads it literally from
    the cwd (#3438): either one inside a listed root refuses, neither passes."""
    home = tmp_path / "private-home"
    tail = state[2:]
    against_home = home / tail if tail else home
    literal = tmp_path / state
    assert guard(tmp_path, home=home, state=state, roots=tmp_path / "unrelated").returncode == 0
    for root in (against_home, literal):
        assert_refused(guard(tmp_path, home=home, state=state, roots=root))


@pytest.mark.parametrize("home", [None, ""])
def test_a_home_tilde_state_without_home_is_refused(tmp_path, home):
    env = {"PATH": "/usr/bin:/bin", "GENESIS_HOME": "~/state",
           "GENESIS_TEST_FENCE_LIVE_ROOTS": str(tmp_path / "unrelated")}
    if home is not None:
        env["HOME"] = home
    result = subprocess.run(
        ["/bin/bash", "-c", '. "$1"; refuse_if_test_fenced_live "$2"',
         "path-contract", str(LIB), str(tmp_path / "private-code")],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=5,
    )
    assert_refused(result)
    assert "tilde GENESIS_HOME with no HOME" in result.stderr


def test_a_home_tilde_state_with_a_newline_terminated_home_is_refused(tmp_path):
    """Backup's command substitution strips HOME's trailing newline, so "~"
    would name the listed root itself while this guard resolved another path."""
    live = tmp_path / "live"
    live.mkdir()
    result = guard(tmp_path, home=str(live) + "\n", state="~", roots=live)
    assert_refused(result)
    assert "newline-terminated HOME" in result.stderr


@pytest.mark.parametrize("resolver", ["exit 7", "exit 0", "printf /private", "printf '\\n'", "printf 'relative\\n'"])
@pytest.mark.parametrize("armed", [False, True])
def test_unprovable_resolution_refuses_only_when_armed(tmp_path, resolver, armed):
    result = guard(tmp_path, resolver=resolver,
                   roots=tmp_path / "unrelated" if armed else None)
    if armed:
        assert_refused(result)
    else:
        assert result.returncode == 0 and result.stderr == ""


@pytest.mark.parametrize("suffix", ["", "[1]*", "\ninside", "\n", "\n\n"])
def test_sibling_prefix_is_not_a_descendant(tmp_path, suffix):
    root = tmp_path / ("protected" + suffix)
    result = guard(tmp_path, acting=str(root) + "-sibling", roots=root)
    assert result.returncode == 0 and result.stderr == ""
