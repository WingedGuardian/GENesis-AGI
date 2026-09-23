"""update.sh heals resident repo-script daemons running pre-pull code.

Origin (measured, 2026-09-04): scripts/tmp_watchgod.sh gained cgroup
OOM-event capture on Aug 21, but the running genesis-tmp-watchgod daemon
had started Jul 3 and nothing in any deploy path restarts resident
daemons — the detector was inert for two weeks, including during the
incident it was built to explain. Timer-fired oneshots re-exec their
script every tick and are immune; genesis-server/bridge have their own
restart handling. The heal lives INSIDE _sync_deploy_targets so it runs
on BOTH the post-merge path and the "Already up to date" no-op path —
the stale daemon above survived several no-op runs, so a step on only
the merge path would never have healed it.

Verdict is observed-state (unit's ExecMainStartTimestamp vs the backing
script's file mtime — mtime is stamped at checkout, i.e. when the code
arrived on THIS install, where a commit timestamp is authored upstream
and misses the started-between-commit-and-pull window). Fail direction:
an unreadable timestamp means no restart (a spurious restart is one
harmless bounce; the heal exists for the opposite miss).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

_UPDATE = Path(__file__).resolve().parents[2] / "scripts" / "update.sh"


@pytest.fixture(scope="module")
def text() -> str:
    return _UPDATE.read_text()


def _extract_func(text: str, name: str) -> str:
    """Extract a `name() { ... }` definition (brace at column 0 closes it)."""
    m = re.search(rf"^{re.escape(name)}\(\) \{{\n(.*?)\n\}}$", text, re.DOTALL | re.MULTILINE)
    assert m, f"function {name} not found in update.sh"
    return f"{name}() {{\n{m.group(1)}\n}}"


def _extract_block(text: str) -> str:
    m = re.search(
        r"# BEGIN unit-restart-check.*?\n(.*?)\n\s*# END unit-restart-check",
        text,
        re.DOTALL,
    )
    assert m, "unit-restart-check block not found in update.sh"
    return m.group(1)


# ── Extraction locks ──────────────────────────────────────────────────────


def test_block_lives_inside_sync_deploy_targets(text: str) -> None:
    """The heal must run on BOTH call paths (post-merge AND no-op), which
    only _sync_deploy_targets reaches — and the phase-order suite pins that
    function to exactly two bare call sites, so the block goes INSIDE it."""
    func = _extract_func(text, "_sync_deploy_targets")
    assert "# BEGIN unit-restart-check" in func
    assert "# END unit-restart-check" in func


def test_table_covers_tmp_watchgod(text: str) -> None:
    """The verified-stale daemon is in the unit table."""
    m = re.search(r'^RESIDENT_UNIT_SCRIPTS="([^"]*)"', text, re.MULTILINE)
    assert m, "RESIDENT_UNIT_SCRIPTS table not found"
    assert "genesis-tmp-watchgod.service:scripts/tmp_watchgod.sh" in m.group(1)
    # Every startup-loaded file, not just ExecStart (sourced-lib finding):
    assert "scripts/lib/alert_queue.sh" in m.group(1)


def test_tables_in_lockstep_with_deploy_health(text: str) -> None:
    """The heal (update.sh) and the alert (deploy_health) share one verdict
    over one unit set — a unit added to only one table is healed-but-never-
    alerted or alerted-forever-but-never-healed. Assert equality, not a
    comment promise."""
    from genesis.observability.snapshots.deploy_health import RESIDENT_UNIT_SCRIPTS

    m = re.search(r'^RESIDENT_UNIT_SCRIPTS="([^"]*)"', text, re.MULTILINE)
    assert m
    bash_pairs = {
        pair.split(":", 1)[0]: tuple(pair.split(":", 1)[1].split(","))
        for pair in m.group(1).split()
    }
    assert bash_pairs == {k: tuple(v) for k, v in RESIDENT_UNIT_SCRIPTS.items()}


def test_block_is_nonfatal_throughout(text: str) -> None:
    """set -e is live at both call sites (ERR trap armed on the no-op path):
    every command in the block that can fail must be guarded."""
    block = _extract_block(text)
    for line in block.splitlines():
        s = line.strip()
        if s.startswith("systemctl --user restart"):
            assert "||" in s, f"unguarded restart under set -e: {s}"


# ── Functional: the pure verdict helper ───────────────────────────────────


def _reason(text: str, *args: str) -> str:
    func = _extract_func(text, "_unit_restart_reason")
    argv = " ".join(f"'{a}'" for a in args)
    r = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{func}\n_unit_restart_reason {argv}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert r.returncode == 0, f"helper must never fail under set -e: {r.stderr}"
    return r.stdout.strip()


class TestUnitRestartReason:
    def test_stale_daemon_restarts(self, text: str) -> None:
        """The origin case: daemon started before its script last changed."""
        out = _reason(text, "1", "1000", "2000", "genesis-tmp-watchgod.service")
        assert "genesis-tmp-watchgod.service" in out

    def test_fresh_daemon_untouched(self, text: str) -> None:
        assert _reason(text, "1", "3000", "2000", "u.service") == ""

    def test_inactive_unit_skipped(self, text: str) -> None:
        """Restarting a stopped unit would START it — enablement is
        bootstrap's decision, not the drift heal's."""
        assert _reason(text, "0", "1000", "2000", "u.service") == ""

    def test_unreadable_start_time_fails_open(self, text: str) -> None:
        assert _reason(text, "1", "", "2000", "u.service") == ""

    def test_unreadable_script_time_fails_open(self, text: str) -> None:
        assert _reason(text, "1", "1000", "", "u.service") == ""

    def test_nonnumeric_facts_fail_open(self, text: str) -> None:
        assert _reason(text, "1", "n/a", "2000", "u.service") == ""


# ── Functional: the loop, with a recording systemctl stub ────────────────


def _run_block(
    text: str,
    tmp_path: Path,
    *,
    active: bool,
    start_epoch: int,
    script_mtime: int,
    show_fails: bool = False,
    script_exists: bool = True,
    lib_mtime: int | None = None,
    restart_fails: bool = False,
    future_mtime: bool = False,
    restart_crashloops: bool = False,
    operator_stops: bool = False,
    installed_unit_differs: bool = False,
) -> tuple[str, list[str], Path]:
    """Run the extracted block against a stub systemctl + a real tmp script."""
    root = tmp_path / "root"
    (root / "scripts" / "lib").mkdir(parents=True)
    (root / "scripts" / "systemd").mkdir(parents=True)
    script = root / "scripts" / "tmp_watchgod.sh"
    lib = root / "scripts" / "lib" / "alert_queue.sh"
    template = root / "scripts" / "systemd" / "genesis-tmp-watchgod.service.template"
    import os

    # The block sources the shared renderer — install the real one.
    render_lib = (
        _UPDATE.parent / "lib" / "render_systemd_template.sh"
    )
    (root / "scripts" / "lib" / "render_systemd_template.sh").write_text(
        render_lib.read_text()
    )
    # HOME is redirected: the render step writes the installed unit under
    # ~/.config/systemd/user — never the real home.
    home = tmp_path / "home"
    installed_unit = home / ".config" / "systemd" / "user" / "genesis-tmp-watchgod.service"
    if installed_unit_differs:
        installed_unit.parent.mkdir(parents=True)
        installed_unit.write_text("# OLD installed unit\n")

    if script_exists:
        script.write_text("#!/bin/bash\n")
        os.utime(script, (script_mtime, script_mtime))
    lib.write_text("#!/bin/bash\n")
    os.utime(lib, (lib_mtime if lib_mtime is not None else script_mtime,) * 2)
    template.write_text("# template\n")
    if future_mtime:
        import time

        fut = int(time.time()) + 10**6
        os.utime(template, (fut, fut))
    else:
        os.utime(template, (script_mtime, script_mtime))

    calls = tmp_path / "systemctl.calls"
    calls.write_text("")
    dead_marker = tmp_path / "unit_dead"
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "systemctl"
    # is-active: the probe reads `active`; after a try-restart that crashed
    # the unit, the post-restart liveness recheck must see it inactive.
    stop_marker = tmp_path / "unit_stopped"
    if restart_crashloops:
        is_active_case = (
            f'  *is-active*) [ -f "{dead_marker}" ] && exit 3 || exit {"0" if active else "3"} ;;\n'
        )
        restart_case = f'  *try-restart*) touch "{dead_marker}" ;;\n'
    elif operator_stops:
        # First is-active (the probe) reads active; every later call reads
        # stopped — an operator stop landing in the is-active→act race. The
        # try-restart no-ops (rc 0, no new invocation): ExecMainStartTimestamp
        # keeps the OLD value below, and the unit must NOT be degraded.
        is_active_case = (
            f'  *is-active*) [ -f "{stop_marker}" ] && exit 3 '
            f'|| {{ touch "{stop_marker}"; exit 0; }} ;;\n'
        )
        restart_case = ""
    else:
        is_active_case = f'  *is-active*) exit {"0" if active else "3"} ;;\n'
        restart_case = '  *try-restart*) exit 1 ;;\n' if restart_fails else ""
    show_line = (
        "  *ExecMainStartTimestamp*) exit 1 ;;\n"
        if show_fails
        else (
            # A REAL restart produces a new main invocation: after
            # dead_marker exists, report a NEWER start timestamp.
            f'  *ExecMainStartTimestamp*) if [ -f "{dead_marker}" ]; then '
            f'date -d @{start_epoch + 1} "+%a %F %T %Z"; else '
            f'date -d @{start_epoch} "+%a %F %T %Z"; fi ;;\n'
        )
    )
    stub.write_text(
        "#!/bin/bash\n"
        f'echo "$*" >> "{calls}"\n'
        'case "$*" in\n'
        + is_active_case
        + show_line
        + restart_case
        + "esac\n"
        "exit 0\n"
    )
    stub.chmod(0o755)

    block = _extract_block(text)
    harness = (
        "set -euo pipefail\n"
        f'PATH="{stub_dir}:$PATH"\n'
        f'GENESIS_ROOT="{root}"\n'
        f'HOME="{home}"\n'
        "_RU_SETTLE_S=0\n"
        'RESIDENT_UNIT_SCRIPTS="genesis-tmp-watchgod.service:scripts/tmp_watchgod.sh,scripts/lib/alert_queue.sh,scripts/systemd/genesis-tmp-watchgod.service.template"\n'
        + _extract_func(text, "_unit_restart_reason")
        + "\n"
        + "HOST_CC_DEGRADED=\"\"\n"
        + block
        + '\necho "RC=$?"\necho "DEGRADED=$HOST_CC_DEGRADED"\n'
    )
    r = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, f"block must not fail under set -e: {r.stderr}"
    return r.stdout, calls.read_text().splitlines(), installed_unit


class TestRestartLoop:
    def test_show_failure_is_survived_and_restarts_nothing(self, text: str, tmp_path: Path) -> None:
        """A probe failure must not abort the run under set -e (the guards
        are load-bearing — this pins them RED-verifiably) and must fail
        OPEN: no timestamp, no restart."""
        out, calls, _unit = _run_block(
            text, tmp_path, active=True, start_epoch=1000, script_mtime=2000,
            show_fails=True,
        )
        assert "RC=0" in out
        assert not any("restart" in c for c in calls), calls

    def test_missing_script_is_survived_and_restarts_nothing(self, text: str, tmp_path: Path) -> None:
        out, calls, _unit = _run_block(
            text, tmp_path, active=True, start_epoch=1000, script_mtime=2000,
            script_exists=False, lib_mtime=500,
        )
        assert "RC=0" in out
        assert not any("restart" in c for c in calls), calls

    def test_stale_running_daemon_is_restarted(self, text: str, tmp_path: Path) -> None:
        """Acceptance bar — the incident shape: daemon started (epoch 1000)
        before the script's code arrived (mtime 2000) → one restart call."""
        _out, calls, _unit = _run_block(text, tmp_path, active=True, start_epoch=1000, script_mtime=2000)
        assert any("restart genesis-tmp-watchgod.service" in c for c in calls), calls

    def test_fresh_daemon_not_restarted(self, text: str, tmp_path: Path) -> None:
        _out, calls, _unit = _run_block(text, tmp_path, active=True, start_epoch=3000, script_mtime=2000)
        assert not any("restart" in c for c in calls), calls

    def test_sourced_lib_change_alone_triggers_restart(self, text: str, tmp_path: Path) -> None:
        """A pull touching only the sourced library must restart the daemon —
        it loaded that code once at startup (external finding)."""
        _out, calls, _unit = _run_block(
            text, tmp_path, active=True, start_epoch=1500,
            script_mtime=1000, lib_mtime=2000,
        )
        assert any("restart genesis-tmp-watchgod.service" in c for c in calls), calls

    def test_restart_failure_marks_degraded(self, text: str, tmp_path: Path) -> None:
        """A failed heal is a degraded deployment, never a silent clean
        (external finding: the accumulator contract at the function head)."""
        out, _calls, _u = _run_block(
            text, tmp_path, active=True, start_epoch=1000, script_mtime=2000,
            restart_fails=True,
        )
        assert "unit_restart_genesis-tmp-watchgod" in out

    def test_inactive_unit_not_started(self, text: str, tmp_path: Path) -> None:
        _out, calls, _unit = _run_block(text, tmp_path, active=False, start_epoch=1000, script_mtime=2000)
        assert not any("restart" in c for c in calls), calls

    def test_missing_main_script_never_restarts_on_partial_facts(self, text: str, tmp_path: Path) -> None:
        """External finding: an unreadable ExecStart must void the unit's
        whole verdict — a newer readable LIBRARY must not restart on
        incomplete freshness facts."""
        out, calls, _unit = _run_block(
            text, tmp_path, active=True, start_epoch=1000, script_mtime=2000,
            script_exists=False, lib_mtime=3000,
        )
        assert "RC=0" in out
        assert not any("restart" in c for c in calls), calls

    def test_future_dated_file_never_restarts(self, text: str, tmp_path: Path) -> None:
        """A future-dated mtime (clock rollback, restored snapshot) is
        untrustworthy like an unreadable one — skip the unit."""
        out, calls, _unit = _run_block(
            text, tmp_path, active=True, start_epoch=1000, script_mtime=2000,
            future_mtime=True,
        )
        assert "RC=0" in out
        assert not any("restart" in c for c in calls), calls

    def test_restart_uses_try_restart(self, text: str, tmp_path: Path) -> None:
        """try-restart preserves an operator-stopped unit across the
        is-active→act race (external finding)."""
        _out, calls, _unit = _run_block(text, tmp_path, active=True, start_epoch=1000, script_mtime=2000)
        assert any("try-restart genesis-tmp-watchgod.service" in c for c in calls), calls
        assert not any("--user restart" in c for c in calls), calls

    def test_crashlooping_restart_marks_degraded(self, text: str, tmp_path: Path) -> None:
        """try-restart exits 0 while the unit sits in activating
        (auto-restart) — the post-restart liveness check must turn that
        into a degraded deployment, not a clean one (external finding)."""
        out, calls, _unit = _run_block(
            text, tmp_path, active=True, start_epoch=1000, script_mtime=2000,
            restart_crashloops=True,
        )
        assert any("try-restart genesis-tmp-watchgod.service" in c for c in calls), calls
        assert "unit_restart_genesis-tmp-watchgod" in out

    def test_operator_stopped_unit_not_marked_degraded(self, text: str, tmp_path: Path) -> None:
        """try-restart exits 0 WITHOUT acting on a unit an operator stopped
        in the is-active→act race — the old ExecMainStartTimestamp proves no
        new invocation, so the intentional stop must not record a degraded
        restart (external finding)."""
        out, calls, _unit = _run_block(
            text, tmp_path, active=True, start_epoch=1000, script_mtime=2000,
            operator_stops=True,
        )
        assert any("try-restart genesis-tmp-watchgod.service" in c for c in calls), calls
        assert "unit_restart_genesis-tmp-watchgod" not in out

    def test_changed_template_is_rendered_before_restart(
        self, text: str, tmp_path: Path
    ) -> None:
        """On the no-op path bootstrap never rendered the newer template, so
        a bare restart would load the OLD directives while the fresh start
        time masked the drift — the heal re-renders + daemon-reloads first
        (external finding)."""
        out, calls, installed = _run_block(
            text, tmp_path, active=True, start_epoch=1000, script_mtime=2000,
            installed_unit_differs=True,
        )
        assert "RC=0" in out
        assert installed.read_text() == "# template\n"
        reloads = [i for i, c in enumerate(calls) if "daemon-reload" in c]
        restarts = [i for i, c in enumerate(calls) if "try-restart" in c]
        assert reloads and restarts and reloads[0] < restarts[-1], calls
