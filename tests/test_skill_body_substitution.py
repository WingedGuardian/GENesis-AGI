"""No `$<digit>` in a skill or command body (#3037).

When Claude Code loads a skill or command with arguments, it substitutes them
into the body before the model reads it. MEASURED on Claude Code 2.1.299, slash
load with arguments `AA BB CC ...`: `$0` became `AA` and `$4` became `EE`
(0-indexed, multi-digit too: `$10`), `$0.015` became `AA.015`, and backticks did
not protect `` `$4` ``. `\\$0` rendered as `$0`: the backslash is consumed, with or
without arguments, so it is no escape for a file other code also reads raw.
Forms that survived: `$ 0`, `${0}`, `$VAR`, `$NF` (`$name` is substituted too when
the frontmatter declares that named argument; no shipped skill or command does).

The silent case is the one that bit: `youtube-fetch`'s `awk '!seen[$0]++'`
rendered as `awk '!seen[Get]++'`, which keeps only the first transcript line and
still exits 0.

The rule is stricter than the loader on purpose (`>$10M` is flagged though the
measured renderer left it alone), so it does not depend on the loader's exact
lookahead. `$ARGUMENTS` is allowed: substituting it is the documented feature.

Covered: every skill under the three skill roots, found by the skill catalog's
own traversal (containers, nested skills, the marker precedence), so this test
and the catalog cannot disagree about what a skill is. That takes in tier-2
skills the repo's own loaders read raw, because an install can link them into
`~/.claude/skills`, where the Skill tool loads them. Also every command,
namespaced subdirectories included. Not covered: agent definitions (no
arguments), `references/*.md` and other support files (read back raw).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.conftest import private_module

_ROOT = Path(__file__).resolve().parent.parent

# The catalog generator decides what a skill is; imported rather than restated
# (its own comment asks consumers to import SKILL_MARKERS).
_CATALOG = private_module(
    "generate_skill_catalog_for_substitution_test", _ROOT / "scripts" / "generate_skill_catalog.py"
)
_MARKERS = _CATALOG.SKILL_MARKERS

#: (root, "skills" or a glob, the fewest files the root may hold).
_ROOTS = (
    (".claude/skills", "skills", 20),
    ("src/genesis/skills", "skills", 15),
    (".agents/skills", "skills", 1),
    (".claude/commands", "**/*.md", 10),
)

_SUBSTITUTED = re.compile(r"\$[0-9]")

#: (repo-relative path, stripped line text) -> why that `$<digit>` is safe, or
#: why it is an intended positional placeholder. Keyed by text, not line number,
#: so an edit above it cannot move the entry onto a different line; the text must
#: occur exactly once in its file, so a copy of it elsewhere is still flagged.
#: Empty at introduction; an entry needs a reason a reviewer can check.
_ALLOWED: dict[tuple[str, str], str] = {}

_FIX = (
    "write `$ 0` in awk, `${N}` in shell, words in prose, and prices as 'N USD'; "
    "if the line is an intended positional placeholder, add it to _ALLOWED with the reason"
)


def _files(root: str, pattern: str) -> list[Path]:
    if pattern != "skills":
        return sorted((_ROOT / root).glob(pattern))
    # Each skill the catalog finds contributes the instruction file the catalog
    # reads for it: the first marker present (a name-only entry has none).
    found = []
    for info in _CATALOG._scan_tier(_ROOT / root, 0, None):
        d = Path(info["path"])
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
    if pattern == "skills":
        # Independent of the catalog: every <name>/SKILL.md, the layout Claude
        # Code itself loads, must be among the files checked.
        direct = set((_ROOT / root).glob("*/SKILL.md"))
        assert direct <= set(found), sorted(str(p) for p in direct - set(found))


def test_no_plugin_ships_skills_or_commands_unchecked():
    # Claude Code also loads <plugin>/skills/*/SKILL.md and <plugin>/commands/;
    # the first plugin to ship one must be added to _ROOTS.
    shipped = sorted(
        p.relative_to(_ROOT).as_posix()
        for kind in ("skills", "commands")
        for p in (_ROOT / "plugins").glob(f"*/{kind}")
    )
    assert not shipped, f"add these plugin roots to _ROOTS: {shipped}"


def _hits(path: Path) -> list[tuple[int, str]]:
    rel = path.relative_to(_ROOT).as_posix()
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    return [
        (n, text)
        for n, text in enumerate(lines, 1)
        if _SUBSTITUTED.search(text) and not ((rel, text) in _ALLOWED and lines.count(text) == 1)
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
        lines = [line.strip() for line in (_ROOT / rel).read_text(encoding="utf-8").splitlines()]
        assert lines.count(text) == 1, (
            f"{rel}: allowlist entry must match exactly one line, found {lines.count(text)}: {text!r}"
        )


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


def test_container_support_dirs_are_not_skills(tmp_path, monkeypatch):
    # A container's README and its docs/ README describe the collection, not a
    # skill (the catalog's own rule); only the nested skill is checked.
    (tmp_path / "skills" / "pack" / "inner").mkdir(parents=True)
    (tmp_path / "skills" / "pack" / "README.md").write_text("collection, $1 each\n")
    (tmp_path / "skills" / "pack" / "inner" / "SKILL.md").write_text("x\n")
    (tmp_path / "skills" / "pack" / "docs").mkdir()
    (tmp_path / "skills" / "pack" / "docs" / "README.md").write_text("costs $5\n")
    monkeypatch.setitem(globals(), "_ROOT", tmp_path)
    found = [p.relative_to(tmp_path).as_posix() for p in _files("skills", "skills")]
    assert found == ["skills/pack/inner/SKILL.md"]


def test_namespaced_commands_are_covered(tmp_path, monkeypatch):
    (tmp_path / "commands" / "ci").mkdir(parents=True)
    (tmp_path / "commands" / "top.md").write_text("x\n")
    (tmp_path / "commands" / "ci" / "build.md").write_text("x\n")
    monkeypatch.setitem(globals(), "_ROOT", tmp_path)
    pattern = dict((r[0], r[1]) for r in _ROOTS)[".claude/commands"]
    found = [p.relative_to(tmp_path).as_posix() for p in _files("commands", pattern)]
    assert found == ["commands/ci/build.md", "commands/top.md"]


def test_an_allowlisted_line_copied_elsewhere_in_its_file_is_flagged(tmp_path, monkeypatch):
    line = "run it as /cmd $1"
    path = tmp_path / "skills" / "s" / "SKILL.md"
    path.parent.mkdir(parents=True)
    monkeypatch.setitem(globals(), "_ROOT", tmp_path)
    monkeypatch.setitem(
        globals(), "_ALLOWED", {("skills/s/SKILL.md", line): "intended placeholder"}
    )
    path.write_text(f"{line}\n")
    assert _hits(path) == []
    path.write_text(f"{line}\nother\n{line}\n")
    assert [n for n, _ in _hits(path)] == [1, 3]
