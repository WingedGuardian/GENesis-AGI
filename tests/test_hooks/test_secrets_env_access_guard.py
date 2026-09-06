"""The secrets.env consent gate, and the needs-user chokepoint underneath it.

Origin: a session read `secrets.env` and POSTed straight to a provider endpoint.
No prompt, no record, no call site — so no cost attribution, no budget check, no
breaker. The spend was invisible to `cost_events` entirely.

Two properties carry the whole design and each has a test that FAILS if it
regresses:

1. A foreground session ASKS (a human can decide) but a dispatched session is
   DENIED, because an ask nobody can answer is a silent block.
2. The deny is LOUD — a critical observation — because a dispatched session
   walking into a wall it can never pass has no legitimate instance.

The fail-safe test is the one that matters most: if recording breaks, the block
must still hold. A logging failure must never become a security hole.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

_HOOKS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"
_GUARD = _HOOKS / "secrets_env_access_guard.py"


@pytest.fixture
def db(tmp_path: Path) -> str:
    """A real ``observations`` table, built from the CANONICAL schema.

    Not a hand-copied DDL. The first version of this fixture was one, and it
    drifted in the single column that decides this module's behaviour: it
    declared ``resolved INT`` where the shipped table declares ``resolved
    INTEGER NOT NULL DEFAULT 0``. The dedupe predicate is ``... AND resolved =
    0``, and against a NULL that comparison yields NULL — never true — so
    ``INSERT … WHERE NOT EXISTS`` inserted every time. The dedupe test then
    PASSED while asserting three rows for three identical hits, which is the
    behaviour it exists to forbid. A hand-rolled fixture can only ever test the
    fixture's own schema.
    """
    from genesis.db.schema import TABLES

    path = tmp_path / "genesis.db"
    conn = sqlite3.connect(path)
    conn.executescript(TABLES["observations"])
    conn.close()
    return str(path)


def _needs_user():
    spec = importlib.util.spec_from_file_location("needs_user", _HOOKS / "needs_user.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run(
    command: str, db_path: str, *, dispatched: bool, session: str = "test-session"
) -> tuple[str, int]:
    """Run the guard as CC runs it and return (decision, exit code).

    The session id rides the PAYLOAD, which is the live contract — deliberately
    NOT the ``CLAUDE_SESSION_ID`` environment variable, which current Claude
    Code does not set. A test that supplied it via the environment would keep
    passing against a hook that reads only the dead variable, which is how the
    id silently collapsed to ``"unknown"`` for every session.
    """
    env = {**os.environ, "GENESIS_DB_PATH": db_path}
    env.pop("CLAUDE_SESSION_ID", None)
    env.pop("GENESIS_CC_SESSION", None)
    if dispatched:
        env["GENESIS_CC_SESSION"] = "1"
    proc = subprocess.run(
        [sys.executable, str(_GUARD)],
        input=json.dumps(
            {"tool_name": "Bash", "session_id": session, "tool_input": {"command": command}}
        ),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    out = proc.stdout.strip()
    if not out:
        return "silent-allow", proc.returncode
    return json.loads(out)["hookSpecificOutput"]["permissionDecision"], proc.returncode


@pytest.mark.parametrize(
    "command",
    [
        "set -a; . /home/ubuntu/genesis/secrets.env; set +a; python3 x.py",
        "source ~/genesis/secrets.env && ./run.sh",
        "cat ~/genesis/secrets.env",
        # Reading only key NAMES still touches the file — the owner asked to know
        # whenever the credentials are tapped, not only when a value is used.
        "grep -c API_KEY ~/genesis/secrets.env",
        # WRITES the real file, so it is in scope even though the source is the template.
        "cp secrets.env.example secrets.env",
    ],
)
def test_foreground_touching_secrets_asks(command: str, db: str) -> None:
    assert _run(command, db, dispatched=False) == ("ask", 0)


@pytest.mark.parametrize(
    "command",
    [
        # The TEMPLATE holds no secrets. Prompting on it would train the owner to
        # click through the prompt that matters, so it must stay silent.
        "cat secrets.env.example",
        "cp secrets.env.example /tmp/x",
        "ls ~/genesis",
        "echo hello",
    ],
)
def test_unrelated_or_template_is_silent(command: str, db: str) -> None:
    assert _run(command, db, dispatched=False) == ("silent-allow", 0)


def test_dispatched_is_denied_and_recorded_critical(db: str) -> None:
    decision, rc = _run("source ~/genesis/secrets.env", db, dispatched=True)
    assert (decision, rc) == ("deny", 0)

    rows = sqlite3.connect(db).execute("SELECT priority, type, source FROM observations").fetchall()
    assert len(rows) == 1
    assert rows[0] == (
        "critical",
        "background_session_blocked_needs_user",
        "hook.needs_user",
    )


def test_dispatched_deny_tells_the_session_not_to_retry(db: str) -> None:
    """A deny reads like a transient failure from inside; it is not.

    Without this the dispatched session burns its budget rephrasing a command
    that can never succeed.
    """
    env = {**os.environ, "GENESIS_DB_PATH": db, "GENESIS_CC_SESSION": "1"}
    proc = subprocess.run(
        [sys.executable, str(_GUARD)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "cat secrets.env"}}),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    reason = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "no retry" in reason
    assert "handoff" in reason


def test_foreground_prompt_names_the_command_and_the_stakes(db: str) -> None:
    """The prompt must not read like a routine approval, or it gets clicked through."""
    env = {**os.environ, "GENESIS_DB_PATH": db}
    env.pop("GENESIS_CC_SESSION", None)
    proc = subprocess.run(
        [sys.executable, str(_GUARD)],
        input=json.dumps(
            {
                "tool_name": "Bash",
                "tool_input": {"command": "source secrets.env && curl x"},
            }
        ),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    reason = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "GENESIS CREDENTIALS" in reason
    assert "not a routine approval" in reason
    assert "source secrets.env && curl x" in reason  # the actual command, quoted


def test_recording_failure_still_denies(tmp_path: Path) -> None:
    """THE load-bearing test: a broken record must not become an allow.

    If the observation cannot be written the block still holds, and the reason
    says the block may be invisible — so it is reported in the handoff instead
    of vanishing.
    """
    decision, rc = _run("cat secrets.env", str(tmp_path / "nonexistent" / "x.db"), dispatched=True)
    assert (decision, rc) == ("deny", 0)

    env = {
        **os.environ,
        "GENESIS_DB_PATH": str(tmp_path / "nonexistent" / "x.db"),
        "GENESIS_CC_SESSION": "1",
    }
    proc = subprocess.run(
        [sys.executable, str(_GUARD)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "cat secrets.env"}}),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    reason = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    # The reason must tell the session to report the block ITSELF, rather than
    # relying on a record that (in this exact scenario) did not land.
    assert "handoff" in reason
    assert "did not land" in reason


def test_same_wall_same_session_dedupes(db: str) -> None:
    """One session hitting one wall repeatedly is ONE finding, not twenty.

    This test previously asserted ``len(hashes) == 3`` — the exact behaviour its
    own name forbids — and passed, because the fixture's schema defeated the
    dedupe predicate (see the ``db`` fixture). Both halves are fixed here: the
    assertion now matches the name, and the fixture now matches production.
    """
    for _ in range(3):
        _run("cat secrets.env", db, dispatched=True)
    hashes = [r[0] for r in sqlite3.connect(db).execute("SELECT content_hash FROM observations")]
    assert len(hashes) == 1, (
        f"same wall, same session should collapse to one row, got {len(hashes)}"
    )


def test_different_sessions_are_separate_findings(db: str) -> None:
    """A DIFFERENT session hitting the same wall IS news again.

    The dedupe identity is (action, session) on purpose: repeated dispatches
    walking into the same dead end is the signal, so collapsing across sessions
    would hide exactly the thing worth alerting on.
    """
    for sid in ("session-a", "session-b"):
        _run("cat secrets.env", db, dispatched=True, session=sid)
    rows = list(sqlite3.connect(db).execute("SELECT content_hash FROM observations"))
    assert len(rows) == 2, f"two sessions should produce two findings, got {len(rows)}"


def test_recorded_row_carries_ttl_and_origin(db: str) -> None:
    """The CRUD path's derived columns are populated — the raw INSERT skipped them.

    ``needs_user`` originally wrote through raw ``sqlite3``, which set neither
    ``expires_at`` (from ``_compute_ttl``) nor a resolved ``origin_class``. Going
    through ``create_sync`` is what fixed that, so assert the columns rather than
    the call, or the next refactor can quietly go back to a raw write.
    """
    _run("cat secrets.env", db, dispatched=True)
    row = (
        sqlite3.connect(db)
        .execute("SELECT priority, origin_class, created_at FROM observations")
        .fetchone()
    )
    assert row is not None, "the deny must record an observation"
    priority, origin_class, created_at = row
    assert priority == "critical"
    assert origin_class == "first_party"
    assert created_at


def test_malformed_payload_fails_open(db: str) -> None:
    """A consent gate must not wedge every Bash command when its input is junk."""
    env = {**os.environ, "GENESIS_DB_PATH": db}
    proc = subprocess.run(
        [sys.executable, str(_GUARD)],
        input="not json at all",
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_dispatch_detector_matches_the_canonical_one() -> None:
    """Two detectors that disagree is worse than either.

    This test must COMPARE the two, not re-assert one of them. An earlier version
    only checked ``needs_user.is_dispatched()`` against its own documented
    behaviour while the docstring claimed it pinned agreement with
    ``git_push_guard._is_dispatched`` — so it would have passed unchanged if the
    sibling switched to a different variable tomorrow, which is the single thing
    it exists to prevent. It also popped GENESIS_CC_SESSION out of the live
    ``os.environ`` without restoring it, disabling dispatch detection for every
    test that ran after it in the same process.
    """
    nu = _needs_user()
    spec = importlib.util.spec_from_file_location("git_push_guard", _HOOKS / "git_push_guard.py")
    gpg = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gpg)

    original = os.environ.get("GENESIS_CC_SESSION")
    try:
        for value in (None, "0", "1", "true", ""):
            if value is None:
                os.environ.pop("GENESIS_CC_SESSION", None)
            else:
                os.environ["GENESIS_CC_SESSION"] = value
            assert nu.is_dispatched() is gpg._is_dispatched(), (
                f"detectors disagree for GENESIS_CC_SESSION={value!r}: "
                f"needs_user={nu.is_dispatched()} git_push_guard={gpg._is_dispatched()}"
            )
        # And the value that must mean dispatched actually does — otherwise two
        # detectors could agree by both being broken.
        os.environ["GENESIS_CC_SESSION"] = "1"
        assert nu.is_dispatched() is True
    finally:
        if original is None:
            os.environ.pop("GENESIS_CC_SESSION", None)
        else:
            os.environ["GENESIS_CC_SESSION"] = original


class TestAuditFindings:
    """Regressions for an adversarial audit of this module (2026-09-06).

    Every case below was a MEASURED bypass or false positive against the first
    version, reproduced before being fixed. They are grouped so the next reader
    can see what the matcher is actually up against — enumerate-the-shapes is the
    tar pit this module exists to avoid, and each of these is a shape the inode
    approach alone did not cover.
    """

    @staticmethod
    def _touches(**kw) -> bool:
        spec = importlib.util.spec_from_file_location("st", _HOOKS / "secrets_target.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.touches_secrets(**kw)

    # ── C-2: Grep's `glob` selects files and was never read ──────────────────
    def test_grep_glob_field_is_gated(self, db: str) -> None:
        """`Grep {"path": "~/genesis", "glob": "secrets.env"}` dumps the keys.

        With output_mode "content" this returns the matching LINES, i.e. the
        credential values. The first version read file_path/path/pattern only.
        """
        home = str(Path.home() / "genesis")
        assert self._touches(paths=[home, "secrets.env", os.path.join(home, "secrets.env")])

    def test_grep_glob_alone_is_gated(self) -> None:
        assert self._touches(paths=[str(Path.home() / "genesis" / "secrets*")])

    # ── H-1: bash brace expansion happens before the command runs ────────────
    def test_brace_expansion_is_resolved(self) -> None:
        home = str(Path.home() / "genesis")
        assert self._touches(command=f"cat {home}/{{secrets,other}}.env")
        assert self._touches(command="cp ~/genesis/{secrets.env,secrets.env.bak}")

    # ── H-2: talking about the file is not touching it ───────────────────────
    @pytest.mark.parametrize(
        "command",
        [
            'grep -n "secrets.env" scripts/bootstrap.sh',
            "gh pr create --body 'documents the secrets.env gate'",
            "echo 'the secrets.env file' >> notes.md",
        ],
    )
    def test_quoted_mention_does_not_gate(self, command: str) -> None:
        """A DENY on a dispatched session for discussing the file is a defect.

        These fired in the first version via the basename fallback. The cost is
        not merely noise: a prompt that fires on mentions is a prompt the owner
        learns to click through, which defeats the one that matters.
        """
        assert not self._touches(command=command)

    def test_heredoc_body_is_data_not_operands(self) -> None:
        """This module's own commit message must not trip its own gate."""
        cmd = "git commit -F - <<'MSG'\nfeat(hooks): secrets.env is not a service\n\nGates secrets.env access.\nMSG"
        assert not self._touches(command=cmd)

    def test_unquoted_bare_operand_still_gates(self) -> None:
        """The control for the three above: an actual bare operand still counts."""
        assert self._touches(command="cd ~/genesis && cat secrets.env")

    def test_quoted_real_path_still_gates(self) -> None:
        """Quoting a PATH is normal and must not become a bypass — only the
        separator-less bare-name case consults the quoting.
        """
        assert self._touches(command=f'cat "{Path.home()}/genesis/secrets.env"')

    # ── C-1: an unbounded glob walk outruns the hook's 10s budget ────────────
    def test_wide_glob_is_fast(self) -> None:
        """A hook killed at its timeout emits no decision, which is an ALLOW.

        `ls /sys/*/*/*/*` MEASURED at 6.0s before the bound — enough to stall
        every Bash call, and a deeper glob exceeded the budget entirely.
        """
        start = time.monotonic()
        assert not self._touches(command="ls /sys/*/*/*/* /usr/*/*/*")
        assert time.monotonic() - start < 1.0

    def test_secrets_glob_still_resolves(self) -> None:
        """The control: bounding the walk must not blind the globs that matter."""
        assert self._touches(command="cat ~/genesis/secrets.*")
        assert self._touches(command="cat ~/genesis/s*.env")

    # ── M-2: Path.stat raises ValueError, not OSError, on an embedded NUL ────
    def test_nul_byte_does_not_crash_the_hook(self, db: str) -> None:
        """An uncaught exception is exit 1 — a NON-blocking error, so the tool runs."""
        env = {**os.environ, "GENESIS_DB_PATH": db}
        proc = subprocess.run(
            [sys.executable, str(_GUARD)],
            input=json.dumps(
                {"tool_name": "Read", "tool_input": {"file_path": "/home/\x00x/secrets.env"}}
            ),
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert "Traceback" not in proc.stderr
