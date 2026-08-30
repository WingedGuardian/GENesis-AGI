"""A CC-supplied ``session_id`` becomes a filesystem PATH COMPONENT in several
hooks, so an id carrying ``/`` or ``..`` escapes the per-session directory —
two of the sites ``mkdir(parents=True)``, so the escape CREATES directories.

The guard already existed in five other hooks, hand-copied in three different
shapes; these sites simply omitted it. This locks the shared validator
(``hook_input.is_safe_session_id``) and its adoption at the previously-unguarded
sites.

Install-agnostic: every hook's base directory is monkeypatched to ``tmp_path``;
nothing touches the real ``~/.genesis``.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


@contextlib.contextmanager
def _not_a_dispatched_session():
    """Neutralise import-time dispatched-session exits for the duration of a load.

    ``scripts/proactive_memory_hook.py`` calls ``sys.exit(0)`` at MODULE level
    when ``GENESIS_CC_SESSION == "1"``, so an in-process load inside a dispatched
    session raises ``SystemExit`` and every test touching that module ERRORS
    instead of testing. That makes the suite green only because the variable
    happens to be unset in the shell that runs it.

    Applied in ``_load`` rather than at the two call sites that surfaced it: any
    future module with an import-time environment guard gets the same protection.
    The previous value is restored, so this never leaks into another test.
    """
    prev = os.environ.pop("GENESIS_CC_SESSION", None)
    try:
        yield
    finally:
        if prev is not None:
            os.environ["GENESIS_CC_SESSION"] = prev


def _load(name: str, rel: str):
    with _not_a_dispatched_session():
        spec = importlib.util.spec_from_file_location(name, _SCRIPTS / rel)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


hook_input = _load("hook_input", "hooks/hook_input.py")


def _run_hook(script: str, payload: dict, home: Path, *, expect_ok: bool = True):
    """Run a hook end to end against a sandboxed HOME, and PROVE it ran.

    One module-level helper rather than one per test class: the two copies this
    replaced were byte-identical, so a divergence between them would have been
    invisible — the same replica shape this suite exists to catch.

    The environment is SCRUBBED, not merely HOME-redirected. `GENESIS_REPO_ROOT`
    points `genesis_urgent_alerts` at a real database and `GENESIS_DB_PATH`
    overrides it outright, so an inherited environment makes the result depend on
    the machine the suite runs on — which contradicts this file's install-agnostic
    contract.

    `expect_ok` asserts the hook exited 0. Without it, a hook that dies before
    printing (a broken `sys.path` insert, an import error) yields EMPTY stdout in
    both arms of a comparison, and an `unsafe.stdout == safe.stdout` assertion
    passes while nothing under test ever executed.
    """
    (home / ".genesis").mkdir(parents=True, exist_ok=True)
    (home / ".genesis" / "cc_context_enabled").touch()
    env = {**os.environ, "HOME": str(home)}
    for _var in ("GENESIS_CC_SESSION", "GENESIS_REPO_ROOT", "GENESIS_DB_PATH"):
        env.pop(_var, None)
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS / script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    if expect_ok:
        assert proc.returncode == 0, (
            f"{script} exited {proc.returncode} — a crashing hook produces empty "
            f"stdout in every arm, so output comparisons pass while nothing ran."
            f"\nstderr:\n{proc.stderr[:2000]}"
        )
    return proc

# Traversal ids that must never reach a path join.
TRAVERSALS = ["../../escaped", "a/b", "..", "../x", "a\\b", "with\x00nul", "abc\n"]
# Ids in the real CC shape (measured: hex UUID v4) must keep working.
# Synthetic ids in CC's observed shape (lowercase hex UUID v4). Deliberately
# NOT real session ids from any install — fixtures stay install-agnostic.
REAL_IDS = [
    "00000000-0000-4000-8000-000000000001",
    "abcdef01-2345-4678-89ab-cdef01234567",
    "0f1e2d3c-4b5a-4967-8877-66554433221f",
]


class TestIsSafeSessionId:
    @pytest.mark.parametrize("sid", REAL_IDS)
    def test_real_session_ids_accepted(self, sid):
        assert hook_input.is_safe_session_id(sid) is True

    @pytest.mark.parametrize("sid", TRAVERSALS)
    def test_traversal_shapes_rejected(self, sid):
        assert hook_input.is_safe_session_id(sid) is False

    def test_empty_rejected(self):
        assert hook_input.is_safe_session_id("") is False

    def test_non_string_rejected(self):
        assert hook_input.is_safe_session_id(None) is False

    def test_length_bound_is_the_filesystem_limit_not_an_arbitrary_cap(self):
        """Both directions, at the boundary.

        Asserting only that something long is rejected lets the bound drift
        downward silently, and a bound below the filesystem's own limit REGRESSES
        ids that are valid path components and that the hand-rolled checks this
        replaces accepted (they had no length limit at all).
        """
        assert hook_input.is_safe_session_id("a" * 255) is True, (
            "255 bytes is a legal filename; rejecting it drops a usable session id"
        )
        assert hook_input.is_safe_session_id("a" * 256) is False

    def test_trailing_newline_rejected(self):
        """``^…$`` would ACCEPT this (``$`` matches before a final newline);
        the shared validator anchors with ``\\A…\\Z``."""
        assert hook_input.is_safe_session_id(REAL_IDS[0] + "\n") is False


class TestSessionIdAccessorRejectsUnsafe:
    def test_unsafe_payload_id_collapses_to_default(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        assert hook_input.session_id({"session_id": "../../escaped"}) == "unknown"

    def test_safe_payload_id_passes_through(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        assert hook_input.session_id({"session_id": REAL_IDS[0]}) == REAL_IDS[0]

    def test_unsafe_env_id_collapses_to_default(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_SESSION_ID", "a/b")
        assert hook_input.session_id({}) == "unknown"

    def test_present_unsafe_payload_id_does_not_fall_back_to_env(self, monkeypatch):
        """A PRESENT payload id is authoritative. Falling through to a stale
        CLAUDE_SESSION_ID would answer with a DIFFERENT session's id, and the
        caller would then read or create that session's sentinels."""
        monkeypatch.setenv("CLAUDE_SESSION_ID", "aaaaaaaa-0000-4000-8000-000000000099")
        assert hook_input.session_id({"session_id": "../../evil"}) == "unknown"

    def test_absent_payload_id_still_uses_the_env_fallback(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_SESSION_ID", REAL_IDS[1])
        assert hook_input.session_id({}) == REAL_IDS[1]


class TestHooksDoNotEscapeSessionDir:
    """The load-bearing RED: these sites build a path from the raw id today."""

    def test_urgent_alerts_buffer_does_not_escape(self, tmp_path, monkeypatch):
        mod = _load("genesis_urgent_alerts", "genesis_urgent_alerts.py")
        monkeypatch.setattr(mod, "_GENESIS_DIR", tmp_path / ".genesis")
        mod._buffer_message("../../escaped", "hi", datetime.now(UTC))
        assert not (tmp_path / "escaped").exists(), "traversal created a dir outside sessions/"
        # Control: without this half the test also passes if _session_dir started
        # returning None for EVERY id — an inert guard and a correct one score the
        # same on the traversal case alone.
        mod._buffer_message(REAL_IDS[0], "hi", datetime.now(UTC))
        assert (tmp_path / ".genesis" / "sessions" / REAL_IDS[0] / "messages.jsonl").exists(), (
            "a safe id must still be buffered — the guard is inert, not selective"
        )

    def test_urgent_alerts_staleness_marker_does_not_escape(self, tmp_path, monkeypatch):
        mod = _load("genesis_urgent_alerts", "genesis_urgent_alerts.py")
        monkeypatch.setattr(mod, "_GENESIS_DIR", tmp_path / ".genesis")
        mod._record_staleness_nudge("../../escaped2", datetime.now(UTC))
        assert not (tmp_path / "escaped2").exists()
        # Same control as above: prove the guard discriminates rather than refuses
        # everything.
        mod._record_staleness_nudge(REAL_IDS[0], datetime.now(UTC))
        assert (tmp_path / ".genesis" / "sessions" / REAL_IDS[0]).exists(), (
            "a safe id must still record its marker"
        )

    def test_proactive_trail_path_does_not_escape(self, tmp_path, monkeypatch):
        mod = _load("proactive_memory_hook", "proactive_memory_hook.py")
        monkeypatch.setattr(mod, "_TRAIL_DIR", tmp_path / "sessions")
        base = (tmp_path / "sessions").resolve()
        # Traversal id -> refused outright.
        assert mod._trail_path("../../escaped") is None
        # A SAFE id must STILL produce a contained path. Without this half, the
        # test passes vacuously the moment the guard starts returning None.
        good = mod._trail_path(REAL_IDS[0])
        assert good is not None, "a safe id must still yield a trail path"
        # RESOLVE both sides — PurePath.parents does NOT collapse "..".
        assert base in good.resolve().parents, f"escaped to {good.resolve()}"


class TestUnsafeIdSkipsOnlyThePathRead:
    """An unsafe id must skip the session-SCOPED read, not the whole hook.

    Both of these hooks do path-INDEPENDENT work after reading the id
    (`genesis_stop_hook` runs its giving-up / review-state checks from the hook
    payload alone; `genesis_session_end` writes last-session metadata that uses
    the id as JSON data, not as a path). An earlier revision of this guard
    returned early on an unsafe id and silently disabled that work.
    """

    def test_session_end_still_writes_metadata_with_unsafe_id(self, tmp_path):
        _run_hook(
            "genesis_session_end.py",
            {"session_id": "../../escaped", "reason": "clear"},
            tmp_path,
        )
        assert (tmp_path / ".genesis" / "last_foreground_session.json").exists(), (
            "path-independent last-session write was skipped for an unsafe id"
        )
        assert not (tmp_path / "escaped").exists()

    def test_session_end_writes_metadata_with_safe_id(self, tmp_path):
        _run_hook(
            "genesis_session_end.py",
            {"session_id": REAL_IDS[0], "reason": "clear"},
            tmp_path,
        )
        assert (tmp_path / ".genesis" / "last_foreground_session.json").exists()

    def test_stop_hook_still_runs_payload_only_checks_with_unsafe_id(self, tmp_path):
        """The giving-up check needs only the assistant message; an unsafe id
        must not suppress it."""
        payload = {
            "session_id": "../../escaped",
            "last_assistant_message": "You'll need to run the migration yourself.",
        }
        unsafe = _run_hook("genesis_stop_hook.py", payload, tmp_path)
        safe = _run_hook(
            "genesis_stop_hook.py", {**payload, "session_id": REAL_IDS[0]}, tmp_path
        )
        # Whatever the payload-only checks emit, an unsafe id must not change it.
        assert unsafe.stdout == safe.stdout, (
            "unsafe session id changed payload-only hook behaviour"
        )
        assert not (tmp_path / "escaped").exists()


class TestSessionPathHelper:
    """The path-returning API. A bool invites each caller to decide what to skip
    on rejection, and that decision is an open set — the id is only ever unsafe
    as a PATH COMPONENT, never as a bound SQL parameter or a JSON value."""

    def test_safe_id_builds_the_path(self, tmp_path):
        p = hook_input.session_path(tmp_path, REAL_IDS[0], "messages.jsonl")
        assert p == tmp_path / REAL_IDS[0] / "messages.jsonl"

    @pytest.mark.parametrize("sid", TRAVERSALS)
    def test_unsafe_id_returns_none(self, tmp_path, sid):
        assert hook_input.session_path(tmp_path, sid, "messages.jsonl") is None

    def test_result_never_escapes_the_base(self, tmp_path):
        built = 0
        for sid in TRAVERSALS + REAL_IDS:
            p = hook_input.session_path(tmp_path, sid)
            if p is None:
                continue
            built += 1
            assert tmp_path.resolve() in p.resolve().parents, f"escaped to {p}"
        assert built == len(REAL_IDS), (
            "every safe id must still build a path — otherwise this loop asserts "
            "nothing and passes vacuously"
        )


class TestRejectionDoesNotDisableNonPathWork:
    """Round-3 class: a path-safety rejection must not gate work that never
    touches the filesystem."""

    def test_urgent_alerts_still_emits_context_for_a_rejected_id(self, tmp_path):
        """BEHAVIOURAL, not a source-text scan: an earlier revision asserted that
        a particular blanking statement was absent from main(), which would still
        pass against an early `if not is_safe_session_id(...): return` — the far
        likelier regression shape. Drive the real entry point instead.

        _emit_temporal_context guards its own file read via _session_dir, and
        _emit_charter_tag uses the id only as a bound SQL parameter, so neither
        may be skipped for an id that merely fails the path rule."""
        payload = {"prompt": "hi", "hook_event_name": "UserPromptSubmit"}
        safe = _run_hook(
            "genesis_urgent_alerts.py", {**payload, "session_id": REAL_IDS[0]}, tmp_path
        )
        unsafe_home = tmp_path / "u"
        unsafe = _run_hook(
            "genesis_urgent_alerts.py",
            {**payload, "session_id": "../../escaped"},
            unsafe_home,
        )
        assert safe.stdout.strip(), "control produced no output — test proves nothing"
        assert "[Clock:" in unsafe.stdout, (
            "payload-only temporal context was suppressed for a rejected id"
        )
        assert not (unsafe_home / "escaped").exists()

    def test_trail_workflow_aborts_when_it_cannot_persist(self, tmp_path, monkeypatch):
        """Without a safe trail path _save_trail discards every update, so each
        turn would look like the FIRST pivot and record a false observation."""
        mod = _load("proactive_memory_hook", "proactive_memory_hook.py")
        monkeypatch.setattr(mod, "_TRAIL_DIR", tmp_path / "sessions")
        assert mod._update_and_format_trail("../../escaped", ["alpha"], "alpha beta") is None


class TestCharterBlockKeepsDbLookupUnguarded:
    """The path guard must sit below the bound-SQL lookup.

    ``_load_charter_db`` binds the id as a SQL PARAMETER, so it is safe for ANY
    id; only ``_load_charter_file`` interpolates it into a path. A guard above
    both would drop a canonical, DB-backed charter (and its ledger) for an id
    that merely fails the path rule — the same class that consumed review rounds
    2 and 3.
    """

    def test_db_charter_survives_a_path_unsafe_id(self, tmp_path, monkeypatch):
        mod = _load("genesis_session_context", "genesis_session_context.py")
        called = {}

        def fake_db(sid, db_path=None):
            called["db"] = sid
            return {"origin_prompt": "the origin", "origin_ts": "2026-08-27"}, []

        def fake_file(sid, sessions_dir=None):
            called["file"] = sid
            return None

        monkeypatch.setattr(mod, "_load_charter_db", fake_db)
        monkeypatch.setattr(mod, "_load_charter_file", fake_file)
        out = mod._charter_emission_block(
            "../../escaped",
            "resume",
            sessions_dir=tmp_path / "sessions",
            db_path=tmp_path / "db.sqlite",
        )
        assert called.get("db") == "../../escaped", "DB lookup was skipped"
        assert "file" not in called, "path-based fallback must NOT run for an unsafe id"
        assert "Session Charter" in out, "canonical charter was suppressed"


class TestSuiteRunsInsideADispatchedSession:
    """These tests must EXERCISE the guard, not error before reaching it.

    ``scripts/proactive_memory_hook.py`` exits at import when
    ``GENESIS_CC_SESSION == "1"``. Before ``_load`` neutralised that, the two
    tests below raised ``SystemExit: 0`` under a dispatched session and were
    green everywhere else purely because the variable was unset — a suite that
    reports success without running is worse than one that fails.
    """

    def test_module_loads_when_the_dispatched_session_flag_is_set(self, monkeypatch):
        monkeypatch.setenv("GENESIS_CC_SESSION", "1")
        mod = _load("proactive_memory_hook", "proactive_memory_hook.py")
        # The guard is reached and still correct, not merely importable.
        assert mod._trail_path("../../escaped") is None

    def test_the_flag_is_restored_after_a_load(self, monkeypatch):
        """The neutralisation is scoped: it must not leak into later tests."""
        monkeypatch.setenv("GENESIS_CC_SESSION", "1")
        _load("proactive_memory_hook", "proactive_memory_hook.py")
        assert os.environ.get("GENESIS_CC_SESSION") == "1"

    def test_an_unset_flag_stays_unset(self, monkeypatch):
        """And it must not INVENT the variable where there was none."""
        monkeypatch.delenv("GENESIS_CC_SESSION", raising=False)
        _load("proactive_memory_hook", "proactive_memory_hook.py")
        assert "GENESIS_CC_SESSION" not in os.environ


class TestTheDocumentedContractMatchesTheCode:
    """The compatibility doc STATES the allow-list; a reader treats it as the
    contract and writes hooks against it.

    This exists because the pattern and its documentation drifted the moment the
    bound changed: the validator moved to 255 while
    `docs/reference/cc-compatibility.md` still specified 128, so the canonical
    doc told another hook author to reject ids the runtime deliberately accepts.
    Comparing the two by eye is what failed; compare them mechanically.
    """

    _DOC = Path(__file__).resolve().parents[2] / "docs" / "reference" / "cc-compatibility.md"

    def test_documented_allow_list_is_the_compiled_one(self):
        text = self._DOC.read_text(encoding="utf-8")
        quoted = re.search(r"allow-list `([^`]+)`", text)
        assert quoted, (
            f"{self._DOC.name} no longer states the allow-list in the expected "
            "form — this guard cannot see drift it cannot find, so update the "
            "pattern here rather than deleting the assertion"
        )
        assert quoted.group(1) == hook_input._SESSION_ID_RE.pattern, (
            f"{self._DOC.name} documents {quoted.group(1)!r} but the validator "
            f"compiles {hook_input._SESSION_ID_RE.pattern!r} — a hook author "
            "following the doc would reject ids the runtime accepts"
        )
