"""Structure checks for the root README: links, anchors and the Contents list.

These tests read STRUCTURE only. They never look at the README's wording, so a
prose edit cannot fail them. What does fail them: a renamed or removed heading
that something still links to, a linked file that moved, or a Contents entry
that is out of document order.

The three README tests run the checker over the real file. The last two run it
over small synthetic documents with known defects, so a checker that stops
finding anything fails here and cannot pass the real file by looking at nothing.

Anchors follow GitHub's heading rule: lowercase, drop everything except word
characters, hyphens and spaces, then turn each space into a hyphen; a repeated
heading gets ``-1``, ``-2``. An explicit ``id`` or ``name`` attribute also counts.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_README = _REPO_ROOT / "README.md"

_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$")
_EXPLICIT_ID = re.compile(r"""<[a-zA-Z][^>]*\s(?:id|name)=["']([^"']+)["']""")
_MD_LINK = re.compile(r"\]\(\s*<?([^)\s>]+)")
_HTML_LINK = re.compile(r"""\s(?:href|src)=["']([^"']+)["']""")
_INLINE_CODE = re.compile(r"`[^`]*`")
_LIST_ITEM = re.compile(r"^\s*[-*+]\s+")
_EXTERNAL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")  # http:, https:, mailto:, ...
_CONTENTS_TITLES = {"contents", "table of contents"}


def _prose_lines(text: str) -> list[tuple[int, str]]:
    """(line number, line) for every line outside a fenced code block."""
    out: list[tuple[int, str]] = []
    in_fence = False
    for number, line in enumerate(text.splitlines(), start=1):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            out.append((number, line))
    return out


def _slug(heading: str) -> str:
    plain = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # [text](url) -> text
    plain = re.sub(r"<[^>]+>", "", plain)
    return re.sub(r"[^\w\- ]", "", plain.lower()).replace(" ", "-")


def _headings(text: str) -> list[tuple[int, str]]:
    """(line number, heading text) for every ATX heading outside code fences."""
    found = []
    for number, line in _prose_lines(text):
        match = _HEADING.match(line)
        if match:
            found.append((number, match.group(1)))
    return found


def _anchors(text: str) -> dict[str, int]:
    """Every anchor the document defines, mapped to the line that defines it."""
    anchors: dict[str, int] = {}
    seen: dict[str, int] = {}
    for number, heading in _headings(text):
        base = _slug(heading)
        count = seen.get(base, 0)
        seen[base] = count + 1
        anchors.setdefault(base if count == 0 else f"{base}-{count}", number)
    for number, line in _prose_lines(text):
        for explicit in _EXPLICIT_ID.findall(line):
            anchors.setdefault(explicit, number)
    return anchors


def _link_targets(text: str) -> list[tuple[int, str]]:
    """(line number, target) for every Markdown or HTML link and image."""
    targets = []
    for number, line in _prose_lines(text):
        line = _INLINE_CODE.sub("", line)
        for target in _MD_LINK.findall(line) + _HTML_LINK.findall(line):
            targets.append((number, target))
    return targets


def _broken_anchors(text: str) -> list[str]:
    anchors = _anchors(text)
    return [
        f"line {number}: {target} matches no heading or explicit id"
        for number, target in _link_targets(text)
        if target.startswith("#") and target[1:] not in anchors
    ]


def _broken_relative_links(text: str, root: Path) -> list[str]:
    problems = []
    for number, target in _link_targets(text):
        if target.startswith("#") or _EXTERNAL.match(target):
            continue
        path = target.split("#", 1)[0].split("?", 1)[0]
        if not (root / path).exists():
            problems.append(f"line {number}: {target} does not exist in the repo")
    return problems


def _contents_entries(text: str) -> list[tuple[int, str]]:
    """(line number, first in-page anchor or '') for each item of the Contents list."""
    lines = _prose_lines(text)
    start = next(
        (
            index
            for index, (_, line) in enumerate(lines)
            if (match := _HEADING.match(line)) and match.group(1).lower() in _CONTENTS_TITLES
        ),
        None,
    )
    if start is None:
        return []
    entries = []
    for number, line in lines[start + 1 :]:
        if not line.strip():
            continue
        if not _LIST_ITEM.match(line):
            break
        in_page = [t for _, t in _link_targets(line) if t.startswith("#")]
        entries.append((number, in_page[0][1:] if in_page else ""))
    return entries


def _contents_problems(text: str) -> list[str]:
    entries = _contents_entries(text)
    if not entries:
        return ["no Contents list found (a heading named Contents followed by a list)"]
    anchors = _anchors(text)
    problems = []
    previous = 0
    for number, anchor in entries:
        if not anchor:
            problems.append(f"line {number}: Contents entry has no in-page link")
        elif anchor not in anchors:
            problems.append(f"line {number}: Contents entry #{anchor} matches no heading")
        elif anchors[anchor] <= previous:
            problems.append(
                f"line {number}: Contents entry #{anchor} is out of document order "
                f"(its section starts at line {anchors[anchor]}, the previous entry's "
                f"at line {previous})"
            )
        else:
            previous = anchors[anchor]
    return problems


def _readme() -> str:
    return _README.read_text(encoding="utf-8")


def test_in_page_anchors_resolve():
    problems = _broken_anchors(_readme())
    assert not problems, "README.md in-page links with no target:\n" + "\n".join(problems)


def test_relative_links_resolve():
    problems = _broken_relative_links(_readme(), _REPO_ROOT)
    assert not problems, "README.md links to missing files:\n" + "\n".join(problems)


def test_contents_entries_match_headings_in_order():
    problems = _contents_problems(_readme())
    assert not problems, "README.md Contents list:\n" + "\n".join(problems)


_SAMPLE = """\
# Title

## Contents

- [First](#first-part)
- [Second · more](#whats-second)

---

## First part

See [the guide](docs/guide.md#top), [a gap](docs/absent.md) and [nowhere](#gone).
An image: <img src="docs/pic.svg"> and a site: [out](https://example.com/x).

```
[inside a fence](#not-a-link) and [a file](not/a/file.md)
```

<a id="whats-second"></a>

## What's second?

## First part
"""


def test_checker_finds_planted_defects(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("x", encoding="utf-8")

    assert _anchors(_SAMPLE) == {
        "title": 1,
        "contents": 3,
        "first-part": 10,
        "whats-second": 21,
        "first-part-1": 23,
    }
    assert _broken_anchors(_SAMPLE) == ["line 12: #gone matches no heading or explicit id"]
    assert _broken_relative_links(_SAMPLE, tmp_path) == [
        "line 12: docs/absent.md does not exist in the repo",
        "line 13: docs/pic.svg does not exist in the repo",
    ]
    assert _contents_problems(_SAMPLE) == []


def test_checker_rejects_a_bad_contents_list():
    swapped = _SAMPLE.replace(
        "- [First](#first-part)\n- [Second · more](#whats-second)",
        "- [Second · more](#whats-second)\n- [First](#first-part)\n- [Gone](#gone)\n- plain",
    )
    assert swapped != _SAMPLE
    assert _contents_problems(swapped) == [
        "line 6: Contents entry #first-part is out of document order "
        "(its section starts at line 12, the previous entry's at line 23)",
        "line 7: Contents entry #gone matches no heading",
        "line 8: Contents entry has no in-page link",
    ]
    assert _contents_problems("## Something else\n\n- [a](#something-else)\n") == [
        "no Contents list found (a heading named Contents followed by a list)"
    ]
