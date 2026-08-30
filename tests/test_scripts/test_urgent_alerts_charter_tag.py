"""Tests for the per-turn ledger inventory tag in genesis_urgent_alerts.

The tag runs on EVERY prompt: the omission matrix (no row / no DB / no table /
locked DB) is the contract that keeps it free when it has nothing to say.

It is an INVENTORY, not a count. A count (`open: 6`) is indistinguishable from
six items already handled; a session ran seven days with its founding asks
open behind exactly that number. Every open row is named, with its id, so the
next turn can act on it or close it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"

_ua_spec = importlib.util.spec_from_file_location(
    "genesis_urgent_alerts", _SCRIPTS_DIR / "genesis_urgent_alerts.py"
)
_ua = importlib.util.module_from_spec(_ua_spec)
_ua_spec.loader.exec_module(_ua)

SID = "sid-tag-1"


def _make_db(tmp_path: Path, *, with_follow_ups: bool = True) -> Path:
    from genesis.db.schema._tables import TABLES

    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True, exist_ok=True)
    db_file = root / "data" / "genesis.db"
    conn = sqlite3.connect(db_file)
    conn.execute(TABLES["session_charters"])
    conn.execute(TABLES["session_ledger"])
    if with_follow_ups:
        conn.execute(TABLES["follow_ups"])
    conn.commit()
    conn.close()
    return root


def _seed(
    tmp_path: Path,
    *,
    mission: str | None = None,
    origin: str | None = "The origin prompt first line.\nSecond line.",
    open_items: int = 0,
    done_items: int = 0,
    compaction_count: int = 1,
    texts: list[str] | None = None,
) -> Path:
    root = _make_db(tmp_path)
    conn = sqlite3.connect(root / "data" / "genesis.db")
    conn.execute(
        "INSERT INTO session_charters (session_id, origin_prompt, mission,"
        " pointers, compaction_count, created_at)"
        " VALUES (?, ?, ?, '[]', ?, '2026-07-13T00:00:00+00:00')",
        (SID, origin, mission, compaction_count),
    )
    for i in range(open_items):
        text = texts[i] if texts and i < len(texts) else f"open item {i}"
        conn.execute(
            "INSERT INTO session_ledger (id, session_id, text, status, added_by, created_at)"
            " VALUES (?, ?, ?, ?, 'foreground', ?)",
            (
                f"{i:08x}" + "0" * 24,
                SID,
                text,
                "open" if i % 2 == 0 else "in_progress",
                f"2026-07-13T00:00:{i:02d}+00:00",
            ),
        )
    for i in range(done_items):
        conn.execute(
            "INSERT INTO session_ledger (id, session_id, text, status, added_by, created_at)"
            " VALUES (?, ?, 'y', 'done', 'foreground', '2026-07-13T00:00:00+00:00')",
            (f"d{i}", SID),
        )
    conn.commit()
    conn.close()
    return root


def _seed_escalation(root: Path, ledger_id: str, follow_up_id: str) -> None:
    key = hashlib.sha256(f"ledger_escalation|{ledger_id}".encode()).hexdigest()
    conn = sqlite3.connect(root / "data" / "genesis.db")
    conn.execute(
        "INSERT INTO follow_ups (id, content, source, strategy, status, priority,"
        " created_at, dedup_key, kind)"
        " VALUES (?, 'x', 'ledger_escalation', 'user_input_needed', 'pending', 'high',"
        " '2026-07-20T00:00:00+00:00', ?, 'follow_up')",
        (follow_up_id, key),
    )
    conn.commit()
    conn.close()


def _tag_output(monkeypatch, capsys, root: Path) -> str:
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(root))
    _ua._emit_charter_tag(SID)
    return capsys.readouterr().out


# ── the inventory ───────────────────────────────────────────────────────


def test_tag_lists_each_open_row_with_id8(monkeypatch, capsys, tmp_path):
    root = _seed(
        tmp_path,
        mission="Ship the ledger",
        open_items=3,
        done_items=2,
        texts=["reach into the sister machine", "infra discrepancy audit", "CBM migration plan"],
    )
    out = _tag_output(monkeypatch, capsys, root)
    lines = out.strip().splitlines()
    assert lines[0] == "[Ledger open: 3 | mission: Ship the ledger]"
    assert lines[1] == "- 00000000 reach into the sister machine"
    assert lines[2] == "- 00000001 [~] infra discrepancy audit"  # in_progress
    assert lines[3] == "- 00000002 CBM migration plan"
    assert len(lines) == 4


def test_tag_caps_rows_and_points_to_charter_md(monkeypatch, capsys, tmp_path):
    root = _seed(tmp_path, mission="m", open_items=_ua._TAG_MAX_ROWS + 3)
    out = _tag_output(monkeypatch, capsys, root)
    lines = out.strip().splitlines()
    assert lines[0].startswith(f"[Ledger open: {_ua._TAG_MAX_ROWS + 3} |")
    rows = [ln for ln in lines if ln.startswith("- ")]
    assert len(rows) == _ua._TAG_MAX_ROWS
    assert lines[-1] == f"…and 3 more — see ~/.genesis/sessions/{SID}/charter.md"


def test_tag_row_text_capped_but_not_to_noise(monkeypatch, capsys, tmp_path):
    root = _seed(tmp_path, mission="m", open_items=1, texts=["w" * 400])
    out = _tag_output(monkeypatch, capsys, root)
    row = [ln for ln in out.splitlines() if ln.startswith("- ")][0]
    assert "w" * _ua._TAG_ROW_CHARS + "…" in row
    assert "w" * (_ua._TAG_ROW_CHARS + 1) not in row


def test_tag_under_size_cap_on_a_busy_ledger(monkeypatch, capsys, tmp_path):
    root = _seed(tmp_path, mission="m" * 1000, open_items=40, texts=["z" * 1000] * 40)
    out = _tag_output(monkeypatch, capsys, root)
    assert len(out.encode("utf-8")) <= _ua._TAG_MAX_BYTES


def test_tag_shows_escalation_link(monkeypatch, capsys, tmp_path):
    root = _seed(tmp_path, mission="m", open_items=1, texts=["reach into the sister machine"])
    _seed_escalation(root, "00000000" + "0" * 24, "f" * 32)
    out = _tag_output(monkeypatch, capsys, root)
    assert "- 00000000 reach into the sister machine → escalated: follow_up ffffffff" in out


def test_tag_without_follow_ups_table_still_lists_rows(monkeypatch, capsys, tmp_path):
    root = _make_db(tmp_path, with_follow_ups=False)
    conn = sqlite3.connect(root / "data" / "genesis.db")
    conn.execute(
        "INSERT INTO session_charters (session_id, origin_prompt, mission, pointers,"
        " compaction_count, created_at) VALUES (?, 'o', 'm', '[]', 1, '2026-07-13T00:00:00+00:00')",
        (SID,),
    )
    conn.execute(
        "INSERT INTO session_ledger (id, session_id, text, status, added_by, created_at)"
        " VALUES ('00000000000000000000000000000000', ?, 'row', 'open', 'foreground',"
        " '2026-07-13T00:00:00+00:00')",
        (SID,),
    )
    conn.commit()
    conn.close()
    out = _tag_output(monkeypatch, capsys, root)
    assert "- 00000000 row" in out
    assert "escalated:" not in out


# ── mission on the head line ────────────────────────────────────────────


def test_tag_mission_unset_after_compaction_says_so(monkeypatch, capsys, tmp_path):
    root = _seed(tmp_path, open_items=1, compaction_count=4)
    out = _tag_output(monkeypatch, capsys, root)
    head = out.strip().splitlines()[0]
    assert head == "[Ledger open: 1 | mission: UNSET after 4 compactions — session_charter_update]"
    assert "origin:" not in out


def test_tag_mission_unset_before_first_compaction_uses_origin(monkeypatch, capsys, tmp_path):
    """Before the first compaction the origin is still in context, so it is the label."""
    root = _seed(tmp_path, open_items=1, compaction_count=0)
    out = _tag_output(monkeypatch, capsys, root)
    head = out.strip().splitlines()[0]
    assert 'origin: "The origin prompt first line."' in head
    assert "Second line" not in out
    assert "UNSET" not in out


def test_tag_open_zero_still_shown(monkeypatch, capsys, tmp_path):
    root = _seed(tmp_path, mission="m", done_items=4)
    out = _tag_output(monkeypatch, capsys, root)
    assert out.strip() == "[Ledger open: 0 | mission: m]"


def test_tag_long_mission_truncated_at_headline_width(monkeypatch, capsys, tmp_path):
    root = _seed(tmp_path, mission="m" * 400)
    out = _tag_output(monkeypatch, capsys, root)
    assert "m" * _ua._TAG_MISSION_CHARS + "…" in out
    assert "m" * (_ua._TAG_MISSION_CHARS + 1) not in out


# ── omission matrix (unchanged contract) ────────────────────────────────


def test_tag_omitted_no_row(monkeypatch, capsys, tmp_path):
    root = _make_db(tmp_path)
    assert _tag_output(monkeypatch, capsys, root) == ""


def test_tag_omitted_stub_row_without_origin_or_mission(monkeypatch, capsys, tmp_path):
    root = _seed(tmp_path, origin=None)
    assert _tag_output(monkeypatch, capsys, root) == ""


def test_tag_omitted_missing_db(monkeypatch, capsys, tmp_path):
    assert _tag_output(monkeypatch, capsys, tmp_path / "nowhere") == ""


def test_tag_omitted_missing_table(monkeypatch, capsys, tmp_path):
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    sqlite3.connect(root / "data" / "genesis.db").close()
    assert _tag_output(monkeypatch, capsys, root) == ""


def test_tag_omitted_locked_db(monkeypatch, capsys, tmp_path):
    root = _seed(tmp_path, mission="m")
    blocker = sqlite3.connect(root / "data" / "genesis.db", timeout=1)
    blocker.isolation_level = None
    blocker.execute("BEGIN EXCLUSIVE")  # blocks even read-only connections
    try:
        assert _tag_output(monkeypatch, capsys, root) == ""
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()


# ── the over-budget / mis-wire SCREAM (repeats every prompt until cleared) ──


def _mk_marker(root: Path, sid: str, part: str, **payload) -> Path:
    d = root / ".genesis" / "sessions" / sid
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"injection_over_budget_{part}.json"
    base = {"part": part, "session_id": sid, "chars": 12345, "budget": 9800, "ts": "2026-08-30T17:00"}
    base.update(payload)
    f.write_text(json.dumps(base))
    return f


@pytest.fixture
def _marker_home(monkeypatch, tmp_path):
    """Point the hook's session/legacy marker dirs at a tmp HOME."""
    monkeypatch.setattr(_ua, "_GENESIS_DIR", tmp_path / ".genesis")
    monkeypatch.setattr(_ua, "_LEGACY_MARKER_DIR", tmp_path / ".genesis" / "session_awareness")
    monkeypatch.setattr(_ua, "_session_dir", lambda sid: tmp_path / ".genesis" / "sessions" / sid)
    return tmp_path


def test_over_budget_marker_screams_every_prompt(capsys, _marker_home):
    _mk_marker(_marker_home, SID, "knowledge")
    _ua._emit_injection_over_budget_alert(SID)
    out = capsys.readouterr().out
    assert "OVER BUDGET" in out
    assert "knowledge (12345/9800 chars" in out
    # Second prompt: still screaming — the marker on disk decides, not memory.
    _ua._emit_injection_over_budget_alert(SID)
    assert "OVER BUDGET" in capsys.readouterr().out


def test_miswire_marker_screams_as_a_wiring_fault(capsys, _marker_home):
    """F1: a mis-wired hook is a DIFFERENT fault from an oversized payload and
    must not be reported as one — its remedy is the settings.json wiring."""
    _mk_marker(_marker_home, SID, "wiring", reason="no --part argument")
    _ua._emit_injection_over_budget_alert(SID)
    out = capsys.readouterr().out
    assert "MIS-WIRED" in out
    assert "no --part argument" in out
    assert "OVER BUDGET" not in out


def test_each_part_screams_independently(capsys, _marker_home):
    """F2: per-(session, part) files — one part's alarm cannot erase another's."""
    _mk_marker(_marker_home, SID, "knowledge", chars=11000)
    _mk_marker(_marker_home, SID, "identity-user", chars=10500)
    _ua._emit_injection_over_budget_alert(SID)
    out = capsys.readouterr().out
    assert "knowledge" in out and "identity-user" in out


def test_another_sessions_marker_is_not_this_sessions_alarm(capsys, _marker_home):
    """F2: the marker used to be global, so one session silenced/alarmed another."""
    _mk_marker(_marker_home, "some-other-session", "knowledge")
    _ua._emit_injection_over_budget_alert(SID)
    assert capsys.readouterr().out == ""


def test_no_marker_no_scream(capsys, _marker_home):
    _ua._emit_injection_over_budget_alert(SID)
    assert capsys.readouterr().out == ""


def test_corrupt_marker_still_screams_rather_than_going_quiet(capsys, _marker_home):
    d = _marker_home / ".genesis" / "sessions" / SID
    d.mkdir(parents=True, exist_ok=True)
    (d / "injection_over_budget_knowledge.json").write_text("not json")
    _ua._emit_injection_over_budget_alert(SID)
    assert "unreadable" in capsys.readouterr().out


# ── parity: the inlined dedup formula equals the package one (F6) ──────────


def test_hook_dedup_formula_matches_package_formula():
    """This hook INLINES the sha256 (it is stdlib-only by design). Without this
    the docstring's parity claim was false for this file — the only parity test
    bound the OTHER hook, so a formula change here would silently unlink rows."""
    from genesis.session_awareness.ledger_escalation_link import escalation_dedup_key

    ledger_id = "b" * 32
    assert _ua._escalation_dedup_key(ledger_id) == escalation_dedup_key(ledger_id)


# ── the overflow pointer survives the byte cap (F5) ────────────────────────


def test_pointer_survives_the_byte_trim(monkeypatch, capsys, tmp_path):
    """A first version popped lines off the end, eating the '…and N more'
    pointer FIRST — a truncated list that looked complete, i.e. exactly the
    count-instead-of-inventory defect this tag exists to fix.

    The cap only bites once rows carry escalation links (MEASURED: 12 open rows
    + 8 links -> 7 rows and NO pointer). A first version of this test omitted
    the links, stayed under the cap, never entered the trim loop, and passed
    against the broken code — caught by mutation, not by reading it.
    """
    root = _seed(tmp_path, mission="m", open_items=12, texts=["z" * 140] * 12)
    for i in range(_ua._TAG_MAX_ROWS):
        _seed_escalation(root, f"{i:08x}" + "0" * 24, f"{i:032x}")
    out = _tag_output(monkeypatch, capsys, root)
    assert len(out.encode("utf-8")) <= _ua._TAG_MAX_BYTES
    rows = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert rows and len(rows) < _ua._TAG_MAX_ROWS, (
        "this case must actually ENTER the trim loop, or it proves nothing"
    )
    tail = [ln for ln in out.splitlines() if ln.startswith("…and ")]
    assert tail, "the overflow pointer must survive the trim"
    assert f"…and {12 - len(rows)} more" in tail[0], "and must name the REAL remainder"
