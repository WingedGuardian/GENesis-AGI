"""The `## Acceptance` PR-body parser (scripts/acceptance_declaration.py).

The contract is issue #2736's "Spec" plus its "Amendment: closed grammar" and
the owner's rulings recorded with the PR. Exercised here: the original Spec's
acceptance list, every row of the amendment's table, one regression per review
finding on the PR (each fails on the pre-rebuild parser), the refusal bounds,
the shared-scanner wiring, and a never-raises property loop.
"""

from __future__ import annotations

import ast
import random
import re
import sys
from pathlib import Path

import pytest

from tests.conftest import private_module

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def mod():
    return private_module("acceptance_declaration", _SCRIPTS / "acceptance_declaration.py")


_HEX32 = "a" * 32
_A = "## Acceptance\n"
_NO_POINTER = "no source pointer (Closes #N, Ledger:, Follow-up:, Spec:, Plan:)"
_UNREADABLE = (
    "cannot read this body reliably: a code fence is indented or inside"
    " a list item or quote (start fences at column 0)"
)


def _issue(n: str) -> dict:
    return {"kind": "issue", "value": n}


# ── Section and bullets (the original Spec) ─────────────────────────


def test_section_with_two_bullets(mod):
    body = "Some intro.\n\n## Acceptance\n\n- first thing\n- second thing\n"
    out = mod.parse_acceptance(body)
    assert out["present"] is True
    assert out["bullets"] == ["first thing", "second thing"]
    assert "no ## Acceptance section" not in out["problems"]
    assert "## Acceptance has no bullets" not in out["problems"]


def test_section_without_bullets(mod):
    out = mod.parse_acceptance("## Acceptance\n\njust prose, no list\n")
    assert out["present"] is False
    assert out["bullets"] == []
    assert "## Acceptance has no bullets" in out["problems"]


def test_section_only_inside_fence_is_not_found(mod):
    out = mod.parse_acceptance("```\n## Acceptance\n\n- fake\n```\n")
    assert out["present"] is False
    assert "no ## Acceptance section" in out["problems"]


def test_level3_heading_is_found(mod):
    out = mod.parse_acceptance("### Acceptance\n\n- one\n")
    assert out["present"] is True
    assert out["bullets"] == ["one"]


def test_section_stops_at_next_heading(mod):
    out = mod.parse_acceptance("## Acceptance\n\n- one\n\n## Other\n\n- not counted\n")
    assert out["bullets"] == ["one"]


def test_no_section(mod):
    out = mod.parse_acceptance("plain body with no headings\n")
    assert out["present"] is False
    assert "no ## Acceptance section" in out["problems"]


def test_first_section_wins(mod):
    out = mod.parse_acceptance("## Acceptance\n- a\n## Acceptance\n- b\n")
    assert out["bullets"] == ["a"]


def test_comment_only_bullet_does_not_count(mod):
    out = mod.parse_acceptance("## Acceptance\n\n- <!-- placeholder -->\n")
    assert out["present"] is False
    assert "## Acceptance has no bullets" in out["problems"]


def test_empty_task_marker_bullets_do_not_count(mod):
    body = "## Acceptance\n\n- [ ]\n- [x]\n- [X]\n- [ ] <!-- placeholder -->\n"
    out = mod.parse_acceptance(body)
    assert out["present"] is False
    assert out["bullets"] == []
    assert "## Acceptance has no bullets" in out["problems"]


def test_checked_task_marker_bullet_keeps_its_text(mod):
    out = mod.parse_acceptance("## Acceptance\n\n- [x] done\n")
    assert out["present"] is True
    assert out["bullets"] == ["[x] done"]


def test_ordered_markers_count(mod):
    out = mod.parse_acceptance("## Acceptance\n\n1) item\n2. other\n")
    assert out["bullets"] == ["item", "other"]


def test_lazy_continuation_joins_bullet(mod):
    out = mod.parse_acceptance("## Acceptance\n\n- first part\nsecond part\n- other\n")
    assert out["bullets"] == ["first part second part", "other"]


def test_indented_continuation_after_blank_line(mod):
    out = mod.parse_acceptance("## Acceptance\n\n- one\n\n  continued detail\n- two\n")
    assert out["bullets"] == ["one continued detail", "two"]


def test_blank_line_then_unindented_text_ends_the_item(mod):
    """Rule 6: the blank line closes the item when the next line is not indented 2+."""
    out = mod.parse_acceptance("## Acceptance\n\n- one\n\nnot part of it\n- two\n")
    assert out["bullets"] == ["one", "two"]


def test_empty_marker_takes_its_continuation(mod):
    out = mod.parse_acceptance("## Acceptance\n-\n  foo\n")
    assert out["bullets"] == ["foo"]


def test_too_many_hashes_is_not_a_section(mod):
    out = mod.parse_acceptance("####### Acceptance\n\n- x\n")
    assert out["present"] is False
    assert "no ## Acceptance section" in out["problems"]


def test_heading_needs_whitespace(mod):
    out = mod.parse_acceptance("##Acceptance\n\n- x\n")
    assert out["present"] is False
    assert "no ## Acceptance section" in out["problems"]


# ── Source pointer (the original Spec) ──────────────────────────────


def test_closes_issue_pointer(mod):
    assert mod.parse_acceptance("## Acceptance\n\n- x\n\nCloses #12\n")["source"] == _issue("12")


def test_ledger_pointer(mod):
    out = mod.parse_acceptance(f"## Acceptance\n\n- x\n\nLedger: {_HEX32}\n")
    assert out["source"] == {"kind": "ledger", "value": _HEX32}


def test_follow_up_pointer(mod):
    out = mod.parse_acceptance(f"## Acceptance\n\n- x\n\nFollow-up: {_HEX32}\n")
    assert out["source"] == {"kind": "follow_up", "value": _HEX32}


def test_spec_pointer(mod):
    out = mod.parse_acceptance("## Acceptance\n\n- x\n\nSpec: round-gate\n")
    assert out["source"] == {"kind": "spec", "value": "round-gate"}


def test_spec_path_is_a_problem(mod):
    out = mod.parse_acceptance("## Acceptance\n\n- x\n\nSpec: a/b\n")
    assert out["source"] is None
    assert "spec/plan pointer must be a name, not a path" in out["problems"]


def test_wrapped_issue_pointers(mod):
    for line in ("- Closes #5", "> Closes #5", ">Closes #5", "**Closes #5**", "_Closes #5_"):
        out = mod.parse_acceptance(f"## Acceptance\n\n- x\n\n{line}\n")
        assert out["source"] == _issue("5"), line


def test_wrapped_ledger_and_followup_pointers(mod):
    out = mod.parse_acceptance(f"## Acceptance\n\n- x\n\n- **Ledger: {_HEX32}**\n")
    assert out["source"] == {"kind": "ledger", "value": _HEX32}
    out = mod.parse_acceptance(f"## Acceptance\n\n- x\n\n> Follow-up: {_HEX32}\n")
    assert out["source"] == {"kind": "follow_up", "value": _HEX32}


def test_list_marker_spec_pointer(mod):
    out = mod.parse_acceptance("## Acceptance\n\n- x\n\n* Spec: my-spec\n")
    assert out["source"] == {"kind": "spec", "value": "my-spec"}


def test_two_wrappers_is_not_a_pointer(mod):
    out = mod.parse_acceptance("## Acceptance\n\n- x\n\n> - Closes #5\n")
    assert out["source"] is None


def test_issue_pointer_with_trailing_text_is_not_a_pointer(mod):
    assert mod.parse_acceptance("## Acceptance\n\n- x\n\nCloses #5 and more\n")["source"] is None


def test_spec_value_trailing_punctuation_is_stripped(mod):
    out = mod.parse_acceptance("## Acceptance\n\n- x\n\nSpec: foo!\n")
    assert out["source"] == {"kind": "spec", "value": "foo"}


def test_invalid_spec_name_wins_over_later_pointer(mod):
    for value in ("foo?bar", "foo@bar"):
        out = mod.parse_acceptance(f"## Acceptance\n\n- x\n\nSpec: {value}\nCloses #3\n")
        assert out["source"] is None, value
        assert "spec/plan pointer is not a valid name" in out["problems"], value


def test_multi_word_spec_is_prose_and_a_later_pointer_wins(mod):
    out = mod.parse_acceptance("## Acceptance\n\n- x\n\nSpec: foo bar\nCloses #3\n")
    assert out["source"] == _issue("3")
    assert out["problems"] == []


def test_malformed_issue_pointer_is_prose(mod):
    """Owner ruling: only Spec/Plan have a malformed form; `Closes #bad` is prose."""
    out = mod.parse_acceptance("## Acceptance\n\n- x\n\nCloses #bad\nCloses #7\n")
    assert out["source"] == _issue("7")


def test_pointer_only_bullet_is_not_a_criterion(mod):
    out = mod.parse_acceptance("## Acceptance\n\n- Closes #5\n")
    assert out["present"] is False
    assert out["bullets"] == []
    assert "## Acceptance has no bullets" in out["problems"]
    assert out["source"] == _issue("5")


def test_task_marker_pointer_is_a_criterion_in_both_scans(mod):
    """A checkbox is not a pointer wrapper, so `- [ ] Closes #5` is a criterion."""
    out = mod.parse_acceptance("## Acceptance\n- [ ] Closes #5\n- real\n\nCloses #1\n")
    assert out["bullets"] == ["[ ] Closes #5", "real"]
    assert out["source"] == _issue("1")


def test_trailing_period_issue_pointer(mod):
    assert mod.parse_acceptance("## Acceptance\n\n- x\n\nCloses #2770.\n")["source"] == _issue(
        "2770"
    )


def test_no_pointer(mod):
    out = mod.parse_acceptance("## Acceptance\n\n- x\n")
    assert out["source"] is None
    assert _NO_POINTER in out["problems"]


# ── Amendment: the closed-grammar table, row by row ─────────────────

_AMENDMENT_ROWS = [
    ("r1", "## Acceptance\n- ship it\n\nCloses #1", ["ship it"], _issue("1")),
    ("r2", _A + "- ship it\n\nMore prose\n\nCloses #1", ["ship it"], _issue("1")),
    ("r3-tight", _A + "- a\n  continued", ["a continued"], None),
    ("r3-loose", _A + "- a\n\n  continued", ["a continued"], None),
    ("r4", _A + "- a\n  - b", ["a", "b"], None),
    ("r5", "## Acceptance\n\n    - code", [], None),
    ("r6", _A + "- a\n   ## Other\n- b", ["a"], None),
    ("r7", "   ## Acceptance\n- a", ["a"], None),
    ("r8", _A + "１. a", [], None),
    ("r9", _A + "- x\n\n１. Closes #3", ["x"], None),
    ("r10", "## Acceptance\n\n    Closes #4\n- a", ["a"], None),
    ("r11-outside", _A + "- x\n\n**Closes #5**.", ["x"], _issue("5")),
    ("r11-inside", _A + "- x\n\n**Closes #5.**", ["x"], _issue("5")),
    (
        "r12",
        "## Acceptance\n- Plan: run the migration, then verify counts\n- other\n\nCloses #3",
        ["Plan: run the migration, then verify counts", "other"],
        _issue("3"),
    ),
]


@pytest.mark.parametrize(
    "body,bullets,source", [r[1:] for r in _AMENDMENT_ROWS], ids=[r[0] for r in _AMENDMENT_ROWS]
)
def test_amendment_rows(mod, body, bullets, source):
    out = mod.parse_acceptance(body)
    assert out["bullets"] == bullets
    assert out["source"] == source
    assert not [p for p in out["problems"] if p.startswith("spec/plan")]


def test_amendment_row12_has_no_problems(mod):
    body = "## Acceptance\n- Plan: run the migration, then verify counts\n- other\n\nCloses #3"
    assert mod.parse_acceptance(body)["problems"] == []


def test_amendment_row13_plan_with_no_name(mod):
    out = mod.parse_acceptance(_A + "- x\n\nPlan:")
    assert out["source"] is None
    assert "spec/plan pointer has no name" in out["problems"]


def test_amendment_row14_malformed_plan_wins_over_later_closes(mod):
    out = mod.parse_acceptance(_A + "- x\n\nPlan: a/b\nCloses #3")
    assert out["source"] is None
    assert "spec/plan pointer must be a name, not a path" in out["problems"]


# ── Review findings on #2762, each a regression on the old parser ───


def test_finding_full_opening_fence(mod):
    """A four-backtick fence documenting a three-backtick one stays hidden."""
    body = "````\n```\n## Acceptance\n- fake\nCloses #9\n```\n````\n" + _A + "- real\n\nCloses #1"
    out = mod.parse_acceptance(body)
    assert out["bullets"] == ["real"]
    assert out["source"] == _issue("1")


@pytest.mark.parametrize("sep", ["\x85", "\x0b", "\x0c", " ", " "])
def test_finding_only_markdown_line_endings_split(mod, sep):
    out = mod.parse_acceptance(_A + f"- x\n\nprose{sep}Closes #5")
    assert out["source"] is None
    assert out["bullets"] == ["x"]


def test_finding_seven_hashes_neither_starts_nor_ends(mod):
    """`####### b` is paragraph text in CommonMark, so it continues the item."""
    assert mod.parse_acceptance(_A + "- a\n####### b\n- c")["bullets"] == ["a ####### b", "c"]


@pytest.mark.parametrize("digits", ["١٢", "１２"])
def test_finding_ascii_issue_digits(mod, digits):
    assert mod.parse_acceptance(_A + f"- x\n\nCloses #{digits}")["source"] is None


def test_finding_ascii_wrapper_digits(mod):
    assert mod.parse_acceptance(_A + "- x\n\n١. Closes #5")["source"] is None


@pytest.mark.parametrize("keyword", ["Cloſes", "Refſ", "Fıxes", "Fİxes"])
def test_unicode_case_folding_does_not_make_a_keyword(mod, keyword):
    """Without re.ASCII, IGNORECASE folds U+017F to `s` and U+0130/U+0131 to `i`.

    Those three are every non-ASCII code point that folds into a keyword letter
    (enumerated over the whole Unicode range, 2026-10-03).
    """
    assert mod.parse_acceptance(_A + f"- x\n\n{keyword} #5")["source"] is None


def test_finding_continuation_lines_kept(mod):
    out = mod.parse_acceptance(_A + "- preserve every field\n  including nested metadata")
    assert out["bullets"] == ["preserve every field including nested metadata"]


def test_finding_indented_pointer_is_not_a_pointer(mod):
    out = mod.parse_acceptance(f"    Closes #5\n\nLedger: {_HEX32}")
    assert out["source"] == {"kind": "ledger", "value": _HEX32}


def test_nested_pointer_item_is_discarded(mod):
    """A nested item whose text is a pointer is not a criterion (the discard rule)."""
    out = mod.parse_acceptance(_A + "- a\n    - Closes #5\n- b")
    assert out["bullets"] == ["a", "b"]


def test_finding_pointer_inside_an_item_closes_it(mod):
    out = mod.parse_acceptance(_A + "- a\n  Closes #3\n- b")
    assert out["bullets"] == ["a", "b"]
    assert out["source"] == _issue("3")


@pytest.mark.parametrize(
    "body",
    [
        _A + "- a\n\n- ```\n  x\n  ```\n\n```\nCloses #9\n```\n\nRefs #1",
        _A + "- x\n\n> ```\n> Closes #7\n> ```\nCloses #1",
        _A + "- x\n\n1. ```\n   Closes #7\n   ```",
        _A + "- x\n\n    ```\nCloses #1",
        _A + "- x\n\n\t~~~\nCloses #1",
        _A + "- a\n  ```\n  x\n- b\n\n```\nCloses #9\n```\n\nRefs #1",
        "   ~~~\nx\n   ~~~\n" + _A + "- a\n\nCloses #1",
        _A + "- ship\n\n> > ```\n> > hidden\n> > ```\n\nCloses #1",
        _A + "- ship\n\n ```\nx\n```\n\nCloses #1",
        _A + "- ship\n\n```\nx\n    ```\nCloses #9\n```\n\nCloses #1",
        _A + "- ship\n\n```x``` is inline\n\n```\nCloses #9\n- fake\n```",
        _A + "- ship\n\n<!-- c -->```\n\n```\nCloses #9\n```",
    ],
    ids=[
        "list-fence-flip", "quoted-fence", "ordered-list-fence", "indent-4", "tab",
        "item-fence-closed-by-item-end", "indent-3", "nested-quote-fence", "nbsp-fence",
        "indented-closer", "inline-triple-backtick", "comment-exposed-fence",
    ],
)  # fmt: skip
def test_misread_fence_refuses_the_body(mod, body):
    """A fence the shared scanner misreads is refused, never guessed through.

    The first body is the measured fail-open: the list fence's closer flips the
    scanner, so the real fence's `Closes #9` became the reported source.
    """
    out = mod.parse_acceptance(body)
    assert out == {"present": False, "bullets": [], "source": None, "problems": [_UNREADABLE]}


@pytest.mark.parametrize(
    "body",
    [
        _A + "- x\n\n```\ncode\n```\n\nCloses #1",
        # Fence-like lines that are fenced content or comment text are not fences.
        _A + "- x\n\n````\n    ```\n  ~~~\n- ```\n````\n\nCloses #1",
        _A + "- x\n\n<!--\n- ```\n    ```\n-->\n\nCloses #1",
    ],
    ids=["plain-fence", "inside-a-longer-fence", "inside-a-comment"],
)
def test_readable_fences_are_not_refused(mod, body):
    """Guard-the-guard: only a fence the scanner misreads trips the refusal."""
    out = mod.parse_acceptance(body)
    assert out["bullets"] == ["x"]
    assert out["source"] == _issue("1")
    assert _UNREADABLE not in out["problems"]


def test_empty_item_takes_no_lazy_text(mod):
    """`-` then unindented text renders as an empty item plus a paragraph."""
    out = mod.parse_acceptance(_A + "-\nCloses #11 and more\n- real")
    assert out["bullets"] == ["real"]


@pytest.mark.parametrize("underline", ["-", "---", "==="])
def test_setext_underline_under_text_ends_the_section(mod, underline):
    """Paragraph text over an underline renders as a heading, which ends the section."""
    out = mod.parse_acceptance(_A + f"- a\n\nsome prose\n{underline}\n  continued\n- not counted")
    assert out["bullets"] == ["a"]


def test_indented_line_continues_paragraph_text_before_an_underline(mod):
    """`text` + an indented line + `-` is one setext heading on GitHub."""
    out = mod.parse_acceptance(_A + "- a\n\nsome text\n\tmore text\n-\n  continued\n- b")
    assert out["bullets"] == ["a"]


def test_rule_after_an_item_is_not_a_setext_underline(mod):
    assert mod.parse_acceptance(_A + "- a\n---\n- b")["bullets"] == ["a", "b"]


def test_empty_item_does_not_survive_a_blank_line(mod):
    """A list item can begin with at most one blank line (CommonMark 0.31.2, List items)."""
    assert mod.parse_acceptance(_A + "-\n\n  foo\n- real")["bullets"] == ["real"]


def test_empty_marker_under_an_item_is_a_sibling(mod):
    assert mod.parse_acceptance(_A + "- a\n-\n  foo")["bullets"] == ["a", "foo"]


def test_thematic_break_is_not_a_criterion(mod):
    out = mod.parse_acceptance(_A + "- - -\n\nCloses #1")
    assert out["bullets"] == []
    assert out["source"] == _issue("1")


def test_item_enders_close_the_open_item(mod):
    assert mod.parse_acceptance(_A + "- a\n***\nmore")["bullets"] == ["a"]
    assert mod.parse_acceptance(_A + "- a\n> note\n- b")["bullets"] == ["a", "b"]
    # Indented 2+, the quote is inside the item on GitHub, so it continues it.
    assert mod.parse_acceptance(_A + "- a\n  > quoted")["bullets"] == ["a > quoted"]


def test_crlf_body(mod):
    out = mod.parse_acceptance("## Acceptance\r\n- a\r\n\r\nmore\r\nCloses #2\r\n")
    assert out["bullets"] == ["a"]
    assert out["source"] == _issue("2")


# ── Bounds ──────────────────────────────────────────────────────────


def test_oversized_body_refused(mod):
    out = mod.parse_acceptance("x" * 65_537)
    assert out["present"] is False
    assert out["problems"] == ["body too large to verify (65537 chars)"]


def test_none_and_empty_body(mod):
    for body in (None, ""):
        out = mod.parse_acceptance(body)
        assert out["present"] is False
        assert out["problems"] == ["empty PR body"]


# ── Regex equality with repo_pulse.py ───────────────────────────────


def test_marker_regexes_match_repo_pulse(mod):
    text = (_REPO / "src/genesis/session_awareness/repo_pulse.py").read_text()
    for name in ("MARKER_RE", "FOLLOWUP_MARKER_RE"):
        m = re.search(
            rf"^{name} = re\.compile\((r?\"(?:[^\"\\]|\\.)*\")(.*)\)$", text, re.MULTILINE
        )
        assert m, f"{name} not found in repo_pulse.py"
        assert getattr(mod, name).pattern == ast.literal_eval(m.group(1))
        # Flags matter too: FOLLOWUP_MARKER_RE anchors per line under MULTILINE.
        expected = 0
        for flag in re.findall(r"re\.([A-Z]+)", m.group(2)):
            expected |= getattr(re, flag)
        assert getattr(mod, name).flags & ~re.UNICODE == expected, name


# ── Shared scanner wiring ───────────────────────────────────────────


def test_scanner_is_check_cc_pin_receipts_readable_body(mod):
    """Visibility comes from the sibling scanner, not a local copy."""
    fn = mod._readable_body()
    assert callable(fn)
    assert fn.__module__ == "_cc_pin_receipts_for_acceptance"
    assert fn.__name__ == "readable_body"


def test_load_failure_reports_problem(mod, monkeypatch):
    monkeypatch.setattr(mod, "_READABLE_BODY_FN", mod._READABLE_BODY_UNSET)
    monkeypatch.setattr(mod, "_load_sibling_readable_body", lambda: None)
    out = mod.parse_acceptance("## Acceptance\n\n- x\n")
    assert out["present"] is False
    assert out["source"] is None
    assert out["problems"] == ["cannot load readable_body from check_cc_pin_receipts.py"]


_HELPER = "def _outside_comments(line, in_comment):\n    return line, in_comment\n"


@pytest.mark.parametrize(
    "source,loads",
    [
        ("def readable_body(body):\n    return body\n" + _HELPER, False),
        ("def readable_body(body, *, keep_blank=False):\n    return body\n", False),
        ("def readable_body(body, *, keep_blank=False):\n    return body\n" + _HELPER, True),
    ],
    ids=["no-keep_blank", "no-comment-helper", "complete"],
)
def test_incomplete_sibling_is_a_load_failure(mod, monkeypatch, tmp_path, source, loads):
    """A sibling without keep_blank or the comment rule the fence check reuses is refused."""
    (tmp_path / "check_cc_pin_receipts.py").write_text(source)
    monkeypatch.setattr(mod, "__file__", str(tmp_path / "acceptance_declaration.py"))
    name = "_cc_pin_receipts_for_acceptance"
    monkeypatch.delitem(sys.modules, name, raising=False)
    try:
        assert (mod._load_sibling_readable_body() is not None) is loads
    finally:
        sys.modules.pop(name, None)


# ── Never raises ────────────────────────────────────────────────────


def test_never_raises_on_random_bodies(mod):
    rng = random.Random(20261001)
    fragments = [
        "## Acceptance", "### Acceptance", "# Acceptance", "   ## Other", "####### x",
        "- item", "* item", "+ item", "1. item", "1) item", "- [ ]", "- [x] done", "-",
        "  continued", "    - code", "\tcode", "- <!-- comment -->", "```", "~~~", "````",
        "- ```", "> ```", "> quote", ">Closes #5", "- - -", "***",
        "Closes #7", "fixes #99", "**Closes #5**.", "Cloſes #5", f"Ledger: {_HEX32}",
        f"Follow-up: {_HEX32}", "Spec: round-gate", "Plan: a/b", "Plan:", "Spec:",
        "Plan: run the migration, then verify", "Spec: foo@bar", "<!--", "-->",
        "## Other", "prose text", "Ledger: nothex", "Refs #", "", " ", "\x85", " ",
    ]  # fmt: skip
    for _ in range(200):
        body = "\n".join(rng.choice(fragments) for _ in range(rng.randint(0, 40)))
        out = mod.parse_acceptance(body)
        assert set(out) == {"present", "bullets", "source", "problems"}
        assert out["present"] is bool(out["bullets"])
