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
import uuid
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
    # IN-BAND, not a marker file: a SessionStart hook's stderr on exit 0 goes to
    # the debug log and never reaches the model, so a mis-wire that only wrote
    # to stderr would be invisible to the one reader who can act on it.
    assert "GENESIS ALERT: SessionStart hook MIS-WIRED" in r.stdout


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
    monkeypatch.setattr(_ctx, "_mirror_path", lambda sid, part: identity_dir / f"{sid}-{part}.md")
    # A test must not rewrite the developer's / CI's real git hooks (F13c).
    monkeypatch.setattr(_ctx, "_sync_genesis_hooks", lambda: None)
    monkeypatch.setattr(_ctx.sys, "argv", ["x", "--part", part])
    monkeypatch.setattr(_ctx.sys, "stdin", __import__("io").StringIO("{}"))
    _ctx._OUT = None
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


def test_charter_part_overhead_fits_the_budget():
    """The charter part must be UN-CUTTABLE by construction, not by luck.

    Its ceiling (`_CHARTER_BLOCK_MAX`) plus everything else the part always
    emits has to leave headroom, or the structured degrade in
    `_shrink_charter_block` — which exists precisely so no open ledger row is
    ever lost — can be undone by a flat cut through the rows it protected.
    MEASURED overhead when this was written: session-config 572 worst case,
    onboarding pointer 607, recovery header ~200. A future block added to this
    part fails HERE instead of silently truncating an agreement.
    """
    session_config = max(
        len(_ctx._session_config_block("high", m, ""))
        for m in ("claude-opus-5[1m]", "claude-zeta-9-preview-20261231[1m]", "")
    )
    onboarding = 607  # the fixed pointer text emitted when the setup floor is unmet
    # A PRODUCTION-shaped mirror path: the real one carries a 36-char session
    # UUID under the user's home, and the header interpolates it. A short
    # fixture path understated this by ~25 chars against a margin measured in
    # the low hundreds.
    header = len(
        _ctx._recovery_header(
            "charter",
            Path.home() / ".genesis" / "sessions" / str(uuid.uuid4()) / "context-charter.md",
        )
    )
    # The mis-wire alert fires ON this part, so the worst case includes it —
    # measured from the real function, never an estimate that can drift.
    miswire = len(_ctx._miswire_alert("no --part argument (settings.json wiring is out of date)"))
    dividers = 8 * 3  # "\n\n---\n\n" plus print()'s newline, three possible
    total = (
        session_config
        + onboarding
        + header
        + miswire
        + dividers
        + _ctx._CHARTER_BLOCK_MAX
        + _ctx._AUDIT_LINE_RESERVE
    )
    assert total <= _ctx._PART_BUDGET, (
        f"charter part could overrun: {total} > {_ctx._PART_BUDGET}. "
        "Lower _CHARTER_BLOCK_MAX or shrink a fixed block."
    )


def test_charter_ceiling_stays_above_the_degrade_floor():
    """The other half of the squeeze, and the one that actually protects ids.

    `_shrink_charter_block`'s last tier renders every open row as a bare id so
    no agreement is ever invisible. If `_CHARTER_BLOCK_MAX` were lowered below
    what that tier costs for a full ledger, the chokepoint would cut INTO the
    ids — undoing the degrade the block exists for, and silently. Measured from
    the real function at the real fetch bound.
    """
    rows = [{"id": f"{i:032x}", "text": "t" * 300, "status": "open"} for i in range(200)]
    assert len(rows) == _ctx._LEDGER_FETCH_MAX
    floor = len(
        _ctx._shrink_charter_block(
            "## Session Charter", "**Ledger (open):**", rows, {}, "_footer_", "sid"
        )
    )
    assert floor <= _ctx._CHARTER_BLOCK_MAX, (
        f"tier-4 degrade needs {floor} chars but the ceiling is "
        f"{_ctx._CHARTER_BLOCK_MAX} — a full ledger would lose row ids to the cut."
    )


# ── the chokepoint, and the mirror that makes a cut recoverable ────────


def test_a_part_that_overruns_is_cut_not_filed(monkeypatch, capsys, tmp_path):
    """The property the marker layer could only REPORT, now enforced.

    Driven through the real emission path with a tiny budget: an oversized
    identity file used to be able to push a part past the harness cap, and the
    only response was a marker saying so after the fact. Now the writer refuses
    to cross the budget at all.
    """
    ident = tmp_path / "identity"
    ident.mkdir()
    monkeypatch.setattr(_ctx, "_PART_BUDGET", 600)
    (ident / "SOUL.md").write_text("S" * 5_000)
    (ident / "STEERING.md").write_text("T" * 5_000)
    out = _direct_part(monkeypatch, capsys, ident, "identity-core")
    assert len(out) <= 600, len(out)
    assert "CUT" in out, "a cut must be announced in-band, never silent"


def test_a_cut_part_names_its_mirror_and_the_source_file(monkeypatch, capsys, tmp_path):
    """A hard cut destroys the tail; the harness at least keeps a filed payload.

    So a cut part must stay recoverable. Two mechanisms share that job, and the
    split is worth stating: a FILE-derived block is truncated by the elision
    ladder BEFORE it reaches the writer, with a marker naming the file to read
    (the file is its own recovery), while the mirror holds everything the hook
    intended to emit. The mirror's whole-text guarantee therefore matters most
    for SYNTHESISED blocks, which exist nowhere else — see the test below.
    """
    ident = tmp_path / "identity"
    ident.mkdir()
    monkeypatch.setattr(_ctx, "_PART_BUDGET", 600)
    (ident / "SOUL.md").write_text("S" * 5_000)
    out = _direct_part(monkeypatch, capsys, ident, "identity-core")
    mirror = ident / "-identity-core.md"
    assert mirror.exists(), "a cut part must still be recoverable from disk"
    assert "SOUL.md" in mirror.read_text(), "the mirror must name the source to re-read"
    assert str(mirror) in out, "the in-band marker must name the mirror"
    assert "could not mirror" not in capsys.readouterr().err


def test_the_mirror_holds_a_synthesised_block_whole(monkeypatch, capsys, tmp_path):
    """The case with no other copy on disk: in-flight state, procedures, the
    charter, the capability roster. If the writer cuts one of those, the mirror
    is the ONLY place the dropped tail still exists."""
    monkeypatch.setattr(_ctx, "_PART_BUDGET", 400)
    mirror = tmp_path / "knowledge.md"
    monkeypatch.setattr(_ctx, "_mirror_path", lambda sid, part: mirror)
    _ctx._OUT = None
    _ctx._begin_part("knowledge", mirror)
    _ctx._emit("SYNTH-" + "Z" * 3_000, block="in-flight")
    _ctx._finish_part("knowledge", "sid")
    body = mirror.read_text()
    assert "SYNTH-" + "Z" * 3_000 in body, "the dropped tail must survive on disk"
    assert len(capsys.readouterr().out) <= 400


def test_a_healthy_part_is_mirrored_too(monkeypatch, capsys, tmp_path):
    """The recovery header names the mirror on EVERY part, so it must exist even
    when nothing was cut — a header pointing at a missing file is worse than one
    that admits the gap."""
    ident = tmp_path / "identity"
    ident.mkdir()
    (ident / "SOUL.md").write_text("S" * 100)
    _direct_part(monkeypatch, capsys, ident, "identity-core")
    assert (ident / "-identity-core.md").exists()


def test_the_audit_line_reports_what_was_dropped(monkeypatch, capsys, tmp_path):
    """After a chokepoint a bare "N/budget" is always green and says nothing;
    the number that carries information is what was CUT and where to read it.

    Driven through a SYNTHESISED block rather than an identity file: the
    elision ladder truncates file-derived content before the writer ever sees
    it, so a file fixture exercises the ladder (its own tests) and not this.
    """
    monkeypatch.setattr(_ctx, "_PART_BUDGET", 400)
    mirror = tmp_path / "knowledge.md"
    monkeypatch.setattr(_ctx, "_mirror_path", lambda sid, part: mirror)
    _ctx._OUT = None
    _ctx._begin_part("knowledge", mirror)
    _ctx._emit("Z" * 3_000, block="in-flight")
    _ctx._finish_part("knowledge", "sid")
    out = capsys.readouterr().out
    assert "intended" in out and "emitted" in out
    assert "CUT" in out
    assert str(mirror) in out


def test_a_healthy_audit_line_does_not_claim_a_cut(monkeypatch, capsys, tmp_path):
    """The control: the cut wording must not appear when nothing was dropped."""
    mirror = tmp_path / "knowledge.md"
    monkeypatch.setattr(_ctx, "_mirror_path", lambda sid, part: mirror)
    _ctx._OUT = None
    _ctx._begin_part("knowledge", mirror)
    _ctx._emit("small", block="in-flight")
    _ctx._finish_part("knowledge", "sid")
    out = capsys.readouterr().out
    assert "CUT" not in out
    assert f"/{_ctx._PART_BUDGET} chars]_" in out


def test_an_unwritable_mirror_is_loud_not_silent(monkeypatch, capsys, tmp_path):
    """Fail-OPEN (a mirror must never break session start) but never fail-QUIET."""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    os.chmod(blocked, 0o500)  # readable, NOT writable — the file lands HERE
    monkeypatch.setattr(_ctx, "_mirror_path", lambda sid, part: blocked / f"{part}.md")
    _ctx._OUT = None
    try:
        _ctx._begin_part("knowledge", blocked / "knowledge.md")
        _ctx._emit("x" * 50, block="b")
        _ctx._finish_part("knowledge", "sid")
    finally:
        os.chmod(blocked, 0o755)
    assert "could not mirror" in capsys.readouterr().err


def test_all_mode_is_not_cut_by_one_entrys_budget(monkeypatch, capsys, tmp_path):
    """`--part all` is a manual/test run that emits every part and is never
    wired, so one hook entry's cap is the wrong bound for it."""
    ident = tmp_path / "identity"
    ident.mkdir()
    (ident / "SOUL.md").write_text("S" * 9_000)
    (ident / "STEERING.md").write_text("T" * 9_000)
    out = _direct_part(monkeypatch, capsys, ident, "all")
    assert "S" * 9_000 in out
    assert "T" * 9_000 in out
