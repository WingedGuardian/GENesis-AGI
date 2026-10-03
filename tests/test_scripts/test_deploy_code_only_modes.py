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
import time
from pathlib import Path

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
        if sep and key in _STATUS_FIELDS:
            out[key] = value.strip()
    assert set(out) == set(_STATUS_FIELDS), r.stdout
    return out


_STATUS_FIELDS = (
    "serving",
    "head",
    "mainpid",
    "invocation",
    "runtime-edits",
    "runtime-overrides",
    "bracket",
)


def _verify(st, token: str, **extra) -> bool:
    """The script's own verdict on a bracket: `status --verify <token>`."""
    r = _run(st, "status", "--verify", token, env=_env(st, **extra))
    assert r.returncode in (0, 1), (r.returncode, r.stdout, r.stderr)
    assert ("bracket: valid" in r.stdout) == (r.returncode == 0), r.stdout
    return r.returncode == 0


_SCRIPT_FILES = (
    "deploy_code_only.sh",
    "lib/guardian_pause.sh",
    "lib/deploy_marker.sh",
    "lib/alert_queue.sh",
    "lib/deploy_status.sh",
    "lib/deploy_checkout.sh",
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
    # Real clock, deliberately: a later deploy merges on the real clock too, and a
    # pull stamped ahead of it reads as the clock stepping back.
    _git(st["root"], "pull", "-q", "--ff-only")


def _env_without_the_root_seam(st, **extra) -> dict:
    env = {k: v for k, v in st["env"].items() if k != "GENESIS_DEPLOY_ROOT"}
    # The shim's unit runs in the seam's directory by default; without the seam,
    # name the fixture tree the script resolves for itself.
    return {**env, "UNIT_DIR": str(st["root"]), **extra}


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
    assert s["bracket"].startswith("b1-"), s
    assert {k: s[k] for k in ("serving", "head", "mainpid", "runtime-edits")} == {
        "serving": head,
        "head": head,
        "mainpid": "1111",
        "runtime-edits": "none",
    }, s
    assert s["invocation"] == "1" * 32 and s["runtime-overrides"] == "none", s
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
    assert start["bracket"].startswith("unknown (HEAD's runtime files differ"), start
    assert not _verify(station, start["bracket"])


def test_a_bracket_reads_valid_over_an_idle_run_and_invalid_across_a_restart(station):
    start = _status(station)
    assert _verify(station, start["bracket"]), "control: nothing happened"
    assert _run(station, "restart").returncode == 0
    assert not _verify(station, start["bracket"])


def test_a_pull_of_docs_or_hooks_leaves_the_bracket_valid(station):
    """HEAD moves, but nothing the server loads: the server under test runs what
    it ran. Commit equality would call this invalid."""
    start = _status(station)
    _advance_upstream(station, "docs", {"docs/x.md": "d\n", "scripts/hooks/h.sh": "h\n"})
    r = _run(station, "pull", env=_env(station, GIT_COMMITTER_DATE=_later(station)))
    assert r.returncode == 0, r.stderr
    end = _status(station)
    assert end["head"] != start["head"] and end["serving"] == start["serving"]
    assert end["bracket"] == start["bracket"]
    assert _verify(station, start["bracket"])


def test_a_deploy_of_docs_or_hooks_neither_stops_nor_restarts(station):
    """An outage and ended dispatched sessions buy nothing when the server's
    files do not change."""
    tip = _advance_upstream(station, "docs", {"docs/x.md": "d\n"})
    r = _run(station, env=_env(station, GIT_COMMITTER_DATE=_later(station)))
    assert r.returncode == 0, r.stderr
    assert "no stop, no restart" in r.stdout and "Nothing to deploy" in r.stdout, r.stdout
    assert _git(station["root"], "rev-parse", "HEAD") == tip
    calls = _calls(station)
    assert not any("stop genesis-server" in c or "restart" in c for c in calls), calls
    assert not _alerts(station)


# ── runtime files held since the boot (Devin, #2557 round 3) ────────────────
# The server imports src/ lazily, so any tree HEAD held since the boot can have
# been loaded. A commit that restores the boot's files makes the two ends match
# and leaves the imported module in memory: only the reflog's history shows it.
def _booted_at(st, name: str, seconds: int) -> tuple[str, dict]:
    """Pull a commit that sets widget.py, and boot the server a minute after it:
    that commit is the boot commit. Returns it and the env naming that boot."""
    sha = _advance_upstream(st, name, {"src/genesis/widget.py": f"v = {name!r}\n"})
    r = _run(st, "pull", env=_env(st, GIT_COMMITTER_DATE=_later(st, seconds)))
    assert r.returncode == 0, r.stderr
    return sha, _env(st, BOOTED_AT=str(st["booted_at"] + seconds + 60))


def _pull_at(st, env: dict, seconds: int) -> subprocess.CompletedProcess:
    r = _run(st, "pull", env={**env, "GIT_COMMITTER_DATE": _later(st, seconds)})
    assert r.returncode == 0, r.stderr
    return r


@pytest.mark.parametrize(
    "restore_pulled_first",
    [False, True],
    ids=["the-deploy-merges-the-restore", "the-restore-was-already-pulled"],
)
def test_a_detour_through_other_runtime_files_forces_the_restart(station, restore_pulled_first):
    """Boot at A; a pull to B (the server may import B's widget.py); C restores A's
    files. Every end-to-end comparison says nothing changed, and the server can
    still run B's module. Already pulled, the restore leaves HEAD at the upstream
    tip, so only the history since the boot shows the detour."""
    a, env = _booted_at(station, "A", 60)
    _advance_upstream(station, "B", {"src/genesis/widget.py": "v = 'B'\n"})
    _pull_at(station, env, 180)
    c = _advance_upstream(station, "C", {"src/genesis/widget.py": "v = 'A'\n"})
    if restore_pulled_first:
        _pull_at(station, env, 240)
    assert _status(station, BOOTED_AT=env["BOOTED_AT"])["serving"] == a, (
        "precondition: the boot commit is known, so the skip is live"
    )
    assert not _restarted(station)
    r = _run(station, env={**env, "GIT_COMMITTER_DATE": _later(station, 300)})
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert _git(station["root"], "rev-parse", "HEAD") == c
    assert "Nothing to deploy" not in r.stdout, r.stdout
    assert _restarted(station), r.stdout


def test_a_bracket_reads_invalid_after_a_detour_through_other_runtime_files(station):
    a, env = _booted_at(station, "A", 60)
    start = _status(station, BOOTED_AT=env["BOOTED_AT"])
    assert start["serving"] == a and not start["bracket"].startswith("unknown"), start
    _advance_upstream(station, "B", {"src/genesis/widget.py": "v = 'B'\n"})
    _pull_at(station, env, 180)
    _advance_upstream(station, "C", {"src/genesis/widget.py": "v = 'A'\n"})
    back = _pull_at(station, env, 240)
    assert "HEAD has held other runtime files" in back.stdout, (
        "the pull's report must not say nothing changed: " + back.stdout
    )
    assert "Nothing the server loads" not in back.stdout, back.stdout
    end = _status(station, BOOTED_AT=env["BOOTED_AT"])
    assert end["serving"] == a, end
    assert end["bracket"].startswith("unknown (since the boot"), end
    assert not _verify(station, start["bracket"], BOOTED_AT=env["BOOTED_AT"])


# ── a late ignored-file collision (Codex P1, #2557 round 3) ─────────────────
def test_an_ignored_file_created_after_the_scan_is_not_overwritten(station):
    """The collision scan runs before the stop; something that writes an ignored
    file into the range's path between the two used to lose it to the
    fast-forward (git overwrites ignored files by default). git itself refuses
    now, and the stopped server goes back up on the unchanged tree."""
    root = station["root"]
    (root / ".git" / "info").mkdir(exist_ok=True)
    (root / ".git" / "info" / "exclude").write_text("secrets.local.yaml\n")
    head = _git(root, "rev-parse", "HEAD")
    _advance_upstream(station, "adds it", {"config/secrets.local.yaml": "upstream\n"})
    local = root / "config" / "secrets.local.yaml"
    r = _run(
        station,
        env=_env(
            station,
            GIT_COMMITTER_DATE=_later(station),
            ON_STOP=f"mkdir -p {root}/config && echo LOCAL > {local}",
        ),
    )
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert local.read_text() == "LOCAL\n", "the fast-forward overwrote an ignored local file"
    assert _git(root, "rev-parse", "HEAD") == head
    assert "git refused the fast-forward" in r.stderr, r.stderr
    calls = _calls(station)
    assert any("stop genesis-server" in c for c in calls), "precondition: the stop ran"
    assert any("start genesis-server" in c for c in calls), "the stopped server stays down"
    assert not _alerts(station), "a refusal changes nothing and pages nobody"


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
    that replaces them does not change this run. The range carries a runtime
    change too, so the deploy restarts and runs both helpers after the merge."""
    _install_the_script(station)
    broken = "import sys\nsys.exit(7)\n"
    _advance_upstream(
        station,
        "helpers change",
        {
            "scripts/lib/serving_commit.py": broken,
            "scripts/lib/manifest_delta.py": broken,
            "src/genesis/m.py": "x = 1\n",
        },
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
    assert not _verify(station, start["bracket"])


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


@pytest.mark.parametrize("mode", ["restart", "pull"])
def test_an_unreadable_tree_status_refuses(station, mode):
    """pull has no later status read to fall back on: this check is its only
    one."""
    _advance_upstream(station)
    head = _git(station["root"], "rev-parse", "HEAD")
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
    r = _run(station, mode)
    assert r.returncode == 1, r.stdout
    assert "cannot read the working tree's status" in r.stderr, r.stderr
    assert not _restarted(station)
    assert _git(station["root"], "rev-parse", "HEAD") == head


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


def test_an_unreadable_expiry_setting_reads_unknown(station):
    """git exits 128 on a gc.reflogExpireUnreachable value it cannot parse, and
    the cutoff is then unknown, so the boot commit is too. Control: the same
    fresh reflog with the setting unset takes git's 30-day default and is known."""
    head = _git(station["root"], "rev-parse", "HEAD")
    assert _status(station)["serving"] == head, "control: the setting unset"
    _git(station["root"], "config", "gc.reflogExpireUnreachable", "not-a-date")
    s = _status(station)
    assert s["serving"].startswith("unknown") and "gc.reflogExpireUnreachable" in s["serving"], s


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


_RACES = {
    "moved": (
        '"$REAL" -C "$GENESIS_DEPLOY_ROOT" -c user.email=t@l -c user.name=t '
        "commit -q --allow-empty -m raced",
        "the checkout moved during this run",
    ),
    "untracked": (
        'echo "y = 1" > "$GENESIS_DEPLOY_ROOT/src/genesis/stray.py"',
        "src/genesis/stray.py",
    ),
}


@pytest.mark.parametrize("race", sorted(_RACES))
def test_a_late_refusal_after_the_stop_restarts_health_checked_and_alerts(station, race):
    """Something outside the lock changes the tree after deploy stopped the
    server. Refusing would leave it down, and starting it without a check would
    hide a broken tree: the restart runs with its health check, and the run ends
    in a critical alert naming the change (owner ruling on #2557)."""
    _advance_upstream(station, "runtime change", {"src/genesis/mod.py": "x = 1\n"})
    act, needle = _RACES[race]
    _git_shim(station, f'"$REAL" "$@" || exit $?\n{act}; exit 0')
    r = _run(station)
    assert r.returncode == 1, r.stdout
    assert needle in r.stderr and "did not check" in r.stderr, r.stderr
    assert "Healthy" in r.stdout, r.stdout
    calls = _calls(station)
    stop = next(i for i, c in enumerate(calls) if "stop genesis-server" in c)
    restart = next(i for i, c in enumerate(calls) if "restart genesis-server" in c)
    assert stop < restart, calls
    alerts = _alerts(station)
    assert len(alerts) == 1, alerts
    body = alerts[0].read_text()
    assert "critical" in body and "did not check" in body and needle in body, body


def test_a_late_refusal_before_anything_changed_is_only_a_refusal(station):
    """Control: with the server untouched, the same check refuses and pages
    nobody. restart mode, the file appearing after the start's checks (the first
    MainPID read is the baseline, read after them)."""
    exec_shim = (
        '#!/bin/bash\nif [[ " $* " == *" MainPID "* ]] && [ -n "${STRAY:-}" ] '
        '&& [ ! -e "$STRAY" ]; then mkdir -p "$(dirname "$STRAY")"; echo y > "$STRAY"; fi\n'
    )
    from tests.test_scripts._deploy_station import exec_file

    real = (station["shims"] / "systemctl").read_text()
    exec_file(station["shims"] / "systemctl", exec_shim + real.split("\n", 1)[1])
    stray = station["root"] / "src" / "genesis" / "stray.py"
    r = _run(station, "restart", env=_env(station, STRAY=str(stray)))
    assert r.returncode == 1, r.stdout
    assert "src/genesis/stray.py" in r.stderr and "nothing was restarted" in r.stderr, r.stderr
    assert not _restarted(station)
    assert not _alerts(station)


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
    start = _status(station)
    assert start["runtime-edits"] == "none" and start["bracket"].startswith("b1-"), start
    (station["root"] / "pyproject.toml").write_text(
        (station["root"] / "pyproject.toml").read_text() + "# edited\n"
    )
    (station["root"] / "src" / "genesis").mkdir(parents=True)
    (station["root"] / "src" / "genesis" / "new.py").write_text("y = 1\n")
    s = _status(station)
    assert s["runtime-edits"].startswith("2 paths:"), s
    assert "pyproject.toml" in s["runtime-edits"] and "src/genesis/new.py" in s["runtime-edits"]
    assert s["bracket"].startswith("unknown (uncommitted runtime edits"), s
    assert not _verify(station, start["bracket"])


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


# ── review round 2 on the PR: what runs is what was checked ─────────────────
def test_a_restart_whose_start_fails_starts_the_server_again(station):
    """systemctl restart stops the old server, then its start fails: the exit
    trap must try to start it again, and the alert says what it found."""
    r = _run(station, "restart", env=_env(station, RESTART_RC="1"))
    assert r.returncode != 0, r.stdout
    calls = _calls(station)
    restart = next(i for i, c in enumerate(calls) if "restart genesis-server" in c)
    assert any(
        c.endswith("start genesis-server") and "restart" not in c for c in calls[restart + 1 :]
    ), calls
    alerts = _alerts(station)
    assert len(alerts) == 1 and "restarting" in alerts[0].read_text(), alerts


def test_a_checkout_moved_during_a_pulls_hook_sync_is_refused(station):
    """pull restarts nothing, so only the check after the hook sync stands
    between a raced tree and a report that it holds the pulled commit."""
    raced = (
        "#!/bin/bash\n"
        'git -C "$GENESIS_DEPLOY_ROOT" -c user.email=t@l -c user.name=t '
        "commit -q --allow-empty -m raced\n"
    )
    _advance_upstream(station, "hooks", {"scripts/hooks/sync-hooks.sh": raced})
    r = _run(station, "pull")
    assert r.returncode == 1, r.stdout
    assert "the checkout moved during this run" in r.stderr, r.stderr
    assert "Pulled" not in r.stdout


def test_deploy_syncs_the_git_hooks_after_the_restart(station):
    """The sync runs a script from the merged tree; with the server stopped for
    the fast-forward, running it first would only lengthen the outage."""
    _advance_upstream(
        station, "hooks", {"scripts/hooks/sync-hooks.sh": _SYNC_STUB, "src/genesis/m.py": "x\n"}
    )
    r = _run(station, env=_env(station, SYNC_LOG=str(station["calls"])))
    assert r.returncode == 0, r.stderr
    calls = _calls(station)
    restart = next(i for i, c in enumerate(calls) if "restart genesis-server" in c)
    synced = next(i for i, c in enumerate(calls) if c == "ran --quiet")
    assert restart < synced, calls


def test_the_units_own_server_holding_the_lock_refuses_nothing(station, outside_server):
    """The ordinary case on a live install: the server lock names the unit's own
    MainPID. That server is the one a restart replaces."""
    r = _run(station, "restart", env=_env(station, MAIN_PID=str(outside_server)))
    assert r.returncode == 0, r.stderr
    assert _restarted(station)


@pytest.mark.parametrize("mode", ["deploy", "restart"])
def test_untracked_runtime_files_refuse_a_restart(station, mode):
    """The editable install imports a new module under src/ as soon as something
    asks for it; startup globs YAML under config/."""
    _advance_upstream(station)
    head = _git(station["root"], "rev-parse", "HEAD")
    (station["root"] / "src" / "genesis").mkdir(parents=True)
    (station["root"] / "src" / "genesis" / "stray.py").write_text("x = 1\n")
    r = _run(station, mode)
    assert r.returncode == 1, r.stdout
    assert "src/genesis/stray.py" in r.stderr and "untracked" in r.stderr, r.stderr
    assert _git(station["root"], "rev-parse", "HEAD") == head
    assert not _restarted(station)
    assert not _alerts(station)


def test_untracked_files_elsewhere_and_a_pull_are_not_refused(station):
    """Control: an untracked file outside what the server loads refuses nothing,
    and a pull (which restarts nothing) is not refused for one inside it."""
    (station["root"] / "notes.txt").write_text("mine\n")
    r = _run(station, "restart")
    assert r.returncode == 0, r.stderr
    (station["root"] / "src").mkdir()
    (station["root"] / "src" / "stray.py").write_text("x = 1\n")
    _advance_upstream(station)
    assert _run(station, "pull").returncode == 0


@pytest.fixture()
def outside_server(station):
    """A live process that looks like a server started outside the unit, named
    by the server's process lock, as update.sh's fallback leaves one."""
    p = subprocess.Popen(["bash", "-c", 'exec -a "python -m genesis serve" sleep 120'])
    cmdline = Path(f"/proc/{p.pid}/cmdline")
    for _ in range(200):
        if b"genesis serve" in cmdline.read_bytes():
            break
        time.sleep(0.01)
    (station["home"] / ".genesis" / "genesis-server.lock").write_text(str(p.pid))
    yield p.pid
    p.kill()
    p.wait()


@pytest.mark.parametrize("mode", ["deploy", "restart"])
def test_a_server_outside_the_unit_refuses(station, outside_server, mode):
    _advance_upstream(station, "runtime change", {"src/genesis/mod.py": "x = 1\n"})
    head = _git(station["root"], "rev-parse", "HEAD")
    r = _run(station, mode)
    assert r.returncode == 1, r.stdout
    assert f"outside the systemd unit (pid {outside_server})" in r.stderr, r.stderr
    assert _git(station["root"], "rev-parse", "HEAD") == head
    assert not any("stop genesis-server" in c or "restart" in c for c in _calls(station))


def test_a_server_lock_naming_something_else_refuses_nothing(station):
    """Control: a stale lock naming a live process that is not a server."""
    p = subprocess.Popen(["sleep", "120"])
    try:
        (station["home"] / ".genesis" / "genesis-server.lock").write_text(str(p.pid))
        assert _run(station, "restart").returncode == 0
    finally:
        p.kill()
        p.wait()


@pytest.mark.parametrize("unit_dir", ["/srv/another-checkout", "-"])
def test_a_unit_running_in_another_directory_refuses_and_status_says_unknown(station, unit_dir):
    """Its working directory (and the secrets.env beside it) is fixed in the unit
    apart from its python: this venv can be run from another tree."""
    _advance_upstream(station)
    head = _git(station["root"], "rev-parse", "HEAD")
    env = _env(station, UNIT_DIR=unit_dir)
    r = _run(station, env=env)
    assert r.returncode == 1, r.stdout
    assert "genesis-server runs in" in r.stderr and "update.sh --post-merge" in r.stderr, r.stderr
    assert _git(station["root"], "rev-parse", "HEAD") == head
    assert _status(station, UNIT_DIR=unit_dir)["serving"].startswith("unknown")


def test_a_reused_pid_reads_invalid_through_the_invocation(station):
    """Same MainPID at both ends, but a new activation: the bracket must see it."""
    start = _status(station)
    end = _status(station, INVOCATION="33333333333333333333333333333333")
    assert end["mainpid"] == start["mainpid"]
    assert _verify(station, start["bracket"])
    assert not _verify(station, start["bracket"], INVOCATION="3" * 32)
    unread = _status(station, INVOCATION="-")
    assert unread["invocation"] == "unknown"
    assert unread["bracket"].startswith("unknown (the server's invocation id is unreadable"), unread
    assert not _verify(station, unread["bracket"], INVOCATION="-"), "two unknowns prove nothing"


@pytest.mark.parametrize("shape", ["directory-to-file", "file-to-directory"])
def test_a_tracked_path_the_range_replaces_is_not_a_collision(station, shape):
    """git replaces a tracked file or directory itself; only what it does not
    track is at risk."""
    if shape == "directory-to-file":
        _advance_upstream(station, "dir", {"thing/child.txt": "c\n"})
    else:
        _advance_upstream(station, "file", {"thing": "f\n"})
    _git(station["root"], "pull", "-q", "--ff-only")
    _git(station["seed"], "rm", "-rq", "thing")
    if shape == "directory-to-file":
        tip = _commit(station["seed"], "now a file", {"thing": "f\n"})
    else:
        tip = _commit(station["seed"], "now a dir", {"thing/child.txt": "c\n"})
    _git(station["seed"], "push", "-q", "origin", "main")
    r = _run(station)
    assert r.returncode == 0, r.stderr
    assert _git(station["root"], "rev-parse", "HEAD") == tip


def test_an_ignored_file_inside_a_replaced_directory_still_refuses(station):
    """Control: the tracked directory goes, but an ignored file in it is not
    git's, and the fast-forward would delete it."""
    _advance_upstream(station, "dir", {"thing/child.txt": "c\n", ".gitignore": "*.env\n"})
    _git(station["root"], "pull", "-q", "--ff-only")
    local = station["root"] / "thing" / "local.env"
    local.write_text("MY SECRET\n")
    _git(station["seed"], "rm", "-rq", "thing")
    _commit(station["seed"], "now a file", {"thing": "f\n"})
    _git(station["seed"], "push", "-q", "origin", "main")
    r = _run(station)
    assert r.returncode == 1, r.stdout
    assert "already exist here, untracked" in r.stderr, r.stderr
    assert local.read_text() == "MY SECRET\n"


def test_status_fingerprints_the_user_config_overlays(station):
    """Codex, #2557 round 3: the loaders prefer ~/.genesis/config/<name>.local.yaml
    (where the dashboard's settings writes land) over the checkout's, and some
    reread it live. An edit there during a validation must void the bracket."""
    overlays = station["home"] / ".genesis" / "config"
    overlays.mkdir(parents=True, exist_ok=True)
    (overlays / "genesis.yaml").write_text("not an overlay: left out\n")
    start = _status(station)
    assert start["runtime-overrides"] == "none", start
    overlay = overlays / "routing.local.yaml"
    overlay.write_text("a: 1\n")
    mid = _status(station)
    assert mid["runtime-overrides"].startswith("1 files, "), mid
    assert mid["bracket"].startswith("b1-"), mid
    overlay.write_text("a: 2\n")
    assert not _verify(station, mid["bracket"])


def test_a_dangling_overlay_link_is_skipped_as_the_loader_skips_it(station):
    """The loader reads an overlay only when is_file() holds, so a dangling link is
    no input; it must not make every bracket read "unreadable"."""
    overlays = station["home"] / ".genesis" / "config"
    overlays.mkdir(parents=True, exist_ok=True)
    (overlays / "gone.local.yaml").symlink_to(overlays / "missing.yaml")
    (overlays / "real.yaml").write_text("a: 1\n")
    (overlays / "linked.local.yaml").symlink_to(overlays / "real.yaml")
    s = _status(station)
    assert s["runtime-overrides"].startswith("1 files, "), (
        "the live link counts, the dangling one not"
    )
    assert s["bracket"].startswith("b1-"), s


def test_every_documented_validation_hold_uses_the_scripts_lock_path():
    """Codex, #2557 round 3: the script locks ${GENESIS_HOME:-$HOME/.genesis}/locks;
    a recipe that hardcodes ~/.genesis locks a different file on an install that
    moves GENESIS_HOME, and the hold then serializes nothing."""
    import re

    root = SCRIPT.parent.parent
    docs = [
        SCRIPT,
        root / ".claude" / "skills" / "genesis-development" / "SKILL.md",
        root / "changelog.d" / "20260927020000-added-deploy-code-only.md",
    ]
    found = 0
    for doc in docs:
        for line in doc.read_text().splitlines():
            for m in re.finditer(r"flock -s -w 7200 (\S+)", line):
                found += 1
                assert m.group(1).startswith('"${GENESIS_HOME:-$HOME/.genesis}/locks/'), (doc, line)
    assert found >= 3, "every doc names the hold"


def test_status_fingerprints_ignored_runtime_overrides(station):
    """git status never lists an ignored config/*.local.yaml; the bracket must
    still see it change. Files the server rewrites itself are left out."""
    _advance_upstream(
        station,
        "ignore",
        {".gitignore": "config/*.local.yaml\nconfig/procedure_triggers.json\n"},
    )
    _git(
        station["root"],
        "pull",
        "-q",
        "--ff-only",
        env=_env(station, GIT_COMMITTER_DATE=_later(station)),
    )
    assert _status(station)["runtime-overrides"] == "none"
    (station["root"] / "config").mkdir()
    override = station["root"] / "config" / "routing.local.yaml"
    override.write_text("a: 1\n")
    start = _status(station)
    assert start["runtime-overrides"].startswith("1 files, "), start
    assert start["runtime-edits"] == "none", "ignored: runtime-edits cannot see it"
    (station["root"] / "config" / "procedure_triggers.json").write_text("{}\n")
    assert _status(station)["runtime-overrides"] == start["runtime-overrides"]
    override.write_text("a: 2\n")
    assert start["bracket"].startswith("b1-"), start
    end = _status(station)
    assert end["runtime-overrides"] != start["runtime-overrides"]
    assert not _verify(station, start["bracket"])
