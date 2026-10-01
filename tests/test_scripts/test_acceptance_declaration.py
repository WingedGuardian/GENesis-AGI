"""The `## Acceptance` PR-body parser (scripts/acceptance_declaration.py).

Exercises both reads: the section/bullet extraction (including fences, heading
levels, and section termination) and the source-pointer shapes (issue, ledger,
follow-up, spec/plan, and the path rejection), plus the refusal bounds and a
never-raises property loop.
"""

from __future__ import annotations

import ast
import importlib.util
import random
import re
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
_REPO = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "acceptance_declaration", _SCRIPTS / "acceptance_declaration.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


_HEX32 = "a" * 32


# ── Section and bullets ─────────────────────────────────────────────


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
    body = "```\n## Acceptance\n\n- fake\n```\n"
    out = mod.parse_acceptance(body)
    assert out["present"] is False
    assert "no ## Acceptance section" in out["problems"]


def test_level3_heading_is_found(mod):
    out = mod.parse_acceptance("### Acceptance\n\n- one\n")
    assert out["present"] is True
    assert out["bullets"] == ["one"]


def test_section_stops_at_next_heading(mod):
    body = "## Acceptance\n\n- one\n\n## Other\n\n- not counted\n"
    out = mod.parse_acceptance(body)
    assert out["bullets"] == ["one"]


def test_no_section(mod):
    out = mod.parse_acceptance("plain body with no headings\n")
    assert out["present"] is False
    assert "no ## Acceptance section" in out["problems"]


def test_comment_only_bullet_does_not_count(mod):
    body = "## Acceptance\n\n- <!-- placeholder -->\n"
    out = mod.parse_acceptance(body)
    assert out["present"] is False
    assert "## Acceptance has no bullets" in out["problems"]


def test_empty_task_marker_bullets_do_not_count(mod):
    body = "## Acceptance\n\n- [ ]\n- [x]\n- [ ] <!-- placeholder -->\n"
    out = mod.parse_acceptance(body)
    assert out["present"] is False
    assert out["bullets"] == []
    assert "## Acceptance has no bullets" in out["problems"]


def test_checked_task_marker_bullet_keeps_its_text(mod):
    out = mod.parse_acceptance("## Acceptance\n\n- [x] done\n")
    assert out["present"] is True
    assert out["bullets"] == ["[x] done"]


def test_ordered_paren_marker_counts(mod):
    out = mod.parse_acceptance("## Acceptance\n\n1) item\n")
    assert out["present"] is True
    assert out["bullets"] == ["item"]


# ── Source pointer ──────────────────────────────────────────────────


def test_closes_issue_pointer(mod):
    out = mod.parse_acceptance("## Acceptance\n\n- x\n\nCloses #12\n")
    assert out["source"] == {"kind": "issue", "value": "12"}


def test_ledger_pointer(mod):
    body = f"## Acceptance\n\n- x\n\nLedger: {_HEX32}\n"
    assert mod.parse_acceptance(body)["source"] == {
        "kind": "ledger",
        "value": _HEX32,
    }


def test_follow_up_pointer(mod):
    body = f"## Acceptance\n\n- x\n\nFollow-up: {_HEX32}\n"
    assert mod.parse_acceptance(body)["source"] == {
        "kind": "follow_up",
        "value": _HEX32,
    }


def test_spec_pointer(mod):
    body = "## Acceptance\n\n- x\n\nSpec: round-gate\n"
    assert mod.parse_acceptance(body)["source"] == {
        "kind": "spec",
        "value": "round-gate",
    }


def test_spec_path_is_a_problem(mod):
    body = "## Acceptance\n\n- x\n\nSpec: a/b\n"
    out = mod.parse_acceptance(body)
    assert out["source"] is None
    assert "spec/plan pointer must be a name, not a path" in out["problems"]


def test_wrapped_issue_pointers(mod):
    for line in ("- Closes #5", "> Closes #5", "**Closes #5**"):
        out = mod.parse_acceptance(f"## Acceptance\n\n- x\n\n{line}\n")
        assert out["source"] == {"kind": "issue", "value": "5"}, line


def test_wrapped_ledger_and_followup_pointers(mod):
    body = f"## Acceptance\n\n- x\n\n- **Ledger: {_HEX32}**\n"
    out = mod.parse_acceptance(body)
    assert out["source"] == {"kind": "ledger", "value": _HEX32}
    body = f"## Acceptance\n\n- x\n\n> Follow-up: {_HEX32}\n"
    out = mod.parse_acceptance(body)
    assert out["source"] == {"kind": "follow_up", "value": _HEX32}


def test_list_marker_spec_pointer(mod):
    out = mod.parse_acceptance("## Acceptance\n\n- x\n\n* Spec: my-spec\n")
    assert out["source"] == {"kind": "spec", "value": "my-spec"}


def test_issue_pointer_with_trailing_text_is_not_a_pointer(mod):
    out = mod.parse_acceptance("## Acceptance\n\n- x\n\nCloses #5 and more\n")
    assert out["source"] is None


def test_invalid_spec_name_wins_over_later_pointer(mod):
    out = mod.parse_acceptance("## Acceptance\n\n- x\n\nSpec: foo!\nCloses #3\n")
    assert out["source"] is None
    assert "spec/plan pointer is not a valid name" in out["problems"]


def test_no_pointer(mod):
    out = mod.parse_acceptance("## Acceptance\n\n- x\n")
    assert out["source"] is None
    assert (
        "no source pointer (Closes #N, Ledger:, Follow-up:, Spec:, Plan:)"
        in out["problems"]
    )


# ── Bounds ──────────────────────────────────────────────────────────


def test_oversized_body_refused(mod):
    body = "x" * 65_537
    out = mod.parse_acceptance(body)
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
            rf"^{name} = re\.compile\((r?\"(?:[^\"\\]|\\.)*\")",
            text,
            re.MULTILINE,
        )
        assert m, f"{name} not found in repo_pulse.py"
        assert getattr(mod, name).pattern == ast.literal_eval(m.group(1))


# ── Never raises ────────────────────────────────────────────────────


def test_never_raises_on_random_bodies(mod):
    rng = random.Random(20261001)
    fragments = [
        "## Acceptance",
        "### Acceptance",
        "# Acceptance",
        "- item",
        "* item",
        "+ item",
        "1. item",
        "- <!-- comment -->",
        "```",
        "~~~",
        "Closes #7",
        "fixes #99",
        f"Ledger: {_HEX32}",
        f"Follow-up: {_HEX32}",
        "Spec: round-gate",
        "Plan: a/b",
        "Spec:",
        "<!--",
        "-->",
        "## Other",
        "prose text",
        "Ledger: nothex",
        "Refs #",
        "",
        " ",
    ]
    for _ in range(200):
        body = "\n".join(rng.choice(fragments) for _ in range(rng.randint(0, 40)))
        out = mod.parse_acceptance(body)
        assert set(out) == {"present", "bullets", "source", "problems"}
