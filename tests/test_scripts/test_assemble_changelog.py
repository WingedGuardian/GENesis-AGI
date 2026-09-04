"""The changelog fragment assembler.

The point of `changelog.d/` is that two branches never write the same filename,
so nothing collides. That property is worth nothing if a fragment can be
silently skipped — a skipped fragment is a changelog entry that never ships and
that nobody notices, because the release section still looks plausible. So the
tests below lean on the classification being TOTAL: every entry in the directory
is either a fragment, the README, or an error.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "assemble_changelog.py"
_spec = importlib.util.spec_from_file_location("assemble_changelog", _MODULE_PATH)
assert _spec and _spec.loader
ac = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ac)


def _fragment(directory: Path, name: str, body: str = "- **A thing.** Detail.") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body + "\n")
    return path


CHANGELOG_SKELETON = """# Changelog

All notable changes are documented here.

---

## [Unreleased]

### Fixed

- an entry that was already here

## [v3.0b9] - 2026-01-01

### Added

- something released
"""


# ── naming contract ──────────────────────────────────────────────────────────


def test_a_well_formed_name_parses_to_its_three_parts() -> None:
    assert ac.parse_fragment_name("20260904210000-fixed-changelog-collisions.md") == (
        "20260904210000",
        "Fixed",
        "changelog-collisions",
    )


@pytest.mark.parametrize(
    "name",
    [
        "fixed-no-timestamp.md",  # no timestamp at all
        "2026090421000-fixed-short.md",  # 13 digits
        "202609042100000-fixed-long.md",  # 15 digits
        "20260904210000-fixed-Upper.md",  # slug must be lowercase
        "20260904210000-fixed-trailing-.md",  # dangling separator
        "20260904210000-fixed.md",  # no slug
        "20260904210000-fixed-thing.txt",  # not markdown
        "20260904210000-fixed-thing.md.bak",  # not markdown, sneakier
    ],
)
def test_a_malformed_name_is_an_error_not_a_skip(name: str) -> None:
    with pytest.raises(ac.FragmentError):
        ac.parse_fragment_name(name)


def test_fourteen_digits_is_not_enough_to_be_a_timestamp() -> None:
    """``20261301000000`` is fourteen digits and is not a date.

    Caught here rather than at release, where it would sort into the wrong
    place and nobody would look.
    """
    with pytest.raises(ac.FragmentError, match="not a real UTC timestamp"):
        ac.parse_fragment_name("20261301000000-fixed-bad-month.md")


def test_an_unknown_category_is_rejected_with_the_valid_set() -> None:
    with pytest.raises(ac.FragmentError, match="unknown category"):
        ac.parse_fragment_name("20260904210000-improved-nice-try.md")


# ── directory classification is TOTAL ────────────────────────────────────────


def test_every_keep_a_changelog_category_is_accepted(tmp_path: Path) -> None:
    d = tmp_path / "changelog.d"
    for key in ("added", "changed", "deprecated", "removed", "fixed", "security"):
        _fragment(d, f"20260904210000-{key}-thing.md")
    assert len(ac.collect_fragments(d)) == 6


def test_a_stray_file_is_an_error_rather_than_being_ignored(tmp_path: Path) -> None:
    """The failure this whole design must not have: an entry that never ships.

    A glob that skips what it does not recognise produces a release section that
    looks complete and is not.
    """
    d = tmp_path / "changelog.d"
    _fragment(d, "20260904210000-fixed-real.md")
    (d / "notes.md").write_text("- I meant this to be an entry\n")
    with pytest.raises(ac.FragmentError, match="not a valid fragment name"):
        ac.collect_fragments(d)


def test_the_readme_is_the_only_exempt_file(tmp_path: Path) -> None:
    d = tmp_path / "changelog.d"
    _fragment(d, "20260904210000-fixed-real.md")
    (d / "README.md").write_text("# changelog.d\n")
    assert len(ac.collect_fragments(d)) == 1


def test_a_nested_directory_is_rejected(tmp_path: Path) -> None:
    """A glob would skip it, and its entries would never ship."""
    d = tmp_path / "changelog.d"
    _fragment(d, "20260904210000-fixed-real.md")
    (d / "subdir").mkdir()
    with pytest.raises(ac.FragmentError, match="flat"):
        ac.collect_fragments(d)


def test_an_empty_fragment_is_rejected(tmp_path: Path) -> None:
    d = tmp_path / "changelog.d"
    _fragment(d, "20260904210000-fixed-empty.md", body="   ")
    with pytest.raises(ac.FragmentError, match="empty"):
        ac.collect_fragments(d)


def test_content_that_is_not_a_bullet_is_rejected(tmp_path: Path) -> None:
    """It is spliced verbatim, so a heading here would corrupt the changelog."""
    d = tmp_path / "changelog.d"
    _fragment(d, "20260904210000-fixed-prose.md", body="### Fixed\n\nsome prose")
    with pytest.raises(ac.FragmentError, match="must start with a Markdown bullet"):
        ac.collect_fragments(d)


def test_a_missing_directory_is_not_an_error(tmp_path: Path) -> None:
    assert ac.collect_fragments(tmp_path / "nope") == []


# ── ordering ─────────────────────────────────────────────────────────────────


def test_fragments_sort_by_category_then_timestamp(tmp_path: Path) -> None:
    d = tmp_path / "changelog.d"
    _fragment(d, "20260904220000-fixed-later.md")
    _fragment(d, "20260904210000-fixed-earlier.md")
    _fragment(d, "20260904230000-added-newest.md")
    got = [(c, p.name) for _ts, c, p in ac.collect_fragments(d)]
    assert got == [
        ("Added", "20260904230000-added-newest.md"),
        ("Fixed", "20260904210000-fixed-earlier.md"),
        ("Fixed", "20260904220000-fixed-later.md"),
    ]


# ── splicing into CHANGELOG.md ───────────────────────────────────────────────


def test_a_missing_category_is_created(tmp_path: Path) -> None:
    out = ac.splice(CHANGELOG_SKELETON, {"Added": ["- **Fresh.** Detail."]})
    assert "### Added" in out
    assert "- **Fresh.** Detail." in out


def test_entries_land_under_unreleased_and_not_in_a_released_section() -> None:
    """The bug that would ship a fix under the wrong version heading."""
    out = ac.splice(CHANGELOG_SKELETON, {"Added": ["- **Fresh.** Detail."]})
    unreleased_at = out.index("## [Unreleased]")
    released_at = out.index("## [v3.0b9]")
    entry_at = out.index("- **Fresh.** Detail.")
    assert unreleased_at < entry_at < released_at


def test_a_released_section_is_left_untouched() -> None:
    out = ac.splice(CHANGELOG_SKELETON, {"Added": ["- **Fresh.** Detail."]})
    tail = out[out.index("## [v3.0b9]") :]
    assert "- something released" in tail
    assert "Fresh" not in tail


def test_a_changelog_without_an_unreleased_heading_is_an_error() -> None:
    with pytest.raises(ac.FragmentError, match="no '## \\[Unreleased\\]'"):
        ac.splice("# Changelog\n\n## [v1.0]\n\n### Added\n\n- a thing\n", {"Fixed": ["- x"]})


def test_multi_paragraph_entries_survive_intact() -> None:
    body = "- **Title.** First paragraph.\n\n  Second paragraph, indented."
    out = ac.splice(CHANGELOG_SKELETON, {"Fixed": [body]})
    assert body in out


def test_dry_run_piped_to_a_short_reader_does_not_traceback(tmp_path: Path) -> None:
    """`--dry-run | head` is the obvious way to use this.

    Without handling, the reader closing the pipe raises BrokenPipeError and the
    interpreter prints a traceback on the way out — noise that reads like the
    tool failed when it did exactly what was asked.
    """
    import subprocess

    proc = subprocess.run(
        f"python3 {_MODULE_PATH} --dry-run | head -1",
        shell=True,
        capture_output=True,
        text=True,
        cwd=_MODULE_PATH.parent.parent,
    )
    assert proc.returncode == 0
    assert "BrokenPipeError" not in proc.stderr
    assert proc.stderr.strip() == "", f"unexpected stderr: {proc.stderr!r}"


# ── placement, graded against the REAL file rather than a fixture ─────────────


REPO_CHANGELOG = _MODULE_PATH.parent.parent / "CHANGELOG.md"


@pytest.mark.parametrize(
    "category", ["Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"]
)
def test_every_category_lands_near_the_top_of_the_real_changelog(category: str) -> None:
    """A fixture cannot catch this; only the real file can.

    MEASURED before the fix: [Unreleased] in this repository spans 3,203 lines
    with 40 category headings, and inserting under the first matching heading
    put a Security entry 810 lines in and a Removed entry 2,267 lines in —
    below unrelated older content, invisible to whoever cuts the release. The
    small fixture said the placement was correct because in a 12-line section
    the first heading IS the top.
    """
    text = REPO_CHANGELOG.read_text()
    out = ac.splice(text, {category: [f"- **Probe {category}.** body"]})
    lines = out.splitlines()
    start = lines.index("## [Unreleased]")
    at = lines.index(f"- **Probe {category}.** body")
    assert at - start < 100, (
        f"{category} entry landed {at - start} lines into [Unreleased]; "
        "placement must not depend on the section's accumulated batch structure"
    )


def test_splicing_the_real_changelog_preserves_every_existing_line() -> None:
    """The fold must ADD only — never move, drop or reorder existing content.

    Checked as a subsequence rather than an equality, because the fold does
    legitimately insert a new category group (and its heading) at the top.
    """
    text = REPO_CHANGELOG.read_text()
    out = ac.splice(text, {"Fixed": ["- **Probe.** body"]})
    before = [ln for ln in text.splitlines() if ln.strip()]
    after = iter(ln for ln in out.splitlines() if ln.strip())
    missing = [ln for ln in before if not any(cand == ln for cand in after)]
    assert not missing, f"{len(missing)} existing line(s) lost or reordered: {missing[:3]}"


# ── the body validator: line 1 was never enough ──────────────────────────────


def test_a_heading_below_line_one_is_rejected(tmp_path: Path) -> None:
    """The corruption is permanent and compounds, so it must never be written.

    MEASURED on the old validator: a body starting with a legal bullet but
    containing `## ` later injected that heading into [Unreleased]; the next
    assembly then treated it as the section's end, orphaning the pre-existing
    entries below it into a phantom release. Exit 0 both times.
    """
    d = tmp_path / "changelog.d"
    _fragment(d, "20260904210000-fixed-x.md", body="- a bullet\n\n## Injected\n\n- more")
    with pytest.raises(ac.FragmentError, match="section heading"):
        ac.collect_fragments(d)


def test_a_top_level_code_fence_is_rejected(tmp_path: Path) -> None:
    d = tmp_path / "changelog.d"
    _fragment(d, "20260904210000-fixed-x.md", body="- a bullet\n\n```\ncode\n```")
    with pytest.raises(ac.FragmentError, match="code fence"):
        ac.collect_fragments(d)


def test_an_indented_code_fence_is_allowed(tmp_path: Path) -> None:
    """Indented, it stays inside the bullet — which is the documented remedy."""
    d = tmp_path / "changelog.d"
    _fragment(d, "20260904210000-fixed-x.md", body="- a bullet\n\n  ```\n  code\n  ```")
    assert len(ac.collect_fragments(d)) == 1


# ── local debris and unreadable files ────────────────────────────────────────


@pytest.mark.parametrize("name", [".DS_Store", ".gitkeep", ".x.md.swp"])
def test_dot_files_are_skipped_not_fatal(tmp_path: Path, name: str) -> None:
    """Refusing to cut a release over untracked editor debris is the wrong
    failure, and it would diverge CI (clean checkout, green) from a
    maintainer's own run (their debris, red)."""
    d = tmp_path / "changelog.d"
    _fragment(d, "20260904210000-fixed-real.md")
    (d / name).write_text("junk")
    assert len(ac.collect_fragments(d)) == 1


def test_an_unreadable_fragment_reports_instead_of_tracebacking(tmp_path: Path) -> None:
    d = tmp_path / "changelog.d"
    p = _fragment(d, "20260904210000-fixed-x.md")
    p.write_bytes(b"- valid start \xff\xfe not utf-8")
    with pytest.raises(ac.FragmentError, match="cannot read fragment"):
        ac.collect_fragments(d)


# ── main(): the destructive path, previously untested ────────────────────────


def _run_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    d = tmp_path / "changelog.d"
    _fragment(d, "20260904210000-fixed-thing.md", body="- **Landed.** body")
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(CHANGELOG_SKELETON)
    monkeypatch.setattr(ac, "FRAGMENT_DIR", d)
    monkeypatch.setattr(ac, "CHANGELOG", cl)
    return ac.main(argv)


def test_main_writes_the_entry_and_consumes_the_fragments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _run_main(tmp_path, monkeypatch, []) == 0
    assert "- **Landed.** body" in (tmp_path / "CHANGELOG.md").read_text()
    assert list((tmp_path / "changelog.d").iterdir()) == []


def test_main_dry_run_consumes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run_main(tmp_path, monkeypatch, ["--dry-run"]) == 0
    assert "- **Landed.** body" in capsys.readouterr().out
    assert (tmp_path / "CHANGELOG.md").read_text() == CHANGELOG_SKELETON
    assert len(list((tmp_path / "changelog.d").iterdir())) == 1


def test_main_check_consumes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _run_main(tmp_path, monkeypatch, ["--check"]) == 0
    assert (tmp_path / "CHANGELOG.md").read_text() == CHANGELOG_SKELETON
    assert len(list((tmp_path / "changelog.d").iterdir())) == 1


def test_a_failed_write_leaves_every_fragment_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fragments are the only copy of these entries.

    A write that dies half-way must never be followed by deleting them, or the
    entries are gone with nothing to recover them from.
    """
    d = tmp_path / "changelog.d"
    _fragment(d, "20260904210000-fixed-thing.md", body="- **Landed.** body")
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(CHANGELOG_SKELETON)
    monkeypatch.setattr(ac, "FRAGMENT_DIR", d)
    monkeypatch.setattr(ac, "CHANGELOG", cl)

    def boom(self, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    with pytest.raises(OSError):
        ac.main([])
    assert len(list(d.iterdir())) == 1, "fragments were deleted despite a failed write"
    assert cl.read_text() == CHANGELOG_SKELETON


def test_the_changelog_is_replaced_atomically_never_written_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kill mid-write must not leave a truncated CHANGELOG.md.

    Distinguishes atomic from in-place: with the temp-then-replace form, a
    failing `replace` leaves the original completely untouched. Written in
    place, the same failure point does not exist and the file has already been
    overwritten — which is what makes this test able to fail.
    """
    d = tmp_path / "changelog.d"
    _fragment(d, "20260904210000-fixed-thing.md", body="- **Landed.** body")
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(CHANGELOG_SKELETON)
    monkeypatch.setattr(ac, "FRAGMENT_DIR", d)
    monkeypatch.setattr(ac, "CHANGELOG", cl)

    def boom(self, *a, **k):
        raise OSError("rename failed")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError):
        ac.main([])
    assert cl.read_text() == CHANGELOG_SKELETON, (
        "CHANGELOG.md was modified despite the replace failing — the write is "
        "not atomic, so a kill mid-write can leave it truncated"
    )
    assert len(list(d.iterdir())) == 1, "fragments deleted despite a failed write"
    assert not (tmp_path / "CHANGELOG.md.tmp").exists(), "left a half-finished twin"
