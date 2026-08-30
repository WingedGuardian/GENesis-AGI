"""The four-part context injection stays under the measured harness cap.

MEASURED 2026-08-30 on CC 2.1.246 (~25 real probe sessions): the harness FILES
a hook's stdout above EXACTLY 10,000 characters (10,000 inline / 10,001 filed;
chars not bytes — 6,000 two-byte chars = 12,044 B arrived inline) and the
budget is PER HOOK ENTRY (two 9,000-char hooks both arrived inline). Above the
cap the model receives a ~2 KB preview and the rest of that part simply never
arrives — the silent-context-loss class that ran unnoticed for a month.

These tests are the CI wall for that class: per-part payloads (with the REAL
tracked identity files) must fit, the four-entry wiring must exist in order,
and the degrade paths must be loud, not silent.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPTS_DIR = _ROOT / "scripts"
_IDENTITY_DIR = _ROOT / "src" / "genesis" / "identity"

_ctx_spec = importlib.util.spec_from_file_location(
    "genesis_session_context_budget", _SCRIPTS_DIR / "genesis_session_context.py"
)
_ctx = importlib.util.module_from_spec(_ctx_spec)
_ctx_spec.loader.exec_module(_ctx)


# ── constants pin the measurement ───────────────────────────────────────


def test_cap_constants_pin_the_measurement():
    assert _ctx._HOOK_STDOUT_CAP == 10_000, (
        "the harness cap constant changed — it is a MEASURED value; re-run the "
        "probe (GENESIS_CTX_PROBE_BYTES) before touching it and update "
        "docs/reference/cc-compatibility.md"
    )
    assert _ctx._PART_BUDGET < _ctx._HOOK_STDOUT_CAP


# ── CI ceilings on the TRACKED identity files ───────────────────────────
# USER.md is install-local (gitignored) — CI cannot see it, so its guard is
# runtime elision, tested below. These ceilings stop a feature PR regrowing a
# tracked file past its part's room, which is exactly how the payload crossed
# the cliff the first time (3,350 B at birth → 11,283 B in five months of
# appends).


def test_conversation_md_ceiling():
    size = len((_IDENTITY_DIR / "CONVERSATION.md").read_text())
    assert size <= 6_500, (
        f"CONVERSATION.md is {size} chars (ceiling 6,500). It shares a 9,800-char "
        "hook budget with the install-local USER.md. Protocol DETAIL belongs in "
        "docs/reference/conversation-protocols.md — add a pointer, not a section."
    )


def test_identity_core_ceiling():
    size = sum(len((_IDENTITY_DIR / n).read_text()) for n in ("SOUL.md", "STEERING.md"))
    assert size <= 9_000, (
        f"SOUL.md + STEERING.md = {size} chars (ceiling 9,000) against the "
        "identity-core part's 9,800-char budget. Trim before shipping — over the "
        "harness cap the ENTIRE part is silently withheld from every session."
    )


# ── the four-entry wiring ───────────────────────────────────────────────


def test_settings_wires_four_parts_in_order():
    """The no-arg all-in-one being wired IS the old bug — make it unrepresentable."""
    settings = json.loads((_ROOT / ".claude" / "settings.json").read_text())
    commands = [
        h["command"] for entry in settings["hooks"]["SessionStart"] for h in entry.get("hooks", [])
    ]
    ctx_cmds = [c for c in commands if "genesis_session_context.py" in c]
    parts = []
    for c in ctx_cmds:
        assert "--part" in c, f"un-parted session_context wiring found: {c!r}"
        parts.append(c.split("--part", 1)[1].strip())
    assert parts == ["charter", "identity-core", "identity-user", "knowledge"], parts


# ── per-part budget, real payloads, real subprocess ─────────────────────


def _run_part(part: str, home: Path, *, extra_env: dict | None = None) -> str:
    (home / ".genesis").mkdir(parents=True, exist_ok=True)
    (home / ".genesis" / "cc_context_enabled").touch()
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    env.update(extra_env or {})
    result = subprocess.run(
        [sys.executable, str(_SCRIPTS_DIR / "genesis_session_context.py"), "--part", part],
        capture_output=True,
        text=True,
        env=env,
        input="{}",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr[-500:]
    return result.stdout


def test_identity_parts_fit_their_budgets_with_real_files(tmp_path):
    """The tracked identity payload, as actually emitted, fits per part."""
    for part in ("identity-core", "identity-user"):
        out = _run_part(part, tmp_path / part)
        assert len(out) <= _ctx._PART_BUDGET, (part, len(out))
        assert f"_[ctx {part}:" in out


def test_each_part_emits_its_audit_line(tmp_path):
    for part in ("charter", "identity-core", "identity-user", "knowledge"):
        out = _run_part(part, tmp_path / part)
        assert f"_[ctx {part}:" in out, f"{part} missing its self-audit line"


def _run_raw(tmp_path: Path, *args: str, sid: str = "11111111-2222-3333-4444-555555555555"):
    (tmp_path / ".genesis").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".genesis" / "cc_context_enabled").touch()
    return subprocess.run(
        [sys.executable, str(_SCRIPTS_DIR / "genesis_session_context.py"), *args],
        capture_output=True,
        text=True,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        input=json.dumps({"session_id": sid, "source": "startup"}),
        timeout=60,
    )


def test_explicit_all_emits_every_part_for_tests_and_manual_runs(tmp_path):
    """`all` is reachable ONLY by explicit request — never as a fallback."""
    r = _run_raw(tmp_path, "--part", "all")
    assert "_[ctx all:" in r.stdout


def test_missing_part_fails_closed_to_charter_and_screams(tmp_path):
    """F1: a mis-wire must NOT fall back to the all-in-one payload.

    Reachable in production by version skew — `.claude/hooks/genesis-hook` runs
    the MAIN-tree script, so a worktree still on single-entry settings.json
    invokes the new script with no --part. The old fallback emitted EVERYTHING
    under one hook entry (a guaranteed filing) AND suppressed the marker, i.e.
    the original bug with its alarm disabled.
    """
    sid = "11111111-2222-3333-4444-555555555555"
    r = _run_raw(tmp_path, sid=sid)
    assert "_[ctx charter:" in r.stdout
    for other in ("identity-core", "identity-user", "knowledge"):
        assert f"_[ctx {other}:" not in r.stdout
    assert len(r.stdout) <= _ctx._PART_BUDGET, "a mis-wire must stay UNDER the cap"
    assert "MIS-WIRED" in r.stderr
    marker = tmp_path / ".genesis" / "sessions" / sid / "injection_over_budget_wiring.json"
    assert marker.exists(), "a mis-wire must leave a marker for the per-prompt scream"
    assert json.loads(marker.read_text())["part"] == "wiring"


def test_unknown_part_fails_closed_the_same_way(tmp_path):
    r = _run_raw(tmp_path, "--part", "bogus")
    assert "_[ctx charter:" in r.stdout
    assert "MIS-WIRED" in r.stderr


# ── runtime degrade for the install-local USER.md ───────────────────────


def _direct_part(monkeypatch, capsys, identity_dir: Path, part: str) -> str:
    monkeypatch.setattr(_ctx, "_IDENTITY_DIR", identity_dir)
    monkeypatch.setattr(_ctx, "_FLAG", identity_dir / "flag")
    (identity_dir / "flag").touch()
    monkeypatch.setattr(_ctx, "_SESSION_START_FILE", identity_dir / "session_start")
    monkeypatch.setattr(_ctx, "_marker_dir", lambda sid: identity_dir / sid)
    # A test must not rewrite the developer's / CI's real git hooks (F13c).
    monkeypatch.setattr(_ctx, "_sync_genesis_hooks", lambda: None)
    monkeypatch.setattr(_ctx.sys, "argv", ["x", "--part", part])
    monkeypatch.setattr(_ctx.sys, "stdin", __import__("io").StringIO("{}"))
    _ctx._emitted_chars = 0
    _ctx.main()
    return capsys.readouterr().out


def test_huge_user_md_elides_conversation_to_pointer(monkeypatch, capsys, tmp_path):
    ident = tmp_path / "identity"
    ident.mkdir()
    (ident / "USER.md").write_text("U" * 8_000)
    (ident / "CONVERSATION.md").write_text("C" * 5_000)
    out = _direct_part(monkeypatch, capsys, ident, "identity-user")
    assert "U" * 8_000 in out
    assert "C" * 5_000 not in out
    assert "omitted for size" in out
    assert len(out) <= _ctx._PART_BUDGET + 200


def test_oversized_user_md_truncates_loudly_never_silently(monkeypatch, capsys, tmp_path):
    ident = tmp_path / "identity"
    ident.mkdir()
    (ident / "USER.md").write_text("U" * 15_000)
    out = _direct_part(monkeypatch, capsys, ident, "identity-user")
    assert "truncated at" in out
    assert len(out) <= _ctx._PART_BUDGET + 200


def test_normal_user_and_conversation_render_whole(monkeypatch, capsys, tmp_path):
    """Control: the degrade paths fire ONLY under pressure."""
    ident = tmp_path / "identity"
    ident.mkdir()
    (ident / "USER.md").write_text("U" * 1_500)
    (ident / "CONVERSATION.md").write_text("C" * 5_000)
    out = _direct_part(monkeypatch, capsys, ident, "identity-user")
    assert "U" * 1_500 in out and "C" * 5_000 in out
    assert "omitted" not in out and "truncated" not in out


# ── over-budget marker lifecycle ────────────────────────────────────────


def test_over_budget_part_writes_marker_and_under_budget_clears_it(monkeypatch, capsys, tmp_path):
    """The marker mechanism itself: over -> keyed entry; under -> cleared.

    Driven through _finish_part directly because the emission paths (correctly)
    protect themselves -- a first version of this test shrank the budget and
    expected a marker, and the identity part TRUNCATED ITSELF back under budget
    instead, which is the better outcome and its own test above.
    """
    monkeypatch.setattr(_ctx, "_marker_dir", lambda sid: tmp_path / sid)
    marker = tmp_path / "sid-x" / "injection_over_budget_knowledge.json"
    _ctx._emitted_chars = 12_000  # simulate a part the ladder could not save
    _ctx._finish_part("knowledge", "sid-x")
    data = json.loads(marker.read_text())
    # One file per (session, part) now, so the payload is the record itself —
    # not a key inside a shared dict. The recorded total includes the audit line
    # (emitted before the marker write), so >= the simulated payload.
    assert data["part"] == "knowledge"
    assert data["chars"] >= 12_000
    assert capsys.readouterr().err  # loud on stderr too
    # F2: a second over-budget part gets its OWN file. The old shared dict was a
    # read-modify-write race across four CONCURRENT hooks, in which an
    # under-budget sibling's clear could erase this entry — a racing alarm.
    _ctx._emitted_chars = 11_000
    _ctx._finish_part("charter", "sid-x")
    other = tmp_path / "sid-x" / "injection_over_budget_charter.json"
    assert marker.exists() and other.exists()
    # Recovery is per part and cannot touch a sibling's file.
    _ctx._emitted_chars = 500
    _ctx._finish_part("knowledge", "sid-x")
    assert not marker.exists()
    assert other.exists(), "clearing one part must not clear another"
    _ctx._emitted_chars = 500
    _ctx._finish_part("charter", "sid-x")
    assert not other.exists()


def test_markers_are_scoped_to_their_session(monkeypatch, capsys, tmp_path):
    """F2: the marker was global, so one session's clean start cleared another's
    live loss (and one session's overflow made unrelated sessions scream)."""
    monkeypatch.setattr(_ctx, "_marker_dir", lambda sid: tmp_path / sid)
    _ctx._emitted_chars = 12_000
    _ctx._finish_part("knowledge", "session-A")
    _ctx._emitted_chars = 500
    _ctx._finish_part("knowledge", "session-B")
    assert (tmp_path / "session-A" / "injection_over_budget_knowledge.json").exists()
    assert not (tmp_path / "session-B" / "injection_over_budget_knowledge.json").exists()


def test_all_mode_never_writes_the_marker(monkeypatch, capsys, tmp_path):
    """A manual full run legitimately exceeds one hook's budget — no false alarm."""
    ident = tmp_path / "identity"
    ident.mkdir()
    monkeypatch.setattr(_ctx, "_PART_BUDGET", 100)
    monkeypatch.setattr(_ctx, "_marker_dir", lambda sid: ident / sid)
    (ident / "SOUL.md").write_text("S" * 1_000)
    _direct_part(monkeypatch, capsys, ident, "all")
    assert not list(ident.glob("*/injection_over_budget*.json"))


def test_unwritable_marker_dir_is_loud_not_silent(monkeypatch, capsys, tmp_path):
    """Security review WARNING 1: the marker write is fail-OPEN (it must never
    break session start) but must not be fail-QUIET — an unwritable sessions dir
    would otherwise take the per-prompt scream with it."""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    os.chmod(blocked, 0o500)  # readable, NOT writable — the file lands HERE
    monkeypatch.setattr(_ctx, "_marker_dir", lambda sid: blocked)
    _ctx._emitted_chars = 12_000
    try:
        _ctx._finish_part("knowledge", "sid")
    finally:
        os.chmod(blocked, 0o755)
    err = capsys.readouterr().err
    assert "could not record" in err
    assert "DEGRADED" in err
