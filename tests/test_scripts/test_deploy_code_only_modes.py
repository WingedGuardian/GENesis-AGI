"""scripts/deploy_code_only.sh: the modes, the boot-commit report, and the git
hook sync.

Same fixture as test_deploy_code_only.py. The fixture's server "booted" when the
checkout was cloned, and a test that needs a move of HEAD to land AFTER the boot
runs the script with GIT_COMMITTER_DATE set, which is the time the reflog records
for the merge.

What is pinned:
  * three modes plus status, each with its contract, and the old flags refused
    with their replacement named;
  * a pull accepts any range, restarts nothing, pages nobody, and names every
    change the running server has not loaded plus the next step, including on a
    tree someone already pulled by hand, and with the boot commit unknown;
  * a restart after a pull moves the boot commit to HEAD;
  * status gives what the validation bracket compares, and a validation
    bracketed across a pull reads invalid;
  * the dependency gate's remedy fits the situation (update.sh with a range to
    merge, update.sh --post-merge at the tip, neither for a too-old python);
  * git hook copies are synced after the merge, without the lock;
  * an unwritable deploy marker refuses; CDPATH cannot move the script's paths.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from tests.test_scripts._deploy_station import SCRIPT
from tests.test_scripts._deploy_station import advance_upstream as _advance_upstream
from tests.test_scripts._deploy_station import alerts as _alerts
from tests.test_scripts._deploy_station import commit as _commit
from tests.test_scripts._deploy_station import git as _git
from tests.test_scripts._deploy_station import install_fixture as _install_fixture
from tests.test_scripts._deploy_station import restarted as _restarted
from tests.test_scripts._deploy_station import run as _run

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="bash-only")

_SYNC_STUB = (
    "#!/bin/bash\n"
    'echo "ran $*" >> "$SYNC_LOG"\n'
    # How many of this process's fds point at update.lock (the script must close it).
    'n=0; for fd in /proc/$$/fd/*; do case "$(readlink "$fd")" in *update.lock) n=$((n+1));; esac; done\n'
    'echo "lock-fds=$n" >> "$SYNC_LOG"\n'
    "exit ${SYNC_RC:-0}\n"
)


def _later(st, seconds: int = 60) -> str:
    return f"@{st['booted_at'] + seconds} +0000"


def _env(st, **extra) -> dict:
    return {**st["env"], **extra}


def _status(st, **extra) -> dict:
    r = _run(st, "status", env=_env(st, **extra))
    assert r.returncode == 0, r.stderr
    out = {}
    for line in r.stdout.splitlines():
        key, sep, value = line.partition(": ")
        if sep and key in ("serving", "head", "mainpid", "runtime-edits"):
            out[key] = value.strip()
    assert set(out) == {"serving", "head", "mainpid", "runtime-edits"}, r.stdout
    return out


def _bracket_valid(start: dict, end: dict) -> bool:
    """The validation bracket as the script's header states it."""
    return (
        start["serving"] == start["head"]
        and end["serving"] == start["serving"]
        and end["head"] == start["head"]
        and end["mainpid"] == start["mainpid"]
        and start["runtime-edits"] == "none"
        and end["runtime-edits"] == "none"
    )


_SCRIPT_FILES = (
    "deploy_code_only.sh",
    "lib/guardian_pause.sh",
    "lib/deploy_marker.sh",
    "lib/alert_queue.sh",
    "lib/port_owned_by.py",
    "lib/manifest_delta.py",
    "lib/serving_commit.py",
    "lib/venv_matches_pyproject.py",
)


def _install_the_script(st) -> None:
    """Put this script and its libs into the fixture's own tree, committed, so
    the script resolves its paths from where it sits (no GENESIS_DEPLOY_ROOT)."""
    files = {f"scripts/{f}": (SCRIPT.parent / f).read_text() for f in _SCRIPT_FILES}
    _advance_upstream(st, "the deploy script", files)
    _git(st["root"], "pull", "-q", "--ff-only")


def _env_without_the_root_seam(st, **extra) -> dict:
    env = {k: v for k, v in st["env"].items() if k != "GENESIS_DEPLOY_ROOT"}
    return {**env, **extra}


# ── the mode interface ──────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("flag", "replacement"),
    [("--no-pull", "deploy_code_only.sh restart"), ("--no-restart", "deploy_code_only.sh pull")],
)
def test_the_old_flags_are_refused_naming_their_replacement(station, flag, replacement):
    head = _git(station["root"], "rev-parse", "HEAD")
    _advance_upstream(station)
    r = _run(station, flag)
    assert r.returncode == 1, r.stderr
    assert replacement in r.stderr, r.stderr
    assert _git(station["root"], "rev-parse", "HEAD") == head
    assert not station["lock"].exists(), "an invalid invocation must not take the lock"


@pytest.mark.parametrize(
    ("args", "needle"),
    [(("pull", "restart"), "one mode at a time"), (("redeploy",), "unknown argument")],
)
def test_an_invalid_mode_is_refused_before_the_lock(station, args, needle):
    r = _run(station, *args)
    assert r.returncode == 1 and needle in r.stderr, r.stderr
    assert not station["lock"].exists()


def test_restart_does_not_fetch(station):
    head = _git(station["root"], "rev-parse", "HEAD")
    _advance_upstream(station)
    r = _run(station, "restart")
    assert r.returncode == 0, r.stderr
    assert _git(station["root"], "rev-parse", "HEAD") == head, "restart moved the tree"
    assert _restarted(station)
    assert f"Healthy — deployed {head}" in r.stdout


def test_pull_restarts_nothing_and_pages_nobody(station):
    tip = _advance_upstream(station, "runtime change", {"src/genesis/mod.py": "x = 1\n"})
    r = _run(station, "pull", env=_env(station, GIT_COMMITTER_DATE=_later(station)))
    assert r.returncode == 0, r.stderr
    assert _git(station["root"], "rev-parse", "HEAD") == tip
    assert not _restarted(station)
    assert not station["marker"].exists()
    assert not _alerts(station), "a pull is reported to the session, never paged"


# ── the report: what the server has not loaded ──────────────────────────────
def test_a_pull_names_every_pending_change_and_the_next_step(station):
    _advance_upstream(
        station,
        "mixed",
        {"src/genesis/mod.py": "x = 1\n", "config/routing.yaml": "a: 1\n", "docs/x.md": "d\n"},
    )
    r = _run(station, "pull", env=_env(station, GIT_COMMITTER_DATE=_later(station)))
    assert r.returncode == 0, r.stderr
    assert "PENDING" in r.stdout, r.stdout
    assert "src/genesis/mod.py" in r.stdout and "config/routing.yaml" in r.stdout, r.stdout
    assert "docs/x.md" not in r.stdout, "the server does not load docs"
    assert "scripts/deploy_code_only.sh restart" in r.stdout, r.stdout


def test_a_tree_pulled_by_hand_is_still_reported(station):
    """Keyed on the boot commit against the tree, not on this run's range: a pull
    made without the script leaves nothing for this run to merge."""
    tip = _advance_upstream(station, "runtime change", {"src/genesis/mod.py": "x = 1\n"})
    _git(
        station["root"],
        "pull",
        "-q",
        "--ff-only",
        env=_env(station, GIT_COMMITTER_DATE=_later(station)),
    )
    r = _run(station, "pull")
    assert r.returncode == 0, r.stderr
    assert "Already at the upstream tip" in r.stdout
    assert _git(station["root"], "rev-parse", "HEAD") == tip
    assert "PENDING" in r.stdout and "src/genesis/mod.py" in r.stdout, r.stdout


def test_a_pull_the_server_does_not_load_reports_nothing_pending(station):
    _advance_upstream(station, "docs", {"docs/x.md": "d\n"})
    r = _run(station, "pull", env=_env(station, GIT_COMMITTER_DATE=_later(station)))
    assert r.returncode == 0, r.stderr
    assert "PENDING" not in r.stdout
    assert "Nothing the server loads" in r.stdout, r.stdout


def test_an_unknown_boot_commit_reports_the_pull_range_instead(station):
    """The reflog starts after this boot: unknown, never a guess. The pull's own
    range is shown in its place, and the pending changes are still named."""
    _advance_upstream(station, "runtime change", {"src/genesis/mod.py": "x = 1\n"})
    r = _run(
        station, "pull", env=_env(station, BOOTED_AT="1000", GIT_COMMITTER_DATE=_later(station))
    )
    assert r.returncode == 0, r.stderr
    assert "booted from is unknown" in r.stdout, r.stdout
    assert "Showing what this pull changed instead" in r.stdout, r.stdout
    assert "PENDING" in r.stdout and "src/genesis/mod.py" in r.stdout, r.stdout


def test_a_restart_after_a_pull_moves_the_boot_commit_to_head(station):
    before = _git(station["root"], "rev-parse", "HEAD")
    tip = _advance_upstream(station, "runtime change", {"src/genesis/mod.py": "x = 1\n"})
    later = _later(station)
    assert _run(station, "pull", env=_env(station, GIT_COMMITTER_DATE=later)).returncode == 0
    mid = _status(station)
    assert (mid["serving"], mid["head"]) == (before, tip), mid
    r = _run(station, "restart", env=_env(station, RESTARTED_AT=str(station["booted_at"] + 120)))
    assert r.returncode == 0, r.stderr
    after = _status(station)
    assert (after["serving"], after["head"]) == (tip, tip), after
    assert after["mainpid"] != mid["mainpid"]


# ── status and the validation bracket ───────────────────────────────────────
def test_status_reports_and_takes_no_lock(station):
    head = _git(station["root"], "rev-parse", "HEAD")
    station["lock"].parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        ["flock", "-x", str(station["lock"]), "bash", "-c", "echo HELD; exec sleep 30"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "HELD"
        s = _status(station)
    finally:
        holder.kill()
        holder.wait()
    assert s == {"serving": head, "head": head, "mainpid": "1111", "runtime-edits": "none"}, s
    assert not _restarted(station)


def test_status_when_the_server_is_not_running(station):
    r = _run(station, "status", env=_env(station, ACTIVE_STATE="inactive"))
    assert r.returncode == 0, r.stderr
    assert "serving: unknown (genesis-server is not running (inactive))" in r.stdout, r.stdout


def test_a_validation_bracketed_across_a_pull_reads_invalid(station):
    """After a pull the tree is ahead of the server on purpose. A bracket on HEAD
    alone (HEAD and MainPID equal at both ends) calls this run valid, while the
    server under test is running the older code."""
    _advance_upstream(station, "runtime change", {"src/genesis/mod.py": "x = 1\n"})
    assert (
        _run(station, "pull", env=_env(station, GIT_COMMITTER_DATE=_later(station))).returncode == 0
    )
    start, end = _status(station), _status(station)
    assert start["head"] == end["head"] and start["mainpid"] == end["mainpid"], (
        "HEAD alone says valid"
    )
    assert not _bracket_valid(start, end)


def test_a_bracket_reads_valid_over_an_idle_run_and_invalid_across_a_restart(station):
    start = _status(station)
    assert _bracket_valid(start, _status(station)), "control: nothing happened"
    assert _run(station, "restart").returncode == 0
    assert not _bracket_valid(start, _status(station))


# ── the dependency gate's remedy ────────────────────────────────────────────
def test_an_incoming_dependency_change_names_update_sh_not_post_merge(station):
    head = _git(station["root"], "rev-parse", "HEAD")
    _advance_upstream(
        station,
        "needs a newer packaging",
        {"pyproject.toml": '[project]\nname = "fixture"\ndependencies = ["packaging>=9999"]\n'},
    )
    r = _run(station)
    assert r.returncode == 1
    assert "run scripts/update.sh instead" in r.stderr, r.stderr
    assert "--post-merge" not in r.stderr, "with a range to merge, update.sh merges and reinstalls"
    assert _git(station["root"], "rev-parse", "HEAD") == head


def test_a_python_too_old_for_the_pyproject_is_not_sent_to_a_reinstall(station):
    head = _git(station["root"], "rev-parse", "HEAD")
    _install_fixture(station["site"], station["root"], requires_python=">=3.99")
    _advance_upstream(
        station,
        "needs python 3.99",
        {
            "pyproject.toml": '[project]\nname = "fixture"\nrequires-python = ">=3.99"\ndependencies = ["packaging"]\n'
        },
    )
    r = _run(station)
    assert r.returncode == 1
    assert "needs a newer Python" in r.stderr, r.stderr
    assert "update.sh" not in r.stderr, "a reinstall into this venv cannot fix it"
    assert _git(station["root"], "rev-parse", "HEAD") == head


# ── git hooks, notes, and the marker ────────────────────────────────────────
def test_git_hooks_are_synced_after_the_merge_without_the_lock(station):
    """The stub exists only upstream, so it ran from the merged tree."""
    log = station["tmp"] / "sync.log"
    _advance_upstream(station, "hooks", {"scripts/hooks/sync-hooks.sh": _SYNC_STUB})
    r = _run(station, "pull", env=_env(station, SYNC_LOG=str(log)))
    assert r.returncode == 0, r.stderr
    assert log.read_text().splitlines() == ["ran --quiet", "lock-fds=0"], log.read_text()
    assert "Git hook copies in sync" in r.stdout


def test_a_sync_hooks_failure_is_reported_not_fatal(station):
    log = station["tmp"] / "sync.log"
    _advance_upstream(station, "hooks", {"scripts/hooks/sync-hooks.sh": _SYNC_STUB})
    r = _run(station, "pull", env=_env(station, SYNC_LOG=str(log), SYNC_RC="2"))
    assert r.returncode == 0, r.stderr
    assert "left a user-modified git hook alone" in r.stdout, r.stdout


def test_restart_does_not_touch_the_git_hooks(station):
    log = station["tmp"] / "sync.log"
    _advance_upstream(station, "hooks", {"scripts/hooks/sync-hooks.sh": _SYNC_STUB})
    _git(station["root"], "pull", "-q", "--ff-only")
    r = _run(station, "restart", env=_env(station, SYNC_LOG=str(log)))
    assert r.returncode == 0, r.stderr
    assert not log.exists(), "restart synced the hooks"


@pytest.mark.parametrize(
    ("changed", "named"),
    [("scripts/lib/disk_guardian.sh", True), ("scripts/lib/unrelated.sh", False)],
)
def test_a_change_to_the_watchgods_code_is_named(station, changed, named):
    """The watchgod's files are read from its own `source` lines."""
    _advance_upstream(
        station,
        "watchgod",
        {
            "scripts/tmp_watchgod.sh": 'source "$_SCRIPT_DIR/lib/disk_guardian.sh"\n',
            "scripts/lib/disk_guardian.sh": "a=1\n",
            "scripts/lib/unrelated.sh": "b=1\n",
        },
    )
    _git(station["root"], "pull", "-q", "--ff-only")
    _advance_upstream(station, "change", {changed: "a=2\n"})
    r = _run(station, "pull")
    assert r.returncode == 0, r.stderr
    assert ("genesis-tmp-watchgod" in r.stdout) is named, r.stdout
    assert (changed in r.stdout) is named, r.stdout


@pytest.mark.parametrize(
    ("changed", "noted"), [("scripts/hooks/x.py", False), ("scripts/bootstrap.sh", True)]
)
def test_the_activation_note_does_not_list_the_hooks_it_applies(station, changed, noted):
    from tests.test_scripts._deploy_station import REPO

    snap = "src/genesis/observability/snapshots/deploy_health.py"
    _advance_upstream(station, "snapshot", {snap: (REPO / snap).read_text()})
    _git(station["root"], "pull", "-q", "--ff-only")
    _advance_upstream(station, "change", {changed: "x = 1\n"})
    r = _run(station, "pull")
    assert r.returncode == 0, r.stderr
    assert ("activation paths" in r.stdout) is noted, r.stdout


def test_a_marker_that_cannot_be_written_refuses_before_anything_moves(station):
    head = _git(station["root"], "rev-parse", "HEAD")
    station["marker"].mkdir()  # a directory where the marker file goes
    _advance_upstream(station)
    r = _run(station)
    assert r.returncode == 1, r.stderr
    assert "cannot write the deploy marker" in r.stderr, r.stderr
    assert _git(station["root"], "rev-parse", "HEAD") == head
    assert not _restarted(station)
    assert station["marker"].is_dir(), "the refusal must leave what it found"


def test_cdpath_cannot_redirect_the_script_to_another_tree(station):
    """Invoked by a relative path from inside its own tree, `cd scripts/..` and
    `cd scripts` would consult CDPATH and land in another tree, printing its path
    into the captured value. No root seam: both of the script's own paths are
    resolved for real."""
    _install_the_script(station)
    decoy = station["tmp"] / "decoy"
    (decoy / "scripts" / "lib").mkdir(parents=True)
    r = subprocess.run(
        ["bash", "scripts/deploy_code_only.sh", "status"],
        cwd=station["root"],
        env=_env_without_the_root_seam(station, CDPATH=str(decoy)),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert f"head: {_git(station['root'], 'rev-parse', 'HEAD')}" in r.stdout, r.stdout


def test_the_helpers_this_run_uses_are_the_ones_it_started_with(station):
    """The script merges the tree it runs from. Helpers that run after the merge
    (the boot-commit reader, the manifest delta) are read at startup, so a merge
    that replaces them does not change this run."""
    _install_the_script(station)
    broken = "import sys\nsys.exit(7)\n"
    _advance_upstream(
        station,
        "helpers change",
        {"scripts/lib/serving_commit.py": broken, "scripts/lib/manifest_delta.py": broken},
    )
    r = subprocess.run(
        ["bash", "scripts/deploy_code_only.sh", "--wait", "5"],
        cwd=station["root"],
        env=_env_without_the_root_seam(station),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert (station["root"] / "scripts/lib/serving_commit.py").read_text() == broken
    assert "Healthy" in r.stdout, r.stdout
    assert "does not confirm" not in r.stdout, "the merged (broken) reader ran"
    assert "manifest-interpreter-failed" not in r.stdout, "the merged (broken) delta ran"
    assert not _alerts(station)


def test_a_bare_pull_during_the_run_reads_invalid(station):
    """A pull outside the lock, mid-run, leaves the boot commit and MainPID alone
    while the server starts importing the new files."""
    start = _status(station)
    _advance_upstream(station, "runtime change", {"src/genesis/mod.py": "x = 1\n"})
    _git(
        station["root"],
        "pull",
        "-q",
        "--ff-only",
        env=_env(station, GIT_COMMITTER_DATE=_later(station)),
    )
    end = _status(station)
    assert (end["serving"], end["mainpid"]) == (start["serving"], start["mainpid"])
    assert not _bracket_valid(start, end)


def test_a_merge_git_refuses_pages_nobody(station):
    """git refuses the fast-forward and the tree is unmoved: a refusal, not a
    failed deploy. (An untracked file in the way is refused earlier, by name, in
    test_a_range_adding_a_file_that_exists_untracked_is_refused.)"""
    head = _git(station["root"], "rev-parse", "HEAD")
    _advance_upstream(station, "adds a file", {"src/genesis/new.py": "x = 1\n"})
    _git_shim(station, 'echo "merge refused" >&2; exit 1')
    r = _run(station, "pull")
    assert r.returncode == 1, r.stderr
    assert "git refused the fast-forward" in r.stderr and "nothing merged" in r.stderr
    assert _git(station["root"], "rev-parse", "HEAD") == head
    assert not _alerts(station), "nothing changed, so nobody is paged"
    assert not station["marker"].exists()


def test_an_unreadable_tree_status_refuses(station):
    real_git = subprocess.run(
        ["bash", "-c", "command -v git"], capture_output=True, text=True, check=True
    ).stdout.strip()
    from tests.test_scripts._deploy_station import exec_file

    exec_file(
        station["shims"] / "git",
        '#!/bin/bash\nfor a in "$@"; do [ "$a" = status ] && exit 128; done\n'
        f'exec "{real_git}" "$@"\n',
    )
    (station["root"] / "pyproject.toml").write_text("# a real tracked edit\n")
    r = _run(station, "restart")
    assert r.returncode == 1, r.stdout
    assert "cannot read the working tree's status" in r.stderr, r.stderr
    assert not _restarted(station)


def test_status_from_a_linked_worktree_reports_the_main_checkout(station):
    wt = station["tmp"] / "elsewhere" / "wt"
    _git(station["root"], "worktree", "add", "-q", "-b", "wt", str(wt))
    _commit(wt, "worktree-only commit")
    r = _run(station, "status", env=_env(station, GENESIS_DEPLOY_ROOT=str(wt)))
    assert r.returncode == 0, r.stderr
    assert "reporting the main checkout" in r.stdout, r.stdout
    assert f"head: {_git(station['root'], 'rev-parse', 'HEAD')}" in r.stdout, r.stdout


def _age_the_reflog(st, days: int) -> int:
    """Re-time the checkout's only reflog entry to *days* ago; returns that time."""
    import time

    log = st["root"] / ".git" / "logs" / "HEAD"
    (line,) = log.read_text().splitlines()
    fields, msg = line.split("\t", 1)
    head = fields.split()
    when = int(time.time()) - days * 86400
    head[-2] = str(when)
    log.write_text(" ".join(head) + "\t" + msg + "\n")
    return when


@pytest.mark.parametrize(
    ("setting", "known"),
    [(None, False), ("never", True)],
    ids=["git-default-30-days", "never-expires"],
)
def test_a_boot_older_than_the_expiry_cutoff_is_unknown(station, setting, known):
    """Past git's cutoff for unreachable reflog entries a detour may have been
    expired as a pair, so an old boot reads unknown; with no expiry it does not."""
    when = _age_the_reflog(station, 40)
    if setting:
        _git(station["root"], "config", "gc.reflogExpireUnreachable", setting)
    s = _status(station, BOOTED_AT=str(when + 1))
    head = _git(station["root"], "rev-parse", "HEAD")
    assert (s["serving"] == head) is known, s


def test_a_deploy_with_nothing_to_deploy_does_not_restart(station):
    r = _run(station)
    assert r.returncode == 0, r.stderr
    assert "Nothing to deploy" in r.stdout, r.stdout
    assert not _restarted(station), "a restart would only end in-flight sessions"


def test_a_deploy_with_an_unknown_boot_commit_still_restarts(station):
    """Control for the skip: it needs the boot commit PROVEN equal to HEAD."""
    r = _run(station, env=_env(station, BOOTED_AT="1000"))
    assert r.returncode == 0, r.stderr
    assert _restarted(station)


# ── review round 1 on the PR ────────────────────────────────────────────────
def _git_shim(st, on_merge: str) -> None:
    """A git that runs *on_merge* (shell, with $REAL as the real git) in place of
    `git … merge …`, and passes everything else through."""
    from tests.test_scripts._deploy_station import exec_file

    real = subprocess.run(
        ["bash", "-c", "command -v git"], capture_output=True, text=True, check=True
    ).stdout.strip()
    exec_file(
        st["shims"] / "git",
        f'#!/bin/bash\nREAL="{real}"\n'
        'for a in "$@"; do if [ "$a" = merge ]; then\n'
        f"{on_merge}\n"
        'fi; done\nexec "$REAL" "$@"\n',
    )


def _calls(st) -> list[str]:
    return st["calls"].read_text().splitlines() if st["calls"].exists() else []


def test_deploy_stops_the_server_before_the_fast_forward(station):
    """No request may run against a mix of old and new modules: the server is
    stopped while the tree is still at the old commit."""
    head = _git(station["root"], "rev-parse", "HEAD")
    tip = _advance_upstream(station, "runtime change", {"src/genesis/mod.py": "x = 1\n"})
    r = _run(station)
    assert r.returncode == 0, r.stderr
    calls = _calls(station)
    stop = next(i for i, c in enumerate(calls) if "stop genesis-server" in c)
    restart = next(i for i, c in enumerate(calls) if "restart genesis-server" in c)
    assert stop < restart
    assert (station["calls"].parent / "systemctl.log.stop_head").read_text().strip() == head
    assert _git(station["root"], "rev-parse", "HEAD") == tip


def test_pull_never_stops_the_server(station):
    _advance_upstream(station, "runtime change", {"src/genesis/mod.py": "x = 1\n"})
    r = _run(station, "pull")
    assert r.returncode == 0, r.stderr
    assert not any("stop genesis-server" in c for c in _calls(station))


def test_a_merge_refused_after_the_stop_starts_the_server_again(station):
    head = _git(station["root"], "rev-parse", "HEAD")
    _advance_upstream(station, "runtime change", {"src/genesis/mod.py": "x = 1\n"})
    _git_shim(station, 'echo "merge refused" >&2; exit 1')
    r = _run(station)
    assert r.returncode == 1, r.stdout
    assert "git refused the fast-forward" in r.stderr, r.stderr
    calls = _calls(station)
    assert any("stop genesis-server" in c for c in calls)
    assert any(c.endswith("start genesis-server") and "restart" not in c for c in calls), calls
    assert _git(station["root"], "rev-parse", "HEAD") == head
    assert not _alerts(station), "nothing changed and the server is back up"


def test_a_checkout_moved_during_the_run_is_refused_and_the_server_started(station):
    """Another tool moves the tree between the checks and the restart: what would
    be restarted is not what was checked."""
    _advance_upstream(station, "runtime change", {"src/genesis/mod.py": "x = 1\n"})
    _git_shim(
        station,
        '"$REAL" "$@" || exit $?\n'
        '"$REAL" -C "$GENESIS_DEPLOY_ROOT" -c user.email=t@l -c user.name=t '
        "commit -q --allow-empty -m raced; exit 0",
    )
    r = _run(station)
    assert r.returncode == 1, r.stdout
    assert "the checkout moved during this run" in r.stderr, r.stderr
    calls = _calls(station)
    assert not any("restart genesis-server" in c for c in calls), "restarted unchecked code"
    assert any(c.endswith("start genesis-server") for c in calls), "left the server down"
    alerts = _alerts(station)
    assert len(alerts) == 1 and "started again" in alerts[0].read_text(), alerts


@pytest.mark.parametrize("where", ["the-path", "a-parent"])
def test_a_range_adding_a_file_that_exists_untracked_is_refused(station, where):
    """git overwrites an IGNORED untracked file on a fast-forward without asking."""
    head = _git(station["root"], "rev-parse", "HEAD")
    _advance_upstream(station, "ignore it", {".gitignore": "local.env\ncache\n"})
    _git(station["root"], "pull", "-q", "--ff-only")
    head = _git(station["root"], "rev-parse", "HEAD")
    local = station["root"] / ("local.env" if where == "the-path" else "cache")
    local.write_text("MY SECRET\n")
    added = "local.env" if where == "the-path" else "cache/x.txt"
    (station["seed"] / added).parent.mkdir(parents=True, exist_ok=True)
    (station["seed"] / added).write_text("upstream\n")
    _git(station["seed"], "add", "-f", added)
    _git(station["seed"], "commit", "-qm", "track it")
    _git(station["seed"], "push", "-q", "origin", "main")
    r = _run(station)
    assert r.returncode == 1, r.stdout
    assert "already exist here, untracked" in r.stderr and added in r.stderr, r.stderr
    assert local.read_text() == "MY SECRET\n"
    assert _git(station["root"], "rev-parse", "HEAD") == head
    assert not any("stop genesis-server" in c for c in _calls(station))


def test_a_rename_into_an_excused_path_is_still_a_dirty_tree(station):
    """Porcelain shows a rename as ONE line naming both paths; excused on the
    destination, the whole record vanished and the deletion with it."""
    _advance_upstream(station, "extra", {"extra.txt": "x\n"})
    _git(station["root"], "pull", "-q", "--ff-only")
    (station["root"] / ".serena").mkdir()
    _git(station["root"], "mv", "extra.txt", ".serena/project.yml")
    r = _run(station, "restart")
    assert r.returncode == 1, r.stdout
    assert "uncommitted tracked changes" in r.stderr and "extra.txt" in r.stderr, r.stderr
    assert not _restarted(station)


def test_status_reports_uncommitted_runtime_edits(station):
    _advance_upstream(station, "module", {"src/genesis/mod.py": "x = 1\n"})
    _git(
        station["root"],
        "pull",
        "-q",
        "--ff-only",
        env=_env(station, GIT_COMMITTER_DATE=_later(station)),
    )
    assert _status(station)["runtime-edits"] == "none"
    (station["root"] / "src" / "genesis" / "mod.py").write_text("x = 2\n")
    (station["root"] / "src" / "genesis" / "new.py").write_text("y = 1\n")
    s = _status(station)
    assert s["runtime-edits"].startswith("2 paths:"), s
    assert "src/genesis/mod.py" in s["runtime-edits"] and "src/genesis/new.py" in s["runtime-edits"]
    assert not _bracket_valid(s, s), "an uncommitted runtime edit invalidates the bracket"


def test_the_health_answer_and_the_identity_are_one_process(station):
    """The unit restarts again between the request and the identity check: the
    answer came from 2222, the check would see 3333. That attempt must not count."""
    _advance_upstream(station)
    env = _env(station, NEW_PID_LATER="3333", PROBE_OWNER="3333")
    r = _run(station, env=env)
    assert r.returncode == 0, r.stderr
    assert "Attempt 1: the port answers, but not from the restarted unit" in r.stdout, r.stdout
    args = (station["tmp"] / "curl_args").read_text().splitlines()
    assert args[0].startswith("-q --noproxy * ") and "http://127.0.0.1:5000/" in args[0], args
