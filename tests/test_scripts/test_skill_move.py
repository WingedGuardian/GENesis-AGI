"""scripts/skill_move.py: the skill-split generator and its check (T-2).

Every test builds a throwaway git repo: a source skill committed at HEAD, a row
file and authored pieces in the working tree. Sources are read at the ref, so a
destination that is also a source must not see its own output.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.conftest import private_module

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "skill_move.py"
sm = private_module("skill_move_under_test", _SCRIPT)

BIG = """---
name: big
description: Big skill.
keywords: [big]
---

# Big

## Keep

Keep paragraph one.
See `references/existing.md` for detail.

## Move

**Move lead.** moved text one.
moved text two, as shown below.
Detail in `references/existing.md`.

## Gone

Dead text.

## Tail

Tail text.
"""

SMALL_FRONT = """---
name: small
description: Small skill.
keywords: [small]
---
"""

ROWS = """version: 1
destinations:
  core: .claude/skills/big/SKILL.md
  small: .claude/skills/small/SKILL.md
  ref: .claude/skills/big/references/moved.md
pieces:
  - row: "1"
    dest: core
    source: .claude/skills/big/SKILL.md
    from: "@start"
    until: "## Move"
  - row: hdr
    dest: small
    file: scripts/hdr/small.md
  - row: "2"
    dest: small
    source: .claude/skills/big/SKILL.md
    from: "## Move"
    until: "## Gone"
    rewrites:
      - {kind: pointer, old: "as shown below", new: "as shown in the next section"}
      - {kind: path, old: "`references/existing.md`.", new: "`.claude/skills/big/references/existing.md`."}
  - row: "3"
    dest: null
    source: .claude/skills/big/SKILL.md
    from: "## Gone"
    until: "## Tail"
    reason: dead
  - row: "4"
    dest: ref
    source: .claude/skills/big/SKILL.md
    from: "## Tail"
    until: "@end"
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / ".claude/skills/big/references").mkdir(parents=True)
    (r / "scripts/hdr").mkdir(parents=True)
    (r / ".claude/skills/big/SKILL.md").write_text(BIG)
    (r / ".claude/skills/big/references/existing.md").write_text("existing\n")
    _git(tmp_path, "init", "-q", "-b", "main", str(r))
    _git(r, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "add", "-A")
    _git(r, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", "base")
    (r / "scripts/hdr/small.md").write_text(SMALL_FRONT)
    (r / "scripts/rows.yaml").write_text(ROWS)
    return r


def _run(repo: Path, *extra: str, rows: str = "scripts/rows.yaml") -> int:
    return sm.main(["--rows", str(repo / rows), "--source-ref", "HEAD", *extra])


def _commit(repo: Path, msg: str, *paths: str) -> None:
    # Commit only the named paths: committing generated outputs would put a
    # destination at the ref that is no piece's source (refused by design).
    _git(repo, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "add", "--", *paths)
    _git(repo, "-c", "user.email=t@example.invalid", "-c", "user.name=t", "commit", "-qm", msg)


def test_generates_every_destination_and_check_passes(repo, capsys):
    assert _run(repo) == 0
    core = (repo / ".claude/skills/big/SKILL.md").read_text()
    small = (repo / ".claude/skills/small/SKILL.md").read_text()
    ref = (repo / ".claude/skills/big/references/moved.md").read_text()
    assert core == BIG.split("## Move")[0].rstrip("\n") + "\n"
    # Authored frontmatter, one blank line, then the moved piece with both rewrites.
    assert small == (
        SMALL_FRONT.rstrip("\n") + "\n\n## Move\n\n**Move lead.** moved text one.\n"
        "moved text two, as shown in the next section.\n"
        "Detail in `.claude/skills/big/references/existing.md`.\n"
    )
    assert ref == "## Tail\n\nTail text.\n"
    assert "Dead text." not in core + small + ref
    capsys.readouterr()
    assert _run(repo, "--check") == 0
    out = capsys.readouterr().out
    assert "check passed" in out
    assert "pointer: 'as shown below' -> 'as shown in the next section'" in out
    assert (
        "path: '`references/existing.md`.' -> '`.claude/skills/big/references/existing.md`.'" in out
    )


def test_check_writes_nothing(repo):
    assert _run(repo, "--check") == 1  # outputs missing on disk
    assert not (repo / ".claude/skills/small").exists()
    assert (repo / ".claude/skills/big/SKILL.md").read_text() == BIG


def test_a_failed_check_writes_nothing(repo):
    rows = (repo / "scripts/rows.yaml").read_text()
    (repo / "scripts/rows.yaml").write_text(rows.replace('until: "## Tail"', 'until: "Dead text."'))
    assert _run(repo) == 1  # "## Gone"'s heading kept, "Dead text." onward unowned: a gap
    assert not (repo / ".claude/skills/small").exists()
    assert (repo / ".claude/skills/big/SKILL.md").read_text() == BIG


def test_reads_sources_at_the_ref_not_the_tree(repo):
    assert _run(repo) == 0
    first = {
        p: (repo / p).read_text() for p in sm.load_rows(repo / "scripts/rows.yaml")[0].values()
    }
    # The core now overwrites its own source in the tree; a second run must not read it.
    assert _run(repo) == 0
    assert {p: (repo / p).read_text() for p in first} == first


def test_misplaced_block_fails_check(repo, capsys):
    assert _run(repo) == 0
    small = repo / ".claude/skills/small/SKILL.md"
    front, body = small.read_text().split("## Move", 1)
    small.write_text("## Move" + body + "\n" + front)
    assert _run(repo, "--check") == 1
    assert (
        ".claude/skills/small/SKILL.md: differs from its assembly at line 1"
        in capsys.readouterr().err
    )


def test_reordered_lines_fail_check(repo, capsys):
    assert _run(repo) == 0
    small = repo / ".claude/skills/small/SKILL.md"
    lines = small.read_text().split("\n")
    i = lines.index("**Move lead.** moved text one.")
    lines[i], lines[i + 1] = lines[i + 1], lines[i]
    small.write_text("\n".join(lines))
    assert _run(repo, "--check") == 1
    assert "differs from its assembly" in capsys.readouterr().err


def test_undeclared_edit_fails_check(repo, capsys):
    assert _run(repo) == 0
    core = repo / ".claude/skills/big/SKILL.md"
    core.write_text(core.read_text().replace("Keep paragraph one.", "Keep paragraph 1."))
    assert _run(repo, "--check") == 1
    err = capsys.readouterr().err
    assert "big/SKILL.md: differs from its assembly at line 11 (piece row 1)" in err
    assert "regenerate with --source-ref origin/main" in err


def test_unaccounted_source_line_fails(repo, capsys):
    rows = (repo / "scripts/rows.yaml").read_text()
    (repo / "scripts/rows.yaml").write_text(rows.replace('from: "## Gone"', 'from: "Dead text."'))
    assert _run(repo) == 1
    err = capsys.readouterr().err
    assert ".claude/skills/big/SKILL.md:20 belongs to no piece: '## Gone'" in err


def test_overlapping_pieces_fail(repo, capsys):
    rows = (repo / "scripts/rows.yaml").read_text()
    (repo / "scripts/rows.yaml").write_text(rows.replace('until: "## Gone"', 'until: "Dead text."'))
    assert _run(repo) == 1
    assert "is in piece 3 and piece 4" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('until: "## Gone"', 'until: "## Nowhere"', "anchor '## Nowhere' matched 0 lines"),
        ('until: "## Gone"', 'until: "## "', "anchor '## ' matched 4 lines"),
        ('until: "## Gone"', 'until: "## Keep"', "is not after from"),
        (
            'until: "## Gone"',
            "until: ### Gone",
            "until must be a non-empty quoted string, got None",
        ),
        ("reason: dead", 'reason: ""', "DELETE reason must be a non-empty quoted string"),
        (
            'old: "as shown below"',
            'old: "moved text"',
            "containing 'above' or 'below'",
        ),
        ('old: "as shown below"', 'old: "as shown below and"', "old text matched 0 times"),
        (
            'new: "`.claude/skills/big/references/existing.md`."',
            'new: "`.claude/skills/big/references/existing.md`, reworded."',
            "a path rewrite may change one path token and nothing else",
        ),
        ("dest: ref", "dest: elsewhere", "unknown destination 'elsewhere'"),
        ("    reason: dead", "    reason: dead\n    colour: red", "unknown keys ['colour']"),
        ("version: 1", "version: true", "expected a mapping with version: 1"),
        (
            "  ref: .claude/skills/big/references/moved.md\n",
            "  ref: .claude/skills/big/references/moved.md\n  ref2: .claude/skills/big/references/moved.md\n",
            "destinations ref and ref2 share the path",
        ),
        (
            "  ref: .claude/skills/big/references/moved.md",
            "  ref: /etc/moved.md",
            "normalized repo-relative path",
        ),
        (
            "  ref: .claude/skills/big/references/moved.md",
            "  ref: .git/hooks/post-checkout",
            "outside .git",
        ),
        (
            "  ref: .claude/skills/big/references/moved.md",
            "  ref: ../outside.md",
            "normalized repo-relative path",
        ),
        (
            "    file: scripts/hdr/small.md",
            "    file: ../outside.md",
            "normalized repo-relative path",
        ),
        ('  - row: "4"\n    dest: ref\n', '  - row: "4"\n', "needs a dest key"),
        (
            "    dest: ref\n",
            "    dest: ref\n    reason: why\n",
            "a reason is required on a DELETE piece and only there",
        ),
        (
            '    until: "## Move"\n',
            '    until: "## Move"\n    must_end_before: true\n',
            "must_end_before must be a positive integer",
        ),
        ("    dest: ref\n", "    dest: [ref]\n", "unknown destination ['ref']"),
        ("    dest: ref\n", "    dest: ref\n    dest: core\n", "duplicate key(s) ['dest']"),
        (
            "    file: scripts/hdr/small.md\n",
            "    file: scripts/hdr/small.md\n    source: .claude/skills/big/SKILL.md\n",
            "an authored piece has a file and a destination only",
        ),
        (
            "    file: scripts/hdr/small.md\n",
            "    file: scripts/hdr/small.md\n    rewrites: [{kind: edit, old: a, new: b}]\n",
            "an authored piece has a file and a destination only",
        ),
        (
            "    file: scripts/hdr/small.md",
            "    file: scripts/hdr/none.md",
            "authored file scripts/hdr/none.md not found",
        ),
        (
            '    source: .claude/skills/big/SKILL.md\n    from: "## Tail"',
            '    source: .claude/skills/big/NOPE.md\n    from: "## Tail"',
            "source .claude/skills/big/NOPE.md does not exist at HEAD",
        ),
        (
            'old: "as shown below"',
            'old: "' + "x" * 90 + ' below"',
            "a pointer rewrite replaces a phrase of at most 80",
        ),
        (
            'new: "as shown in the next section"}',
            'new: "as shown in the next section", why: x}',
            "needs exactly kind",
        ),
    ],
    ids=[
        "missing",
        "ambiguous",
        "backwards",
        "unquoted-heading",
        "no-reason",
        "pointer-shape",
        "rewrite-miss",
        "path-shape",
        "bad-dest",
        "unknown-key",
        "version-bool",
        "duplicate-dest-path",
        "absolute-dest",
        "dest-in-git",
        "dest-outside",
        "file-outside",
        "no-dest-key",
        "reason-not-delete",
        "must-end-bool",
        "dest-list",
        "duplicate-yaml-key",
        "authored-with-source",
        "authored-with-rewrites",
        "authored-missing",
        "source-missing-at-ref",
        "pointer-too-long",
        "rewrite-extra-key",
    ],
)
def test_row_file_errors_exit_2(repo, capsys, old, new, message):
    rows = (repo / "scripts/rows.yaml").read_text()
    assert rows.count(old) == 1
    (repo / "scripts/rows.yaml").write_text(rows.replace(old, new))
    assert _run(repo) == 2
    assert message in capsys.readouterr().err


def test_an_empty_piece_is_refused(repo, capsys):
    # until on the same line as from: an empty range, not a zero-line piece.
    rows = (repo / "scripts/rows.yaml").read_text()
    (repo / "scripts/rows.yaml").write_text(rows.replace('until: "## Gone"', 'until: "## Move"'))
    assert _run(repo) == 2
    assert "is not after from" in capsys.readouterr().err


def test_a_rewrite_matching_twice_is_refused(repo, capsys):
    rows = (repo / "scripts/rows.yaml").read_text()
    edit = '      - {kind: edit, old: "moved text", new: "text"}\n'
    marker = '      - {kind: pointer, old: "as shown below", new: "as shown in the next section"}\n'
    assert rows.count(marker) == 1
    (repo / "scripts/rows.yaml").write_text(rows.replace(marker, marker + edit))
    assert _run(repo) == 2
    assert "rewrite 2: old text matched 2 times" in capsys.readouterr().err


def test_unresolved_link_fails(repo, capsys):
    rows = (repo / "scripts/rows.yaml").read_text()
    # Without the path rewrite the moved `references/existing.md` resolves from
    # small's own root, where it does not exist.
    (repo / "scripts/rows.yaml").write_text(
        rows.replace(
            '      - {kind: path, old: "`references/existing.md`.", new: "`.claude/skills/big/references/existing.md`."}\n',
            "",
        )
    )
    assert _run(repo) == 1
    assert "row 2: path references/existing.md does not resolve" in capsys.readouterr().err


def test_link_already_broken_at_the_ref_is_reported_not_failed(repo, capsys):
    big = repo / ".claude/skills/big/SKILL.md"
    big.write_text(
        BIG.replace("Keep paragraph one.", "Keep paragraph one, see `references/absent.md`.")
    )
    _commit(repo, "broken link at base", ".claude/skills/big/SKILL.md")
    assert _run(repo) == 0
    assert "note: references/absent.md was already unresolvable" in capsys.readouterr().out


def test_repo_path_to_missing_skill_fails(repo, capsys):
    (repo / "scripts/hdr/small.md").write_text(
        SMALL_FRONT + "\nLoad `.claude/skills/nowhere/SKILL.md`.\n"
    )
    assert _run(repo) == 1
    assert "path .claude/skills/nowhere/SKILL.md does not resolve" in capsys.readouterr().err


def test_repo_path_to_a_generated_skill_resolves(repo):
    rows = (repo / "scripts/rows.yaml").read_text()
    (repo / "scripts/hdr/router.md").write_text("Router: load `.claude/skills/small/SKILL.md`.\n")
    (repo / "scripts/rows.yaml").write_text(
        rows.replace(
            "  - row: hdr\n",
            "  - row: router\n    dest: core\n    file: scripts/hdr/router.md\n  - row: hdr\n",
        )
    )
    assert _run(repo) == 0
    assert "Router: load" in (repo / ".claude/skills/big/SKILL.md").read_text()


def test_destination_existing_at_the_ref_but_no_source_is_refused(repo, capsys):
    (repo / ".claude/skills/big/references/moved.md").write_text("someone's file\n")
    _commit(repo, "a file at the destination path", ".claude/skills/big/references/moved.md")
    assert _run(repo) == 2
    assert "exists at HEAD but is no piece's source" in capsys.readouterr().err
    assert (repo / ".claude/skills/big/references/moved.md").read_text() == "someone's file\n"


def test_orphan_output_fails(repo, capsys):
    assert _run(repo) == 0
    (repo / ".claude/skills/small/OLD.md").write_text("left behind\n")
    assert _run(repo, "--check") == 1
    assert (
        ".claude/skills/small/OLD.md: added since HEAD beside a destination"
        in capsys.readouterr().err
    )


def test_bad_frontmatter_fails(repo, capsys):
    (repo / "scripts/hdr/small.md").write_text(SMALL_FRONT.replace("name: small", "name: tiny"))
    assert _run(repo) == 1
    assert "frontmatter name 'tiny' != directory 'small'" in capsys.readouterr().err
    (repo / "scripts/hdr/small.md").write_text(SMALL_FRONT.replace("keywords: [small]\n", ""))
    assert _run(repo) == 1
    assert "frontmatter lacks keywords" in capsys.readouterr().err
    (repo / "scripts/hdr/small.md").write_text(
        SMALL_FRONT.replace("description: Small skill.\n", "")
    )
    assert _run(repo) == 1
    assert "frontmatter lacks description" in capsys.readouterr().err


def test_must_end_before_overrun_fails(repo, capsys):
    rows = (repo / "scripts/rows.yaml").read_text()
    (repo / "scripts/rows.yaml").write_text(
        rows.replace('    until: "## Move"\n', '    until: "## Move"\n    must_end_before: 40\n', 1)
    )
    assert _run(repo) == 1
    assert "piece row 1 ends at char" in capsys.readouterr().err


def test_join_rule_one_blank_line_between_pieces(repo):
    # The authored header ends without a blank line and the moved piece starts
    # with a heading: exactly one blank line between them, one newline at EOF.
    (repo / "scripts/hdr/small.md").write_text(SMALL_FRONT + "\n\n\n")
    assert _run(repo) == 0
    small = (repo / ".claude/skills/small/SKILL.md").read_text()
    assert "---\n\n## Move\n" in small and "---\n\n\n" not in small
    assert small.endswith("existing.md`.\n") and not small.endswith("\n\n")


def test_report_names_ranges_and_pointers(repo, capsys):
    assert _run(repo) == 0
    out = capsys.readouterr().out
    assert "row 2: .claude/skills/big/SKILL.md:14-19 ['## Move' .. 'Detail in" in out
    assert "source .claude/skills/big/SKILL.md: 26 lines, 22 moved, 4 deleted" in out


def test_regenerating_after_one_inserted_line_adds_only_that_line(repo):
    assert _run(repo) == 0
    before = {
        p: (repo / p).read_text() for p in sm.load_rows(repo / "scripts/rows.yaml")[0].values()
    }
    # Main moves: one line inserted inside a moved range. The tree still holds
    # the first run's outputs, so restore the source before committing.
    big = repo / ".claude/skills/big/SKILL.md"
    big.write_text(
        BIG.replace(
            "moved text two, as shown below.\n", "moved text two, as shown below.\nINSERTED.\n"
        )
    )
    _commit(repo, "main moved", ".claude/skills/big/SKILL.md")
    assert _run(repo) == 0
    after = {p: (repo / p).read_text() for p in before}
    assert after[".claude/skills/small/SKILL.md"] == before[
        ".claude/skills/small/SKILL.md"
    ].replace("next section.\n", "next section.\nINSERTED.\n")
    for p in before:
        if p != ".claude/skills/small/SKILL.md":
            assert after[p] == before[p], p


def test_not_in_a_git_repo_exits_2(tmp_path, capsys):
    (tmp_path / "rows.yaml").write_text(ROWS)
    assert sm.main(["--rows", str(tmp_path / "rows.yaml")]) == 2
    assert "is not inside a git repository" in capsys.readouterr().err


def _rows(repo: Path) -> Path:
    return repo / "scripts/rows.yaml"


def _edit_rows(repo: Path, old: str, new: str) -> None:
    text = _rows(repo).read_text()
    assert text.count(old) == 1, old
    _rows(repo).write_text(text.replace(old, new))


def test_a_link_resolving_only_in_the_tree_is_not_pre_existing(repo, capsys):
    # The link resolved at the ref; the branch deleted its target. That is a new
    # break, so it fails rather than being excused as already broken.
    (repo / ".claude/skills/other").mkdir()
    (repo / ".claude/skills/other/SKILL.md").write_text("other\n")
    big = repo / ".claude/skills/big/SKILL.md"
    big.write_text(
        BIG.replace(
            "Keep paragraph one.", "Keep paragraph one, see `.claude/skills/other/SKILL.md`."
        )
    )
    _commit(
        repo, "a link that resolves", ".claude/skills/big/SKILL.md", ".claude/skills/other/SKILL.md"
    )
    (repo / ".claude/skills/other/SKILL.md").unlink()
    assert _run(repo) == 1
    assert "row 1: path .claude/skills/other/SKILL.md does not resolve" in capsys.readouterr().err


def test_editing_an_existing_file_under_a_destination_directory_is_allowed(repo):
    (repo / ".claude/skills/big/references/existing.md").write_text("existing, edited\n")
    assert _run(repo) == 0
    assert _run(repo, "--check") == 0


@pytest.mark.parametrize(
    "line",
    [
        "See [the guide](references/nowhere.md).",
        "See `../big/references/nowhere.md`.",
        "See `../../../../outside.md`.",
    ],
    ids=["markdown-link", "dotdot-path", "outside-repo"],
)
def test_other_link_forms_are_checked(repo, capsys, line):
    (repo / "scripts/hdr/small.md").write_text(SMALL_FRONT + "\n" + line + "\n")
    assert _run(repo) == 1
    assert "does not resolve" in capsys.readouterr().err


def test_a_path_rewrite_target_must_resolve(repo, capsys):
    _edit_rows(
        repo,
        'new: "`.claude/skills/big/references/existing.md`."',
        'new: "`.claude/skills/big/references/typo.md`."',
    )
    assert _run(repo) == 1
    assert "path rewrite target" in capsys.readouterr().err


def test_joins_that_change_blank_lines_are_reported(repo, capsys):
    assert _run(repo) == 0
    assert "join row" not in capsys.readouterr().out  # the fixture joins on exactly one blank line
    (repo / "scripts/hdr/small.md").write_text(SMALL_FRONT + "\n\n")
    assert _run(repo) == 0
    assert "join row hdr | row 2: 3 blank line(s) -> 1" in capsys.readouterr().out


def test_check_compares_bytes_not_text(repo, capsys):
    assert _run(repo) == 0
    core = repo / ".claude/skills/big/SKILL.md"
    core.write_bytes(core.read_bytes().replace(b"\n", b"\r\n"))
    assert _run(repo, "--check") == 1
    assert "big/SKILL.md: differs from its assembly" in capsys.readouterr().err


def test_file_modes_follow_the_target_or_the_umask(repo):
    import os

    core = repo / ".claude/skills/big/SKILL.md"
    core.chmod(0o640)
    assert _run(repo) == 0
    umask = os.umask(0)
    os.umask(umask)
    assert core.stat().st_mode & 0o777 == 0o640
    assert (repo / ".claude/skills/small/SKILL.md").stat().st_mode & 0o777 == 0o666 & ~umask


def test_an_unwritable_destination_writes_nothing(repo, capsys):
    # The last output's directory is read-only: the first two must not be
    # written either (temp files are all written before any rename).
    refs = repo / ".claude/skills/big/references"
    refs.chmod(0o500)
    try:
        assert _run(repo) == 2
    finally:
        refs.chmod(0o700)
    assert "no file written" in capsys.readouterr().err
    assert not (repo / ".claude/skills/small/SKILL.md").exists()
    assert sorted(x.name for x in refs.iterdir()) == ["existing.md"]
    # The temp files already written for the first two outputs are removed.
    for d in (repo / ".claude/skills/big", repo / ".claude/skills/small"):
        assert not [x.name for x in d.iterdir() if x.name.startswith(".SKILL.md.")], d
    assert (repo / ".claude/skills/big/SKILL.md").read_text() == BIG
    assert not (repo / ".claude/skills/big/references/moved.md").exists()


def test_authored_pieces_mid_destination_and_empty_ones(repo, capsys):
    (repo / "scripts/hdr/mid.md").write_text("Mid text.\n")
    (repo / "scripts/hdr/empty.md").write_text("\n\n")
    _edit_rows(
        repo,
        '  - row: "2"\n    dest: small\n',
        "  - row: mid\n    dest: small\n    file: scripts/hdr/mid.md\n"
        "  - row: empty\n    dest: small\n    file: scripts/hdr/empty.md\n"
        '  - row: "2"\n    dest: small\n',
    )
    assert _run(repo) == 0
    small = (repo / ".claude/skills/small/SKILL.md").read_text()
    assert "---\n\nMid text.\n\n## Move\n" in small
    assert "row empty: scripts/hdr/empty.md is empty, skipped" in capsys.readouterr().out


def test_edit_rewrites_and_leftover_pointers_are_reported_in_full(repo, capsys):
    _edit_rows(
        repo,
        '      - {kind: pointer, old: "as shown below", new: "as shown in the next section"}\n',
        '      - {kind: edit, old: "moved text one.", new: "moved text 1."}\n',
    )
    assert _run(repo) == 0
    out = capsys.readouterr().out
    assert "edit: 'moved text one.' -> 'moved text 1.'" in out
    assert any(
        "pointer to check:" in line and "as shown below" in line for line in out.splitlines()
    )


def test_a_destination_with_no_pieces_fails(repo, capsys):
    _edit_rows(repo, "destinations:\n", "destinations:\n  extra: .claude/skills/extra/SKILL.md\n")
    assert _run(repo) == 1
    assert "destination extra has no non-empty pieces" in capsys.readouterr().err


def test_a_path_outside_the_repo_fails_even_if_it_exists(repo, capsys):
    # tmp_path/outside.md exists, one level above the repo: resolving it is
    # still a failure, never an accepted link.
    (repo.parent / "outside.md").write_text("outside\n")
    (repo / "scripts/hdr/small.md").write_text(SMALL_FRONT + "\nSee `../../../../outside.md`.\n")
    assert _run(repo) == 1
    assert "path ../../../../outside.md does not resolve" in capsys.readouterr().err


@pytest.mark.parametrize(
    "ref", ["--output=pwned", "-x", "no-such-branch"], ids=["option", "flag", "unknown"]
)
def test_a_source_ref_that_is_not_a_commit_is_refused(repo, capsys, ref):
    # Only authored pieces: no source read can fail first, so the ref reaches
    # every git call. It must be refused before any, and create nothing.
    (repo / "scripts/hdr/only.md").write_text("only\n")
    (repo / "scripts/only.yaml").write_text(
        "version: 1\ndestinations:\n  x: docs/x.md\npieces:\n  - {row: a, dest: x, file: scripts/hdr/only.md}\n"
    )
    assert sm.main(["--rows", str(repo / "scripts/only.yaml"), f"--source-ref={ref}"]) == 2
    assert "--source-ref" in capsys.readouterr().err
    assert not (repo / "pwned").exists() and not (repo / "docs").exists()


def test_a_symlink_on_a_destination_path_is_refused(repo, capsys, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / ".claude/skills/small").symlink_to(outside, target_is_directory=True)
    _commit(repo, "a symlinked skill directory", ".claude/skills/small")
    assert _run(repo) == 2
    assert "passes through a symlink (.claude/skills/small)" in capsys.readouterr().err
    assert list(outside.iterdir()) == []


def test_an_authored_file_that_is_a_symlink_is_refused(repo, capsys, tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("not for the repo\n")
    (repo / "scripts/hdr/small.md").unlink()
    (repo / "scripts/hdr/small.md").symlink_to(secret)
    assert _run(repo) == 2
    err = capsys.readouterr().err
    assert "passes through a symlink" in err and "not for the repo" not in err


def test_a_destination_that_is_a_directory_is_refused(repo, capsys):
    (repo / ".claude/skills/big/references/moved.md").mkdir()
    assert _run(repo) == 2
    assert "exists and is not a regular file" in capsys.readouterr().err
    assert (repo / ".claude/skills/big/SKILL.md").read_text() == BIG


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (
            "  ref: .claude/skills/big/references/moved.md",
            '  ref: "."',
            "normalized repo-relative path",
        ),
        ("  ref: .claude/skills/big/references/moved.md", "  ref: docs/.git/x.md", "outside .git"),
        ("pieces:\n", "pieces: 5\nunused:\n", "pieces must be a list"),
    ],
    ids=["dot", "git-mid-path", "pieces-not-list"],
)
def test_more_row_file_shapes_exit_2(repo, capsys, old, new, message):
    _edit_rows(repo, old, new)
    assert _run(repo) == 2
    assert message in capsys.readouterr().err


def test_rewrites_must_be_a_list(repo, capsys):
    _edit_rows(repo, '    until: "## Tail"\n', '    until: "## Tail"\n    rewrites: 5\n')
    assert _run(repo) == 2
    assert "rewrites must be a list" in capsys.readouterr().err


def test_an_unexpected_error_exits_2_not_1(repo, capsys, monkeypatch):
    def boom(*_args):
        raise KeyError("x")

    monkeypatch.setattr(sm, "_frontmatter_problem", boom)
    assert _run(repo) == 2
    assert "unexpected KeyError" in capsys.readouterr().err


# --- round-1 review fixes ---------------------------------------------------------------


def test_a_crlf_source_is_refused_not_converted(repo, capsys):
    (repo / ".claude/skills/big/SKILL.md").write_bytes(BIG.replace("\n", "\r\n").encode())
    _commit(repo, "crlf", ".claude/skills/big/SKILL.md")
    assert _run(repo) == 2
    assert "has a CR at line 1" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("data", "message"),
    [(SMALL_FRONT.replace("\n", "\r\n").encode(), "has a CR"), (b"\xff\xfe x\n", "is not UTF-8")],
    ids=["crlf", "not-utf8"],
)
def test_an_authored_piece_must_be_lf_utf8(repo, capsys, data, message):
    (repo / "scripts/hdr/small.md").write_bytes(data)
    assert _run(repo) == 2
    assert message in capsys.readouterr().err


def test_a_destination_inside_another_destination_is_refused(repo, capsys):
    rows = (repo / "scripts/rows.yaml").read_text()
    (repo / "scripts/rows.yaml").write_text(
        rows.replace(
            "  ref: .claude/skills/big/references/moved.md\n",
            "  ref: .claude/skills/big/references/moved.md\n  dirdest: .claude/skills/big/references\n",
        )
    )
    assert _run(repo) == 2
    assert "is a directory of destination ref" in capsys.readouterr().err


def test_non_ascii_destinations_are_outputs_not_orphans(repo, capsys):
    rows = (repo / "scripts/rows.yaml").read_text()
    (repo / "scripts/rows.yaml").write_text(
        rows.replace(
            "  ref: .claude/skills/big/references/moved.md",
            '  ref: ".claude/skills/big/references/movéd\\tx.md"',  # YAML \t: a real tab
        )
    )
    assert _run(repo) == 0
    assert _run(repo, "--check") == 0
    (repo / ".claude/skills/small/ÖLD.md").write_text("left behind\n")
    assert _run(repo, "--check") == 1
    assert ".claude/skills/small/ÖLD.md: added since HEAD" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("link", "ok"),
    [("[e](existing.md)", True), ("[e](references/existing.md)", False)],
    ids=["file-relative", "skill-root-relative"],
)
def test_markdown_links_in_a_reference_file_resolve_from_that_file(repo, capsys, link, ok):
    rows = (repo / "scripts/rows.yaml").read_text()
    (repo / "scripts/hdr/refnote.md").write_text(f"See {link}.\n")
    (repo / "scripts/rows.yaml").write_text(
        rows + "  - row: note\n    dest: ref\n    file: scripts/hdr/refnote.md\n"
    )
    assert _run(repo) == (0 if ok else 1)
    if not ok:
        assert "path (references/existing.md) does not resolve" in capsys.readouterr().err


def _with_move_in_ref(repo: Path, ref_path: str) -> None:
    """Route the '## Move' section (a 3-line paragraph) to the ref destination,
    placed at ``ref_path``."""
    rows = ROWS.replace('  - row: "2"\n    dest: small', '  - row: "2"\n    dest: ref')
    rows = rows.replace("  ref: .claude/skills/big/references/moved.md", f"  ref: {ref_path}")
    (repo / "scripts/rows.yaml").write_text(rows)


def test_a_destination_renamed_into_another_skill_leaves_a_caught_orphan(repo, capsys):
    # Old and new directories are both outside every current destination's own
    # directory, so only the content rule can find the old output.
    _with_move_in_ref(repo, ".claude/skills/big/references/moved.md")
    assert _run(repo) == 0
    _with_move_in_ref(repo, ".claude/skills/small/references/moved.md")
    assert _run(repo) == 1
    err = capsys.readouterr().err
    assert ".claude/skills/big/references/moved.md:" in err
    assert "repeats text of .claude/skills/big/SKILL.md" in err


def test_a_committed_non_ascii_output_is_not_an_orphan(repo):
    # The `git diff --diff-filter=A` listing (outputs committed since the ref),
    # as opposed to the untracked listing the test above exercises.
    base = _git(repo, "rev-parse", "HEAD").strip()
    rows = (repo / "scripts/rows.yaml").read_text()
    (repo / "scripts/rows.yaml").write_text(
        rows.replace("references/moved.md", "references/movéd.md")
    )
    assert _run(repo) == 0
    _commit(repo, "outputs", ".claude/skills")
    args = ["--rows", str(repo / "scripts/rows.yaml"), "--source-ref", base, "--check"]
    assert sm.main(args) == 0


def test_a_destination_moved_out_of_a_non_skill_directory_leaves_a_caught_orphan(repo, capsys):
    _with_move_in_ref(repo, ".claude/skills/aux/notes.md")
    assert _run(repo) == 0
    _with_move_in_ref(repo, ".claude/skills/small/references/notes.md")
    assert _run(repo) == 1
    assert ".claude/skills/aux/notes.md:" in capsys.readouterr().err


# --- round-2 class audit -----------------------------------------------------------------


def test_an_unrelated_file_added_elsewhere_is_not_an_orphan(repo):
    other = repo / ".claude/skills/other/SKILL.md"
    other.parent.mkdir(parents=True)
    other.write_text("---\nname: other\n---\n\nUnrelated line one.\nUnrelated two.\nAnd three.\n")
    assert _run(repo) == 0


def test_an_authored_piece_kept_beside_a_destination_is_an_input_not_an_orphan(repo):
    rows = (repo / "scripts/rows.yaml").read_text()
    (repo / ".claude/skills/small").mkdir(parents=True, exist_ok=True)
    (repo / "scripts/hdr/small.md").rename(repo / ".claude/skills/small/_hdr.md")
    (repo / "scripts/rows.yaml").write_text(
        rows.replace("file: scripts/hdr/small.md", "file: .claude/skills/small/_hdr.md")
    )
    assert _run(repo) == 0
    assert _run(repo, "--check") == 0


def test_a_rename_paired_committed_orphan_is_still_found(repo, capsys):
    # git's rename detection pairs a committed old output with an identical file
    # deleted since the ref; --no-renames keeps it an addition.
    _with_move_in_ref(repo, ".claude/skills/big/references/moved.md")
    assert _run(repo) == 0
    old = repo / ".claude/skills/big/references/moved.md"
    legacy = repo / "docs/legacy.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(old.read_text())
    _commit(repo, "legacy copy at the ref", "docs/legacy.md")
    ref = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "rm", "-q", "docs/legacy.md")
    _commit(repo, "the old output, committed", ".claude/skills/big/references/moved.md")
    _with_move_in_ref(repo, ".claude/skills/small/references/moved.md")
    assert sm.main(["--rows", str(repo / "scripts/rows.yaml"), "--source-ref", ref]) == 1
    assert ".claude/skills/big/references/moved.md:" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("file: scripts/hdr/small.md", "file: .claude/skills/small/SKILL.md", "is destination"),
        (
            "  ref: .claude/skills/big/references/moved.md",
            "  ref: scripts/rows.yaml",
            "the row file scripts/rows.yaml is a destination",
        ),
        ('new: "as shown in the next section"', 'new: "as shown\\r\\nhere"', "new text has a CR"),
        (
            '    source: .claude/skills/big/SKILL.md\n    from: "## Gone"',
            '    source: .claude/skills/big/references\n    from: "## Gone"',
            "is a tree, not a file",
        ),
    ],
    ids=["authored-is-dest", "rows-is-dest", "cr-in-rewrite", "tree-source"],
)
def test_round_two_row_file_refusals(repo, capsys, old, new, message):
    rows = (repo / "scripts/rows.yaml").read_text()
    assert rows.count(old) == 1
    (repo / "scripts/rows.yaml").write_text(rows.replace(old, new))
    assert _run(repo) == 2
    assert message in capsys.readouterr().err


def test_a_source_that_is_no_destination_must_leave_the_tree(repo, capsys):
    rows = (repo / "scripts/rows.yaml").read_text()
    (repo / "scripts/rows.yaml").write_text(
        rows + "  - row: ex\n    dest: ref\n    source: .claude/skills/big/references/existing.md\n"
        '    from: "@start"\n    until: "@end"\n'
    )
    assert _run(repo) == 1
    assert (
        ".claude/skills/big/references/existing.md: a source that is no destination is still in the index"
        in capsys.readouterr().err
    )


def test_a_crlf_output_on_disk_is_named_as_line_endings(repo, capsys):
    assert _run(repo) == 0
    out = repo / ".claude/skills/small/SKILL.md"
    out.write_bytes(out.read_bytes().replace(b"\n", b"\r\n"))
    assert _run(repo, "--check") == 1
    assert "line endings (CR on disk)" in capsys.readouterr().err


def test_a_committed_non_ascii_orphan_is_found_by_its_real_name(repo, capsys):
    base = _git(repo, "rev-parse", "HEAD").strip()
    stale = repo / ".claude/skills/zz/ölD.md"
    stale.parent.mkdir(parents=True)
    stale.write_text(
        "**Move lead.** moved text one.\nmoved text two, as shown below.\nDetail in `references/existing.md`.\n"
    )
    _commit(repo, "a stale copy", ".claude/skills/zz")
    assert sm.main(["--rows", str(repo / "scripts/rows.yaml"), "--source-ref", base]) == 1
    assert ".claude/skills/zz/ölD.md:1: added since" in capsys.readouterr().err


# --- round-3 re-review: each orphan-scan guard pinned ------------------------------------

_MOVED = "**Move lead.** moved text one.\nmoved text two, as shown below.\nDetail in `references/existing.md`.\n"


def _plant(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_two_repeated_lines_are_not_a_paragraph(repo):
    _plant(
        repo, "docs/quote.md", "**Move lead.** moved text one.\nmoved text two, as shown below.\n"
    )
    assert _run(repo) == 0


def test_a_new_skill_sharing_frontmatter_fields_is_not_an_orphan(repo):
    front = "---\nname: big\ndescription: Big skill.\nkeywords: [big]\n---\n"
    _plant(repo, "docs/newskill/SKILL.md", front + "\nAn unrelated body.\n")
    assert _run(repo) == 0


@pytest.mark.parametrize(
    ("rel", "setup"),
    [("docs/stale.txt", None), ("docs/link.md", "symlink"), ("docs/big.md", "size")],
    ids=["other-suffix", "symlink", "over-size"],
)
def test_orphan_scan_guards(repo, capsys, monkeypatch, rel, setup):
    if setup == "symlink":
        _plant(repo, ".claude/skills/stale-target.txt", _MOVED)
        (repo / "docs").mkdir(exist_ok=True)
        (repo / rel).symlink_to(repo / ".claude/skills/stale-target.txt")
    else:
        _plant(repo, rel, _MOVED)
    if setup == "size":
        monkeypatch.setattr(sm, "_ORPHAN_MAX_BYTES", 10)
    assert _run(repo) == 0
    if setup == "size":
        assert "note: orphan scan skipped docs/big.md (over 10 bytes)" in capsys.readouterr().out


def test_the_row_file_kept_beside_a_destination_is_an_input(repo):
    (repo / "scripts/rows.yaml").rename(repo / ".claude/skills/big/rows.yaml")
    assert _run(repo, rows=".claude/skills/big/rows.yaml") == 0


def test_a_symlinked_row_file_is_an_input_too(repo):
    (repo / ".claude/skills/big/rows-link.yaml").symlink_to(repo / "scripts/rows.yaml")
    assert _run(repo, rows=".claude/skills/big/rows-link.yaml") == 0


def test_a_source_removed_from_the_tree_but_not_the_index_still_fails(repo, capsys):
    rows = (repo / "scripts/rows.yaml").read_text()
    (repo / "scripts/rows.yaml").write_text(
        rows + "  - row: ex\n    dest: ref\n    source: .claude/skills/big/references/existing.md\n"
        '    from: "@start"\n    until: "@end"\n'
    )
    (repo / ".claude/skills/big/references/existing.md").unlink()
    assert _run(repo) == 1
    assert "is still in the index; git rm it" in capsys.readouterr().err


@pytest.mark.skipif(__import__("os").geteuid() == 0, reason="root reads mode-000 files")
def test_an_unreadable_added_file_is_noted_not_silent(repo, capsys):
    _plant(repo, "docs/locked.md", _MOVED)
    (repo / "docs/locked.md").chmod(0)
    try:
        assert _run(repo) == 0
    finally:
        (repo / "docs/locked.md").chmod(0o644)
    assert "note: orphan scan skipped docs/locked.md (PermissionError)" in capsys.readouterr().out


def test_a_crlf_file_at_a_destination_path_gets_the_exists_at_ref_error(repo, capsys):
    (repo / ".claude/skills/big/references/moved.md").write_bytes(b"someone's\r\nfile\r\n")
    _commit(repo, "a crlf file at the destination path", ".claude/skills/big/references/moved.md")
    assert _run(repo) == 2
    assert "exists at HEAD but is no piece's source" in capsys.readouterr().err
