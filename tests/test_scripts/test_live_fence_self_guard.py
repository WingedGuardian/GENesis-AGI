"""The deploy scripts' own test-fence guard (rework of #3314, PR A).

``refuse_if_test_fenced_live`` in ``scripts/lib/live_system_guard.sh`` is the
self-guard half of the live fence: when the suite-wide fence exports
``GENESIS_TEST_FENCE_LIVE_ROOTS`` (a colon-separated list of real paths no
test may let a deploy script act on), each deploy script refuses — exit 97 —
as its first statement after resolving the root it acts on, before any side
effect. Because the check runs inside the script after bash's own expansion,
it holds in every descendant however the script was launched — the launch
forms the deleted argv/shell-string modelling could not refuse.

These tests run the REAL scripts against a FIXTURE tree: a directory holding
copies of the script and the guard library, named in
``GENESIS_TEST_FENCE_LIVE_ROOTS``. With the variable set the script must exit
97 and log the refusal; with it unset the script must take its normal path.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
GUARD_LIB = SCRIPTS / "lib" / "live_system_guard.sh"
REFUSED = "REFUSED (test fence)"
BOOTSTRAP_BOUNDARY = 'bootstrap_refuse_if_server_live "$@" || exit 3\n'
BOOTSTRAP_STOP = 'echo "test fixture: bootstrap startup boundary" >&2\nexit 125\n'


def _bootstrap_startup_source() -> str:
    source = (SCRIPTS / "bootstrap.sh").read_text()
    assert source.count(BOOTSTRAP_BOUNDARY) == 1
    return source.split(BOOTSTRAP_BOUNDARY)[0] + BOOTSTRAP_BOUNDARY + BOOTSTRAP_STOP


def _assert_bootstrap_contained(script: Path) -> None:
    # Independently check the builder's output: changing its extraction point
    # must not change the safety assertion along with it.
    source = (SCRIPTS / "bootstrap.sh").read_text()
    assert source.count(BOOTSTRAP_BOUNDARY) == 1
    prefix, boundary, _rest = source.partition(BOOTSTRAP_BOUNDARY)
    assert script.read_text() == prefix + boundary + BOOTSTRAP_STOP, (
        "bootstrap fixture must end at the startup boundary before installation"
    )

# (script file, extra env mapping the fixture's acting root, argv,
#  expected rc on the normal path, a marker proving the run got past the guard)
# backup.sh/restore.sh act on ${GENESIS_DIR:-$HOME/genesis}, not their checkout.
# A bare "rc != 97" would also pass on a fixture that failed to load the script
# at all, so the normal-path test asserts each script's own observable outcome.
DEPLOY_SCRIPTS = [
    ("update.sh", {}, ["--definitely-bad"], 1, "unknown argument"),
    # Arg parsing sits after the lib sourcing; "bogus-mode" reaches its own
    # `die` usage line.
    ("deploy_code_only.sh", {}, ["bogus-mode"], 1, "unknown argument"),
    # GENESIS_DIR names the fixture, which has no data/genesis.db: the first
    # real step fails cleanly after the guard has already run.
    ("backup.sh", {"GENESIS_DIR": "{fixture}"}, [], 1, "SQLite source database not found"),
    ("restore.sh", {"GENESIS_DIR": "{fixture}"}, ["--help"], 0, "Usage:"),
    # Exercise the actual startup's existing live-server refusal. The fixture
    # ends immediately after this guard, before recovery and installation.
    ("bootstrap.sh", {}, [], 3, "REFUSED: genesis-server is RUNNING"),
    # The fixture has no deploy_candidates.py: the exec fails inside the engine
    # launch, proving the shell wrapper ran past the guard.
    ("deploy_candidates", {}, ["bogus-command"], 2, "deploy_candidates.py"),
]


def _env(home: Path, fixture: Path | None, log: Path | None, extra: dict,
         *, armed: bool = True) -> dict:
    """A child env: private HOME, fence variables controlled per test."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    # Retain the suite's bus fence alongside its PATH/systemctl fence.
    for key in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
        if key in os.environ:
            env[key] = os.environ[key]
    for key, value in extra.items():
        env[key] = value.format(fixture=str(fixture), home=str(home))
    if armed and fixture is not None:
        env["GENESIS_TEST_FENCE_LIVE_ROOTS"] = str(fixture)
    if log is not None:
        env["GENESIS_TEST_FENCE_LOG"] = str(log)
    return env


@pytest.fixture()
def fixture_tree(tmp_path: Path) -> Path:
    """Real scripts/libs; bootstrap is structurally limited to its startup."""
    tree = tmp_path / "fixture"
    (tree / "scripts").mkdir(parents=True)
    (tree / "scripts" / "lib").symlink_to(SCRIPTS / "lib", target_is_directory=True)
    for name, _extra, _argv, _rc, _marker in DEPLOY_SCRIPTS:
        target = tree / "scripts" / name
        if name == "bootstrap.sh":
            target.write_text(_bootstrap_startup_source())
        else:
            shutil.copy2(SCRIPTS / name, target)
    return tree


def _run(argv: list[str], env: dict, **kw) -> subprocess.CompletedProcess:
    # Check before launch: an accidentally restored full bootstrap is never run.
    for arg in argv:
        if Path(arg).name == "bootstrap.sh":
            _assert_bootstrap_contained(Path(arg))
    return subprocess.run(
        argv, env=env, capture_output=True, text=True, timeout=60, **kw
    )


def test_bootstrap_fixture_is_structurally_contained(fixture_tree):
    _assert_bootstrap_contained(fixture_tree / "scripts" / "bootstrap.sh")


@pytest.mark.parametrize("name", ["backup.sh", "restore.sh"])
def test_normal_path_uses_private_fixture_root(fixture_tree, tmp_path, monkeypatch, name):
    case = next(case for case in DEPLOY_SCRIPTS if case[0] == name)

    def inspect_only(argv, env, **kw):
        assert env["GENESIS_DIR"] == str(fixture_tree)
        assert "GENESIS_TEST_FENCE_LIVE_ROOTS" not in env
        return subprocess.CompletedProcess(argv, case[3], stdout=case[4], stderr="")

    monkeypatch.setitem(globals(), "_run", inspect_only)
    test_script_unset_takes_normal_path(fixture_tree, tmp_path, *case)


# ── per-script refusal on the fixture ────────────────────────────────


@pytest.mark.parametrize(
    ("name", "extra", "argv", "normal_rc", "marker"), DEPLOY_SCRIPTS
)
def test_script_refuses_fenced_root(fixture_tree, tmp_path, name, extra, argv,
                                    normal_rc, marker):
    home = tmp_path / f"home-{name}"
    home.mkdir()
    log = tmp_path / f"hits-{name}.log"
    env = _env(home, fixture_tree, log, extra)
    proc = _run(["bash", str(fixture_tree / "scripts" / name), *argv], env)
    assert proc.returncode == 97, (
        f"{name} must exit 97 on a fenced root:\n{proc.stdout}\n{proc.stderr}"
    )
    assert REFUSED in proc.stderr
    assert REFUSED in log.read_text()


@pytest.mark.parametrize(
    ("name", "extra", "argv", "normal_rc", "marker"), DEPLOY_SCRIPTS
)
def test_script_unset_takes_normal_path(fixture_tree, tmp_path, name, extra,
                                        argv, normal_rc, marker):
    """Variable unset: the guard is a no-op and the script takes its normal
    path — proven by the script's OWN outcome (rc + marker), not merely the
    absence of a refusal, so a fixture that failed to load the script could
    not pass."""
    home = tmp_path / f"home-normal-{name}"
    home.mkdir()
    env = _env(home, fixture_tree, None, extra, armed=False)
    if name == "bootstrap.sh":
        # Controlled positive live detection, without contacting any service.
        bindir = tmp_path / "live-probe-bin"
        bindir.mkdir()
        for command, rc in (("systemctl", 0), ("pgrep", 1)):
            stub = bindir / command
            stub.write_text(f"#!/bin/sh\nexit {rc}\n")
            stub.chmod(0o755)
        env["PATH"] = str(bindir) + os.pathsep + env["PATH"]
    proc = _run(["bash", str(fixture_tree / "scripts" / name), *argv], env)
    combined = proc.stdout + proc.stderr
    if normal_rc is None:
        assert proc.returncode != 97, combined
    else:
        assert proc.returncode == normal_rc, (
            f"{name} on its normal path should exit {normal_rc}:\n{combined}"
        )
    assert REFUSED not in combined
    if marker:
        assert marker in combined, f"{name} did not reach its normal path:\n{combined}"


def test_empty_variable_takes_normal_path(fixture_tree, tmp_path):
    home = tmp_path / "home-empty"
    home.mkdir()
    env = _env(home, None, None, {})
    env["GENESIS_TEST_FENCE_LIVE_ROOTS"] = ""
    proc = _run(["bash", str(fixture_tree / "scripts" / "update.sh"), "--bad"], env)
    assert proc.returncode == 1  # the usage error, not the fence
    assert "unknown argument" in proc.stderr


def test_refusal_precedes_update_self_copy(fixture_tree, tmp_path):
    """update.sh's guard sits before the copy-to-temp block: a fenced run must
    leave no genesis-update-* temp copy under HOME."""
    home = tmp_path / "home-nocopy"
    (home / "tmp").mkdir(parents=True)
    env = _env(home, fixture_tree, None, {})
    proc = _run(["bash", str(fixture_tree / "scripts" / "update.sh")], env)
    assert proc.returncode == 97
    assert not list((home / "tmp").iterdir()), "the self-copy is a side effect"


# ── the five launch forms, refused by the script itself ──────────────


@pytest.mark.parametrize(
    "form",
    [
        "plain",
        "nested-c",
        "bash-env",
        "rcfile",
        "here-string",
    ],
)
def test_five_launch_forms_refused(fixture_tree, tmp_path, form):
    """Every launch form in the rework spec's table is refused BY THE SCRIPT
    (exit 97), where the old argv/shell-string judge allowed three of them."""
    home = tmp_path / f"home-{form}"
    home.mkdir()
    log = tmp_path / f"hits-{form}.log"
    script = fixture_tree / "scripts" / "update.sh"
    env = _env(home, fixture_tree, log, {})
    if form == "plain":
        argv = ["bash", str(script)]
    elif form == "nested-c":
        argv = ["bash", "-c", f"bash {script}"]
    elif form == "bash-env":
        argv = ["bash", "-c", "true"]
        env["BASH_ENV"] = str(script)
    elif form == "rcfile":
        argv = ["bash", "--rcfile", str(script), "-i"]
    else:  # here-string
        argv = ["bash", "-c", "bash <<< $'bash " + str(script) + "'"]
    proc = _run(argv, env, stdin=subprocess.DEVNULL)
    assert proc.returncode == 97, (
        f"launch form {form} was not refused:\n{proc.stdout}\n{proc.stderr}"
    )
    assert REFUSED in log.read_text()


# ── the guard function's own semantics ───────────────────────────────


def _guard(home: Path, roots: str | None, acting: str, extra_env: dict | None = None):
    env = _env(home, None, None, {})
    if roots is not None:
        env["GENESIS_TEST_FENCE_LIVE_ROOTS"] = roots
    if extra_env:
        env.update(extra_env)
    return _run(
        [
            "bash",
            "-c",
            f'. "{GUARD_LIB}"; refuse_if_test_fenced_live "$1"',
            "guard",
            acting,
        ],
        env,
    )


def test_guard_unset_and_empty_allow(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    assert _guard(home, None, str(tmp_path)).returncode == 0
    assert _guard(home, "", str(tmp_path)).returncode == 0


def test_guard_root_equal_under_and_outside(tmp_path):
    root = tmp_path / "live"
    root.mkdir()
    home = tmp_path / "h"
    home.mkdir()
    inside = _guard(home, str(root), str(root / "scripts"))
    assert inside.returncode == 1
    assert "under or equal" not in inside.stderr and REFUSED in inside.stderr
    outside = _guard(home, str(root), str(tmp_path / "elsewhere"))
    assert outside.returncode == 0


def test_guard_matches_home_and_genesis_home(tmp_path):
    root = tmp_path / "live"
    home = root / "home"  # HOME sits under the live root
    home.mkdir(parents=True)
    proc = _guard(home, str(root), str(tmp_path / "unrelated"))
    assert proc.returncode == 1, proc.stderr
    other = tmp_path / "h2"
    other.mkdir()
    proc = _guard(other, str(root), str(tmp_path / "unrelated"), {"GENESIS_HOME": str(root / ".genesis")})
    assert proc.returncode == 1, proc.stderr


def test_guard_symlinked_and_nonexistent_candidate(tmp_path):
    root = tmp_path / "live"
    (root / "sub").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(root / "sub")
    home = tmp_path / "h"
    home.mkdir()
    assert _guard(home, str(root), str(link)).returncode == 1
    assert _guard(home, str(root), str(root / "not" / "there" / "yet")).returncode == 1


def test_guard_listed_root_with_glob_chars(tmp_path):
    """A listed root whose name holds glob characters compares as itself: the
    colon-split list must not pathname-expand (for-unquoted would turn
    ``live[1]`` into an existing ``live1`` and allow the fenced tree)."""
    bracketed = tmp_path / "live[1]"
    (bracketed / "sub").mkdir(parents=True)
    (tmp_path / "live1").mkdir()  # the path the unquoted expansion would yield
    home = tmp_path / "h"
    home.mkdir()
    proc = _guard(home, str(bracketed), str(bracketed / "sub"))
    assert proc.returncode == 1, proc.stderr
    assert _guard(home, str(bracketed), str(tmp_path / "live1")).returncode == 0


def test_guard_listed_root_with_a_newline(tmp_path):
    """A listed root whose name holds a literal newline still fences: a
    line-oriented split (``read``) would drop everything after the first
    line and allow the fenced tree."""
    newline_root = tmp_path / "live\nroot"
    (newline_root / "sub").mkdir(parents=True)
    home = tmp_path / "h"
    home.mkdir()
    proc = _guard(home, str(newline_root), str(newline_root / "sub"))
    assert proc.returncode == 1, proc.stderr
    assert _guard(home, str(newline_root), str(tmp_path / "other")).returncode == 0


def test_guard_filesystem_root_fences_everything(tmp_path):
    """A listed ``/`` fences every absolute target — the descendant match must
    not degenerate to a ``//`` prefix."""
    home = tmp_path / "h"
    home.mkdir()
    assert _guard(home, "/", "/").returncode == 1
    assert _guard(home, "/", str(tmp_path)).returncode == 1


def test_guard_appends_to_log(tmp_path):
    root = tmp_path / "live"
    root.mkdir()
    home = tmp_path / "h"
    home.mkdir()
    log = tmp_path / "hits.log"
    env = _env(home, None, log, {})
    env["GENESIS_TEST_FENCE_LIVE_ROOTS"] = str(root)
    proc = _run(
        [
            "bash",
            "-c",
            f'. "{GUARD_LIB}"; refuse_if_test_fenced_live "{root}"',
        ],
        env,
    )
    assert proc.returncode == 1
    assert REFUSED in log.read_text()
