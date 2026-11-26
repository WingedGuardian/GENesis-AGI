"""The spec skill stays small, names each reference where it is needed, and its
analysis hash is stable across approval and a first amendment."""

import hashlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO / ".claude" / "skills" / "spec-session"
SKILL = SKILL_DIR / "SKILL.md"
ANALYZE = SKILL_DIR / "references" / "spec-analyze.md"
MAX_BYTES = 12_000


def test_skill_stays_under_its_size_ceiling():
    size = len(SKILL.read_bytes())
    assert size <= MAX_BYTES, f"spec-session/SKILL.md is {size} bytes; the ceiling is {MAX_BYTES}"


def test_no_unconditional_reading_list():
    assert "Also read" not in SKILL.read_text(encoding="utf-8")


def test_moved_sections_live_in_their_references():
    capabilities = SKILL_DIR / "references" / "existing-capabilities.md"
    native = SKILL_DIR / "references" / "github-native.md"
    cap_text = capabilities.read_text(encoding="utf-8")
    native_text = native.read_text(encoding="utf-8")
    assert cap_text.startswith("# Auditing Existing Capabilities")
    assert native_text.startswith("# Never reinvent what GitHub already does natively")
    # A paragraph from the middle and the end of each, so a truncated move fails.
    assert "6. Confidence is capped by enumeration completeness." in cap_text
    assert "stays the package-level structural companion." in cap_text
    assert "**How the layers actually split**" in native_text
    assert "looks exactly like a hand-rolled implementation of something\nnovel." in native_text
    body = SKILL.read_text(encoding="utf-8")
    assert "references/existing-capabilities.md" in body
    assert "references/github-native.md" in body
    pointers = {
        REPO / ".claude/skills/build-session/SKILL.md": (
            "spec-session/references/existing-capabilities.md",
            "spec-session/references/github-native.md",
        ),
        REPO / ".claude/skills/genesis-development/references/ai-code-audit.md": (
            "spec-session/references/existing-capabilities.md",
        ),
        REPO / "docs/architecture/CURRENT.md": (
            "spec-session/references/existing-capabilities.md",
        ),
    }
    for path, needles in pointers.items():
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            assert needle in text, (path, needle)


def test_every_reference_the_skill_names_exists():
    body = SKILL.read_text(encoding="utf-8")
    named = set(re.findall(r"`references/([\w.-]+\.md)`", body))
    assert named, "the skill names no references; the pattern is broken"
    missing = sorted(n for n in named if not (SKILL_DIR / "references" / n).is_file())
    assert missing == []


def _hash_snippet() -> str:
    text = ANALYZE.read_text(encoding="utf-8")
    blocks = re.findall(r"```python\n(.*?)```", text, re.S)
    assert len(blocks) == 1, "spec-analyze.md must carry exactly one python snippet"
    snippet = blocks[0]
    assert snippet.count("\n") <= 20, "the hash snippet must stay at most 20 lines"
    return snippet


def _hash(tmp_path: Path, spec_text: str) -> str:
    script = tmp_path / "h.py"
    script.write_text(_hash_snippet(), encoding="utf-8")
    spec = tmp_path / "spec.md"
    spec.write_text(spec_text, encoding="utf-8")
    out = subprocess.run(
        [sys.executable, str(script), str(spec)], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


_SPEC = """---
status: draft
updated: 2026-11-24
---

# A spec

## Decided

- The build keeps the widget at 30 days.
"""


def test_hash_matches_its_written_definition(tmp_path):
    """Pins the definition itself: every approved report's sha256 depends on it."""
    body = "\n# A spec\n\n## Decided\n\n- The build keeps the widget at 30 days.\n"
    assert _hash(tmp_path, _SPEC) == hashlib.sha256(body.encode("utf-8")).hexdigest()


def test_hash_ignores_frontmatter_and_a_first_amendment(tmp_path):
    base = _hash(tmp_path, _SPEC)
    assert re.fullmatch(r"[0-9a-f]{64}", base)
    approved = _SPEC.replace("status: draft", "status: approved (2026-11-24)")
    assert _hash(tmp_path, approved) == base
    assert _hash(tmp_path, _SPEC + "\n## Amendments\n\n- AM-1 ...\n") == base
    assert _hash(tmp_path, _SPEC + "## Amendments\n- AM-1 ...\n") == base
    assert _hash(tmp_path, _SPEC + "\n## Spec analysis: spec.md\nInputs: ...\n") == base


def test_hash_changes_when_the_body_changes(tmp_path):
    base = _hash(tmp_path, _SPEC)
    assert _hash(tmp_path, _SPEC.replace("30 days", "31 days")) != base


_B3, _B4, _T3 = "`" * 3, "`" * 4, "~" * 3


@pytest.mark.parametrize(
    ("opener", "closer"),
    [
        (_B3 + "markdown", _B3),
        (_B4 + "markdown", _B4),  # a longer run, holding a three-backtick example
        (_T3 + "markdown", _T3),
        (_T3 + "a`b", _T3),  # a tilde opener may carry a backtick
        ("   " + _B3, "   " + _B3),  # indented up to three spaces
        (_B3, _B4),  # closed by a longer run
    ],
)
def test_a_fenced_delimiter_does_not_end_the_body(tmp_path, opener, closer):
    """A spec showing `## Amendments` in a code example of any CommonMark fence form
    still hashes what follows it, and a real amendment section still does not count."""
    inner = f"{_B3}\n## Amendments\n{_B3}\n" if opener.startswith(_B4) else "## Amendments\n"
    example = f"{opener}\n{inner}## Spec analysis: x.md\n{closer}\n"
    spec = _SPEC.replace("## Decided", example + "\n## Decided")
    base = _hash(tmp_path, spec)
    assert _hash(tmp_path, spec.replace("30 days", "60 days")) != base
    assert _hash(tmp_path, spec + "\n## Amendments\n\n- AM-1 ...\n") == base


@pytest.mark.parametrize(
    "line",
    ["    " + _B3, "``", _B3 + "a`b"],
    ids=["four-space-indent", "two-backticks", "backtick-in-info"],
)
def test_a_line_that_is_not_a_fence_hides_nothing(tmp_path, line):
    """A line CommonMark does not read as a fence opener leaves a real amendment real."""
    spec = _SPEC + f"\n{line}\n\n## Amendments\n\n- AM-1 ...\n"
    assert _hash(tmp_path, spec) == _hash(tmp_path, _SPEC + f"\n{line}\n")


def test_a_closer_with_trailing_spaces_closes_and_one_with_info_does_not(tmp_path):
    closed = _SPEC + f"\n{_B3}\nx\n{_B3}   \n\n## Amendments\n\n- AM-1 ...\n"
    assert _hash(tmp_path, closed) == _hash(tmp_path, _SPEC + f"\n{_B3}\nx\n{_B3}   \n")
    still_open = _SPEC.replace("## Decided", f"{_B3}\n{_B3}python\n## Amendments\n{_B3}\n\n## Decided")
    assert _hash(tmp_path, still_open.replace("30 days", "60 days")) != _hash(tmp_path, still_open)


def test_an_unclosed_fence_runs_to_the_end(tmp_path):
    """As in CommonMark, so an amendment after it is hashed: the change is loud, never silent."""
    spec = _SPEC + f"\n{_B3}\nunclosed\n"
    assert _hash(tmp_path, spec + "\n## Amendments\n\n- AM-1 ...\n") != _hash(tmp_path, spec)


@pytest.mark.parametrize("inner", [_B3 * 2, _T3], ids=["other-character", "shorter-run"])
def test_a_shorter_or_other_marker_does_not_close_a_fence(tmp_path, inner):
    """Inside a long tilde fence, a backtick line or a shorter run closes nothing."""
    example = f"{_T3 * 2}\n{inner}\n## Amendments\n{_T3 * 2}\n"
    spec = _SPEC.replace("## Decided", example + "\n## Decided")
    assert _hash(tmp_path, spec.replace("30 days", "60 days")) != _hash(tmp_path, spec)


def test_approval_report_is_saved_for_every_handoff():
    """Every reader of frozen ids keys on <stem>.approved.analysis.md existing, so an
    issue handoff saves it too and posts a copy, never instead of saving it."""
    body = " ".join(SKILL.read_text(encoding="utf-8").split())
    step7 = body[body.index("7. **Save the final analysis report**") : body.index("8. **Write intent")]
    assert "approved.analysis.md" in step7
    assert "save the report beside the spec all the same" in step7
    assert "post a verbatim copy as a comment on the issue" in step7
    assert "instead" not in step7
