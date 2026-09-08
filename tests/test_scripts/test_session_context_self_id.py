"""A dispatched session must be told its own CC session id.

A FOREGROUND session learns it from the per-turn `[Clock: … | Session: xxxxxxxx]`
tag that `genesis_urgent_alerts` emits. That hook returns immediately when
`GENESIS_CC_SESSION=1`, so a dispatched session was never told — and every
provenance field it wrote (`follow_up_create(source_session=…)`,
`session_ledger_add`) was NULL by construction. Not a defect in the writer: an
input it was never given.

Run as a SUBPROCESS rather than by importing the module, because the hook does
its work at import time and reads the id from stdin — the thing under test is
what a real invocation prints, and an in-process import would test a different
program shape.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "genesis_session_context.py"
_SID = "abcd1234-ffff-0000-1111-222233334444"


def _isolated_home(tmp_path: Path) -> Path:
    """A HOME whose only Genesis state is the eject-lever marker.

    `main()` returns immediately when `~/.genesis/cc_context_enabled` is absent
    (`scripts/genesis_session_context.py`, "Eject lever"), so a subprocess that
    inherits a clean CI runner's HOME prints NOTHING and every positive
    assertion below fails for a reason that has nothing to do with the block
    under test — while passing on any developer machine, where the marker is
    always there. The test states its own precondition instead of inheriting
    one (Codex P1, PR #1622).
    """
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True, exist_ok=True)
    (home / ".genesis" / "cc_context_enabled").touch()
    # Guard the guard: if this marker ever stops being the lever, the tests
    # below must fail as "the fixture is wrong", not as "the feature is gone".
    assert (home / ".genesis" / "cc_context_enabled").is_file()
    return home


def _run(tmp_path: Path, session_id: str, *, dispatched: bool) -> str:
    home = _isolated_home(tmp_path)
    env = dict(os.environ)
    env["HOME"] = str(home)
    # HOME alone is NOT isolation, and believing it was is how this test read
    # the live production database. `Path.home()` reads HOME first on POSIX, so
    # the script's own `~/.genesis/…` literals do follow it — but the genesis
    # PACKAGE resolves its database through `genesis.env.genesis_db_path()`,
    # which HOME never touches, so `_load_cognitive_state` opened the real DB
    # (and issued PRAGMA against it) from inside a unit test. Every escape route
    # gets named explicitly:
    env["GENESIS_DB_PATH"] = str(home / "genesis" / "data" / "genesis.db")
    env.pop("GENESIS_REPO_ROOT", None)
    env.pop("GENESIS_HOME", None)
    # …and the foreground branch spawns a DETACHED repo-pulse worker that
    # outlives the subprocess, reaches for `gh`, and writes into a `tmp_path`
    # pytest is about to delete. The nearest sibling that runs this same script
    # as a subprocess (tests/test_scripts/test_surface_open_prs.py) already
    # sets all four of these; this test had adopted only two of them.
    env["GENESIS_REPO_PULSE_DISABLED"] = "1"
    if dispatched:
        env["GENESIS_CC_SESSION"] = "1"
    else:
        env.pop("GENESIS_CC_SESSION", None)
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        input=json.dumps({"session_id": session_id, "hook_event_name": "SessionStart"}),
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    return proc.stdout


def test_a_dispatched_session_is_told_its_own_id(tmp_path):
    out = _run(tmp_path, _SID, dispatched=True)
    assert "## This Session" in out, out[:400]
    assert _SID in out, "the FULL id — a dispatched session has no per-turn tag to derive it from"


def test_it_names_what_the_id_is_for(tmp_path):
    """The id alone is inert. The block has to say which argument it feeds, or
    it is one more line of context nobody acts on — and the failure it fixes is
    silent (a NULL column), so nothing prompts the session to look for it."""
    out = _run(tmp_path, _SID, dispatched=True)
    assert "source_session" in out
    assert "follow_up_create" in out


def test_it_names_the_ledger_argument_correctly(tmp_path):
    """The two tools spell provenance differently — `follow_up_create` takes
    `source_session`, `session_ledger_add` takes `session_id`. Instructing the
    wrong keyword is worse than instructing nothing: the call is rejected before
    the handler runs, so the agreement is never recorded at all (Codex P2).
    """
    out = _run(tmp_path, _SID, dispatched=True)
    block = out.split("## This Session", 1)[1].split("---", 1)[0]
    assert "session_ledger_add(session_id=" in block
    # NOT a control on the old text — the old wording never contained this
    # literal either, so it passed before the fix and proves nothing on its
    # own. It is here to catch the specific REGRESSION of someone "tidying"
    # the two calls back into one shared keyword.
    assert "session_ledger_add(source_session=" not in block
    assert "follow_up_create(source_session=" in block


def test_the_subprocess_does_not_read_the_live_production_database(tmp_path):
    """HERMETICITY, asserted rather than assumed — this test used to open the
    real DB and nothing said so.

    Repointing HOME is not isolation: the script's own `~/.genesis/…` literals
    follow HOME, but the genesis PACKAGE resolves its database through
    `genesis.env.genesis_db_path()`, which HOME never touches. So
    `_load_cognitive_state` read production rows (and issued PRAGMA against the
    live file) from inside a unit test, and the only visible symptom was a
    passing test. Asserting on a marker that ONLY populated production data can
    produce is what turns that back into a failure.
    """
    out = _run(tmp_path, _SID, dispatched=True)
    for marker in ("Active Context", "Strategic Focus", "container_memory_pct"):
        assert marker not in out, f"live production data reached the test: {marker!r}"


def test_an_empty_session_id_emits_no_block(tmp_path):
    """An id the hook was not given must not become an empty backticked field —
    a block claiming to name the session while naming nothing is worse than its
    absence."""
    out = _run(tmp_path, "", dispatched=True)
    assert "## This Session" not in out


def test_a_foreground_session_does_NOT_get_the_block(tmp_path):
    """CONTROL, and it is the one that keeps this narrow: a foreground session
    already gets the id every turn from the urgent-alerts tag. Emitting it here
    too would be duplicate context in the highest-salience slot, and would make
    this test pass for a change that simply prints the block unconditionally.
    """
    out = _run(tmp_path, _SID, dispatched=False)
    assert "## This Session" not in out
