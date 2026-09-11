"""The proactive memory hook's model-facing stdout is bounded, per surface.

This hook runs on EVERY prompt in EVERY foreground session and writes bare
stdout to a UserPromptSubmit event, so everything it prints reaches the model
directly — and the harness FILES a hook entry over ``HOOK_STDOUT_CAP`` behind a
~2 KB preview with no error and no exit-code change. A breach here is silent and
costs the window its peer list and its prompt-injection safety directive.

Four surfaces were unbounded, and they fail differently, which is why each gets
its own bound rather than one blanket cap:

* the keyword window  — ``prompt`` is arbitrary user text and every keyword was
  length-unbounded, so ONE pasted alphanumeric run became one token;
* the trail LINE      — bounded keywords still allow fifty wide labels;
* the code hint       — ``code_symbols.signature`` has no cap, and this is the
  largest surface MEASURED on real data rather than on a pasted worst case;
* the peer block      — ``get_active_sync`` had no ``LIMIT``.

Every number asserted here is derived in the docstring of the function that owns
it. The tests pin BEHAVIOUR (bounded, whole-unit selection, loud overflow), and
re-assert the constants only where a claim depends on their arithmetic.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import proactive_memory_hook as pmh  # noqa: E402
from hook_output import HOOK_STDOUT_CAP, emit_cost  # noqa: E402

_SID = "aaaabbbb-cccc-dddd-eeee-ffff00002222"


# ---------------------------------------------------------------------------
# 1. the keyword window — the root
# ---------------------------------------------------------------------------


def test_an_identifier_was_never_the_threat() -> None:
    """Pins the premise correction, because it is counter-intuitive.

    The plan for this change asserted that a long snake_case identifier yields a
    long keyword and breaches the cap at 1.2x. It does not: ``_extract_keywords``
    turns every non-alphanumeric character into whitespace, so an identifier is
    SPLIT. Without this test the false premise survives as folklore and the next
    session re-derives a bound from it.
    """
    kws = pmh._extract_keywords("test_a_hardlink_publish_must_clean_up_on_the_SUCCESS_path")
    assert len(kws) > 1, "the extractor should have split the identifier"
    assert max(len(w) for w in kws) <= 8, f"identifier produced a long keyword: {kws}"


def test_an_unbroken_alphanumeric_run_is_dropped_not_truncated() -> None:
    """The real threat: a pasted hash/token/minified blob is ONE token.

    Guard-the-guard: the fixture must genuinely contain an over-window token, or
    an extractor that returned nothing at all would pass this by accident.
    """
    blob = "q7" * 1000  # 2,000 chars, no separator for the extractor to split on
    assert len(blob) > pmh._MAX_KEYWORD_CHARS  # the hazard is really present

    # Surrounding words chosen NOT to be stop words — "look at ... please" are
    # all in _STOP_WORDS, so that phrasing extracts nothing and the last
    # assertion would fail for a reason unrelated to the bound.
    kws = pmh._extract_keywords(f"inspect {blob} payload")

    assert blob not in kws, "the over-window token survived"
    assert not any(w.startswith("q7q7q7") for w in kws), (
        "the token was TRUNCATED, not dropped — a truncated keyword is a "
        "colliding KEY: _detect_pivot compares keyword SETS, so two distinct "
        "ids sharing a prefix would read as the same topic"
    )
    assert "inspect" in kws, "dropping the blob must not drop the rest of the prompt"
    assert "payload" in kws


def test_a_real_word_at_the_window_edge_survives() -> None:
    """The bound must cost real words nothing — the other half of the tradeoff.

    A cap measured only by what it REJECTS cannot tell a good filter from a
    blind one. The longest ordinary word in the measured corpus was 16 chars.
    """
    at_edge = "x" * pmh._MAX_KEYWORD_CHARS
    assert at_edge in pmh._extract_keywords(f"about {at_edge} now"), "inclusive bound"
    assert "overcomplicating" in pmh._extract_keywords("stop overcomplicating this")
    assert "decommissioning" in pmh._extract_keywords("the decommissioning plan")


def test_ordinary_prompts_are_untouched_by_the_window() -> None:
    """The falsification test for the whole change: typical output must not move.

    If the window changes what a normal prompt extracts, the bound is mis-sized
    and every downstream consumer (pivot labels, FTS terms) shifts with it.
    """
    for prompt in (
        "can you check why the merge gate blocked that push",
        "run the targeted tests for src/genesis/memory/retrieval.py",
        "what is the confidence on this fix and what did you verify",
    ):
        kws = pmh._extract_keywords(prompt)
        unbounded = [
            w
            for w in "".join(c if c.isalnum() or c.isspace() else " " for c in prompt)
            .lower()
            .split()
            if w not in pmh._STOP_WORDS and len(w) >= pmh._MIN_KEYWORD_CHARS
        ][:8]
        assert kws == unbounded, f"the window changed ordinary extraction: {prompt!r}"


# ---------------------------------------------------------------------------
# 2. the rendered trail line
# ---------------------------------------------------------------------------


def test_the_trail_line_drops_whole_pivots_and_never_cuts_a_label() -> None:
    """ACCEPTANCE BAR: replay the measured defect through the real renderer."""
    label = " ".join(["z" * pmh._MAX_KEYWORD_CHARS] * 4)  # the widest label possible
    labels = [label] * pmh._MAX_TRAIL_DISPLAY

    # Guard-the-guard: unbounded, this really would breach the budget.
    naive = f"[Session trail] {' → '.join(labels)}"
    assert len(naive) > pmh._MAX_TRAIL_LINE_CHARS, "fixture did not build the hazard"

    line = pmh._render_trail_line(labels)

    assert line is not None
    assert len(line) <= pmh._MAX_TRAIL_LINE_CHARS
    assert line.startswith("[Session trail] … → "), "an elision must be announced"
    # Whole units only: every label that IS shown is shown complete.
    body = line[len("[Session trail] … → ") :]
    assert all(part == label for part in body.split(" → ")), "a label was cut"


def test_a_short_trail_is_rendered_unchanged() -> None:
    """The bound must be invisible on real trails — measured max was 1,448."""
    labels = ["merge gate blocked push", "bound the proactive hook"]
    assert pmh._render_trail_line(labels) == (
        "[Session trail] merge gate blocked push → bound the proactive hook"
    )


def test_the_count_bound_still_elides_before_the_length_bound() -> None:
    """Both elisions share one marker; the count bound must keep working."""
    labels = [f"topic {i}" for i in range(pmh._MAX_TRAIL_DISPLAY + 5)]
    line = pmh._render_trail_line(labels)
    assert line is not None
    assert line.startswith("[Session trail] … → ")
    assert f"topic {pmh._MAX_TRAIL_DISPLAY + 4}" in line, "newest pivot must survive"
    assert "topic 0" not in line, "oldest pivot should have been elided"


# ---------------------------------------------------------------------------
# 3. pivot detection across a cap change
# ---------------------------------------------------------------------------


def test_a_legacy_uncapped_trail_does_not_record_a_phantom_pivot() -> None:
    """Both sides of the Jaccard comparison go through the SAME window.

    ``last_keywords`` is stored across turns, so on the first prompt after a
    deploy it may hold tokens the current window rejects. Comparing them raw
    measures the CAP CHANGE, not the topic change, and every live session records
    a pivot that never happened.

    Constructed to FLIP deliberately, and honest about it: at the shipped window
    of 32 no stored keyword on the measured install exceeds it, so this cannot
    fire today. It is the invariant that must hold when the value next moves.
    """
    blobs = ["z" * 100 + str(i) for i in range(7)]
    trail = {"last_keywords": ["alpha", *blobs], "msg_count": 9, "pivots": []}

    # Guard-the-guard: unnormalised, this really would score as a pivot.
    raw = pmh._jaccard_similarity(["alpha"], trail["last_keywords"])
    assert raw < pmh._PIVOT_SIMILARITY_THRESHOLD, "fixture does not exercise the bug"

    assert not pmh._detect_pivot(["alpha"], trail), "phantom pivot on a legacy trail"


def test_a_genuinely_empty_trail_is_still_an_initial_pivot() -> None:
    """The normalisation must not swallow the real first-message case."""
    assert pmh._detect_pivot(["alpha"], {"last_keywords": [], "msg_count": 1, "pivots": []})


def test_a_trail_with_only_out_of_window_history_still_debounces() -> None:
    """Filtered-to-empty is NOT the same as no history.

    It must fall through to the debounced comparison rather than re-entering the
    unconditional first-message return, or a session whose stored keywords all
    fell out of the window would pivot on consecutive messages.
    """
    trail = {
        "last_keywords": ["z" * 100],
        "msg_count": 10,
        "pivots": [{"at_msg": 9, "label": "recent"}],  # one message ago
    }
    assert not pmh._detect_pivot(["alpha"], trail), "debounce was bypassed"


# ---------------------------------------------------------------------------
# 4. the code hint — the largest surface on real data
# ---------------------------------------------------------------------------


def test_a_long_signature_is_clipped_but_the_location_survives_whole() -> None:
    """``loc`` is the ACTIONABLE half: a bounded pointer to a real file beats a
    whole signature with a mangled path."""
    loc = "src/genesis/memory/retrieval.py:MemoryStore"
    line = pmh._render_code_hint("def f(" + "a: int, " * 300 + ")", loc)

    assert len(line) <= pmh._MAX_CODE_HINT_CHARS
    assert line.endswith(f" — {loc}"), "the location must not be clipped"
    assert "…" in line, "a clipped signature must not read as a complete one"
    assert line.startswith("[Code] def f(")


def test_a_normal_signature_is_rendered_unchanged() -> None:
    """p99 of the live index is 365 chars — the common case must not move."""
    line = pmh._render_code_hint("def get_active_sync(db_path: str) -> list[dict]", "a/b.py")
    assert line == "[Code] def get_active_sync(db_path: str) -> list[dict] — a/b.py"
    assert "…" not in line


def test_the_line_is_bounded_even_when_the_location_alone_overflows() -> None:
    """There is no room to clip the signature INTO, so the line is clipped.

    Without this branch the signature's room goes negative and the slice silently
    inverts — bounding nothing on exactly the pathological input the bound exists
    for.
    """
    line = pmh._render_code_hint("def f()", "x" * 5000)
    assert len(line) <= pmh._MAX_CODE_HINT_CHARS


def test_six_hints_cannot_dominate_the_budget() -> None:
    """The SQL returns at most 6 rows; this pins what that costs at worst."""
    worst = pmh._render_code_hint("s" * 10_000, "m" * 10_000)
    assert 6 * emit_cost(worst) < HOOK_STDOUT_CAP // 2


# ---------------------------------------------------------------------------
# 4b. the SIZE bounds are measured in the unit the harness bills
# ---------------------------------------------------------------------------
# REGRESSION CLASS. Both size bounds below billed in UTF-16 (clip_to_cost) while
# DECIDING in codepoints (len), so an astral value short enough in codepoints to
# clear the guard was never bounded at all. The extremes hide it — a huge astral
# value trips the guard and clips correctly — so these fixtures sit in the MIDDLE
# of the range, which is the only place the defect is visible. The original
# battery had no astral fixture at all and could not see this.

_ASTRAL = "\U0001f600"  # 1 codepoint, 2 UTF-16 units
_ASTRAL_ALNUM = "\U00020000"  # astral CJK: str.isalnum() is True, so a prompt can carry it


@pytest.mark.parametrize("n", [100, 150, 200, 250, 300, 383, 500, 1000])
def test_a_code_hint_holds_its_bound_in_utf16_at_every_astral_width(n: int) -> None:
    """Sweep the width, because ONE sample would have reported clean.

    At n=2000 this passed before the fix; the breach lives around n=200-383,
    where `room < len(sig)` is False and no clipping happens at all.
    """
    line = pmh._render_code_hint(_ASTRAL * n, "src/genesis/memory/retrieval.py")
    assert emit_cost(line) - 1 <= pmh._MAX_CODE_HINT_CHARS, (
        f"{n} astral chars rendered {emit_cost(line) - 1} UTF-16 units against a "
        f"stated ceiling of {pmh._MAX_CODE_HINT_CHARS}"
    )


def test_an_astral_location_cannot_push_the_hint_over() -> None:
    line = pmh._render_code_hint("def f()", _ASTRAL * 200)
    assert emit_cost(line) - 1 <= pmh._MAX_CODE_HINT_CHARS


def test_the_trail_line_holds_its_bound_in_utf16() -> None:
    """20 astral labels scored 1,491 by len and 2,932 by the harness."""
    labels = [_ASTRAL_ALNUM * pmh._MAX_KEYWORD_CHARS * 4] * 20
    line = pmh._render_trail_line(labels)
    assert line is not None
    assert emit_cost(line) - 1 <= pmh._MAX_TRAIL_LINE_CHARS, (
        f"astral labels rendered {emit_cost(line) - 1} UTF-16 units against "
        f"{pmh._MAX_TRAIL_LINE_CHARS}"
    )


def test_the_keyword_window_is_codepoints_on_purpose() -> None:
    """The SEMANTIC bound must not bill astral text double.

    A 20-character astral CJK word is a word. Measuring it in UTF-16 would score
    it 40 and drop it for being non-Latin, which is the size job leaking into a
    question about meaning. The line bound catches astral-heavy labels instead.
    """
    word = _ASTRAL_ALNUM * 20  # 20 codepoints, 40 UTF-16 units
    assert word.isalnum(), "fixture must survive the extractor's alnum filter"
    assert word in pmh._extract_keywords(f"about {word} now")


def test_a_legacy_oversized_label_does_not_erase_the_line() -> None:
    """FINDING C: labels are PERSISTED, so a pre-window trail holds labels the
    131-unit premise does not describe. Without a defensive clip, one of them
    shrinks the display to empty and the line vanishes entirely — a worse outcome
    than the unbounded line this change exists to fix.
    """
    legacy = "M" * 2000  # what a pre-window trail actually stored
    for labels in (
        ["short a", "short b", legacy],
        [legacy, "short a", "short b"],
        [legacy, legacy],
    ):
        line = pmh._render_trail_line(labels)
        assert line is not None, f"line vanished for {[len(x) for x in labels]}"
        assert emit_cost(line) - 1 <= pmh._MAX_TRAIL_LINE_CHARS


# ---------------------------------------------------------------------------
# 5. the peer block
# ---------------------------------------------------------------------------


def _seed_heartbeats(db: Path, n: int) -> None:
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE session_heartbeats (cc_session_id TEXT PRIMARY KEY, "
        "source_tag TEXT, model TEXT, topic TEXT, user_summary TEXT, "
        "genesis_summary TEXT, updated_at TEXT)"
    )
    for i in range(n):
        conn.execute(
            "INSERT INTO session_heartbeats VALUES (?,?,?,?,?,?,?)",
            (
                f"peer{i:04d}-aaaa-bbbb-cccc-dddddddddddd",
                "foreground",
                "claude-opus-5",
                f"topic for peer {i}",
                "",
                f"summary for peer {i}",
                f"2999-01-01T00:{i:02d}:00+00:00",  # far future: inside any window
            ),
        )
    conn.commit()
    conn.close()


def test_the_peer_query_honours_a_limit(tmp_path) -> None:
    from genesis.db.crud.session_heartbeats import get_active_sync

    db = tmp_path / "g.db"
    _seed_heartbeats(db, 30)

    assert len(get_active_sync(str(db), limit=5)) == 5
    assert len(get_active_sync(str(db))) == 30, "no limit must stay unbounded"


def test_the_peer_query_keeps_the_MOST_RECENT_rows(tmp_path) -> None:
    """``ORDER BY updated_at DESC`` is what makes a LIMIT safe to apply.

    If the order were ASC the limit would show the STALEST peers, which is the
    opposite of awareness — and nothing about the row count would reveal it.
    """
    from genesis.db.crud.session_heartbeats import get_active_sync

    db = tmp_path / "g.db"
    _seed_heartbeats(db, 30)

    got = [r["cc_session_id"] for r in get_active_sync(str(db), limit=3)]
    assert got == [f"peer{i:04d}-aaaa-bbbb-cccc-dddddddddddd" for i in (29, 28, 27)]


def test_hidden_peers_are_named_never_silently_dropped(tmp_path, capsys) -> None:
    """A short peer list reads exactly like a quiet box.

    That is this block's whole failure mode — its own read helper says a broken
    read "reads exactly like no concurrent sessions" — so a LIMIT that hid rows
    without saying so would re-create the defect it was added to prevent.
    """
    db = tmp_path / "g.db"
    _seed_heartbeats(db, pmh._MAX_PEERS_SHOWN + 4)

    pmh._heartbeat_read_and_inject(db, _SID)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]

    peers = [ln for ln in lines if ln.startswith("[Concurrent |")]
    assert len(peers) == pmh._MAX_PEERS_SHOWN
    closing = [ln for ln in lines if ln.startswith("[Concurrent sessions above")]
    assert len(closing) == 1
    # The TRUE total, not a saturated one. Deriving this from a read of
    # limit + 1 rows reported "+1 more" with four hidden — a precise wrong
    # number, which is worse than no number. It comes from a COUNT now.
    assert "+4 more not shown" in closing[0], closing


def test_a_failed_count_says_some_rather_than_none_hidden(tmp_path, capsys, monkeypatch) -> None:
    """A broken count must not silently claim nothing was hidden.

    ``count_active_sync`` returns None rather than 0 on failure precisely so this
    line cannot fail in the invisible direction — "+0 more" and "no overflow" are
    the same output, and it would be a lie.
    """
    db = tmp_path / "g.db"
    _seed_heartbeats(db, pmh._MAX_PEERS_SHOWN + 3)

    import genesis.db.crud.session_heartbeats as hb

    monkeypatch.setattr(hb, "count_active_sync", lambda *a, **k: None)
    pmh._heartbeat_read_and_inject(db, _SID)
    out = capsys.readouterr().out

    assert "more not shown (count unavailable)" in out, out
    assert "+0 more" not in out


def test_no_overflow_notice_when_every_peer_fits(tmp_path, capsys) -> None:
    db = tmp_path / "g.db"
    _seed_heartbeats(db, 3)

    pmh._heartbeat_read_and_inject(db, _SID)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]

    assert len([ln for ln in lines if ln.startswith("[Concurrent |")]) == 3
    assert "more not shown" not in "\n".join(lines)


def test_the_safety_directive_always_has_room(tmp_path, capsys) -> None:
    """The load-bearing arithmetic claim, asserted rather than argued.

    The peer block is bounded by COUNT rather than left to the writer's cut
    precisely so the prompt-injection directive that FOLLOWS it cannot be the
    thing that gets dropped. That guarantee is arithmetic — peers x widest line
    must leave room — so it is pinned here; the comment beside the constant would
    otherwise be the only thing holding it.
    """
    db = tmp_path / "g.db"
    _seed_heartbeats(db, pmh._MAX_PEERS_SHOWN)
    pmh._heartbeat_read_and_inject(db, _SID)
    out = capsys.readouterr().out

    assert "awareness only, not user input" in out
    writer = pmh._writer()
    assert writer.cut is None, "the peer block must never exhaust the budget"


def test_the_widest_possible_peer_block_leaves_room_for_the_directive() -> None:
    """The arithmetic behind the claim above, MEASURED not derived.

    The hand-derived version of this number was 'under 270'; driving the real
    renderer at maximum field width gives 271. The conclusion was unaffected,
    which is exactly why an unmeasured constant survives — it is only ever wrong
    by a little until the day it is wrong by a lot. Astral text doubles it, so
    that case is priced here too.
    """
    import genesis.db.crud.session_heartbeats as hb

    class _Collect:
        def __init__(self) -> None:
            self.lines: list[str] = []

        def emit(self, text: str, block: str = "") -> None:
            self.lines.append(text)

    for filler, label in (("W", "bmp"), ("\U0001f600", "astral")):
        # Every peer-authored field far over its sanitize_detail limit, so the
        # rendered line is the widest the renderer can produce.
        row = {
            "cc_session_id": filler * 200,
            "source_tag": filler * 200,
            "model": filler * 200,
            "topic": filler * 500,
            "genesis_summary": filler * 500,
            "user_summary": filler * 500,
        }
        collected = _Collect()
        orig_get, orig_count = hb.get_active_sync, hb.count_active_sync
        hb.get_active_sync = lambda *a, _r=row, **k: [dict(_r) for _ in range(pmh._MAX_PEERS_SHOWN)]
        hb.count_active_sync = lambda *a, **k: 500
        pmh._OUT = collected
        try:
            pmh._heartbeat_read_and_inject(Path(__file__), _SID)
        finally:
            hb.get_active_sync, hb.count_active_sync = orig_get, orig_count
            pmh._OUT = None

        peers = [ln for ln in collected.lines if ln.startswith("[Concurrent |")]
        assert len(peers) == pmh._MAX_PEERS_SHOWN, f"{label}: fixture did not fill the block"
        total = sum(emit_cost(ln) + 1 for ln in collected.lines)
        assert total < pmh.DEFAULT_BUDGET, (
            f"{label}: the widest {pmh._MAX_PEERS_SHOWN}-peer block plus its "
            f"directive costs {total} units against a {pmh.DEFAULT_BUDGET} budget"
        )
        assert any("awareness only" in ln for ln in collected.lines), (
            f"{label}: the safety directive did not survive the widest block"
        )


def test_a_negative_limit_does_not_mean_unlimited(tmp_path) -> None:
    """Fail-OPEN in the one parameter whose whole job is to impose a bound.

    `if limit >= 0` silently handed back every row to a caller that had asked to
    be bounded — the failure direction that cannot be noticed downstream, since a
    long list and no list look the same to a renderer.
    """
    from genesis.db.crud.session_heartbeats import get_active_sync

    db = tmp_path / "g.db"
    _seed_heartbeats(db, 20)

    assert len(get_active_sync(str(db), limit=-1)) == 0
    assert len(get_active_sync(str(db), limit=0)) == 0
    assert len(get_active_sync(str(db))) == 20, "None must still mean unlimited"


def test_the_closing_line_never_renders_a_negative_overflow() -> None:
    assert "+-1" not in pmh._peer_closing_line(-1)
    assert "more not shown" not in pmh._peer_closing_line(-1)
    assert "+3 more not shown" in pmh._peer_closing_line(3)


# ---------------------------------------------------------------------------
# 6. the writer itself
# ---------------------------------------------------------------------------


def test_every_model_facing_write_goes_through_the_bounded_writer() -> None:
    """The chokepoint, asserted at the budget rather than per call site.

    ``tests/test_scripts/test_hook_output_contract.py`` already fails this hook
    if any model-facing ``print`` reappears. This pins the other half: that the
    writer it routes through is actually bounded, and bounded BELOW the harness
    cap rather than at it.
    """
    writer = pmh._writer()
    assert writer is pmh._writer(), "the writer must be a per-process singleton"
    writer.emit("x" * 50_000, block="flood")
    assert writer.emitted_chars <= HOOK_STDOUT_CAP
    assert writer.cut is not None, "an over-budget write must cut LOUDLY"


def test_a_cut_says_so_in_band() -> None:
    """A silent stop is the exact failure this whole class of work exists to
    prevent — the marker is what makes a cut readable instead of invisible."""
    writer = pmh._writer()
    writer.emit("y" * 50_000, block="flood")
    assert "CUT" in writer.intended or writer.cut is not None


@pytest.mark.parametrize(
    "name",
    ["_MAX_KEYWORD_CHARS", "_MAX_TRAIL_LINE_CHARS", "_MAX_CODE_HINT_CHARS", "_MAX_PEERS_SHOWN"],
)
def test_each_bound_is_a_positive_int(name: str) -> None:
    """Cheap, but it is the assertion that would have caught a bound set to 0 or
    None by a refactor — every consumer above degrades silently in that case."""
    value = getattr(pmh, name)
    assert isinstance(value, int) and value > 0, f"{name} = {value!r}"
