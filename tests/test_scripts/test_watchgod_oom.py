"""tmp_watchgod durable OOM capture, attributed by cgroup slice COUNTERS.

On a NEW container oom_kill the watchgod writes a durable snapshot and pages
once — unless every new kill is a capped workload hitting its OWN cap, which
the contained slices' own memory.events prove (`oom_kill` = where the victim
lived, `oom` = whose limit fired). The journal is used only to name units.

Why counters and not journal notices (#3036): systemd v255 logs one notice per
counter INCREASE however many processes died, and newer systemd words it
differently. The fixtures below are a fake cgroup tree: a container root
memory.events plus a user-manager directory holding the contained slices.

tmux and journalctl are STUBS (never a real session, never the real journal).
queue_alert_try is overridden to a call-log so we can assert page-once. The
script's sourcing guard loads its functions without starting the daemon.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

_WATCHGOD = Path(__file__).resolve().parents[2] / "scripts" / "tmp_watchgod.sh"

_TMUX_STUB = "#!/usr/bin/env bash\nexit 0\n"

# journalctl stub: STUB_JOURNAL is printed (one unit per line, as
# `--output-fields=USER_UNIT -o cat` prints them); STUB_JOURNAL_RC fails it;
# STUB_JOURNAL_ARGLOG records the arguments.
_JOURNALCTL_STUB = r"""#!/usr/bin/env bash
[[ -n "${STUB_JOURNAL_ARGLOG:-}" ]] && echo "$*" >> "$STUB_JOURNAL_ARGLOG"
[[ "${STUB_JOURNAL_RC:-0}" != 0 ]] && exit "$STUB_JOURNAL_RC"
[[ -n "${STUB_JOURNAL:-}" ]] && printf '%s\n' "$STUB_JOURNAL"
exit 0
"""


def _make_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _sandbox(tmp_path):
    home = tmp_path / "home"
    (home / ".genesis" / "logs").mkdir(parents=True)
    (home / ".genesis" / "alerts").mkdir(parents=True)
    (home / ".genesis" / "cc-tmp").mkdir(parents=True)
    bind = tmp_path / "bin"
    bind.mkdir()
    _make_exec(bind / "tmux", _TMUX_STUB)
    _make_exec(bind / "journalctl", _JOURNALCTL_STUB)
    return home, bind


_PRELUDE = (
    "queue_alert_try() { "
    '[[ -e "$HOME/.genesis/alerts/FAIL" ]] && return 1; '
    'local _n_file="$HOME/.genesis/alerts/FAIL_N" _n=0; '
    '[[ -f "$_n_file" ]] && _n=$(cat "$_n_file"); '
    'if (( _n > 0 )); then echo $(( _n - 1 )) > "$_n_file"; return 1; fi; '
    'echo "ALERT $*" >> "$HOME/.genesis/alerts/calls.log"; }; '
    'queue_alert() { queue_alert_try "$@" || true; }; '
)


def _events(path: Path, kills: int, ooms: int) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "memory.events").write_text(
        f"low 0\nhigh 0\nmax 0\noom {ooms}\noom_kill {kills}\noom_group_kill 0\n"
    )


class _Tree:
    """A fake cgroup tree: container root + the user manager's slices."""

    def __init__(self, tmp_path):
        self.root = tmp_path / "cg"
        self.user = self.root / "user"
        self.capped = self.user / "app.slice" / "app-capped.slice"
        self.workload = self.user / "genesis.slice" / "genesis-workload.slice"

    def set(self, root=(0, 0), capped=(0, 0), workload=None):
        _events(self.root, *root)
        if capped is None:
            shutil.rmtree(self.capped, ignore_errors=True)
        else:
            _events(self.capped, *capped)
        if workload is None:
            shutil.rmtree(self.workload, ignore_errors=True)
        else:
            _events(self.workload, *workload)

    def env(self, extra=None):
        env = {
            "OOM_EVENTS_FILE": str(self.root / "memory.events"),
            "OOM_USER_CGROUP_DIR": str(self.user),
        }
        env.update(extra or {})
        return env


def _run(home, bind, snippet, env):
    full = dict(os.environ)
    full.update(HOME=str(home), PATH=f"{bind}:{os.environ['PATH']}")
    full.update({k: str(v) for k, v in env.items()})
    out = subprocess.run(
        ["bash", "-c", f"source '{_WATCHGOD}'\n{_PRELUDE}{snippet}"],
        env=full, capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    return out.stdout


def _arm(home, bind, tree, extra=None) -> str:
    return _run(home, bind, 'echo "S=$(_oom_arm_baseline)"', tree.env(extra)).split("S=")[1].strip()


def _tick(home, bind, tree, spec, extra=None) -> str:
    return _run(home, bind, f'echo "S=$(check_oom_events \'{spec}\')"', tree.env(extra)).split(
        "S="
    )[1].strip()


def _calls(home) -> str:
    f = home / ".genesis" / "alerts" / "calls.log"
    return f.read_text() if f.exists() else ""


def _oom_log(home) -> str:
    f = home / ".genesis" / "logs" / "oom_events.log"
    return f.read_text() if f.exists() else ""


def _watchgod_log(home) -> str:
    return "".join(p.read_text() for p in (home / ".genesis" / "logs").glob("*.log"))


def _setup(tmp_path, **state):
    home, bind = _sandbox(tmp_path)
    tree = _Tree(tmp_path)
    tree.set(**state)
    return home, bind, tree


# ── arming and quiet ticks ────────────────────────────────────────────────


def test_arm_records_root_and_every_contained_slice(tmp_path):
    home, bind, tree = _setup(tmp_path, root=(4, 4), capped=(3, 3))
    spec = _arm(home, bind, tree)
    k, o, states = spec.split(":", 2)
    assert (k, o) == ("4", "4")
    assert "app-capped.slice=" in states and ",3,3" in states
    assert "genesis-workload.slice=-" in states, "an absent slice is recorded as absent"


def test_arm_removes_the_old_journal_cursor(tmp_path):
    home, bind, tree = _setup(tmp_path)
    cursor = home / ".genesis" / "logs" / ".oom_journal_cursor"
    cursor.write_text("s=old")
    _arm(home, bind, tree)
    assert not cursor.exists()


def test_unreadable_root_means_monitoring_unavailable(tmp_path):
    home, bind, tree = _setup(tmp_path)
    spec = _arm(home, bind, tree, {"OOM_EVENTS_FILE": str(tmp_path / "nope")})
    assert spec == ""


def test_no_new_kill_is_silent(tmp_path):
    home, bind, tree = _setup(tmp_path, root=(4, 4), capped=(3, 3))
    spec = _arm(home, bind, tree)
    assert _tick(home, bind, tree, spec).startswith("4:4:")
    assert _calls(home) == "" and _oom_log(home) == ""


def test_late_arm_baselines_and_decides_nothing(tmp_path):
    home, bind, tree = _setup(tmp_path, root=(9, 9))
    spec = _tick(home, bind, tree, "")
    assert spec.startswith("9:9:")
    assert _calls(home) == ""
    assert "armed late" in _watchgod_log(home)


# ── the attribution rules ─────────────────────────────────────────────────


def test_a_capped_job_killed_at_its_own_cap_does_not_page(tmp_path):
    home, bind, tree = _setup(tmp_path, root=(4, 4), capped=(3, 3))
    spec = _arm(home, bind, tree)
    tree.set(root=(5, 5), capped=(4, 4))
    new = _tick(home, bind, tree, spec, {"STUB_JOURNAL": "genesis-job-x-1.scope"})
    assert new.startswith("5:5:")
    assert _calls(home) == ""
    log = _oom_log(home)
    assert "oom_kill 4 -> 5 (+1)" in log, "the snapshot is kept"
    assert "contained 1 in app-capped.slice +1" in log and "PAGE" not in log
    assert "not paging" in _watchgod_log(home)


def test_two_kills_in_one_capped_job_are_both_accounted(tmp_path):
    """The case notice-counting got wrong: systemd logs ONE notice for both."""
    home, bind, tree = _setup(tmp_path, root=(4, 4), capped=(3, 3))
    spec = _arm(home, bind, tree)
    tree.set(root=(6, 5), capped=(5, 4))
    _tick(home, bind, tree, spec, {"STUB_JOURNAL": "genesis-job-x-1.scope"})
    assert _calls(home) == ""


def test_r1_a_kill_outside_the_slices_pages_and_names_the_unit(tmp_path):
    home, bind, tree = _setup(tmp_path, root=(4, 4), capped=(3, 3))
    spec = _arm(home, bind, tree)
    tree.set(root=(5, 5), capped=(3, 3))
    _tick(home, bind, tree, spec, {"STUB_JOURNAL": "genesis-server.service"})
    calls = _calls(home)
    assert "emergency watchgod:oom" in calls and calls.rstrip().endswith("watchgod:oom:5")
    assert "outside the contained slices" in calls and "genesis-server.service" in calls


def test_r1_a_mixed_tick_pages(tmp_path):
    home, bind, tree = _setup(tmp_path, root=(4, 4), capped=(3, 3))
    spec = _arm(home, bind, tree)
    tree.set(root=(6, 6), capped=(4, 4))
    _tick(home, bind, tree, spec)
    assert "1 of 2 kill(s) outside" in _calls(home)


def test_r2_a_limit_outside_the_slices_pages_even_with_a_contained_victim(tmp_path):
    """The container's own limit fired and the kernel picked a capped batch."""
    home, bind, tree = _setup(tmp_path, root=(4, 4), capped=(3, 3))
    spec = _arm(home, bind, tree)
    tree.set(root=(5, 6), capped=(4, 4))  # one more limit hit than the slice saw
    _tick(home, bind, tree, spec)
    assert "a memory limit outside the contained slices fired" in _calls(home)


def test_r3_a_contained_kill_with_no_limit_of_its_own_pages(tmp_path):
    """Victim inside, trigger above the container: no oom anywhere we see."""
    home, bind, tree = _setup(tmp_path, root=(4, 4), capped=(3, 3))
    spec = _arm(home, bind, tree)
    tree.set(root=(5, 4), capped=(4, 3))
    _tick(home, bind, tree, spec)
    assert "with no limit of its own firing" in _calls(home)


def test_more_contained_kills_than_container_kills_pages(tmp_path):
    home, bind, tree = _setup(tmp_path, root=(4, 4), capped=(3, 3))
    spec = _arm(home, bind, tree)
    tree.set(root=(5, 5), capped=(6, 6))
    _tick(home, bind, tree, spec)
    assert "report more kills" in _calls(home)


def test_unreadable_root_limit_counter_pages(tmp_path):
    home, bind, tree = _setup(tmp_path, root=(4, 4), capped=(3, 3))
    spec = _arm(home, bind, tree)
    tree.set(root=(5, 5), capped=(4, 4))
    (tree.root / "memory.events").write_text("oom_kill 5\n")  # no `oom` line
    _tick(home, bind, tree, spec)
    assert "trigger is unknown" in _calls(home)


# ── slice lifecycle (the baselines) ────────────────────────────────────────


def test_a_slice_born_since_the_last_tick_counts_from_zero(tmp_path):
    home, bind, tree = _setup(tmp_path, root=(4, 4), capped=None)
    spec = _arm(home, bind, tree)
    assert "app-capped.slice=-" in spec
    tree.set(root=(5, 5), capped=(1, 1))
    _tick(home, bind, tree, spec)
    assert _calls(home) == ""


def test_a_recreated_slice_counts_from_its_new_start(tmp_path):
    home, bind, tree = _setup(tmp_path, root=(4, 4), capped=(3, 3))
    spec = _arm(home, bind, tree)
    ino0 = tree.capped.stat().st_ino
    # Build the new directory while the old one still exists, so the filesystem
    # cannot hand back the freed inode (ext4 can on rmdir+mkdir).
    fresh = tree.capped.with_name("app-capped.slice.new")
    _events(fresh, 1, 1)
    shutil.rmtree(tree.capped)
    fresh.rename(tree.capped)
    _events(tree.root, 5, 5)
    assert tree.capped.stat().st_ino != ino0
    _tick(home, bind, tree, spec)
    assert _calls(home) == ""


def test_counters_going_backwards_on_the_same_slice_page(tmp_path):
    home, bind, tree = _setup(tmp_path, root=(4, 4), capped=(3, 3))
    spec = _arm(home, bind, tree)
    tree.set(root=(5, 5), capped=(1, 1))  # same directory, smaller counters
    _tick(home, bind, tree, spec)
    assert "went backwards" in _calls(home)


def test_an_unreadable_slice_explains_nothing(tmp_path):
    home, bind, tree = _setup(tmp_path, root=(4, 4), capped=(3, 3))
    spec = _arm(home, bind, tree)
    tree.set(root=(5, 5), capped=(4, 4))
    (tree.capped / "memory.events").write_text("garbage\n")
    new = _tick(home, bind, tree, spec)
    assert "app-capped.slice unreadable" in _calls(home)
    assert "app-capped.slice=?" in new


def test_an_unknown_baseline_explains_nothing(tmp_path):
    """A slice that was unreadable last tick: its delta is unknown, so the kill
    pages, and the next tick baselines it afresh."""
    home, bind, tree = _setup(tmp_path, root=(5, 5), capped=(4, 4))
    spec = "4:4:app-capped.slice=?;genesis-workload.slice=-"
    new = _tick(home, bind, tree, spec)
    assert "no known baseline" in _calls(home)
    assert ",4,4" in new


def test_no_user_cgroup_dir_means_nothing_is_contained(tmp_path):
    home, bind, tree = _setup(tmp_path, root=(4, 4), capped=(3, 3))
    spec = _arm(home, bind, tree, {"OOM_USER_CGROUP_DIR": str(tmp_path / "missing")})
    tree.set(root=(5, 5), capped=(4, 4))
    _tick(home, bind, tree, spec, {"OOM_USER_CGROUP_DIR": str(tmp_path / "missing")})
    assert "emergency watchgod:oom" in _calls(home)


def test_a_snapshot_that_will_not_hold_still_pages(tmp_path):
    """A kill landing between the root read and the slice read would credit a
    slice for a kill the root has not counted; the read retries, and pages if
    the counters never hold still."""
    home, bind, tree = _setup(tmp_path, root=(4, 4), capped=(3, 3))
    spec = _arm(home, bind, tree)
    tree.set(root=(5, 5), capped=(4, 4))
    counter = tmp_path / "n"
    counter.write_text("0")
    # Each read runs in a command substitution, so the count lives in a file.
    moving = (
        f'_read_oom_kill() {{ local n; n=$(cat {counter}); echo $((n+1)) > {counter}; '
        'echo $((10+n)); }; '
        f'echo "S=$(check_oom_events \'{spec}\')"'
    )
    _run(home, bind, moving, tree.env())
    assert "would not hold still" in _calls(home)


# ── configuration ────────────────────────────────────────────────────────


def test_contained_slice_list_is_validated(tmp_path):
    home, bind, tree = _setup(tmp_path)
    out = _run(
        home, bind, "_oom_contained_slices | tr '\\n' ' '",
        tree.env({"OOM_CONTAINED_SLICES": (
            "app.slice app-capped.slice app-capped-x.slice bad..slice -.slice "
            "genesis-workload.slice unit@.slice"
        )}),
    )
    assert out.split() == ["app-capped.slice", "genesis-workload.slice"]


def test_slice_names_nest_by_dash(tmp_path):
    home, bind, tree = _setup(tmp_path)
    out = _run(home, bind, "_oom_slice_relpath a-b-c.slice", tree.env())
    assert out == "a.slice/a-b.slice/a-b-c.slice"


def test_default_contained_slices(tmp_path):
    home, bind, tree = _setup(tmp_path)
    out = _run(home, bind, 'echo "$OOM_CONTAINED_SLICES"', tree.env())
    assert out.split() == ["app-capped.slice", "genesis-workload.slice"]


def test_a_workload_slice_kill_is_contained_too(tmp_path):
    home, bind, tree = _setup(tmp_path, root=(4, 4), capped=(3, 3), workload=(0, 0))
    spec = _arm(home, bind, tree)
    tree.set(root=(5, 5), capped=(3, 3), workload=(1, 1))
    _tick(home, bind, tree, spec)
    assert _calls(home) == ""


# ── naming (journal) ─────────────────────────────────────────────────────


def test_naming_reads_the_structured_field_by_message_id(tmp_path):
    home, bind, tree = _setup(tmp_path, root=(4, 4), capped=(3, 3))
    spec = _arm(home, bind, tree)
    tree.set(root=(5, 5), capped=(3, 3))
    arglog = tmp_path / "jargs"
    _tick(home, bind, tree, spec, {
        "STUB_JOURNAL": "app.slice\nsvc.service\nsvc.service", "STUB_JOURNAL_ARGLOG": arglog,
    })
    args = arglog.read_text()
    assert "MESSAGE_ID=fe6faa94e7774663a0da52717891d8ef" in args
    assert "--output-fields=USER_UNIT" in args
    calls = _calls(home)
    assert "recent journal OOM notices: svc.service" in calls, "slices dropped, names de-duplicated"


def test_an_unavailable_journal_still_pages(tmp_path):
    home, bind, tree = _setup(tmp_path, root=(4, 4), capped=(3, 3))
    spec = _arm(home, bind, tree)
    tree.set(root=(5, 5), capped=(3, 3))
    _tick(home, bind, tree, spec, {"STUB_JOURNAL_RC": 1})
    assert "recent journal OOM notices: none found" in _calls(home)


# ── owed delivery (#2514) ─────────────────────────────────────────────────


def test_failed_enqueue_owes_the_page_in_the_spec(tmp_path):
    home, bind, tree = _setup(tmp_path, root=(3, 3), capped=(0, 0))
    spec = _arm(home, bind, tree)
    tree.set(root=(5, 5), capped=(0, 0))
    (home / ".genesis" / "alerts" / "FAIL").touch()
    new = _tick(home, bind, tree, spec)
    assert new.startswith("5:5:") and new.endswith(":owed=3-5"), new
    assert _calls(home) == ""
    assert "could not be queued" in _watchgod_log(home)


def test_owed_page_is_retried_next_tick_and_then_cleared(tmp_path):
    home, bind, tree = _setup(tmp_path, root=(3, 3), capped=(0, 0))
    spec = _arm(home, bind, tree)
    tree.set(root=(5, 5), capped=(0, 0))
    (home / ".genesis" / "alerts" / "FAIL").touch()
    s1 = _tick(home, bind, tree, spec)
    (home / ".genesis" / "alerts" / "FAIL").unlink()
    s2 = _tick(home, bind, tree, s1)
    s3 = _tick(home, bind, tree, s2)
    assert "owed=" not in s2 and "owed=" not in s3
    calls = _calls(home)
    assert calls.count("emergency watchgod:oom") == 1, calls
    assert "delayed page" in calls and calls.rstrip().endswith("watchgod:oom:5")


def test_new_kill_while_owed_folds_both_ranges_into_one_page(tmp_path):
    home, bind, tree = _setup(tmp_path, root=(5, 5), capped=(0, 0))
    spec = _arm(home, bind, tree) + ":owed=3-5"
    tree.set(root=(7, 7), capped=(0, 0))
    (home / ".genesis" / "alerts" / "FAIL_N").write_text("1")  # the retry fails
    new = _tick(home, bind, tree, spec)
    assert new.startswith("7:") and "owed=" not in new
    calls = _calls(home)
    assert calls.count("emergency watchgod:oom") == 1
    assert "oom_kill 5->7" in calls and "oom_kill 3->5" in calls


def test_owed_page_survives_a_contained_kill_tick(tmp_path):
    home, bind, tree = _setup(tmp_path, root=(5, 5), capped=(0, 0))
    spec = _arm(home, bind, tree) + ":owed=3-5"
    tree.set(root=(6, 6), capped=(1, 1))
    (home / ".genesis" / "alerts" / "FAIL").touch()
    new = _tick(home, bind, tree, spec)
    assert new.endswith(":owed=3-5"), "a contained tick neither pages nor drops the debt"


def test_unreadable_counter_keeps_what_is_owed(tmp_path):
    home, bind, tree = _setup(tmp_path, root=(5, 5))
    spec = _arm(home, bind, tree) + ":owed=3-5"
    (home / ".genesis" / "alerts" / "FAIL").touch()
    new = _tick(home, bind, tree, spec, {"OOM_EVENTS_FILE": str(tmp_path / "gone")})
    assert new == spec


def test_new_kill_while_still_failing_extends_the_owed_range(tmp_path):
    home, bind, tree = _setup(tmp_path, root=(5, 5), capped=(0, 0))
    spec = _arm(home, bind, tree) + ":owed=3-5"
    tree.set(root=(7, 7), capped=(0, 0))
    (home / ".genesis" / "alerts" / "FAIL").touch()
    new = _tick(home, bind, tree, spec)
    assert new.endswith(":owed=3-7"), new
    assert _calls(home) == ""


def test_successful_retry_and_a_new_kill_page_separately(tmp_path):
    home, bind, tree = _setup(tmp_path, root=(5, 5), capped=(0, 0))
    spec = _arm(home, bind, tree) + ":owed=3-5"
    tree.set(root=(7, 7), capped=(0, 0))
    new = _tick(home, bind, tree, spec)
    assert "owed=" not in new
    calls = _calls(home)
    assert calls.count("emergency watchgod:oom") == 2, calls
    assert "watchgod:oom:5" in calls and "watchgod:oom:7" in calls


def test_unreadable_counter_still_delivers_what_is_owed(tmp_path):
    home, bind, tree = _setup(tmp_path, root=(5, 5))
    spec = _arm(home, bind, tree) + ":owed=3-5"
    new = _tick(home, bind, tree, spec, {"OOM_EVENTS_FILE": str(tmp_path / "gone")})
    assert "owed=" not in new
    assert "delayed page" in _calls(home)


def test_malformed_counter_field_keeps_what_is_owed(tmp_path):
    home, bind, tree = _setup(tmp_path, root=(5, 5))
    (home / ".genesis" / "alerts" / "FAIL").touch()
    new = _tick(home, bind, tree, "garbage:x:y=z:owed=3-5")
    assert new.endswith(":owed=3-5"), new


def test_unqueued_page_logs_who_was_killed(tmp_path):
    home, bind, tree = _setup(tmp_path, root=(4, 4), capped=(3, 3))
    spec = _arm(home, bind, tree)
    tree.set(root=(5, 5), capped=(3, 3))
    (home / ".genesis" / "alerts" / "FAIL").touch()
    _tick(home, bind, tree, spec, {"STUB_JOURNAL": "svc.service"})
    log = _watchgod_log(home)
    assert "could not be queued" in log
    assert "outside the contained slices" in log and "svc.service" in log


def test_slice_list_is_split_without_globbing(tmp_path):
    home, bind, tree = _setup(tmp_path)
    (tmp_path / "x.slice").touch()
    out = _run(
        home,
        bind,
        f"cd {tmp_path}; _oom_contained_slices",
        tree.env({"OOM_CONTAINED_SLICES": "*.slice app-capped.slice"}),
    )
    assert out.split() == ["app-capped.slice"]
# ── wiring ───────────────────────────────────────────────────────────────


def test_the_daemon_loop_calls_the_check_with_the_armed_baseline():
    src = _WATCHGOD.read_text()
    assert "oom_baseline=$(_oom_arm_baseline)" in src
    assert 'oom_baseline=$(check_oom_events "$oom_baseline")' in src
