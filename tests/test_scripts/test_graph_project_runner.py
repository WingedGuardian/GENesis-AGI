"""The shell runner's EXIT CODES, which nothing else covers.

Every property that matters here lives in bash, so the Python suite around
`graphstore_project` cannot see any of it — an adversarial review named that as
this change's biggest coverage gap, and it was right: the first version of the
runner exited 0 on three failure paths and every Python test still passed.

The distinction these tests defend:

* exit 0  = there was nothing to do, or someone else is doing it. Benign.
* exit 75 = the tick could not run. EX_TEMPFAIL, so the unit goes red and an
            operator can see it.

Collapsing the second into the first is the fail-open the whole design exists
to prevent. A permanent condition — a read-only ``$GENESIS_HOME``, ``flock``
missing from the unit PATH — would otherwise report SUCCESS every hour forever
while the projection silently stopped being rebuilt, and nothing downstream can
notice, because a stale projection and a current one are indistinguishable from
the engine's side.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RUNNER = REPO / "scripts" / "graph_project_runner.sh"

#: EX_TEMPFAIL. The same code the sibling `code_intel_runner.sh` uses for the
#: same class of "this tick could not run".
TEMPFAIL = 75


def _run(*, home: Path, repo: Path, timeout: int = 120) -> subprocess.CompletedProcess:
    env = dict(os.environ, GENESIS_HOME=str(home), GENESIS_REPO_DIR=str(repo))
    # A real venv is never reached by these tests, but the runner checks for one
    # before anything else, so the fixtures below have to provide it.
    return subprocess.run(
        ["bash", str(RUNNER)],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@pytest.fixture
def fake_repo(tmp_path):
    """A repo whose venv python exists and reports success.

    Written as a real executable file: the runner tests `-x`, so a stub that is
    merely present is not enough.
    """
    repo = tmp_path / "repo"
    (repo / ".venv" / "bin").mkdir(parents=True)
    py = repo / ".venv" / "bin" / "python"
    py.write_text("#!/usr/bin/env bash\necho 'stub: projected'\nexit 0\n", encoding="utf-8")
    py.chmod(0o755)
    return repo


def test_an_uncreatable_lock_directory_is_tempfail_not_success(tmp_path, fake_repo):
    """THE REGRESSION THIS FILE EXISTS FOR.

    `$GENESIS_HOME` whose parent is a FILE makes `mkdir -p` fail — standing in
    for the real cases (read-only or full home). This returned 0 before, which
    is an hourly green unit reporting a projection that never happens.
    """
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file\n", encoding="utf-8")

    res = _run(home=blocker / "genesis", repo=fake_repo)
    assert res.returncode == TEMPFAIL, (
        f"expected {TEMPFAIL} (EX_TEMPFAIL) so the unit goes red, got "
        f"{res.returncode}. stdout={res.stdout!r}"
    )
    assert "cannot create lock directory" in res.stdout


def test_flock_missing_from_path_is_tempfail_not_success(tmp_path, fake_repo):
    """`flock` absent from the unit's PATH is the other permanent-shaped case,
    and the unit template ships a deliberately narrow PATH — so this is not
    hypothetical, it is one editing mistake away."""
    home = tmp_path / "genesis"
    home.mkdir()
    # A PATH with `mkdir` but WITHOUT `flock`, so the runner actually reaches
    # the branch under test. An EMPTY PATH does not work: it also removes
    # `mkdir`, so the script exits at the earlier lock-directory branch with the
    # same code for a different reason. That is exactly why this test asserts
    # the MESSAGE and not just the exit code — the first version of it passed
    # while testing nothing of the kind.
    slim_bin = tmp_path / "slim-bin"
    slim_bin.mkdir()
    mkdir_src = shutil.which("mkdir")
    assert mkdir_src, "no mkdir on PATH — cannot construct this fixture"
    (slim_bin / "mkdir").symlink_to(mkdir_src)
    assert shutil.which("flock", path=str(slim_bin)) is None, "fixture leaked flock"

    env = dict(
        os.environ, GENESIS_HOME=str(home), GENESIS_REPO_DIR=str(fake_repo), PATH=str(slim_bin)
    )
    res = subprocess.run(
        ["/bin/bash", str(RUNNER)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert res.returncode == TEMPFAIL, f"got {res.returncode}, stdout={res.stdout!r}"
    assert "lock unavailable" in res.stdout


@pytest.mark.skipif(
    shutil.which("flock") is None,
    reason="needs flock to HOLD the lock from outside; the runner's own "
    "flock-missing path is covered by test_flock_missing_from_path_is_tempfail",
)
def test_a_held_lock_is_a_clean_no_op(tmp_path, fake_repo):
    """The ONE case that is legitimately 0: another tick is already projecting.

    The work is being done. Going red here would make a collision with the
    manual run the docs recommend look like a failure.

    WAITS for the holder to actually take the lock before running the child.
    An earlier version started the holder and immediately invoked the runner,
    which races: if the holder had not yet acquired, the runner took the lock,
    ran to completion and the test passed for entirely the wrong reason. A
    reviewer caught it. Confirming acquisition is cheap; a flaky test that
    passes wrongly is not.
    """
    home = tmp_path / "genesis"
    (home / "locks").mkdir(parents=True)
    lock = home / "locks" / "graph-project-runner.lock"
    lock.touch()

    with subprocess.Popen(["flock", "-x", str(lock), "sleep", "30"]) as holder:
        try:
            # Poll until a non-blocking attempt FAILS, which is positive proof
            # the holder owns it — not a sleep, which would only make the race
            # rarer.
            deadline = time.monotonic() + 30
            while True:
                probe = subprocess.run(
                    ["flock", "-n", str(lock), "true"], capture_output=True, check=False
                )
                if probe.returncode != 0:
                    break
                assert time.monotonic() < deadline, "holder never acquired the lock"
                time.sleep(0.05)

            res = _run(home=home, repo=fake_repo, timeout=60)
        finally:
            holder.terminate()
            holder.wait(timeout=30)

    assert res.returncode == 0, f"a held lock must be 0, got {res.returncode}"
    assert "already in progress" in res.stdout
    assert "stub: projected" not in res.stdout, (
        "the runner must not have projected while the lock was held"
    )


def test_a_missing_venv_is_a_clean_no_op(tmp_path):
    """Deliberately 0, and stated so nobody 'fixes' it to 75.

    Without an interpreter the runner cannot even ask whether this install has
    an engine, and an install with no venv is broken in a way bootstrap
    surfaces. An hourly red unit would add noise, not information.
    """
    home = tmp_path / "genesis"
    home.mkdir()
    bare = tmp_path / "no-venv-repo"
    bare.mkdir()

    res = _run(home=home, repo=bare)
    assert res.returncode == 0
    assert "no venv interpreter" in res.stdout


def test_the_entrypoints_exit_code_is_propagated_unaltered(tmp_path):
    """An armed install whose projection FAILS must fail the unit.

    If this is swallowed, the staleness bound goes quiet exactly when it stops
    holding — the same fail-open as the lock paths, one layer down.
    """
    home = tmp_path / "genesis"
    home.mkdir()
    repo = tmp_path / "repo"
    (repo / ".venv" / "bin").mkdir(parents=True)
    py = repo / ".venv" / "bin" / "python"
    py.write_text("#!/usr/bin/env bash\necho 'stub: boom' >&2\nexit 3\n", encoding="utf-8")
    py.chmod(0o755)

    res = _run(home=home, repo=repo)
    assert res.returncode == 3, (
        f"the entrypoint's exit code must reach systemd unaltered, got {res.returncode}"
    )


def test_it_survives_an_unset_home(tmp_path, fake_repo):
    """`set -u` + `$HOME` aborts with "unbound variable" in a stripped
    environment, and a systemd unit is exactly where that happens.

    A repo-wide contract test asserts the guard PATTERN is present
    (test_home_guard_coverage.py); this asserts the guard WORKS, which is a
    different claim — a pattern can be present and still not resolve.
    """
    env = {
        k: v for k, v in os.environ.items() if k != "HOME"
    }
    env["GENESIS_HOME"] = str(tmp_path / "genesis")
    env["GENESIS_REPO_DIR"] = str(fake_repo)
    assert "HOME" not in env

    res = subprocess.run(
        ["bash", str(RUNNER)], env=env, capture_output=True, text=True, timeout=120, check=False
    )
    assert "unbound variable" not in res.stderr, res.stderr
    # It reached the stub interpreter, which is proof it got past every $HOME
    # dereference rather than merely not crashing early.
    assert "stub: projected" in res.stdout, f"stdout={res.stdout!r} stderr={res.stderr!r}"
    assert res.returncode == 0
