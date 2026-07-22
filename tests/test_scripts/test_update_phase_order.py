"""Phase-order lock for scripts/update.sh (minimal-downtime reorder, R3).

The reorder hoists ``git fetch`` above the service stop (so a slow/hung fetch no
longer extends the downtime window) and moves the two CLAUDE.md regenerations
out of the stop→restart window to after the restart. Both moves are safe ONLY if
a strict phase order holds, so this test locks that order against the REAL
script text (extraction-style, no execution) — it is what keeps the reorder from
silently regressing.

Critical safety invariant asserted here: the pre-stop fetch is guarded
EXPLICITLY and runs BEFORE the ERR trap arms. Routing a pre-stop fetch failure
through the trap would be harmful — ``_do_rollback`` force-stops the server and
restarts only ``WERE_RUNNING`` (empty before the stop), leaving the server down
on a transient network blip. The fetch-failure path must clean up and ``exit 1``
without ``_do_rollback``.

Markers are anchored on actual command lines (not echo strings or comment
prose), so a comment that merely mentions a phase name cannot fool the ordering.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"

# Command-line markers (unique real statements, not comment prose).
FETCH = 'git -C "$GENESIS_ROOT" fetch "$UPDATE_REMOTE" main'
STOP = "--- Stopping services for update ---"
TRAP = "\ntrap _on_err ERR"
MERGE = 'git -C "$GENESIS_ROOT" merge "$UPDATE_REMOTE/main" --no-edit'
RESTART = "--- Restarting services ---"
REFRESH = "--- Refreshing Network Identity in ~/.claude/CLAUDE.md ---"
DONE = '\n_write_state "done"'
ROLLBACK_TAG = 'ROLLBACK_TAG="pre-update-'
# The "active deploy marker": _write_state "fetching" flips
# env.update_in_progress() true (defers the watchdog crash-restart guard).
FETCHING_MARKER = '\n_write_state "fetching"'


@pytest.fixture(scope="module")
def text() -> str:
    return UPDATE_SH.read_text()


def _idx(text: str, marker: str) -> int:
    i = text.find(marker)
    assert i != -1, f"marker not found in update.sh: {marker!r}"
    return i


def test_fetch_appears_exactly_once(text):
    """The old in-window fetch must be gone — no second fetch inside the stop
    window, or downtime is not actually shortened."""
    assert text.count(FETCH) == 1, "expected exactly one git fetch of the remote"
    assert text.count('echo "--- Fetching latest ---"') == 1


def test_fetch_before_stop_before_trap_before_merge(text):
    """fetch (pre-stop, pre-trap) < stop < trap < merge."""
    f, s, t, m = (_idx(text, x) for x in (FETCH, STOP, TRAP, MERGE))
    assert f < s, "fetch must be hoisted ABOVE the service stop"
    assert s < t, "the ERR trap must arm after the stop (unchanged)"
    assert t < m, "the merge must run under the ERR trap"
    assert f < t, (
        "SAFETY: the pre-stop fetch must run BEFORE the ERR trap arms — a fetch "
        "failure must not reach _do_rollback (which would leave the server down)"
    )


def test_rollback_tag_created_before_trap(text):
    assert _idx(text, ROLLBACK_TAG) < _idx(text, TRAP)


def test_fetch_before_active_deploy_marker(text):
    """The fetch must run BEFORE `_write_state "fetching"` flips
    env.update_in_progress() true — otherwise a slow/hung fetch runs with the
    server up but the watchdog's crash-restart guard deferred (Codex P2)."""
    assert _idx(text, FETCH) < _idx(text, FETCHING_MARKER), (
        'pre-stop fetch must precede the _write_state "fetching" deploy marker'
    )
    # ...and the rollback tag must exist before the fetch, so the fetch-failure
    # cleanup (git tag -d) references a real tag under set -u.
    assert _idx(text, ROLLBACK_TAG) < _idx(text, FETCH)


def test_pre_stop_fetch_is_post_merge_gated(text):
    """The hoisted fetch must be inside an ``if [[ "$POST_MERGE" == "false" ]]``
    so the --post-merge CC-conflict re-entry (code already merged) skips it."""
    f = _idx(text, FETCH)
    preamble = text[max(0, f - 300) : f]
    assert '[[ "$POST_MERGE" == "false" ]]' in preamble, "pre-stop fetch must be POST_MERGE-gated"


def test_fetch_failure_cleans_up_and_exits_without_rollback(text):
    """On fetch failure: delete the rollback tag + state file, exit 1, and NEVER
    call _do_rollback (server must stay up)."""
    f = _idx(text, FETCH)
    # The fetch block ends at the first standalone `fi` after the fetch.
    block = text[f : text.index("\nfi\n", f)]
    assert 'tag -d "$ROLLBACK_TAG"' in block, "fetch-failure must delete the rollback tag"
    assert "_clear_deploy_state" in block, (
        "fetch-failure must clear the state (via _clear_deploy_state)"
    )
    assert "exit 1" in block, "fetch-failure must exit 1"
    # Check CODE lines only — a comment may legitimately mention _do_rollback.
    code = "\n".join(ln for ln in block.splitlines() if not ln.lstrip().startswith("#"))
    assert "_do_rollback" not in code, "SAFETY: pre-stop fetch failure must NOT invoke _do_rollback"


def test_claude_md_refresh_after_restart_before_done(text):
    """CLAUDE.md regeneration moved out of the downtime window: after the
    restart, before the 'done' state (so the restarted server's collector is
    still update_in_progress-gated and cannot race the write)."""
    assert text.count(REFRESH) == 1, "network-identity refresh must appear once"
    r, restart, d = _idx(text, REFRESH), _idx(text, RESTART), _idx(text, DONE)
    assert restart < r, "CLAUDE.md refresh must run AFTER the restart"
    assert r < d, "CLAUDE.md refresh must run BEFORE _write_state 'done'"


def test_sync_deploy_targets_still_two_bare_calls(text):
    """Guardrail parity with test_update_settings_local_transition.py: the
    reorder must not change the bare-call count."""
    bare = [ln for ln in text.splitlines() if ln.strip() == "_sync_deploy_targets"]
    assert len(bare) == 2, f"expected 2 bare _sync_deploy_targets calls, got {len(bare)}"


def test_update_sh_syntax_ok():
    """bash -n must pass after the reorder."""
    r = subprocess.run(["bash", "-n", str(UPDATE_SH)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
