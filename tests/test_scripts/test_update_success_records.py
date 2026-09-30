"""Success rows in update_history must never hide a server that is not running (#2145).

`scripts/update.sh` writes `status='success'` from three places: two on the
no-change path and one at the end of a full update. Each must be able to carry
`genesis-server-not-restarted`, and the no-change path must never write a newer
success row over anything but nothing-recorded or a prior success while the
server is down, because P6's recovery detection reads only the NEWEST status.

The history writer, the status reader, the health probe and the no-change
decision block are driven for real against SQLite fixtures and PATH shims; the call-site invariants are asserted
against the shipped script text, enumerated by the HELPER NAME rather than by the
marker literal (grepping the marker finds only the site that already has it).
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE = REPO_ROOT / "scripts" / "update.sh"

_INHERITED_PREFIXES = ("GENESIS_", "GIT_")


def _clean_env(**overrides: str) -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items() if not key.startswith(_INHERITED_PREFIXES)
    }
    env.update(overrides)
    return env


def _block(text: str, marker: str) -> str:
    match = re.search(
        rf"# BEGIN {re.escape(marker)}.*?\n(.*?)# END {re.escape(marker)}",
        text,
        re.DOTALL,
    )
    assert match, f"missing {marker} block"
    return match.group(1)


def _function(text: str, name: str) -> str:
    start = text.index(f"{name}() {{")
    next_function = re.search(r"\n[A-Za-z_][A-Za-z0-9_]*\(\) \{", text[start + 1 :])
    assert next_function, f"missing function boundary after {name}"
    return text[start : start + 1 + next_function.start()]


def _history_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE update_history ("
        "id TEXT, old_tag TEXT, new_tag TEXT, old_commit TEXT, new_commit TEXT, "
        "status TEXT, rollback_tag TEXT, failure_reason TEXT, "
        "degraded_subsystems TEXT, started_at TEXT, completed_at TEXT)"
    )
    con.commit()
    con.close()


def _case_arms(case_text: str) -> dict[str, str]:
    """Map each `label)` of a bash case statement to its body (labels normalised)."""
    body = case_text.split(" in", 1)[1]
    arms: dict[str, str] = {}
    for chunk in body.split(";;"):
        match = re.match(r"\s*([^()\n]+)\)(.*)", chunk, re.DOTALL)
        if match:
            label = "|".join(part.strip() for part in match.group(1).split("|"))
            arms[label] = match.group(2)
    return arms


# ── the call-site invariants (#2145 acceptance) ─────────────────────────────


def test_every_success_record_uses_marker_capable_variable() -> None:
    """Every success writer must use the SAME predicate, not merely a
    marker-capable variable.

    #2145's criterion is worded "passes a variable that can hold
    `genesis-server-not-restarted`", and its PURPOSE is that no success row can
    name the new HEAD while nothing is serving it. Those are not the same test:
    the P6 site used to pass `_OPERATOR_STOP`, which is only ever true when
    `WERE_RUNNING` was ENTIRELY empty — and `genesis-bridge` populates that array
    too, so a bridge-up / server-down run satisfied the letter while missing the
    purpose. (Today that run rolls back at P6's health gate before any writer,
    #2633; the derivation must still be right for when it does not.) So assert the
    DERIVATION, not the variable name.

    Enumeration is by the HELPER NAME and spelling-agnostic: any quoting, any
    arity. A gate whose denominator is one spelling is a denylist.
    """
    text = UPDATE.read_text()

    invocations = re.findall(r"^[ \t]*_record_update_history[ \t]+(\S.*)$", text, re.M)
    assert len(invocations) >= 3, f"found only {len(invocations)}: {invocations}"

    success = [a for a in invocations if re.split(r"[ \t]+", a)[0].strip("\"'") == "success"]
    assert len(success) == 3, f"expected 3 success writers, got {len(success)}: {success}"

    markers = []
    for args in success:
        parts = re.findall(r'"[^"]*"|\S+', args)
        assert len(parts) == 3, f"unexpected arity in `{args}`: {parts}"
        markers.append(parts[2].strip('"'))
    assert markers == ["$_nd_degraded", "$_nd_degraded", "$_p6_degraded"], markers

    # Each boolean fed to the helper answers "was genesis-server left
    # un-restarted?" — and the two sites answer it DIFFERENTLY on purpose:
    # P6 verifies health and ROLLS BACK on failure, so membership in WERE_RUNNING
    # is the whole question there; the no-delta path has NO health loop and its
    # restart reports success even when the unit never came back, so it must
    # probe health after the attempt. `_OPERATOR_STOP` is disallowed at
    # both: it answers "was the array empty?", a different question.
    feeders = re.findall(r'_success_degraded_subsystems[ \t]*\\?\s*"[^"]*"[ \t]+"([^"]+)"', text)
    assert len(feeders) == 2, f"expected 2 helper call sites, got: {feeders}"
    assert all("_OPERATOR_STOP" not in f for f in feeders), feeders
    assert sorted(f.lstrip("$").strip("{}") for f in feeders) == [
        "_nd_server_not_restarted",
        "_p6_server_not_restarted",
    ], feeders

    membership = r'\[\[ " \$\{WERE_RUNNING\[\*\]\} " == \*" genesis-server "\* \]\]'
    assert re.search(membership + r"[\s\\]*\|\|[ \t]*_p6_server_not_restarted=true", text), (
        "P6 must derive its boolean from WERE_RUNNING membership"
    )
    # No-delta: DEFAULT true, cleared only when the server was meant to be up AND
    # the health endpoint answers after the restart attempt (unit state reads a
    # crash-looping server as `activating`).
    assert "_nd_server_not_restarted=true\n" in text
    assert re.search(
        r"if " + membership + r" && _server_health_ok; then\s*\n\s*_nd_server_not_restarted=false",
        text,
    ), "no-delta must clear the marker only after a health probe"


def test_p6_treats_an_unreadable_status_as_a_recovery() -> None:
    """Unknown is not "nothing failed".

    Recovery costs a restart plus a health check with rollback on failure;
    reading unknown as an operator stop would record success over what may be a
    live failure. Allowlist: exactly these three statuses recover, nothing else.
    """
    text = UPDATE.read_text()
    # Word boundary: `_nd_last_status=…` (the no-delta site) contains this name.
    match = re.search(r'(?<![\w])_last_status="\$\(_latest_update_status\)"', text)
    assert match, "P6 status read not found"
    case_start = text.index('case "$_last_status" in', match.end())
    case = text[case_start : text.index("esac", case_start)]
    arms = _case_arms(case)
    recovering = sorted(
        status
        for label, body in arms.items()
        if "_recovery=true" in body
        for status in label.split("|")
    )
    assert recovering == ["failed", "rolled_back", "unreadable"]


def test_no_delta_path_never_records_success_over_an_unresolved_failure() -> None:
    """With the server down, a latest `failed`/`rolled_back` row must stay latest.

    P6's recovery detection reads only the NEWEST update_history status, and when
    last_update_failure.json was never written that row is the only signal it has.
    """
    text = UPDATE.read_text()
    start = text.index('elif [ -n "$_nd_base_degraded" ]; then')
    branch = text[start : text.index("Nothing to do.", start)]
    assert '_nd_last_status="$(_latest_update_status)"' in branch
    guard = branch[branch.index('case "$_nd_last_status" in') : branch.index("esac")]
    arms = _case_arms(guard)
    # ALLOWLIST: only nothing-recorded or a prior success may be recorded over.
    # Any other status, including one added to the table later, falls to the
    # catch-all, which does not record.
    recording = [label for label, body in arms.items() if "_record_update_history" in body]
    assert recording == ['""|success'], f"only empty/success may record; recording: {recording}"
    assert "*" in arms and "_record_update_history" not in arms["*"]
    # P6 reads the SAME status through the SAME reader, so the two cannot drift.
    assert re.findall(r"(\w+)=\"\$\(_latest_update_status\)\"", text) == [
        "_nd_last_status",
        "_last_status",
    ]
    assert text.count("SELECT status FROM update_history ORDER BY datetime(started_at) DESC") == 1


def test_noop_history_does_not_duplicate_pre_update_degraded() -> None:
    """The writer appends PRE_UPDATE_DEGRADED itself; the no-delta value must not."""
    text = UPDATE.read_text()
    assert '"${HOST_CC_DEGRADED:-}" "$_nd_server_not_restarted"' in text


# ── the helpers, driven for real ─────────────────────────────────────────────


def test_success_degraded_helper_marks_server_not_restarted() -> None:
    script = (
        "set -euo pipefail\n"
        + _block(UPDATE.read_text(), "success-degraded-subsystems")
        + '\n_success_degraded_subsystems "container_cc_sync" true\n'
        + '_success_degraded_subsystems "container_cc_sync" false\n'
        + '_success_degraded_subsystems "" true\n'
    )
    result = subprocess.run(
        ["bash", "-c", script], check=True, env=_clean_env(), capture_output=True, text=True
    )
    assert result.stdout.splitlines() == [
        "container_cc_sync,genesis-server-not-restarted",
        "container_cc_sync",
        "genesis-server-not-restarted",
    ]


def test_deploy_outcome_probes_behave(tmp_path: Path) -> None:
    """`_server_health_ok` through curl and systemctl shims: only a healthy answer
    reads as up, a unit that says it is starting is waited for (bounded), and a
    unit in any other state is not; and `_latest_update_status` against a real
    SQLite fixture: absent is "", unreadable says so, the newest row wins, and the
    database read is $GENESIS_ROOT's, never a fixed home path."""
    block = _block(UPDATE.read_text(), "deploy-outcome-probes")
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    calls = tmp_path / "curl_calls"

    def healthy(
        unit_state: str, answers: list[int], wait: int = 10, direct_pid: str = ""
    ) -> tuple[bool, int]:
        """<answers> are curl's exit codes, call by call (the last repeats).
        <direct_pid> stands for a process _start_genesis_server direct-started."""
        calls.write_text("")
        seq = " ".join(str(a) for a in answers)
        (shim_dir / "curl").write_text(
            "#!/bin/sh\n"
            f'echo x >> "{calls}"\n'
            f'n=$(wc -l < "{calls}")\n'
            f"set -- {seq}\n"
            'i=1; rc=0; for a in "$@"; do rc=$a; [ "$i" -ge "$n" ] && break; i=$((i + 1)); done\n'
            'exit "$rc"\n'
        )
        (shim_dir / "curl").chmod(0o755)
        # An empty state is an UNREADABLE answer, as a lost user bus gives it:
        # nothing on stdout, an error on stderr, a non-zero exit.
        (shim_dir / "systemctl").write_text(
            f"#!/bin/sh\nprintf '%s' '{unit_state}'\nexit 0\n"
            if unit_state
            else "#!/bin/sh\necho 'Failed to connect to bus' >&2\nexit 1\n"
        )
        (shim_dir / "systemctl").chmod(0o755)
        result = subprocess.run(
            [
                "bash",
                "-c",
                f"_SERVER_HEALTH_WAIT_SECS={wait}\n_SERVER_HEALTH_POLL_SECS=1\n"
                + (f"_SERVER_DIRECT_PID={direct_pid}\n" if direct_pid else "")
                + block
                + "\n_server_health_ok",
            ],
            capture_output=True,
            text=True,
            env=_clean_env(PATH=f"{shim_dir}:{os.environ['PATH']}"),
        )
        return result.returncode == 0, len(calls.read_text().splitlines())

    assert healthy("active", [0]) == (True, 1)
    # Booting: a 503 (22) and a refused connection (7), then healthy — waited for.
    assert healthy("activating", [22, 7, 0]) == (True, 3)
    # Starting forever (a crash loop reads `activating`): the ELAPSED bound ends it,
    # after a few attempts (the exact count depends on the clock, not a counter).
    ok, n = healthy("activating", [7], wait=2)
    assert not ok and 1 <= n <= 4, n
    # An affirmative stopped state stops at once: no waiting on a stopped server.
    for state in ("inactive", "failed", "deactivating"):
        assert healthy(state, [7]) == (False, 1), f"unit state {state!r}"
    # An UNREADABLE state is not evidence of death (P6 reads it the same way):
    # keep polling, so a server still booting behind a lost bus is waited for...
    assert healthy("", [7, 22, 0]) == (True, 3), "unreadable state must keep polling"
    # ...and the elapsed bound still ends it when it never comes back.
    ok, n = healthy("", [7], wait=2)
    assert not ok and 2 <= n <= 4, n
    # 7 refused, 22 an HTTP error, 28 timed out — none reads as up.
    for rc in (7, 22, 28):
        assert healthy("inactive", [rc])[0] is False, rc
    # A direct start (no unit): waited for while its process lives...
    sleeper = subprocess.Popen(["sleep", "60"])
    try:
        assert healthy("inactive", [7, 7, 0], direct_pid=str(sleeper.pid)) == (True, 3)
    finally:
        sleeper.kill()
        sleeper.wait()
    # ...and not at all once it is gone, whether the unit reads stopped or unreadable.
    assert healthy("inactive", [7], direct_pid=str(sleeper.pid)) == (False, 1)
    assert healthy("", [7], direct_pid=str(sleeper.pid)) == (False, 1)
    # A transfer that hangs counts against the bound: each attempt takes 3s here,
    # so a 3s bound allows ONE attempt (a sleep-only counter would allow four).
    slow = shim_dir / "slow-curl"
    slow.write_text(f'#!/bin/sh\necho x >> "{calls}"\nsleep 3\nexit 28\n')
    slow.chmod(0o755)
    (shim_dir / "systemctl").write_text("#!/bin/sh\nprintf activating\nexit 0\n")
    calls.write_text("")
    r = subprocess.run(
        [
            "bash",
            "-c",
            "_SERVER_HEALTH_WAIT_SECS=3\n_SERVER_HEALTH_POLL_SECS=1\n"
            + f'curl() {{ "{slow}"; }}\n'
            + block
            + "\n_server_health_ok",
        ],
        capture_output=True,
        text=True,
        env=_clean_env(PATH=f"{shim_dir}:{os.environ['PATH']}"),
    )
    assert r.returncode != 0
    assert len(calls.read_text().splitlines()) <= 2, calls.read_text()

    home = tmp_path / "home"
    root = tmp_path / "install"
    # The venv is MISSING — the state a failed update can leave behind. The
    # history WRITER cannot run then (it imports genesis) and says so; the
    # READER is stdlib-only, so it must still read the row a previous run left.
    missing_venv = tmp_path / "missing-venv"
    metadata_python = _function(UPDATE.read_text(), "_metadata_python")

    def latest(path: str | None = None) -> str:
        env = _clean_env(HOME=str(home))
        if path is not None:
            env["PATH"] = path
        result = subprocess.run(
            [
                "/bin/bash",
                "-c",
                f'GENESIS_ROOT="{root}"\nVENV_DIR="{missing_venv}"\n'
                + metadata_python
                + "\n"
                + block
                + "\n_latest_update_status",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    assert latest() == "", "no database must read as empty, not fail"
    db = root / "data" / "genesis.db"
    db.parent.mkdir(parents=True)
    sqlite3.connect(db).execute("PRAGMA user_version = 1")
    assert latest() == "", "a database with no history table yet is ABSENT, not unreadable"

    con = sqlite3.connect(db)
    con.execute("CREATE TABLE update_history (status TEXT, started_at TEXT)")
    con.execute("INSERT INTO update_history VALUES ('success', '2026-01-01T00:00:00')")
    con.execute("INSERT INTO update_history VALUES ('rolled_back', '2026-01-02T00:00:00')")
    con.commit()
    con.close()
    assert latest() == "rolled_back", "the NEWEST row, by started_at, must win"

    # started_at is `date -Iseconds`: LOCAL time with its offset. Across a DST
    # fall-back the later run can carry the textually SMALLER value (01:10-05:00
    # is 06:10Z, after 01:30-04:00 = 05:30Z), so the newest row is by INSTANT.
    con = sqlite3.connect(db)
    con.execute("INSERT INTO update_history VALUES ('success', '2026-11-01T01:30:00-04:00')")
    con.execute("INSERT INTO update_history VALUES ('failed', '2026-11-01T01:10:00-05:00')")
    con.commit()
    con.close()
    assert latest() == "failed", "newest by instant, not by text, across a DST fall-back"

    decoy = home / "genesis" / "data" / "genesis.db"
    decoy.parent.mkdir(parents=True)
    con = sqlite3.connect(decoy)
    con.execute("CREATE TABLE update_history (status TEXT, started_at TEXT)")
    con.execute("INSERT INTO update_history VALUES ('success', '2027-01-01T00:00:00')")
    con.commit()
    con.close()
    assert latest() == "failed", "must read $GENESIS_ROOT's database, not ~/genesis's"

    no_python = tmp_path / "no-python-bin"
    no_python.mkdir()
    assert latest(path=str(no_python)) == "unreadable", "no usable interpreter"
    db.write_bytes(b"this is not a sqlite database" * 100)
    assert latest() == "unreadable", "a database that exists but cannot be read"


def test_metadata_python_honors_a_per_writer_version_floor(tmp_path: Path) -> None:
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    python312 = shutil.which("python3.12") or shutil.which("python3")
    assert python312 is not None
    fake311 = shim_dir / "python3"
    fake311.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"(3, 12)"*) exit 1;;\n'
        '  *"(3, 11)"*) exit 0;;\n'
        "esac\n"
        f'exec {python312} "$@"\n'
    )
    fake311.chmod(0o755)
    script = (
        "set -u\n"
        f'VENV_DIR="{tmp_path / "missing-venv"}"\n'
        + _function(UPDATE.read_text(), "_metadata_python")
        + "\n"
        "if _metadata_python 12 >/dev/null; then echo unexpected-312; fi\n"
        "_metadata_python 11\n"
    )
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": str(shim_dir), "HOME": str(tmp_path / "home")},
    )
    assert result.returncode == 0, result.stderr
    assert "unexpected-312" not in result.stdout
    assert result.stdout.strip().splitlines() == [str(fake311)]


def _run_writer(tmp_path: Path, venv_dir: Path) -> tuple[subprocess.CompletedProcess, tuple | None]:
    """Drive the REAL `_record_update_history` against a fixture DB, with the
    real PATH (so a system python3.12, which cannot import genesis, is on offer)."""
    repo = tmp_path / "repo"
    db_path = repo / "data" / "genesis.db"
    _history_db(db_path)
    script = (
        "set -euo pipefail\n"
        f'GENESIS_ROOT="{repo}"\n'
        f'VENV_DIR="{venv_dir}"\n'
        "OLD_TAG=old\nOLD_COMMIT=oldsha\nNEW_TAG=new\nNEW_COMMIT=newsha\n"
        'ROLLBACK_TAG=rollback\nSTARTED_AT=started\nPRE_UPDATE_DEGRADED="backup:process_exit"\n'
        + _function(UPDATE.read_text(), "_record_update_history")
        + '\n_record_update_history "success" "" "container_cc_sync"\n'
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=_clean_env(HOME=str(tmp_path / "home")),
    )
    con = sqlite3.connect(db_path)
    row = con.execute(
        "SELECT status, old_commit, new_commit, degraded_subsystems FROM update_history"
    ).fetchone()
    con.close()
    return result, row


def test_update_history_records_through_the_venv(tmp_path: Path) -> None:
    """Control for the test below: with a venv interpreter present the row lands,
    carrying both the caller's degraded value and PRE_UPDATE_DEGRADED."""
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    # The interpreter running this suite can import genesis and its dependencies.
    # A wrapper, not a symlink: a symlinked python outside its venv finds no
    # pyvenv.cfg next to itself and loses the venv's site-packages.
    (venv / "bin" / "python").write_text(f'#!/bin/sh\nexec {sys.executable} "$@"\n')
    (venv / "bin" / "python").chmod(0o755)
    result, row = _run_writer(tmp_path, venv)
    assert result.returncode == 0, result.stderr
    assert "WARNING" not in result.stderr, result.stderr
    assert row == ("success", "oldsha", "newsha", "container_cc_sync,backup:process_exit")


def test_update_history_without_the_venv_warns_instead_of_skipping_silently(
    tmp_path: Path,
) -> None:
    """The insert imports genesis (and through it aiosqlite), which a system
    interpreter cannot, so without the venv no row can be written. It used to
    return without a word; now it says so."""
    result, row = _run_writer(tmp_path, tmp_path / "missing-venv")
    assert result.returncode == 0, result.stderr
    assert "failed to record update_history entry" in result.stderr
    assert "no venv interpreter" in result.stderr
    assert row is None


# ── the no-change decision, EXECUTED over its whole input matrix ────────────


def _no_delta_block(text: str) -> str:
    start = text.index("    _nd_server_not_restarted=true\n")
    return text[start : text.index("    _restart_tmp_watchgod_if_stale", start)]


def test_no_change_decision_matrix(tmp_path: Path) -> None:
    """Run the SHIPPED no-change block with its probes stubbed, over every
    combination of: genesis-server running at entry, health after the restart,
    something degraded, a leftover failure file, and the latest recorded status.

    Expected, per the narrow fix (#2145):
    - the failure file is removed, with a success row, only when the server was
      running at entry AND answers health now;
    - otherwise a row is written only when something is degraded, and with the
      server not back only over nothing-recorded or a prior success;
    - every row written with the server not back carries the marker, and no
      other row does.
    """
    text = UPDATE.read_text()
    block = _no_delta_block(text)
    helper = _block(text, "success-degraded-subsystems")
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    failure = home / ".genesis" / "last_update_failure.json"
    cases = 0
    for running in (True, False):
        for health in (True, False):
            for degraded in ("", "container_cc_sync"):
                for has_failure in (True, False):
                    for latest in (
                        "",
                        "success",
                        "failed",
                        "rolled_back",
                        "conflicts_pending",
                        "unreadable",
                    ):
                        if has_failure:
                            failure.write_text("{}")
                        elif failure.exists():
                            failure.unlink()
                        script = (
                            "set -Eeuo pipefail\n"
                            + helper
                            + f"_server_health_ok() {{ {'return 0' if health else 'return 1'}; }}\n"
                            + f"_latest_update_status() {{ echo '{latest}'; }}\n"
                            + '_record_update_history() { echo "RECORD[$1|$3]"; }\n'
                            + f"WERE_RUNNING=({'genesis-server' if running else ''})\n"
                            + f'HOST_CC_DEGRADED="{degraded}"\nPRE_UPDATE_DEGRADED=""\n'
                            + block
                        )
                        r = subprocess.run(
                            ["bash", "-c", script],
                            capture_output=True,
                            text=True,
                            env=_clean_env(HOME=str(home)),
                        )
                        assert r.returncode == 0, (
                            running,
                            health,
                            degraded,
                            has_failure,
                            latest,
                            r.stderr,
                        )
                        cases += 1
                        back = running and health
                        records = re.findall(r"RECORD\[success\|([^]]*)\]", r.stdout)
                        if has_failure and back:
                            want_record = True
                        elif degraded:
                            want_record = back or latest in ("", "success")
                        else:
                            want_record = False
                        label = (running, health, degraded, has_failure, latest)
                        assert len(records) == (1 if want_record else 0), (label, r.stdout)
                        if records:
                            assert ("genesis-server-not-restarted" in records[0]) is (not back), (
                                label,
                                records,
                            )
                        assert failure.exists() is (has_failure and not back), (label, r.stdout)
    assert cases == 2 * 2 * 2 * 2 * 6
