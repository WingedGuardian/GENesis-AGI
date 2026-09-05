"""The post-update degraded-subsystem check must actually observe subsystems.

Regression cover for a SILENT FAIL-OPEN in the deploy gate: the check parsed a
``subsystems`` key off ``/api/genesis/health``, but that response has no such
key (verified against a running server: 24 top-level keys, ``subsystems``
absent — the only endpoint using that name returns a LIST, not a mapping). So
the computed ``DEGRADED`` was ALWAYS empty and the branch guarded by it had
never fired on any install.

Scope of what this gate can and cannot catch, which is why it is ADVISORY:
``GenesisRuntime._CRITICAL_SUBSYSTEMS`` is ``{db, observability, router}`` and
``_bootstrapped`` is false unless all three are ok. A critical failure does not
merely 503 — ``hosting/standalone.py`` returns early leaving ``_app`` None and
``serve()`` then exits, so the process does not come up at all, the health wait
exhausts, and the deploy rolls back on its own. What reaches this check is the
OTHER ~31 subsystems, whose failure leaves the server bootstrapped, answering
200, and shipping silently. Reverting a whole deploy over one non-critical
subsystem is too blunt for that, so the finding is recorded, never rolled back.

EVERY negative outcome is a ``check:`` sentinel rather than an empty string.
Empty means "checked, nothing wrong"; returning that for a check that did not
happen is the exact defect being fixed, so the replacement source gets no
benefit of the doubt either. The ``check:`` prefix also keeps these tokens
distinguishable from real subsystem names once they reach the update-failure
alert, which interpolates the column verbatim.

These drive the SHIPPED block extracted from update.sh, with ``curl`` shimmed on
PATH so the test can never reach a real server on :5000.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

UPDATE_SH = Path(__file__).resolve().parents[2] / "scripts" / "update.sh"

# A realistic health payload: the shape the endpoint ACTUALLY returns. The
# absence of a "subsystems" key is the whole point — a fixture that invented one
# would test a server that does not exist.
_HEALTH_JSON = json.dumps(
    {
        "status": "healthy",
        "timestamp": "2026-01-01T00:00:00Z",
        "infrastructure": {"genesis.db": {"status": "healthy"}},
        "services": {},
        "queues": {},
    }
)

_ANCHOR = "2026-01-01T00:00:00+00:00"
_AFTER_ANCHOR = "2026-01-01T00:00:10+00:00"
_BEFORE_ANCHOR = "2025-01-01T00:00:00+00:00"


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


def _run_degraded(
    tmp_path: Path,
    text: str,
    *,
    doc: dict | None,
    restart_at: str = _ANCHOR,
    label: str = "case",
) -> tuple[str, str, int]:
    """Drive the shipped block. Returns (DEGRADED value, combined output, rc)."""
    home = tmp_path / f"home_{label}"
    (home / ".genesis").mkdir(parents=True)
    if doc is not None:
        (home / ".genesis" / "bootstrap_manifest.json").write_text(json.dumps(doc))

    # Shim curl so the block cannot reach the real server on this box.
    bindir = tmp_path / f"bin_{label}"
    bindir.mkdir()
    curl = bindir / "curl"
    curl.write_text(f"#!/bin/bash\ncat <<'EOF'\n{_HEALTH_JSON}\nEOF\n")
    curl.chmod(0o755)

    harness = f"""#!/bin/bash
set -Eeuo pipefail
HEALTH_OK=true
RESTART_AT="{restart_at}"
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
    combined = out.stdout + out.stderr
    value = ""
    for line in out.stdout.splitlines():
        if line.startswith("DEGRADED=["):
            value = line[len("DEGRADED=[") : -1]
    return value, combined, out.returncode


def _fresh(manifest: dict) -> dict:
    return {"bootstrapped": True, "manifest": manifest, "persisted_at": _AFTER_ANCHOR}


# ── detection ──────────────────────────────────────────────────────────────


def test_failed_subsystem_is_detected(tmp_path: Path, text: str) -> None:
    """THE REPRO. A non-critical subsystem that failed to init must be named.

    Fails on the pre-fix code: it parsed a `subsystems` key the health response
    does not have, so DEGRADED came back empty and the deploy shipped silently.
    """
    value, combined, rc = _run_degraded(
        tmp_path,
        text,
        doc=_fresh({"db": "ok", "router": "ok", "outreach": "failed: boom"}),
        label="failed",
    )
    assert rc == 0, combined
    assert value == "outreach", f"failed subsystem not surfaced; DEGRADED={value!r}"


def test_unknown_status_counts_as_failed(tmp_path: Path, text: str) -> None:
    """The gate must be the STRICTER of the two readers of this manifest.
    runtime/_capabilities.py treats anything not ok/degraded as failed; matching
    that means a status value added later cannot quietly pass the gate."""
    value, combined, rc = _run_degraded(
        tmp_path, text, doc=_fresh({"db": "ok", "inbox": "wedged"}), label="unknown"
    )
    assert rc == 0, combined
    assert value == "inbox", f"unrecognised status must count as failed; got {value!r}"


def test_all_ok_reports_nothing(tmp_path: Path, text: str) -> None:
    value, combined, rc = _run_degraded(
        tmp_path, text, doc=_fresh({"db": "ok", "router": "ok"}), label="allok"
    )
    assert rc == 0, combined
    assert value == "", f"clean manifest must report nothing, got {value!r}"


def test_degraded_status_is_not_a_failure(tmp_path: Path, text: str) -> None:
    """`degraded` means an optional dependency is absent — normal on many
    installs (no Ollama, no optional API key). Counting it would make the gate
    cry wolf on exactly the installs least able to act on it."""
    value, combined, rc = _run_degraded(
        tmp_path, text, doc=_fresh({"db": "ok", "voice": "degraded"}), label="degr"
    )
    assert rc == 0, combined
    assert value == "", f"'degraded' must not count as failed, got {value!r}"


# ── fail-closed paths: each reports a sentinel, never an empty "all clear" ──


def test_stale_manifest_is_not_reported_clean(tmp_path: Path, text: str) -> None:
    """A manifest predating this RESTART describes an earlier boot — the bridge
    and the interactive terminal write this same file."""
    doc = {"bootstrapped": True, "manifest": {"db": "ok"}, "persisted_at": _BEFORE_ANCHOR}
    value, combined, rc = _run_degraded(tmp_path, text, doc=doc, label="stale")
    assert rc == 0, combined
    assert value == "check:manifest-stale", f"got {value!r}"


def test_undated_manifest_is_not_reported_clean(tmp_path: Path, text: str) -> None:
    value, combined, rc = _run_degraded(
        tmp_path, text, doc={"bootstrapped": True, "manifest": {"db": "ok"}}, label="undated"
    )
    assert rc == 0, combined
    assert value == "check:manifest-undated", f"got {value!r}"


def test_missing_manifest_key_is_not_reported_clean(tmp_path: Path, text: str) -> None:
    """The bug being fixed was trusting a key that was not there. The
    REPLACEMENT source must not repeat it: a fresh document with no usable
    payload is unknown, not a clean bill of health."""
    doc = {"bootstrapped": True, "persisted_at": _AFTER_ANCHOR}
    value, combined, rc = _run_degraded(tmp_path, text, doc=doc, label="nokey")
    assert rc == 0, combined
    assert value == "check:manifest-empty", f"got {value!r}"


def test_empty_manifest_is_not_reported_clean(tmp_path: Path, text: str) -> None:
    value, combined, rc = _run_degraded(tmp_path, text, doc=_fresh({}), label="emptyman")
    assert rc == 0, combined
    assert value == "check:manifest-empty", f"got {value!r}"


def test_empty_anchor_does_not_disable_the_freshness_gate(tmp_path: Path, text: str) -> None:
    """An empty anchor must FAIL the gate, not delete it. Folded into the
    comparison as `anchor and ...`, a falsy anchor silently skips the staleness
    test and an ancient manifest reads as this deploy's."""
    doc = {"bootstrapped": True, "manifest": {"db": "ok"}, "persisted_at": _BEFORE_ANCHOR}
    value, combined, rc = _run_degraded(tmp_path, text, doc=doc, restart_at="", label="noanchor")
    assert rc == 0, combined
    assert value == "check:no-restart-anchor", f"got {value!r}"


def test_missing_manifest_file_is_not_reported_clean(tmp_path: Path, text: str) -> None:
    value, combined, rc = _run_degraded(tmp_path, text, doc=None, label="missing")
    assert rc == 0, combined
    assert value.startswith("check:manifest-unreadable("), f"got {value!r}"
    assert "FileNotFoundError" in value, f"cause must be named for diagnosis; got {value!r}"


# ── contract ───────────────────────────────────────────────────────────────


def test_failure_is_advisory_never_a_rollback(tmp_path: Path, text: str) -> None:
    """The check surfaces; it does not revert. A critical failure already
    prevents the server coming up at all, and one non-critical subsystem must
    not destroy an otherwise-good deploy."""
    _, combined, rc = _run_degraded(
        tmp_path,
        text,
        doc=_fresh({"db": "ok", "outreach": "failed: boom", "inbox": "failed: boom"}),
        label="advisory",
    )
    assert "ROLLBACK-CALLED" not in combined, f"degraded check must not roll back:\n{combined}"
    assert rc == 0, combined


def test_finding_is_folded_into_the_recorded_degraded_column(text: str) -> None:
    """SOURCE PIN, mirroring test_cc_updater_suppression's fold test: the value
    must reach update_history, or "surfaced, not silent" is true only of one
    run's console output. The fold sits outside the extractor's span, so no
    behavioural test above covers it.
    """
    fold = re.search(
        r'if \[ -n "\$\{DEGRADED:-\}" \]; then\s*\n\s*_p6_degraded="\$\{_p6_degraded:\+\$_p6_degraded,\}\$DEGRADED"',
        text,
    )
    assert fold is not None, "DEGRADED must be folded into _p6_degraded before it is recorded"
    # Anchor on the UNIQUE success call that carries the column. Plain
    # `_record_update_history "success"` also matches the two no-op-path calls
    # earlier in the file, which is a different code path and orders the wrong way.
    record = '_record_update_history "success" "" "$_p6_degraded"'
    assert text.count(record) == 1, "success-with-degraded call is no longer unique — re-anchor"
    assert fold.start() < text.index(record), (
        "the fold must happen before the success row is written"
    )


def test_restart_anchor_is_stamped_at_the_restart_not_script_start(text: str) -> None:
    """The freshness anchor must be RESTART_AT, stamped next to the restart. Using
    $STARTED_AT (top of script) would accept any manifest written during the whole
    multi-minute deploy, including one from a concurrently-booting runtime."""
    assert 'RESTART_AT="$(date -Iseconds)"' in text
    assert text.index('RESTART_AT="$(date -Iseconds)"') < text.index(
        'if [ "$HEALTH_OK" = "true" ]; then'
    ), "anchor must be stamped before the health/degraded check reads it"
    assert 'RESTART_AT="$RESTART_AT" python3' in text, "the check must consume the restart anchor"
