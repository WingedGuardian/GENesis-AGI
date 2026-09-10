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

import pytest

from tests.conftest import require_access_denied

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
    # `utf16_len`, not `len`: this ceiling's whole job is to gate a tracked file
    # against a part budget the writer enforces in UTF-16 code units. Measured in
    # codepoints it is PERMISSIVE — a 6,500-codepoint astral file costs up to
    # 13,000 units, passes here, and blows the part. A guard that measures in a
    # different unit from the thing it guards has stopped guarding it.
    size = _ctx.utf16_len((_IDENTITY_DIR / "CONVERSATION.md").read_text())
    assert size <= 6_500, (
        f"CONVERSATION.md is {size} chars (ceiling 6,500). It shares a 9,800-char "
        "hook budget with the install-local USER.md. Protocol DETAIL belongs in "
        "docs/reference/conversation-protocols.md — add a pointer, not a section."
    )


def test_identity_core_ceiling():
    # Same unit as the budget it guards — see test_conversation_md_ceiling.
    size = sum(_ctx.utf16_len((_IDENTITY_DIR / n).read_text()) for n in ("SOUL.md", "STEERING.md"))
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
        # endswith, not a prefix `in`: a 16-char prefix check cannot see a line
        # that was truncated after character 17, which is exactly how the
        # undersized audit reserve stayed invisible.
        assert out.rstrip("\n").splitlines()[-1].endswith("]_"), out[-120:]


def test_each_part_emits_its_audit_line(tmp_path):
    for part in ("charter", "identity-core", "identity-user", "knowledge"):
        out = _run_part(part, tmp_path / part)
        audit = out.rstrip("\n").splitlines()[-1]
        assert audit.startswith(f"_[ctx {part}:"), f"{part} missing its self-audit line"
        assert audit.endswith("]_"), f"{part} audit line was truncated: {audit!r}"


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
    """`all` is reachable ONLY by explicit request — never as a fallback.

    Run WITHOUT a session id: that is the manual/test shape. A live session
    payload plus `all` is now a mis-wire (see
    test_all_with_a_live_session_payload_degrades_to_charter_and_screams) —
    a hook invocation always carries a session_id, a person at a terminal
    need not.
    """
    r = _run_raw(tmp_path, "--part", "all", sid="")
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
    # …nor spawn the repo-pulse worker. `main()` starts it for real on the
    # charter path, so `--part all` forked a background subprocess against the
    # live repo on every run of this file — a side effect a budget test has no
    # business having, and one that only shows up as flakiness under load.
    monkeypatch.setattr(_ctx, "_spawn_repo_pulse_worker", lambda *a, **k: None)
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
    # The writer's contract is `<= budget`, so assert exactly that. The old
    # `+ 200` equalled HOOK_STDOUT_CAP, i.e. this assertion could not fail for
    # ANY overrun below the harness cliff — vacuous in precisely the range these
    # two pressure tests exist to cover.
    assert _ctx.utf16_len(out) <= _ctx._PART_BUDGET


def test_conversation_sized_to_the_real_room_is_not_elided(monkeypatch, capsys, tmp_path):
    """The audit-line headroom is reserved ONCE, by the writer.

    ``_begin_part`` constructs the writer with ``reserve=_AUDIT_LINE_RESERVE``,
    so ``room`` already excludes it. The call site subtracted it a SECOND time,
    which made a 120-character band just under the ceiling read as "does not
    fit": CONVERSATION.md was replaced by a pointer, or the file before it
    truncated, for output that would have arrived whole. Degrading is the right
    move under real pressure and a silent loss without it.

    Calibrated at runtime rather than hard-coded, so the test tracks the budget
    constants instead of pinning a number that drifts the next time one moves.
    """
    ident = tmp_path / "identity"
    ident.mkdir()
    (ident / "USER.md").write_text("U" * 200)

    # Pass 1: measure what one character of CONVERSATION.md costs in situ.
    (ident / "CONVERSATION.md").write_text("C")
    baseline = len(_direct_part(monkeypatch, capsys, ident, "identity-user"))
    free = _ctx._PART_BUDGET - baseline
    assert free > _ctx._AUDIT_LINE_RESERVE, "fixture too large to probe the band"

    # Pass 2: land inside the true room but within the audit reserve of it —
    # exactly the band the double-count wrongly excluded.
    #
    # `free` is measured against the BUDGET, while `fits` measures against the
    # ceiling (budget minus the reserve), and the baseline already spent the
    # audit line out of that reserve. So `free - _AUDIT_LINE_RESERVE` sits a
    # little under the true room — inside it by the audit line's own length,
    # and more than a reserve below where the double-counted check would allow.
    body = "C" * (free - _ctx._AUDIT_LINE_RESERVE)
    (ident / "CONVERSATION.md").write_text(body)
    out = _direct_part(monkeypatch, capsys, ident, "identity-user")
    assert body in out, "CONVERSATION.md was degraded though it fitted"
    assert "omitted for size" not in out
    assert len(out) <= _ctx._PART_BUDGET


def test_oversized_user_md_truncates_loudly_never_silently(monkeypatch, capsys, tmp_path):
    ident = tmp_path / "identity"
    ident.mkdir()
    (ident / "USER.md").write_text("U" * 15_000)
    out = _direct_part(monkeypatch, capsys, ident, "identity-user")
    assert "truncated at" in out
    # See the sibling elide test: `+ 200` was HOOK_STDOUT_CAP, so this could not
    # fail below the cliff. The writer guarantees `<= budget`.
    assert _ctx.utf16_len(out) <= _ctx._PART_BUDGET


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
        _ctx.emit_cost(_ctx._session_config_block("high", m, ""))
        for m in ("claude-opus-5[1m]", "claude-zeta-9-preview-20261231[1m]", "")
    )
    # MEASURED from the shipped string, never hand-copied: this test's whole job
    # is to fail when the part grows, and a copied length lets an ordinary prose
    # edit to that block overrun the budget with this test still green.
    onboarding = _ctx.emit_cost(_ctx._ONBOARDING_BLOCK)
    # A PRODUCTION-shaped mirror path: the real one carries a 36-char session
    # UUID under the user's home, and the header interpolates it. A short
    # fixture path understated this by ~25 chars against a margin measured in
    # the low hundreds.
    # Every term below is billed with `emit_cost` — the writer's own cost
    # function — not `len`. Two reasons, both measured: the unit must match the
    # budget (UTF-16 code units), and each block pays for the newline `print`
    # appends, which a bare `len` omits. Counting with `len` and charging the
    # newline for only the dividers reported 5 units MORE headroom than exists,
    # on a margin of 48.
    header = _ctx.emit_cost(
        _ctx._recovery_header(
            "charter",
            Path.home() / ".genesis" / "sessions" / str(uuid.uuid4()) / "context-charter.md",
        )
    )
    # The mis-wire alert fires ON this part, so the worst case includes it —
    # measured from the real function, never an estimate that can drift.
    miswire = _ctx.emit_cost(
        _ctx._miswire_alert("no --part argument (settings.json wiring is out of date)")
    )
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

    PRODUCTION SHAPE, not a convenient one. The first version passed
    `footer="_footer_"` (8 chars) and `session_id="sid"` (3) while production
    interpolates a 36-char UUID into BOTH the note and the footer, and the
    worst case — tier 4 is only reached with a full ledger, which is also when
    the overflow note is set — makes the footer ~191. That understated the
    floor by 245 chars and left the real margin at 81, not the 326 the source
    comment claimed. The sibling test 40 lines up already carried this exact
    lesson in its own comment ("a short fixture path understated this by ~25
    chars"); it was not carried across.
    """
    sid = str(uuid.uuid4())
    rows = [{"id": f"{i:032x}", "text": "t" * 300, "status": "open"} for i in range(200)]
    assert len(rows) == _ctx._LEDGER_FETCH_MAX
    footer = (
        f"_Compactions: 99 · ledger: {_ctx._LEDGER_FETCH_MAX} open / 999 closed"
        f" · {_ctx._LEDGER_OVERFLOW_NOTE}"
        f" · full charter: ~/.genesis/sessions/{sid}/charter.md_"
    )
    floor = len(
        _ctx._shrink_charter_block(
            "## Session Charter (persists across compaction)",
            "**Ledger (open) — close via session_ledger_update:**",
            rows,
            {},
            footer,
            sid,
        )
    )
    assert floor <= _ctx._CHARTER_BLOCK_MAX, (
        f"tier-4 degrade needs {floor} chars but the ceiling is "
        f"{_ctx._CHARTER_BLOCK_MAX} — a full ledger would lose row ids to the cut."
    )
    # The margin is the number a future editor needs; assert it is real, and
    # report it, so "there is room" is never inferred from a bare pass.
    margin = _ctx._CHARTER_BLOCK_MAX - floor
    assert margin >= 50, f"only {margin} chars of headroom above the tier-4 floor ({floor})"


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
    # Asserted on the LAST line, not anywhere in the output. `str(mirror)` is
    # also satisfied by the recovery header that `_begin_part` emits first, and
    # "CUT" by the cut marker — so the previous `in out` form passed while the
    # audit line itself was truncated. It named the audit line and pinned none
    # of it.
    audit = out.rstrip("\n").splitlines()[-1]
    assert audit.startswith("_[ctx knowledge:"), audit
    assert audit.endswith("]_"), audit
    assert "intended" in audit and "emitted" in audit and "CUT" in audit
    assert str(mirror) in audit


def test_unreadable_essential_knowledge_is_loud_like_an_identity_file(
    monkeypatch, capsys, tmp_path
):
    """The class is "a maintained file this window needed and could not read".

    This diff made an unreadable IDENTITY file emit a loud in-band alert and
    left essential knowledge on `pass  # advisory`. That asymmetry is not a
    considered exception — CLAUDE.md's own account of the incident names the
    loss as "identity, charter AND essential knowledge". `.exists()` has already
    passed by then, so reaching the handler means permissions or I/O, which is
    something an operator can act on, unlike absence.
    """
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    ek = home / ".genesis" / "essential_knowledge.md"
    ek.write_text("L1 CONTEXT")
    ek.chmod(0o000)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    try:
        require_access_denied(ek)
        ident = tmp_path / "identity"
        ident.mkdir()
        out = _direct_part(monkeypatch, capsys, ident, "knowledge")
        assert "GENESIS ALERT: essential knowledge could not be read" in out
        assert "L1 context is MISSING" in out
    finally:
        ek.chmod(0o644)


#: Every script that writes model-facing stdout through `BoundedStdout`. The
#: lock below covers ALL of them: it used to read one, while this same branch
#: created a second emitter — so the class was half-locked and read as locked.
_EMITTERS = ("genesis_session_context.py", "genesis_urgent_alerts.py")

#: Names that denote a BUDGET. Subtracting from one of these is a caller
#: computing "how much room is left" — the re-derivation the chokepoint deletes.
_BUDGET_NAMES = {
    "_PART_BUDGET",
    "HOOK_STDOUT_CAP",
    "_HOOK_STDOUT_CAP",
    "DEFAULT_BUDGET",
    "_TAG_MAX_BYTES",
}


def _budget_offenders(src: str) -> list[str]:
    """Budget DECISIONS taken outside the writer, in ``src``.

    Shared by the lock and by the lock's own positive control, so the control
    exercises the same detector the lock trusts.

    Two shapes, because banning names alone left the behaviour reachable:
    the writer's decision inputs (`.room`, `.fits`, `_fits`, `_tail`), and any
    subtraction FROM a budget constant, which is the same arithmetic spelled by
    hand. Deliberately NOT banned: reading `emitted_chars`/`intended_chars` to
    REPORT, multiplying a budget to CONSTRUCT a writer, and adding lengths to
    derive a reserve — reading what happened is not the defect; branching on
    what is left is.
    """
    import ast

    tree = ast.parse(src)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in {"room", "fits"}:
            offenders.append(f"line {node.lineno}: .{node.attr}")
        elif isinstance(node, ast.Name) and node.id in {"_fits", "_tail"}:
            offenders.append(f"line {node.lineno}: {node.id}")
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
            left = {n.id for n in ast.walk(node.left) if isinstance(n, ast.Name)}
            if left & _BUDGET_NAMES:
                offenders.append(f"line {node.lineno}: hand-derived room from a budget constant")
    return offenders


def test_no_budget_arithmetic_outside_the_writer():
    """THE LOCK for the budget chokepoint. A chokepoint without one is a convention.

    Five review findings in this module were a caller re-deriving what the
    writer already knows: the audit reserve counted twice, a `fits` that
    undercharged by one, a reserve of 120 for a 206-char line, a pointer
    reserved in a part that cannot emit it, a `keep` computed from the budget
    constant instead of the room. Fixing those five left the SHAPE that
    produced them — a caller allowed to do the arithmetic — completely intact.

    So the arithmetic is now unrepresentable here rather than merely correct:
    the emitter asks `emit_or_degrade` what happened and never asks how many
    characters are left. This test is what keeps it that way, because the next
    person to need "just one `fits` call" will otherwise reintroduce the class.

    SCOPE, stated rather than implied: this bans the DECISION inputs — how much
    room is left, and whether something fits. It deliberately allows
    `emitted_chars` / `intended_chars`, which the audit line REPORTS. Reading
    what happened is not the defect; branching on how much space remains is.
    """
    offenders: list[str] = []
    for name in _EMITTERS:
        offenders += [f"{name} {o}" for o in _budget_offenders((_SCRIPTS_DIR / name).read_text())]
    assert not offenders, (
        "budget arithmetic leaked back into an emitter: "
        + "; ".join(offenders)
        + ". Say what the degrade LOOKS like via emit_or_degrade(pointer=…, "
        "notice=…) and let the writer settle the numbers."
    )


def test_the_budget_lock_can_itself_fail():
    """The lock's POSITIVE CONTROL — it must flag each way back to the class.

    Banning two attribute NAMES is not banning the behaviour: a hand
    re-derivation like `_PART_BUDGET - _AUDIT_LINE_RESERVE - emitted` is exactly
    the defect the chokepoint exists to delete, and the name-only version of
    this lock stayed GREEN on it. Asserting the detector fires is what makes the
    green above mean something — otherwise "no offenders" and "no detector" are
    the same result.
    """
    evasions = {
        "room attribute": "def f(w):\n    return w.room > 10\n",
        "fits helper": "def f(t):\n    return _fits(t)\n",
        "hand re-derivation": "def f(emitted):\n    return _PART_BUDGET - _AUDIT_LINE_RESERVE - emitted\n",
        "cap re-derivation": "def f(used):\n    return HOOK_STDOUT_CAP - used\n",
    }
    for label, sample in evasions.items():
        assert _budget_offenders(sample), f"detector is blind to: {label}"

    # The other direction — these must NOT be flagged, or the lock is a nuisance
    # that gets deleted by whoever hits it next.
    allowed = {
        "reporting emitted": "def f(w):\n    return f'{w.emitted_chars}/{_PART_BUDGET}'\n",
        "constructing the writer": "def f():\n    return _PART_BUDGET * len(_PARTS)\n",
        "deriving a reserve": "X = len(_audit_line('p', 1, 1)) + _NEWLINE_COST\n",
    }
    for label, sample in allowed.items():
        assert not _budget_offenders(sample), f"false positive on: {label}"


def test_the_audit_reserve_fits_the_line_it_reserves_for(tmp_path):
    """The reserve is DERIVED from the renderer, and must stay ahead of it.

    Rebuilds the same worst case `_AUDIT_LINE_RESERVE` is computed from, so the
    constant and the line cannot drift. This is the pin the old round number
    lacked: 120 was chosen once and the line grew past it, and because
    `_cut_here` fills the ceiling by construction, `room` at `emit_final` time
    is ALWAYS exactly the reserve — so being short by any amount truncates
    deterministically rather than occasionally.
    """
    worst = _ctx._audit_line(
        "identity-user",
        99_999,
        99_999,
        cut=("x" * _ctx._AUDIT_BLOCK_LABEL_MAX, 99_999),
        where=(
            f" — full text: {Path.home()}/.genesis/sessions/{'0' * 36}/context-identity-user.md"
        ),
    )
    assert len(worst) + 1 <= _ctx._AUDIT_LINE_RESERVE, (
        f"reserve {_ctx._AUDIT_LINE_RESERVE} < worst-case audit line {len(worst)} + newline"
    )


def test_the_block_label_in_an_audit_line_is_bounded(tmp_path, monkeypatch, capsys):
    """A block label is a developer string, so its length is not bounded by
    anything — and it lands inside the line the reserve was measured for."""
    mirror = tmp_path / "k.md"
    monkeypatch.setattr(_ctx, "_PART_BUDGET", 900)
    monkeypatch.setattr(_ctx, "_mirror_path", lambda sid, part: mirror)
    _ctx._OUT = None
    _ctx._begin_part("knowledge", mirror)
    _ctx._emit("Z" * 3_000, block="b" * 500)
    _ctx._finish_part("knowledge", "sid")
    audit = capsys.readouterr().out.rstrip("\n").splitlines()[-1]
    assert audit.endswith("]_"), audit
    assert "b" * (_ctx._AUDIT_BLOCK_LABEL_MAX + 1) not in audit


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
    # Mode bits do not stop root or anything holding CAP_DAC_OVERRIDE, and CI
    # containers routinely run as root. There the write SUCCEEDS, no warning is
    # emitted, and the assertion below fails for a reason that has nothing to
    # do with the behaviour under test. Prove the premise before relying on it.
    try:
        probe = blocked / ".write-probe"
        probe.write_text("x")
        probe.unlink()
    except OSError:
        pass  # genuinely unwritable — the premise holds
    else:
        os.chmod(blocked, 0o755)
        pytest.skip("this process writes through mode bits (root / CAP_DAC_OVERRIDE)")
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


def test_a_long_session_id_does_not_clip_the_audit_line(tmp_path, monkeypatch, capsys):
    """The reserve must fit the audit line for EVERY id the validator accepts.

    `_AUDIT_LINE_RESERVE`'s worst case bakes in a 36-char session id, but
    `is_safe_session_id` accepts up to 255 — and the id lands INSIDE the line,
    via the mirror path. MEASURED: a 64-char id overruns the reserve by 13
    UTF-16 units, a 255-char id by 204. Because `_cut_here` fills to the ceiling
    by construction, `room` at `emit_final` time is exactly the reserve, so the
    overrun clips the line's TAIL — which is the mirror pointer, the one part
    the reader needs. The reserve is now derived from the REAL mirror path at
    `_begin_part`, floored at the constant so ordinary UUIDs change nothing.
    """
    sid = "s" * 255
    mirror = tmp_path / sid / "context-knowledge.md"
    monkeypatch.setattr(_ctx, "_mirror_path", lambda s, p: mirror)
    _ctx._OUT = None
    _ctx._begin_part("knowledge", mirror)
    _ctx._emit("Z" * 20_000, block="essential-knowledge")
    _ctx._finish_part("knowledge", sid)
    out = capsys.readouterr().out
    audit = out.rstrip("\n").splitlines()[-1]
    assert audit.endswith("]_"), f"audit line clipped: …{audit[-80:]!r}"
    assert str(mirror) in audit, "the mirror pointer is the part that must survive"
    assert _ctx.utf16_len(out) <= _ctx._PART_BUDGET


def test_all_with_a_live_session_payload_degrades_to_charter_and_screams(tmp_path):
    """`--part all` is a manual/test path — a REAL session must never get it.

    `all` grants ONE hook entry the four-part sum (39,200 units), so a wiring
    edit that passes `--part all` recreates the original whole-payload filing —
    and the old code accepted it silently, with `miswired` empty, so neither the
    in-band alert nor the miswire log said anything. The discriminator is the
    payload: a hook invocation always carries a session_id; the manual/test path
    does not have to. With a live id, `all` now degrades exactly like any other
    mis-wire: charter only, loud, in-band.
    """
    sid = "11111111-2222-3333-4444-555555555555"
    r = _run_raw(tmp_path, "--part", "all", sid=sid)
    assert "_[ctx charter:" in r.stdout
    for other in ("identity-core", "identity-user", "knowledge"):
        assert f"_[ctx {other}:" not in r.stdout, f"{other} leaked into a mis-wired all"
    assert len(r.stdout) <= _ctx._PART_BUDGET
    assert "MIS-WIRED" in r.stderr


def test_a_malformed_probe_value_does_not_silence_the_injection(tmp_path):
    """A typo in GENESIS_CTX_PROBE_BYTES must not erase the whole window.

    The env var is inherited by all four SessionStart entries, so the old
    `except ValueError: return` turned one stale exported value into four empty
    parts — charter, identity and knowledge all gone, with no audit line and no
    miswire record: the silent-loss class this whole branch exists to kill,
    reachable by a shell typo. A malformed value now warns on stderr and falls
    through to NORMAL injection; probe mode engages only on a valid integer.
    """
    (tmp_path / ".genesis").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".genesis" / "cc_context_enabled").touch()
    r = subprocess.run(
        [sys.executable, str(_SCRIPTS_DIR / "genesis_session_context.py"), "--part", "charter"],
        capture_output=True,
        text=True,
        env={
            "HOME": str(tmp_path),
            "PATH": "/usr/bin:/bin",
            "GENESIS_CTX_PROBE_BYTES": "10k",  # a human wrote this, not a probe
        },
        input="{}",
        timeout=60,
    )
    assert r.returncode == 0, r.stderr[-400:]
    assert "_[ctx charter:" in r.stdout, "a malformed probe value silenced the whole part"
    assert "PROBE" in r.stderr, "the malformed value must be reported, not ignored"
    assert "PROBE-START" not in r.stdout, "probe mode must not engage on garbage"
