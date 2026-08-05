"""Tests for scripts/ci/private_pattern_scan.py — the CI private-pattern leak gate.

Covers every fail-open edge the former inline-shell gate hit (blank-line
empty-regex, ``++``-content, malformed pattern) plus the core contract
(counts-only output, correct exit codes).
"""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "private_pattern_scan.py"
_spec = importlib.util.spec_from_file_location("private_pattern_scan", _MODULE_PATH)
pps = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(pps)


# --- load_patterns -----------------------------------------------------------


def test_load_ignores_blank_and_comment_lines():
    text = "# a comment\n\n\\bhost\\b\n   \n# another\n\\b10\\.[0-9]+\\b\n"
    pats = pps.load_patterns(text)
    assert len(pats) == 2


def test_load_tolerates_trailing_newline_no_empty_pattern():
    # The secret is joined with "\n" + trailing "\n" (round-1 bug source): the
    # trailing blank line must NOT become an empty (match-all) pattern.
    pats = pps.load_patterns("\\bhost\\b\n")
    assert len(pats) == 1
    assert not any(p.search("totally unrelated line") for p in pats)


def test_load_empty_raises():
    with pytest.raises(pps.PatternError):
        pps.load_patterns("")


def test_load_all_blank_or_comment_raises():
    with pytest.raises(pps.PatternError):
        pps.load_patterns("# just a comment\n\n   \n")


def test_load_invalid_regex_raises_without_leaking_content():
    with pytest.raises(pps.PatternError) as exc:
        pps.load_patterns("\\bok\\b\n[unbalanced\n")
    msg = str(exc.value)
    assert "line 2" in msg  # names the line, not the content
    assert "unbalanced" not in msg  # never echoes the (secret) pattern text


# --- count_matching_lines ----------------------------------------------------


def test_clean_haystack_zero():
    pats = pps.load_patterns("\\b10\\.[0-9]+\\.[0-9]+\\.[0-9]+\\b\n")
    assert pps.count_matching_lines("nothing to see here\nplain text\n", pats) == 0


def test_matching_haystack_counts_lines():
    pats = pps.load_patterns("\\b10\\.[0-9]+\\.[0-9]+\\.[0-9]+\\b\n")
    hay = "clean\n+HOST = 10.5.6.7\nclean2\n+OTHER = 10.9.9.9\n"
    assert pps.count_matching_lines(hay, pats) == 2


def test_added_content_starting_with_plusplus_is_scanned():
    # Round-2 fix: a diff line for added content '++x' renders as '+++x'; it must
    # still be scanned (the old '^+++' header filter wrongly dropped it).
    pats = pps.load_patterns("\\b10\\.[0-9]+\\.[0-9]+\\.[0-9]+\\b\n")
    assert pps.count_matching_lines("+++10.5.6.7 leak\n", pats) == 1


# --- main() exit codes + contract -------------------------------------------


def _run_main(tmp_path, pattern_text, stdin_text, monkeypatch):
    pf = tmp_path / "patterns.txt"
    pf.write_text(pattern_text, encoding="utf-8")
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    return pps.main(["--patterns", str(pf)])


def test_main_clean_exit0(tmp_path, monkeypatch, capsys):
    rc = _run_main(tmp_path, "\\bsecret-host\\b\n", "nothing here\n", monkeypatch)
    assert rc == pps.EXIT_CLEAN
    assert "CLEAN" in capsys.readouterr().out


def test_main_leak_exit1_counts_only_no_content(tmp_path, monkeypatch, capsys):
    rc = _run_main(tmp_path, "\\bsecret-host\\b\n", "+ref to secret-host here\n", monkeypatch)
    assert rc == pps.EXIT_LEAK
    out = capsys.readouterr().out
    assert "match count: 1" in out
    assert "secret-host" not in out  # never echoes the matched content


def test_main_empty_secret_fails_loud_exit3(tmp_path, monkeypatch, capsys):
    rc = _run_main(tmp_path, "\n   \n# c\n", "anything\n", monkeypatch)
    assert rc == pps.EXIT_PROVISIONING
    assert "unusable" in capsys.readouterr().err


def test_main_malformed_pattern_fails_loud_exit3(tmp_path, monkeypatch, capsys):
    rc = _run_main(tmp_path, "[unbalanced\n", "anything\n", monkeypatch)
    assert rc == pps.EXIT_PROVISIONING
    err = capsys.readouterr().err
    assert "unusable" in err
    assert "unbalanced" not in err  # no content leak in the error


def test_main_missing_file_exit3(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("x\n"))
    rc = pps.main(["--patterns", str(tmp_path / "does-not-exist.txt")])
    assert rc == pps.EXIT_PROVISIONING
