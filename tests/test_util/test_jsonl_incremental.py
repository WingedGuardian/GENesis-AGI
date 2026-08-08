"""Tests for genesis.util.jsonl.read_transcript_delta — incremental byte-offset resume.

The extraction cycle re-reads every extractable transcript from byte 0 each pass;
``read_transcript_delta`` makes that incremental: seek to a stored byte offset and
read only the delta.  The load-bearing INVARIANT these tests pin:

    if a returned ``new_byte_offset`` is later passed back as ``start_byte`` with the
    matching ``start_line``, it is the byte position of the START of line
    ``new_line_count`` — so a resumed read continues exactly where the prior one
    stopped, with ABSOLUTE line numbers preserved.

Everything degrades safely: a NULL/absent ``start_byte`` falls back to a full scan
from byte 0 (legacy behaviour), and a stored offset past EOF resets to 0.
"""

from __future__ import annotations

import json

from genesis.util.jsonl import (
    read_transcript_delta,
    read_transcript_messages,
)

# --------------------------------------------------------------------------- #
# fixture helpers — write real CC-format JSONL and compute expected byte offsets
# --------------------------------------------------------------------------- #


def _user(text: str) -> str:
    return json.dumps({"type": "user", "message": {"content": text}}, ensure_ascii=False)


def _asst(text: str) -> str:
    return json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
        ensure_ascii=False,
    )


def _summary() -> str:
    # A non-user/assistant entry — read but yields NO ConversationMessage.
    return json.dumps({"type": "summary", "summary": "meta"})


def _write(path, lines: list[str], *, trailing_newline: bool = True) -> None:
    data = "\n".join(lines)
    if lines and trailing_newline:
        data += "\n"
    path.write_bytes(data.encode("utf-8"))


def _line_start_offsets(lines: list[str]) -> list[int]:
    """offs[i] = byte offset of the start of line i; offs[len(lines)] = EOF."""
    offs = [0]
    for ln in lines:
        offs.append(offs[-1] + len((ln + "\n").encode("utf-8")))
    return offs


# --------------------------------------------------------------------------- #
# first read (start_byte=None) — full scan, reports offsets
# --------------------------------------------------------------------------- #


def test_first_read_scans_from_zero_and_reports_offsets(tmp_path):
    lines = [_user("a"), _asst("b"), _user("c")]
    p = tmp_path / "t.jsonl"
    _write(p, lines)
    offs = _line_start_offsets(lines)

    d = read_transcript_delta(p, start_line=0, start_byte=None)

    assert [m.line_number for m in d.messages] == [0, 1, 2]
    assert [m.text for m in d.messages] == ["a", "b", "c"]
    assert d.new_line_count == 3
    assert d.new_byte_offset == offs[3]  # EOF
    assert d.unchanged is False
    assert d.truncated_reset is False
    # each message's end_byte == start of the NEXT line
    assert [m.end_byte for m in d.messages] == [offs[1], offs[2], offs[3]]


# --------------------------------------------------------------------------- #
# THE INVARIANT — new_byte_offset is the byte start of line new_line_count
# --------------------------------------------------------------------------- #


def test_invariant_byte_offset_is_line_start(tmp_path):
    lines = [_user("x"), _summary(), _asst("y"), _summary(), _user("z")]
    p = tmp_path / "t.jsonl"
    _write(p, lines)
    offs = _line_start_offsets(lines)

    d = read_transcript_delta(p, start_line=0, start_byte=None)

    # independent computation: offset of the start of line `new_line_count`
    assert d.new_byte_offset == offs[d.new_line_count]


# --------------------------------------------------------------------------- #
# unchanged file — stat-gate returns immediately, no reparse
# --------------------------------------------------------------------------- #


def test_unchanged_file_returns_empty_no_work(tmp_path):
    lines = [_user("a"), _asst("b"), _user("c")]
    p = tmp_path / "t.jsonl"
    _write(p, lines)
    first = read_transcript_delta(p, start_line=0, start_byte=None)

    second = read_transcript_delta(
        p, start_line=first.new_line_count, start_byte=first.new_byte_offset
    )

    assert second.unchanged is True
    assert second.messages == []
    assert second.new_byte_offset == first.new_byte_offset
    assert second.new_line_count == first.new_line_count


# --------------------------------------------------------------------------- #
# grown file — read ONLY the delta, absolute line numbers preserved
# --------------------------------------------------------------------------- #


def test_grown_file_returns_only_new_with_absolute_line_numbers(tmp_path):
    lines = [_user("a"), _asst("b"), _user("c")]
    p = tmp_path / "t.jsonl"
    _write(p, lines)
    first = read_transcript_delta(p, start_line=0, start_byte=None)

    grown = lines + [_asst("d"), _user("e")]
    _write(p, grown)
    offs = _line_start_offsets(grown)

    d = read_transcript_delta(p, start_line=first.new_line_count, start_byte=first.new_byte_offset)

    assert [m.line_number for m in d.messages] == [3, 4]  # ABSOLUTE, not 0/1
    assert [m.text for m in d.messages] == ["d", "e"]
    assert d.new_line_count == 5
    assert d.new_byte_offset == offs[5]
    assert d.unchanged is False


# --------------------------------------------------------------------------- #
# non-message lines advance the offset but yield no messages
# (the "empty but changed" case the extraction empty-branch dual-writes on)
# --------------------------------------------------------------------------- #


def test_non_message_lines_advance_offset_yield_no_messages(tmp_path):
    lines = [_summary(), _summary(), _summary()]
    p = tmp_path / "t.jsonl"
    _write(p, lines)
    offs = _line_start_offsets(lines)

    d = read_transcript_delta(p, start_line=0, start_byte=None)

    assert d.messages == []
    assert d.unchanged is False  # file WAS read (not stat-gated)
    assert d.new_line_count == 3
    assert d.new_byte_offset == offs[3]


# --------------------------------------------------------------------------- #
# truncation / rotation — stored offset past EOF → reset to 0
# --------------------------------------------------------------------------- #


def test_truncation_resets_and_rereads_from_zero(tmp_path):
    big = [_user("a"), _asst("b"), _user("c"), _asst("d")]
    p = tmp_path / "t.jsonl"
    _write(p, big)
    first = read_transcript_delta(p, start_line=0, start_byte=None)

    # transcript rotated/replaced with a SHORTER file
    short = [_user("x"), _asst("y")]
    _write(p, short)

    d = read_transcript_delta(p, start_line=first.new_line_count, start_byte=first.new_byte_offset)

    assert d.truncated_reset is True
    assert [m.line_number for m in d.messages] == [0, 1]  # re-read from top
    assert [m.text for m in d.messages] == ["x", "y"]


# --------------------------------------------------------------------------- #
# active append — a partial trailing line (no \n) is NOT consumed
# --------------------------------------------------------------------------- #


def test_partial_trailing_line_not_consumed(tmp_path):
    complete = [_user("a"), _asst("b")]
    p = tmp_path / "t.jsonl"
    # two complete lines + a THIRD partial line mid-write (no trailing newline)
    _write(p, complete)  # ends with \n
    offs = _line_start_offsets(complete)
    with open(p, "ab") as f:
        f.write(_user("partial-not-terminated").encode("utf-8"))  # no \n

    d = read_transcript_delta(p, start_line=0, start_byte=None)

    assert [m.text for m in d.messages] == ["a", "b"]  # partial excluded
    assert d.new_line_count == 2
    assert d.new_byte_offset == offs[2]  # points at START of the partial line

    # complete the partial line, resume from the stored offset → now it reads
    with open(p, "ab") as f:
        f.write(b"\n")
    d2 = read_transcript_delta(p, start_line=d.new_line_count, start_byte=d.new_byte_offset)
    assert [m.line_number for m in d2.messages] == [2]
    assert [m.text for m in d2.messages] == ["partial-not-terminated"]


# --------------------------------------------------------------------------- #
# multibyte UTF-8 — byte offsets are byte-accurate, no torn characters
# --------------------------------------------------------------------------- #


def test_multibyte_line_offsets_are_byte_accurate(tmp_path):
    lines = [_user("café ☕ 日本語"), _asst("plain")]
    p = tmp_path / "t.jsonl"
    _write(p, lines)
    offs = _line_start_offsets(lines)

    d = read_transcript_delta(p, start_line=0, start_byte=None)

    assert d.messages[0].text == "café ☕ 日本語"
    assert d.messages[0].end_byte == offs[1]  # byte length, not char count
    assert d.new_byte_offset == offs[2]


# --------------------------------------------------------------------------- #
# legacy fallback — start_byte=None with start_line>0 skips-but-scans
# (existing rows whose last_extracted_byte is still NULL)
# --------------------------------------------------------------------------- #


def test_legacy_scan_with_start_line_skips_but_tracks_bytes(tmp_path):
    lines = [_user("a"), _asst("b"), _user("c"), _asst("d"), _user("e")]
    p = tmp_path / "t.jsonl"
    _write(p, lines)
    offs = _line_start_offsets(lines)

    d = read_transcript_delta(p, start_line=3, start_byte=None)

    assert [m.line_number for m in d.messages] == [3, 4]  # only >= start_line
    assert d.new_line_count == 5
    assert d.new_byte_offset == offs[5]  # whole file scanned for the offset


# --------------------------------------------------------------------------- #
# parity — the untouched read_transcript_messages still behaves identically
# --------------------------------------------------------------------------- #


def test_read_transcript_messages_parity(tmp_path):
    lines = [_user("a"), _summary(), _asst("b"), _user("c")]
    p = tmp_path / "t.jsonl"
    _write(p, lines)

    old = read_transcript_messages(p, start_line=0)
    new = read_transcript_delta(p, start_line=0, start_byte=None).messages

    assert [(m.role, m.text, m.line_number) for m in old] == [
        (m.role, m.text, m.line_number) for m in new
    ]


def test_io_failure_sets_failed_flag_not_a_stat_gate(tmp_path):
    """A stat()/read() error returns ``failed=True`` with an empty, NON-``unchanged``
    delta.

    The empty-result branch in ``run_extraction_cycle`` advances BOTH watermarks
    whenever ``not unchanged``. Without a distinct failure flag, a transient stat()
    failure on a freshly-migrated session (``last_extracted_byte`` NULL → ``start_byte``
    None → ``new_byte_offset`` coerced to 0) paired with a nonzero ``start_line`` would
    persist byte=0 against that line watermark — the invariant violation that makes the
    next cycle re-read the ENTIRE transcript from byte 0.
    """
    missing = tmp_path / "never_created.jsonl"  # stat() raises FileNotFoundError (OSError)
    d = read_transcript_delta(missing, start_line=40000, start_byte=None)

    assert d.failed is True
    assert d.messages == []
    assert d.unchanged is False  # a genuine failure, NOT the stat-gate no-op
