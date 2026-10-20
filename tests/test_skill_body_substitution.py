"""No `$<digit>` in a skill or command body (#3037).

When Claude Code loads a skill or command with arguments, it substitutes them
into the body before the model reads it. MEASURED on Claude Code 2.1.299, slash
load with arguments `AA BB CC ...`: `$0` became `AA` and `$4` became `EE`
(0-indexed, multi-digit too: `$10`), `$0.015` became `AA.015`, and backticks did
not protect `` `$4` ``. `\\$0` rendered as `$0`: the backslash is consumed, with or
without arguments, so it is no escape for a file other code also reads raw.
Forms that survived: `$ 0`, `${0}`, `$VAR`, `$NF`.

The silent case is the one that bit: `youtube-fetch`'s `awk '!seen[$0]++'`
rendered as `awk '!seen[Get]++'`, which keeps only the first transcript line and
still exits 0.

The rule is stricter than the loader on purpose (`>$10M` is flagged though the
measured renderer left it alone), so it does not depend on the loader's exact
lookahead. `$ARGUMENTS` is allowed: substituting it is the documented feature.

Covered: every `SKILL.md` under the three skill roots, nested ones included.
That takes in tier-2 skills the repo's own loaders read raw, because an install
can link them into `~/.claude/skills`, where the Skill tool loads them. Also
every command. Not
covered: agent definitions (no arguments) and `references/*.md` (read back raw).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.conftest import private_module

_ROOT = Path(__file__).resolve().parent.parent

# The instruction-file names a skill directory may use, in precedence order,
# imported from the catalog generator rather than restated (its own comment
# asks consumers to import it).
_MARKERS = private_module(
    "generate_skill_catalog_for_substitution_test", _ROOT / "scripts" / "generate_skill_catalog.py"
).SKILL_MARKERS

#: (root, "skills" or a glob, the fewest files the root may hold).
_ROOTS = (
    (".claude/skills", "skills", 20),
    ("src/genesis/skills", "skills", 15),
    (".agents/skills", "skills", 1),
    (".claude/commands", "*.md", 10),
)

_SUBSTITUTED = re.compile(r"\$[0-9]")

#: (repo-relative path, stripped line text) -> why that `$<digit>` is safe. Keyed
#: by text, not line number, so an edit above it cannot move the entry onto a
#: different line. Empty at introduction; an entry needs a reason a reviewer can
#: check.
_ALLOWED: dict[tuple[str, str], str] = {}

_FIX = "write `$ 0` in awk, `${N}` in shell, words in prose, and prices as 'N USD'"


def _files(root: str, pattern: str) -> list[Path]:
    if pattern != "skills":
        return sorted((_ROOT / root).glob(pattern))
    # Each skill directory, at any depth, contributes the instruction file the
    # catalog would pick: the first marker present. A references/ directory
    # holds a skill's reference files, never a skill.
    found = []
    for d in sorted(p for p in (_ROOT / root).rglob("*") if p.is_dir()):
        if "references" in d.relative_to(_ROOT / root).parts:
            continue
        marker = next((d / m for m in _MARKERS if (d / m).is_file()), None)
        if marker is not None:
            found.append(marker)
    return found


@pytest.mark.parametrize(("root", "pattern", "floor"), _ROOTS, ids=[r[0] for r in _ROOTS])
def test_each_root_is_found(root, pattern, floor):
    # Guard the guard, per root: a broken glob for one root would otherwise
    # pass on the others' files and make its own check vacuous.
    found = _files(root, pattern)
    assert len(found) >= floor, f"{root}/{pattern}: {len(found)} files, expected >= {floor}"


def _hits(path: Path) -> list[tuple[int, str]]:
    rel = path.relative_to(_ROOT).as_posix()
    return [
        (n, line.strip())
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _SUBSTITUTED.search(line) and (rel, line.strip()) not in _ALLOWED
    ]


def test_no_skill_or_command_body_carries_a_dollar_digit():
    bad = [
        f"{path.relative_to(_ROOT).as_posix()}:{n}: {text[:100]}"
        for root, pattern, _ in _ROOTS
        for path in _files(root, pattern)
        for n, text in _hits(path)
    ]
    assert not bad, (
        "argument substitution rewrites `$<digit>` when a skill or command is loaded "
        f"with arguments (#3037); {_FIX}:\n" + "\n".join(bad)
    )


def test_every_allowlist_entry_still_points_at_a_dollar_digit():
    for (rel, text), reason in _ALLOWED.items():
        assert reason.strip(), f"{rel}: allowlist entry has no reason: {text!r}"
        assert _SUBSTITUTED.search(text), f"{rel}: allowlist entry has no `$<digit>`: {text!r}"
        lines = {line.strip() for line in (_ROOT / rel).read_text(encoding="utf-8").splitlines()}
        assert text in lines, f"{rel}: stale allowlist entry, line no longer present: {text!r}"


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        ("awk '!seen[$0]++'", True),
        ("field `$4`", True),
        ("$10 and $12", True),
        ("about $0.015 per call", True),
        ("~$0.01/M tokens", True),
        ("escaped \\$0", True),
        (">$10M", True),  # stricter than the loader, by design
        ("awk '!seen[$ 0]++'", False),
        ("${0} and ${4}", False),
        ('"$VTT" and $VAR', False),
        ("awk '{print $NF}'", False),
        ("Read `$ARGUMENTS` as prose", False),
        ("0.015 USD", False),
    ],
)
def test_the_rule_matches_the_measured_renderer(text, flagged):
    assert bool(_SUBSTITUTED.search(text)) is flagged


def test_every_skill_marker_spelling_is_covered(tmp_path, monkeypatch):
    # A skill whose instruction file is skill.md or README.md is checked too.
    assert set(_MARKERS) >= {"SKILL.md", "skill.md", "README.md"}
    for i, marker in enumerate(_MARKERS):
        (tmp_path / "skills" / f"s{i}").mkdir(parents=True)
        (tmp_path / "skills" / f"s{i}" / marker).write_text("cost $1 here\n")
    (tmp_path / "skills" / "s0" / "references").mkdir()
    (tmp_path / "skills" / "s0" / "references" / "README.md").write_text("not a skill $1\n")
    monkeypatch.setitem(globals(), "_ROOT", tmp_path)
    found = [p.relative_to(tmp_path).as_posix() for p in _files("skills", "skills")]
    assert found == [f"skills/s{i}/{m}" for i, m in enumerate(_MARKERS)]
