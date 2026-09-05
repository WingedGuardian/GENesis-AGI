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
import os
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
    got = [(c, p.name) for _ts, c, p, _raw in ac.collect_fragments(d)]
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
    with pytest.raises(ac.FragmentError, match="Markdown heading"):
        ac.collect_fragments(d)


def test_a_top_level_code_fence_is_rejected(tmp_path: Path) -> None:
    d = tmp_path / "changelog.d"
    _fragment(d, "20260904210000-fixed-x.md", body="- a bullet\n\n```\ncode\n```")
    with pytest.raises(ac.FragmentError, match="code fence"):
        ac.collect_fragments(d)


def test_a_four_space_indented_code_fence_is_allowed(tmp_path: Path) -> None:
    """Four spaces is the documented remedy, so it must keep working.

    TWO is not, and used to be what the error message told people to do: under
    a wider bullet (`-   text`) a two-space indent is still column 2 of the
    document, and CommonMark treats up to three spaces as unindented anyway.
    """
    d = tmp_path / "changelog.d"
    _fragment(
        d, "20260904210000-fixed-x.md", body="- a bullet\n\n    ```\n    code\n    ```"
    )
    assert len(ac.collect_fragments(d)) == 1


def test_a_two_space_indented_fence_is_rejected(tmp_path: Path) -> None:
    """The old error message recommended exactly this, and it was wrong."""
    d = tmp_path / "changelog.d"
    _fragment(d, "20260904210000-fixed-x.md", body="- a bullet\n\n  ```\n  code\n")
    with pytest.raises(ac.FragmentError, match="code fence"):
        ac.collect_fragments(d)


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


# ── --check validates the TARGET, not just the inputs ────────────────────────


def test_check_rejects_a_changelog_with_no_unreleased_heading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Validating only the fragments certifies half the contract.

    A PR that renames or deletes the heading would leave the job green and the
    mismatch would surface at the first release — to whoever is cutting it
    rather than to whoever caused it.
    """
    d = tmp_path / "changelog.d"
    _fragment(d, "20260904210000-fixed-thing.md")
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text("# Changelog\n\n## [v1.0]\n\n### Fixed\n\n- released\n")
    monkeypatch.setattr(ac, "FRAGMENT_DIR", d)
    monkeypatch.setattr(ac, "CHANGELOG", cl)
    assert ac.main(["--check"]) == 2
    assert "no '## [Unreleased]' heading" in capsys.readouterr().err


def test_check_rejects_a_missing_changelog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    d = tmp_path / "changelog.d"
    _fragment(d, "20260904210000-fixed-thing.md")
    monkeypatch.setattr(ac, "FRAGMENT_DIR", d)
    monkeypatch.setattr(ac, "CHANGELOG", tmp_path / "CHANGELOG.md")
    assert ac.main(["--check"]) == 2
    assert "cannot read CHANGELOG.md" in capsys.readouterr().err


def test_a_directory_named_readme_md_cannot_hide_fragments(tmp_path: Path) -> None:
    """The name exemption must not out-rank the flat-directory rule.

    Exempting by name first let a DIRECTORY called README.md be skipped whole,
    hiding every fragment inside it while --check reported success — a hole
    straight through the total-classification guarantee.
    """
    d = tmp_path / "changelog.d"
    d.mkdir(parents=True)
    nested = d / "README.md"
    nested.mkdir()
    (nested / "20260904210000-fixed-lost.md").write_text("- lost entry\n")
    with pytest.raises(ac.FragmentError, match="flat"):
        ac.collect_fragments(d)
# ── --base: a fragment must not vanish outside the release fold ──────────────


def _repo_with_a_committed_fragment(
    tmp_path: Path, body: str = "- **Kept.** x"
) -> tuple[Path, str]:
    """A real git repo holding one fragment and a CHANGELOG, plus its base SHA.

    Built with plumbing rather than a porcelain commit so it depends on neither
    an ambient git identity nor this repository's own hooks.
    """
    import subprocess as sp

    repo = tmp_path / "repo"
    (repo / "changelog.d").mkdir(parents=True)
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "T",
        "GIT_AUTHOR_EMAIL": "t@e.invalid",
        "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@e.invalid",
    }

    def git(*args: str) -> str:
        proc = sp.run(["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True)
        return proc.stdout.strip()

    git("init", "--quiet", "-b", "trunk")
    (repo / "CHANGELOG.md").write_text(CHANGELOG_SKELETON)
    (repo / "changelog.d" / "20260904210000-fixed-kept.md").write_text(body + "\n")
    git("add", "-A")
    base = git("commit-tree", git("write-tree"), "-m", "base")
    git("update-ref", "refs/heads/trunk", base)
    return repo, base


def _patch_module_to(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ac, "REPO_ROOT", repo)
    monkeypatch.setattr(ac, "FRAGMENT_DIR", repo / "changelog.d")
    monkeypatch.setattr(ac, "CHANGELOG", repo / "CHANGELOG.md")


def test_deleting_a_fragment_without_touching_the_changelog_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Before assembly a fragment is the only copy of its entry.

    Every remaining file still validates, so without a base comparison the job
    passes while the entry silently disappears from the next release.
    """
    repo, base = _repo_with_a_committed_fragment(tmp_path)
    _patch_module_to(repo, monkeypatch)
    (repo / "changelog.d" / "20260904210000-fixed-kept.md").unlink()
    assert ac.main(["--check", "--base", base]) == 2
    assert "entries are not in CHANGELOG.md" in capsys.readouterr().err


def test_the_release_fold_may_delete_every_fragment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one legitimate deletion, recognised by its pairing, not by a token.

    A release fold removes the fragments AND rewrites CHANGELOG.md in the same
    change; that combination is the signal, so no override is needed.
    """
    repo, base = _repo_with_a_committed_fragment(tmp_path)
    _patch_module_to(repo, monkeypatch)
    (repo / "changelog.d" / "20260904210000-fixed-kept.md").unlink()
    (repo / "CHANGELOG.md").write_text(
        CHANGELOG_SKELETON.replace("### Fixed\n", "### Fixed\n\n- **Kept.** x\n")
    )
    assert ac.main(["--check", "--base", base]) == 0


def test_adding_a_fragment_is_always_fine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, base = _repo_with_a_committed_fragment(tmp_path)
    _patch_module_to(repo, monkeypatch)
    (repo / "changelog.d" / "20260904220000-added-new.md").write_text("- **New.** y\n")
    assert ac.main(["--check", "--base", base]) == 0


def test_an_unreadable_base_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A base that cannot be read means the rule checked NOTHING.

    A silent pass there is indistinguishable from a clean result — which is the
    failure mode this whole convention exists to avoid.
    """
    repo, _base = _repo_with_a_committed_fragment(tmp_path)
    _patch_module_to(repo, monkeypatch)
    assert ac.main(["--check", "--base", "0" * 40]) == 2
    assert "cannot read fragments at base" in capsys.readouterr().err
# ── the heading/fence checks are CLASSES, not the reported spellings ─────────


@pytest.mark.parametrize(
    "bad_line",
    [
        "# Injected",
        "## Injected",
        "### Fixed",
        "###### Deep",
        "#\tTab separated",
        "##",
    ],
)
def test_every_unindented_heading_level_is_rejected(tmp_path: Path, bad_line: str) -> None:
    """`## ` was the reported spelling; the class is any ATX heading.

    A `# ` or `### ` at column zero restructures the spliced result just as
    thoroughly, and enumerating only the reported case would have left the rest
    of the class shipping.
    """
    d = tmp_path / "changelog.d"
    _fragment(d, "20260904210000-fixed-x.md", body=f"- a bullet\n\n{bad_line}\n\n- more")
    with pytest.raises(ac.FragmentError, match="Markdown heading"):
        ac.collect_fragments(d)


@pytest.mark.parametrize("fence", ["```", "~~~", "````", "~~~~~"])
def test_both_fence_characters_are_rejected(tmp_path: Path, fence: str) -> None:
    """Markdown has two fence characters; only backticks were covered."""
    d = tmp_path / "changelog.d"
    _fragment(d, "20260904210000-fixed-x.md", body=f"- a bullet\n\n{fence}\ncode\n")
    with pytest.raises(ac.FragmentError, match="code fence"):
        ac.collect_fragments(d)


@pytest.mark.parametrize(
    "ok_line", ["    # indented", "    ```", "    ~~~", "not a heading"]
)
def test_four_space_indented_constructs_stay_allowed(
    tmp_path: Path, ok_line: str
) -> None:
    """Four-space indenting is the documented remedy, so it must keep working."""
    d = tmp_path / "changelog.d"
    _fragment(d, "20260904210000-fixed-x.md", body=f"- a bullet\n\n{ok_line}\n")
    assert len(ac.collect_fragments(d)) == 1


# ── the dotfile exemption stops at Markdown ──────────────────────────────────


def test_a_committed_dot_markdown_fragment_is_not_skipped(tmp_path: Path) -> None:
    """The debris exemption must not become a way to hide an entry.

    A committed `.20260904…-fixed-hidden.md` would be skipped in a clean CI
    checkout, --check would report success, and release assembly would omit the
    entry — the silent omission this directory's classification exists to stop.
    """
    d = tmp_path / "changelog.d"
    _fragment(d, "20260904210000-fixed-real.md")
    (d / ".20260904210000-fixed-hidden.md").write_text("- hidden entry\n")
    with pytest.raises(ac.FragmentError, match="not a valid fragment name"):
        ac.collect_fragments(d)


@pytest.mark.parametrize("debris", [".DS_Store", ".gitkeep", ".x.md.swp", ".foo.txt"])
def test_non_markdown_debris_is_still_skipped(tmp_path: Path, debris: str) -> None:
    d = tmp_path / "changelog.d"
    _fragment(d, "20260904210000-fixed-real.md")
    (d / debris).write_text("junk")
    assert len(ac.collect_fragments(d)) == 1


# ── cleanup failure rolls back, so a rerun stays correct ─────────────────────


def test_a_failed_cleanup_rolls_the_changelog_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Half-done is worse than not-done here, because the obvious remedy is wrong.

    With the changelog already rewritten and the fragments still present,
    re-running the documented command folds them in a SECOND time and duplicates
    the entries in the release notes. All-or-nothing keeps a rerun correct.
    """
    d = tmp_path / "changelog.d"
    _fragment(d, "20260904210000-fixed-thing.md", body="- **Landed.** body")
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(CHANGELOG_SKELETON)
    monkeypatch.setattr(ac, "FRAGMENT_DIR", d)
    monkeypatch.setattr(ac, "CHANGELOG", cl)

    def boom(self, *a, **k):
        raise OSError("directory not writable")

    monkeypatch.setattr(Path, "unlink", boom)
    assert ac.main([]) == 2
    assert cl.read_text() == CHANGELOG_SKELETON, (
        "CHANGELOG.md kept its new content while the fragments survived — a "
        "rerun would fold them in again and duplicate the entries"
    )
    assert len(list(d.iterdir())) == 1
    assert "have been restored" in capsys.readouterr().err
# ── the failure path that actually destroys data ─────────────────────────────


def test_a_cleanup_failure_partway_restores_every_fragment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The rollback must be all-or-nothing, not all-or-PARTIAL.

    MEASURED before the fix: with unlink failing on the SECOND fragment, the
    changelog rolled back but the first fragment stayed deleted — its entry in
    neither place — while the message told the operator state was consistent and
    to re-run, which would ship a release missing it.

    The previous test for this stubbed unlink to raise on the FIRST call, so
    nothing was ever deleted and the property it named was never exercised. The
    stub here fails on the second call for exactly that reason.
    """
    d = tmp_path / "changelog.d"
    for i, slug in enumerate(("one", "two", "three")):
        _fragment(d, f"2026090421000{i}-fixed-{slug}.md", body=f"- **{slug}.** body")
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(CHANGELOG_SKELETON)
    monkeypatch.setattr(ac, "FRAGMENT_DIR", d)
    monkeypatch.setattr(ac, "CHANGELOG", cl)

    before = {p.name for p in d.iterdir()}
    real_unlink = Path.unlink
    calls = {"n": 0}

    def fail_on_second(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("directory not writable")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", fail_on_second)
    assert ac.main([]) == 2
    assert {p.name for p in d.iterdir()} == before, (
        "a fragment deleted before the failure was not restored — its entry is "
        "now in neither the changelog nor changelog.d"
    )
    assert cl.read_text() == CHANGELOG_SKELETON
    assert "have been restored" in capsys.readouterr().err


# ── --base cannot be disarmed by merely touching the changelog ───────────────


def test_an_unrelated_changelog_edit_does_not_excuse_a_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ "The file changed" is not "the entry arrived".

    A typo fix or a stray newline in CHANGELOG.md satisfied the old test and
    disarmed the deletion rule for the whole change — and hand-editing the
    changelog is precisely what the convention forbids, so the two gaps
    compounded.
    """
    repo, base = _repo_with_a_committed_fragment(tmp_path)
    _patch_module_to(repo, monkeypatch)
    (repo / "changelog.d" / "20260904210000-fixed-kept.md").unlink()
    (repo / "CHANGELOG.md").write_text(
        CHANGELOG_SKELETON.replace("- an entry that was already here", "- a typo fixed")
    )
    assert ac.main(["--check", "--base", base]) == 2
    assert "entries are not in CHANGELOG.md" in capsys.readouterr().err


def test_a_trailing_newline_touch_does_not_excuse_a_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base = _repo_with_a_committed_fragment(tmp_path)
    _patch_module_to(repo, monkeypatch)
    (repo / "changelog.d" / "20260904210000-fixed-kept.md").unlink()
    (repo / "CHANGELOG.md").write_text(CHANGELOG_SKELETON + "\n")
    assert ac.main(["--check", "--base", base]) == 2


def test_renaming_a_fragment_is_refused_like_a_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rename is a deletion under a new name as far as the entry is concerned."""
    repo, base = _repo_with_a_committed_fragment(tmp_path)
    _patch_module_to(repo, monkeypatch)
    old = repo / "changelog.d" / "20260904210000-fixed-kept.md"
    old.rename(repo / "changelog.d" / "20260904220000-fixed-renamed.md")
    assert ac.main(["--check", "--base", base]) == 2


# ── --base is a guard flag, never a way to trigger the fold ──────────────────


def test_base_without_check_refuses_instead_of_folding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reaching for the guard flag must not perform the irreversible operation.

    MEASURED before the fix: `--base origin/main` alone folded the fragments and
    deleted them, exit 0, because the base handling sat inside the check branch
    and argparse accepted the combination without comment.
    """
    d = tmp_path / "changelog.d"
    _fragment(d, "20260904210000-fixed-thing.md", body="- **Landed.** body")
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(CHANGELOG_SKELETON)
    monkeypatch.setattr(ac, "FRAGMENT_DIR", d)
    monkeypatch.setattr(ac, "CHANGELOG", cl)
    with pytest.raises(SystemExit) as exc:
        ac.main(["--base", "origin/main"])
    assert exc.value.code == 2
    assert cl.read_text() == CHANGELOG_SKELETON
    assert len(list(d.iterdir())) == 1
@pytest.mark.parametrize(
    "bad_line",
    [
        " ## One space",
        "  ## Two spaces",
        "   ## Three spaces",
        "  # Under a wide bullet",
    ],
)
def test_a_heading_indented_up_to_three_spaces_is_still_rejected(
    tmp_path: Path, bad_line: str
) -> None:
    """CommonMark treats up to three spaces of indent as unindented.

    This is why the error message says FOUR spaces: under `-   a bullet` the
    content column is 4, so a two-space indent is still column 2 of the document
    and still a heading. The message used to say two, and a test pinned that as
    allowed — the advice and its test were wrong together.
    """
    d = tmp_path / "changelog.d"
    _fragment(d, "20260904210000-fixed-x.md", body=f"-   a bullet\n\n{bad_line}\n")
    with pytest.raises(ac.FragmentError, match="heading or rule"):
        ac.collect_fragments(d)


@pytest.mark.parametrize(
    "bad_body",
    [
        "- a bullet\n\nA heading\n===\n",  # setext H1
        "- a bullet\n\nA heading\n---\n",  # setext H2
        "- a bullet\n\n***\n",  # thematic break
        "- a bullet\n\n___\n",  # thematic break, underscores
        "- a bullet\n\n<div>raw</div>\n",  # HTML block
    ],
)
def test_headings_without_a_hash_and_html_blocks_are_rejected(
    tmp_path: Path, bad_body: str
) -> None:
    """A heading does not need a '#'.

    Setext underlines a line with = or -, and a thematic break or HTML block
    ends the surrounding block just as decisively. Matching only '#' would have
    left the whole no-hash half of the class shipping.
    """
    d = tmp_path / "changelog.d"
    _fragment(d, "20260904210000-fixed-x.md", body=bad_body)
    with pytest.raises(ac.FragmentError):
        ac.collect_fragments(d)


def test_fragments_are_read_as_utf8_regardless_of_the_ambient_locale(
    tmp_path: Path,
) -> None:
    """The reads must not depend on the operator's locale.

    Otherwise `--check` certifies green in CI's UTF-8 environment while the
    actual fold tracebacks on a machine with an 8-bit locale — the same
    split-surface failure the dotfile exemption is scoped to avoid. The env
    below defeats PEP 538 coercion, which would otherwise hide the difference.
    """
    import subprocess

    d = tmp_path / "changelog.d"
    _fragment(d, "20260904210000-fixed-x.md", body="- **Café.** Prose with an em—dash.")
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(CHANGELOG_SKELETON)
    driver = tmp_path / "drive.py"
    driver.write_text(
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('ac', {str(_MODULE_PATH)!r})\n"
        "ac = importlib.util.module_from_spec(spec); spec.loader.exec_module(ac)\n"
        "from pathlib import Path\n"
        f"ac.FRAGMENT_DIR = Path({str(d)!r})\n"
        f"ac.CHANGELOG = Path({str(cl)!r})\n"
        "sys.exit(ac.main(['--dry-run']))\n"
    )
    proc = subprocess.run(
        [os.sys.executable, str(driver)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "LC_ALL": "C",
            "LANG": "C",
            "PYTHONUTF8": "0",
            "PYTHONCOERCECLOCALE": "0",
        },
    )
    assert "UnicodeDecodeError" not in proc.stderr, (
        f"reading depended on the ambient locale: {proc.stderr[-400:]}"
    )
    assert proc.returncode == 0, proc.stderr[-400:]
# ── the fold claim requires the WHOLE entry, as exact lines ──────────────────


def test_a_partial_fold_that_drops_continuation_lines_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First-line-only was satisfiable by a fold that lost the paragraphs.

    The deleted fragment's ENTIRE nonblank body must arrive in the changelog
    diff, line for line — a fold that carried the headline and dropped the
    continuation is a partial loss the old check certified as complete.
    """
    repo, base = _repo_with_a_committed_fragment(
        tmp_path, body="- **Kept.** headline\n\n  A continuation paragraph."
    )
    _patch_module_to(repo, monkeypatch)
    (repo / "changelog.d" / "20260904210000-fixed-kept.md").unlink()
    (repo / "CHANGELOG.md").write_text(
        CHANGELOG_SKELETON.replace("### Fixed\n", "### Fixed\n\n- **Kept.** headline\n")
    )
    assert ac.main(["--check", "--base", base]) == 2


def test_a_substring_coincidence_does_not_count_as_a_fold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matching inside a glued blob let `- fix` be "preserved" by any added
    line that happened to contain that text. Lines must match as LINES."""
    repo, base = _repo_with_a_committed_fragment(tmp_path, body="- fix")
    _patch_module_to(repo, monkeypatch)
    (repo / "changelog.d" / "20260904210000-fixed-kept.md").unlink()
    (repo / "CHANGELOG.md").write_text(
        CHANGELOG_SKELETON.replace(
            "### Fixed\n", "### Fixed\n\n- fixed something unrelated\n"
        )
    )
    assert ac.main(["--check", "--base", base]) == 2


def test_a_complete_fold_of_a_multiline_fragment_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "- **Kept.** headline\n\n  A continuation paragraph."
    repo, base = _repo_with_a_committed_fragment(tmp_path, body=body)
    _patch_module_to(repo, monkeypatch)
    (repo / "changelog.d" / "20260904210000-fixed-kept.md").unlink()
    (repo / "CHANGELOG.md").write_text(
        CHANGELOG_SKELETON.replace(
            "### Fixed\n",
            "### Fixed\n\n- **Kept.** headline\n\n  A continuation paragraph.\n",
        )
    )
    assert ac.main(["--check", "--base", base]) == 0


# ── an interrupt mid-cleanup restores everything and stays an interrupt ──────


def test_ctrl_c_mid_cleanup_restores_everything_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catching only OSError left Ctrl-C with the worst of both: changelog
    rewritten, some fragments surviving, and the documented re-run then folds
    the survivors a second time. The interrupt must roll back like any other
    failure — and still terminate the process as an interrupt, not as success.
    """
    d = tmp_path / "changelog.d"
    for i, slug in enumerate(("one", "two", "three")):
        _fragment(d, f"2026090421000{i}-fixed-{slug}.md", body=f"- **{slug}.** body")
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(CHANGELOG_SKELETON)
    monkeypatch.setattr(ac, "FRAGMENT_DIR", d)
    monkeypatch.setattr(ac, "CHANGELOG", cl)

    before = {p.name for p in d.iterdir()}
    real_unlink = Path.unlink
    calls = {"n": 0}

    def interrupt_on_second(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", interrupt_on_second)
    with pytest.raises(KeyboardInterrupt):
        ac.main([])
    assert {p.name for p in d.iterdir()} == before
    assert cl.read_text() == CHANGELOG_SKELETON


# ── round-4 findings: heading authenticity, symlinks, mid-assembly edits ──────


def test_an_indented_unreleased_heading_is_not_a_fold_target() -> None:
    """Four spaces of indent make '## [Unreleased]' a code line, not a heading.

    CommonMark renders the four-space form as an indented code block, so folding
    after it puts every generated entry outside any real section. When it is the
    only match, the splice must refuse rather than target it.
    """
    code_only = "# Changelog\n\n    ## [Unreleased]\n\n## [v1] - 2026-01-01\n"
    with pytest.raises(ac.FragmentError):
        ac.splice(code_only, {"Fixed": ["- x"]})


def test_check_target_rejects_an_indented_unreleased_heading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text("# Changelog\n\n    ## [Unreleased]\n")
    monkeypatch.setattr(ac, "CHANGELOG", cl)
    with pytest.raises(ac.FragmentError):
        ac._check_target()


def test_a_real_heading_below_an_indented_copy_is_the_target() -> None:
    """splice picks the genuine heading, not a code-block occurrence above it."""
    text = (
        "# Changelog\n\n    ## [Unreleased]\n\n## [Unreleased]\n\n"
        "### Fixed\n\n- old\n"
    )
    out = ac.splice(text, {"Added": ["- new entry"]})
    lines = out.splitlines()
    assert lines.index("- new entry") > lines.index("## [Unreleased]")
    # nothing was inserted right after the indented (code) copy
    assert lines[lines.index("    ## [Unreleased]") + 1] == ""


def test_a_three_space_indented_heading_is_still_a_heading() -> None:
    """CommonMark allows up to three spaces of indent before a heading."""
    out = ac.splice("# Changelog\n\n   ## [Unreleased]\n", {"Fixed": ["- y"]})
    assert "- y" in out


def test_a_symlinked_fragment_is_rejected(tmp_path: Path) -> None:
    """A fragment must be a regular file.

    A symlink with a valid fragment name publishes ANOTHER file's body under
    this name (a 'security' link to a 'fixed' fragment ships one body under two
    categories), and the file deleted at fold time is not the artifact whose
    content was validated.
    """
    d = tmp_path / "changelog.d"
    real = _fragment(d, "20260904210000-fixed-real.md")
    link = d / "20260904210001-security-alias.md"
    link.symlink_to(real)
    with pytest.raises(ac.FragmentError, match="symlink"):
        ac.collect_fragments(d)


def test_an_edit_landing_mid_assembly_is_never_silently_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A save between the assembly read and cleanup must not vanish.

    The fold deletes fragments after splicing their text into CHANGELOG.md. If
    a fragment changed in between, deleting it destroys the newer bytes while
    the changelog carries the older ones — an uncommitted edit lost with
    nothing in git to recover it. The fold must refuse and roll back instead,
    leaving the newer bytes on disk.
    """
    d = tmp_path / "changelog.d"
    frag = _fragment(d, "20260904210000-fixed-thing.md", body="- **Original.** body")
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(CHANGELOG_SKELETON)
    monkeypatch.setattr(ac, "FRAGMENT_DIR", d)
    monkeypatch.setattr(ac, "CHANGELOG", cl)
    orig_render = ac.render_sections

    def render_then_edit(fragments):  # type: ignore[no-untyped-def]
        out = orig_render(fragments)
        frag.write_text(
            "- **Original.** body\n- **Late edit.** saved during assembly\n"
        )
        return out

    monkeypatch.setattr(ac, "render_sections", render_then_edit)
    rc = ac.main([])
    surviving = frag.read_text() if frag.exists() else ""
    # The late edit must survive somewhere; silently gone is the failure mode.
    assert "Late edit." in surviving or "Late edit." in cl.read_text()
    assert rc == 2
    assert cl.read_text() == CHANGELOG_SKELETON  # rolled back, rerun correct


def test_a_fenced_unreleased_heading_is_not_a_fold_target() -> None:
    """A '## [Unreleased]' inside a fenced code block is content, not a heading.

    The indent rule alone does not close the class: a fence puts an unindented
    copy of the heading line into code context, and folding after it lands the
    entries inside the fence — rendered as literal code, above the real
    section.
    """
    text = (
        "# Changelog\n\n```md\n## [Unreleased]\n```\n\n## [Unreleased]\n\n"
        "### Fixed\n\n- old\n"
    )
    out = ac.splice(text, {"Added": ["- new entry"]})
    lines = out.splitlines()
    real = max(i for i, ln in enumerate(lines) if ln == "## [Unreleased]")
    assert lines.index("- new entry") > lines.index("```", 1)  # after fence
    assert lines.index("- new entry") > real  # after the real heading


def test_check_target_rejects_a_fenced_only_unreleased_heading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text("# Changelog\n\n```md\n## [Unreleased]\n```\n")
    monkeypatch.setattr(ac, "CHANGELOG", cl)
    with pytest.raises(ac.FragmentError):
        ac._check_target()


def test_a_non_regular_file_is_rejected_before_it_is_read(tmp_path: Path) -> None:
    """The invariant is a REGULAR file, not merely a non-symlink.

    A FIFO with a valid fragment name would hang the read forever; a socket
    corrupts it. Both must be classified before any read is attempted.
    """
    import socket as _socket

    d = tmp_path / "changelog.d"
    d.mkdir()
    sock = _socket.socket(_socket.AF_UNIX)
    try:
        sock.bind(str(d / "20260904210000-fixed-sock.md"))
        with pytest.raises(ac.FragmentError, match="regular file"):
            ac.collect_fragments(d)
    finally:
        sock.close()
