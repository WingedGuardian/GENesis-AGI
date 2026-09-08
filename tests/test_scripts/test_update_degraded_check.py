"""The post-update degraded-subsystem check must actually observe subsystems.

Regression cover for a SILENT FAIL-OPEN in the deploy gate: the check parsed a
``subsystems`` key off ``/api/genesis/health``, but that response has no such key
(verified against a running server: 24 top-level keys, ``subsystems`` absent —
the only endpoint using that name returns a LIST). ``DEGRADED`` was therefore
ALWAYS empty and the branch guarded by it had never fired on any install.

Two properties of the manifest vocabulary shape the replacement, and testing a
status in isolation gets both wrong:

1. ``failed:`` is almost unreachable — ``_run_init_step`` records it only when an
   exception ESCAPES, and 30 of 33 modules under ``runtime/init/`` catch their own
   (``perception.py``: ``except Exception: logger.exception(...)``). A subsystem
   that CRASHED lands in the manifest as ``degraded``.
2. ``degraded`` is ambiguous — it is also the normal, PERMANENT state of an absent
   optional dependency, so reporting it outright cries wolf on every deploy.

Hence a DELTA against a pre-restart baseline: ``ok -> degraded`` is a regression
this deploy introduced whichever flavour it is, while an already-degraded
subsystem stays quiet.

Ownership is decided by IDENTITY, not recency. The manifest path is user-global
and the server is not its only writer (bridge, interactive terminal), so the
runtime stamps the writing ``pid`` and this check compares it against the unit's
``MainPID``. "Written recently" cannot establish whose a file is; "written by the
process systemd is running as genesis-server" is a yes/no fact.

Every negative outcome is a ``check:`` sentinel, never an empty string. Empty
means "checked, nothing wrong"; returning that for a check that did not happen is
the exact defect being fixed. The prefix also keeps these tokens distinguishable
from real subsystem names once they reach the update-failure alert, which
interpolates the column verbatim.

These drive the SHIPPED block extracted from update.sh, with ``curl`` and
``systemctl`` shimmed on PATH so the test can never reach the real server.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
UPDATE_SH = REPO / "scripts" / "update.sh"

_HEALTH_JSON = json.dumps({"status": "healthy", "infrastructure": {}, "services": {}})
_PID = "4242"
_OLD_PID = "1111"


@pytest.fixture
def text() -> str:
    return UPDATE_SH.read_text()


def _extract_degraded_block(text: str) -> str:
    """The shipped post-health degraded-subsystem check.

    A text-position dependency on a region under active change; it fails loudly
    via these asserts rather than silently extracting the wrong span.
    """
    start = text.find('if [ "$HEALTH_OK" = "true" ]; then')
    end = text.find("# Verify services are active")
    assert start != -1, "start marker moved — update the extractor"
    assert end != -1 and end > start, "end marker moved — update the extractor"
    return text[start:end].replace('"$VENV_DIR/bin/python"', "python3")


def _doc(manifest: dict, pid: str = _PID) -> dict:
    return {
        "bootstrapped": True,
        "manifest": manifest,
        "persisted_at": "2026-01-01T00:00:00+00:00",
        "pid": int(pid),
    }


def _run(
    tmp_path: Path,
    text: str,
    *,
    after: dict | None,
    before: dict | None = None,
    main_pid: str = _PID,
    pid_before: str | None = None,
    no_python: bool = False,
    label: str = "case",
) -> tuple[str, str, int]:
    """Drive the shipped block. Returns (DEGRADED value, combined output, rc).

    ``after``/``before`` are whole manifest DOCUMENTS (or None to omit).
    ``pid_before`` defaults to _OLD_PID when a baseline is given; pass "0"
    explicitly to reproduce a pid read from a STOPPED unit.
    """
    home = tmp_path / f"home_{label}"
    (home / ".genesis").mkdir(parents=True)
    if after is not None:
        (home / ".genesis" / "bootstrap_manifest.json").write_text(json.dumps(after))

    bindir = tmp_path / f"bin_{label}"
    bindir.mkdir()
    (bindir / "curl").write_text(f"#!/bin/bash\ncat <<'EOF'\n{_HEALTH_JSON}\nEOF\n")
    (bindir / "curl").chmod(0o755)
    # systemctl shim: only `show ... -p MainPID --value` is consulted here.
    (bindir / "systemctl").write_text(f"#!/bin/bash\necho '{main_pid}'\n")
    (bindir / "systemctl").chmod(0o755)
    if no_python:
        # Shadow python3 so the interpreter leg fails (127), exercising the bash
        # fallback that no other test in this file reaches.
        (bindir / "python3").write_text("#!/bin/bash\nexit 127\n")
        (bindir / "python3").chmod(0o755)

    before_json = json.dumps(before) if before is not None else ""
    pid_b = pid_before if pid_before is not None else (_OLD_PID if before is not None else "")
    harness = f"""#!/bin/bash
set -Eeuo pipefail
HEALTH_OK=true
MANIFEST_BEFORE={json.dumps(before_json)}
SERVER_PID_BEFORE="{pid_b}"
_do_rollback() {{ echo "ROLLBACK-CALLED: $*"; exit 9; }}
{_extract_degraded_block(text)}
echo "DEGRADED=[${{DEGRADED:-}}]"
"""
    script = tmp_path / f"h_{label}.sh"
    script.write_text(harness)
    out = subprocess.run(
        ["bash", str(script)],
        env={**os.environ, "HOME": str(home), "PATH": f"{bindir}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    value = ""
    for line in out.stdout.splitlines():
        if line.startswith("DEGRADED=["):
            value = line[len("DEGRADED=[") : -1]
    return value, out.stdout + out.stderr, out.returncode


# ── the regression this exists to catch ────────────────────────────────────


def test_ok_to_degraded_regression_is_detected(tmp_path: Path, text: str) -> None:
    """THE REPRO — the case both reviewers surfaced.

    perception (and 29 other init modules) swallow their own exceptions, so a
    CRASH is recorded as "degraded", not "failed:". Testing the status alone
    misses it; the delta against the pre-restart baseline catches it.
    """
    value, out, rc = _run(
        tmp_path,
        text,
        before=_doc({"db": "ok", "perception": "ok"}, _OLD_PID),
        after=_doc({"db": "ok", "perception": "degraded"}),
        label="regress",
    )
    assert rc == 0, out
    assert value == "perception", f"ok->degraded regression not surfaced; got {value!r}"


def test_persistently_degraded_stays_quiet(tmp_path: Path, text: str) -> None:
    """An absent optional dependency (no Ollama, no optional key) is degraded
    before AND after. Reporting it would cry wolf on every deploy of exactly the
    installs least able to act on it."""
    value, out, rc = _run(
        tmp_path,
        text,
        before=_doc({"db": "ok", "voice": "degraded"}, _OLD_PID),
        after=_doc({"db": "ok", "voice": "degraded"}),
        label="persist",
    )
    assert rc == 0, out
    assert value == "", f"pre-existing degradation must stay quiet, got {value!r}"


def test_hard_failure_reported_regardless_of_baseline(tmp_path: Path, text: str) -> None:
    value, out, rc = _run(
        tmp_path,
        text,
        before=_doc({"db": "ok", "router": "failed: boom"}, _OLD_PID),
        after=_doc({"db": "ok", "router": "failed: boom"}),
        label="hard",
    )
    assert rc == 0, out
    assert value == "router", f"a hard failure must report every time; got {value!r}"


def test_all_ok_reports_nothing(tmp_path: Path, text: str) -> None:
    value, out, rc = _run(
        tmp_path,
        text,
        before=_doc({"db": "ok", "router": "ok"}, _OLD_PID),
        after=_doc({"db": "ok", "router": "ok"}),
        label="allok",
    )
    assert rc == 0, out
    assert value == "", f"clean deploy must report nothing, got {value!r}"


def test_improvement_is_not_reported(tmp_path: Path, text: str) -> None:
    """degraded -> ok is a fix, not a regression."""
    value, out, rc = _run(
        tmp_path,
        text,
        before=_doc({"db": "ok", "voice": "degraded"}, _OLD_PID),
        after=_doc({"db": "ok", "voice": "ok"}),
        label="improve",
    )
    assert rc == 0, out
    assert value == "", f"an improvement must not be reported, got {value!r}"


# ── ownership: identity, not recency ───────────────────────────────────────


def test_manifest_written_by_another_process_is_rejected(tmp_path: Path, text: str) -> None:
    """The bridge and the interactive terminal write this same user-global file.
    A manifest whose pid is not the serving unit's MainPID says nothing about
    this deploy — and must not read as a clean bill of health."""
    value, out, rc = _run(
        tmp_path,
        text,
        before=_doc({"db": "ok", "perception": "ok"}, _OLD_PID),
        after=_doc({"db": "ok", "perception": "ok"}, "9999"),  # someone else's
        label="notours",
    )
    assert rc == 0, out
    assert value == "check:manifest-not-this-server", f"got {value!r}"


def test_no_server_pid_is_not_reported_clean(tmp_path: Path, text: str) -> None:
    value, out, rc = _run(
        tmp_path,
        text,
        before=_doc({"db": "ok"}, _OLD_PID),
        after=_doc({"db": "ok"}),
        main_pid="0",  # systemd reports 0 for an inactive unit
        label="nopid",
    )
    assert rc == 0, out
    assert value == "check:no-server-pid", f"got {value!r}"


def test_missing_manifest_names_its_cause(tmp_path: Path, text: str) -> None:
    value, out, rc = _run(tmp_path, text, before=None, after=None, label="missing")
    assert rc == 0, out
    assert value.startswith("check:manifest-unreadable("), f"got {value!r}"
    assert "FileNotFoundError" in value, f"cause must be named for diagnosis; got {value!r}"


def test_absent_baseline_is_declared_not_hidden(tmp_path: Path, text: str) -> None:
    """First deploy on an install: the check still runs but can only see hard
    failures. It says so rather than emitting a confident-looking empty result."""
    value, out, rc = _run(
        tmp_path,
        text,
        before=None,
        after=_doc({"db": "ok", "voice": "degraded"}),
        label="nobase",
    )
    assert rc == 0, out
    assert "check:no-baseline" in value, f"reduced capability must be declared; got {value!r}"
    assert "voice" not in value, "without a baseline a degraded subsystem is not a regression"


def test_baseline_from_another_process_is_not_trusted(tmp_path: Path, text: str) -> None:
    """A baseline is only a baseline if it belonged to the server it precedes."""
    stale = {"bootstrapped": True, "manifest": {"db": "ok"}, "pid": 777}  # not SERVER_PID_BEFORE
    value, out, rc = _run(
        tmp_path,
        text,
        before=stale,
        after=_doc({"db": "ok"}),
        label="badbase",
    )
    assert rc == 0, out
    assert "check:no-baseline" in value, f"got {value!r}"


def test_stopped_unit_baseline_pid_is_rejected(tmp_path: Path, text: str) -> None:
    """THE CONTROL FOR THE CAPTURE-POINT BUG. systemd reports MainPID "0" for a
    stopped unit, and "0" is a TRUTHY string — so a guard of the form
    `if raw and pid_before:` accepts it and then fails the comparison silently,
    emitting check:no-baseline, which is indistinguishable from a legitimate first
    deploy. An earlier revision captured the baseline pid AFTER the stop and was
    therefore a permanent no-op with a fully green suite."""
    value, out, rc = _run(
        tmp_path,
        text,
        before=_doc({"db": "ok", "perception": "ok"}, _OLD_PID),
        after=_doc({"db": "ok", "perception": "degraded"}),
        pid_before="0",
        label="stoppedpid",
    )
    assert rc == 0, out
    assert "check:no-baseline" in value, f"a stopped-unit pid must not be trusted; got {value!r}"


def test_new_subsystem_arriving_broken_is_detected(tmp_path: Path, text: str) -> None:
    """A deploy that ADDS an init step whose module swallows its own exception
    records it as `degraded` with no baseline entry. Gating the regression arm on
    `name in before` would make the likeliest real regression invisible."""
    value, out, rc = _run(
        tmp_path,
        text,
        before=_doc({"db": "ok"}, _OLD_PID),
        after=_doc({"db": "ok", "newthing": "degraded"}),
        label="newbroken",
    )
    assert rc == 0, out
    assert value == "newthing", f"a subsystem arriving broken must be named; got {value!r}"


def test_new_subsystem_arriving_healthy_is_quiet(tmp_path: Path, text: str) -> None:
    value, out, rc = _run(
        tmp_path,
        text,
        before=_doc({"db": "ok"}, _OLD_PID),
        after=_doc({"db": "ok", "newthing": "ok"}),
        label="newok",
    )
    assert rc == 0, out
    assert value == "", f"a healthy new subsystem is not a regression; got {value!r}"


def test_disappeared_subsystem_is_detected(tmp_path: Path, text: str) -> None:
    """A manifest key is written on BOTH branches of _run_init_step, so an absent
    key means the step never ran — a deleted or newly-skipped subsystem, which is
    exactly a 'this deploy broke something' event."""
    value, out, rc = _run(
        tmp_path,
        text,
        before=_doc({"db": "ok", "reflex": "ok"}, _OLD_PID),
        after=_doc({"db": "ok"}),
        label="gone",
    )
    assert rc == 0, out
    assert value == "reflex:gone", f"a vanished subsystem must be named; got {value!r}"


def test_empty_manifest_gets_its_own_sentinel(tmp_path: Path, text: str) -> None:
    """ "Ours but says nothing" and "someone else wrote it" send a reader to
    completely different places, so they must not share a sentinel."""
    value, out, rc = _run(
        tmp_path,
        text,
        before=_doc({"db": "ok"}, _OLD_PID),
        after={"bootstrapped": True, "pid": int(_PID), "manifest": {}},
        label="emptyman",
    )
    assert rc == 0, out
    assert value == "check:manifest-empty", f"got {value!r}"


def test_interpreter_failure_is_not_reported_clean(tmp_path: Path, text: str) -> None:
    """The bash fallback leg. Nothing else in this file reaches it."""
    value, out, rc = _run(
        tmp_path,
        text,
        before=_doc({"db": "ok"}, _OLD_PID),
        after=_doc({"db": "ok"}),
        no_python=True,
        label="nopy",
    )
    assert rc == 0, out
    assert value == "check:manifest-interpreter-failed", f"got {value!r}"


# ── contract ───────────────────────────────────────────────────────────────


def test_failure_is_advisory_never_a_rollback(tmp_path: Path, text: str) -> None:
    _, out, rc = _run(
        tmp_path,
        text,
        before=_doc({"db": "ok", "inbox": "ok", "outreach": "ok"}, _OLD_PID),
        after=_doc({"db": "ok", "inbox": "failed: x", "outreach": "degraded"}),
        label="advisory",
    )
    assert "ROLLBACK-CALLED" not in out, f"degraded check must not roll back:\n{out}"
    assert rc == 0, out


def test_finding_is_folded_into_the_recorded_degraded_column(text: str) -> None:
    """SOURCE PIN, mirroring test_cc_updater_suppression's fold test: the value
    must reach update_history, or "surfaced, not silent" is true only of one
    run's console output. The fold sits outside the extractor's span."""
    fold = re.search(
        r'if \[ -n "\$\{DEGRADED:-\}" \]; then\s*\n\s*'
        r'_p6_degraded="\$\{_p6_degraded:\+\$_p6_degraded,\}\$DEGRADED"',
        text,
    )
    assert fold is not None, "DEGRADED must be folded into _p6_degraded before it is recorded"
    record = '_record_update_history "success" "" "$_p6_degraded"'
    assert text.count(record) == 1, "success-with-degraded call is no longer unique — re-anchor"
    assert fold.start() < text.index(record), "fold must precede the success row"


def test_baseline_is_captured_before_the_restart(text: str) -> None:
    """The delta needs a PRE-restart snapshot; capturing it after would compare
    the new manifest against itself and report nothing, ever."""
    assert 'MANIFEST_BEFORE="$(cat "$HOME/.genesis/bootstrap_manifest.json"' in text
    # THE invariant that actually broke: the pid must be read while the old server
    # is still ALIVE. `systemctl show -p MainPID` returns "0" for a stopped unit, so
    # capturing anywhere after the stop can never match the manifest's real pid —
    # the baseline is silently rejected on every deploy and the delta becomes a
    # no-op that still emits a healthy-looking sentinel. An earlier revision of this
    # change did exactly that and the whole suite stayed green.
    capture = text.index('MANIFEST_BEFORE="$(cat')
    first_stop = text.index("    _stop_genesis_server\n")
    assert capture < first_stop, (
        "baseline must be captured BEFORE the server is stopped — a stopped unit "
        "reports MainPID 0 and the baseline can never be matched"
    )
    assert text.index("SERVER_PID_BEFORE=") < first_stop, (
        "the baseline pid must be read while the old server is still running"
    )


def test_runtime_stamps_the_writing_pid(text: str) -> None:
    """The identity bind is only possible because the runtime records who wrote
    the manifest. Pin it here: this check is the consumer, and a silent removal
    upstream would turn the bind into a permanent check:manifest-not-this-server."""
    cap = (REPO / "src" / "genesis" / "runtime" / "_capabilities.py").read_text()
    assert '"pid": os.getpid(),' in cap, "bootstrap manifest must record its writer's pid"
