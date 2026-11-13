"""Shared patch parsing must preserve added bytes that native Git represents."""

from __future__ import annotations

import os
import subprocess

import pytest

from genesis.contribution.sanitize import parse_diff


def _patch(content: str, hunk: str = "@@ -0,0 +1 @@") -> str:
    return (
        "diff --git a/sample.txt b/sample.txt\n"
        "new file mode 100644\n"
        f"index {'0' * 40}..{'1' * 40}\n"
        "--- /dev/null\n+++ b/sample.txt\n"
        f"{hunk}\n+{content}\n"
    )


@pytest.mark.parametrize("separator", ["\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"])
def test_non_lf_characters_stay_inside_added_content(separator):
    content = "before" + separator + "SYNTHETIC_PRIVATE_MARKER"
    assert parse_diff(_patch(content)).added_lines == [("sample.txt", 1, content)]


@pytest.mark.parametrize("prefix", ["+", "++", "++ ", "+++", "+++ ", "@@ ", "diff --git ", "--- "])
def test_added_content_is_not_reinterpreted_as_a_patch_header(prefix):
    content = prefix + "SYNTHETIC_PRIVATE_MARKER"
    parsed = parse_diff(_patch(content))
    assert parsed.added_lines == [("sample.txt", 1, content)]
    assert parsed.file_paths == ["sample.txt"]


def test_tolerant_mode_retains_incomplete_legacy_fixture():
    patch = _patch("useful", "@@ -10,6 +10,8 @@")
    assert parse_diff(patch).added_lines == [("sample.txt", 10, "useful")]
    with pytest.raises(ValueError, match="^[a-z_]+$"):
        parse_diff(patch, strict=True)


def test_strict_counts_multiple_hunks_and_no_final_newline():
    patch = (
        "diff --git a/sample.txt b/sample.txt\n"
        f"index {'1' * 40}..{'2' * 40} 100644\n"
        "--- a/sample.txt\n+++ b/sample.txt\n"
        "@@ -1 +1 @@\n-old\n+new\n"
        "@@ -4,0 +5,2 @@\n+++ content\n+last\n"
        "\\ No newline at end of file\n"
    )
    assert parse_diff(patch, strict=True).added_lines == [
        ("sample.txt", 1, "new"),
        ("sample.txt", 5, "++ content"),
        ("sample.txt", 6, "last"),
    ]


@pytest.mark.parametrize("patch", [
    _patch("one", "@@ -0,0 +1,2 @@"),
    _patch("one") + "+unexpected\n",
    "diff --cc sample.txt\n@@@ -1 -1 +1 @@@\n++unexpected\n",
    "diff --git a/sample.txt b/sample.txt\nBinary files a/sample.txt and b/sample.txt differ\n",
    "diff --git a/sample.txt b/sample.txt\nindex 1234567..7654321 160000\n",
    "unexpected private material\n",
])
def test_strict_rejects_incomplete_or_unsupported_input_without_echoing(patch):
    with pytest.raises(ValueError, match="^[a-z_]+$"):
        parse_diff(patch, strict=True)


def test_strict_mode_only_patch_needs_no_hunk():
    patch = "diff --git a/sample.txt b/sample.txt\nold mode 100644\nnew mode 100755\n"
    parsed = parse_diff(patch, strict=True)
    assert parsed.file_paths == ["sample.txt"]
    assert parsed.added_lines == []


def test_strict_does_not_misread_truncated_content_as_mode_only():
    patch = (
        "diff --git a/sample.txt b/sample.txt\nold mode 100644\nnew mode 100755\n"
        "--- a/sample.txt\n+++ b/sample.txt\n"
    )
    with pytest.raises(ValueError, match="^[a-z_]+$"):
        parse_diff(patch, strict=True)


def test_strict_rejects_non_byte_octal_path_escape():
    patch = (
        'diff --git "a/\\777" "b/\\777"\n'
        '--- "a/\\777"\n+++ "b/\\777"\n'
        "@@ -0,0 +1 @@\n+content\n"
    )
    with pytest.raises(ValueError, match="^[a-z_]+$"):
        parse_diff(patch, strict=True)


_EDIT_HEADER = "diff --git a/sample.txt b/sample.txt\n"
_EDIT_INDEX = f"index {'1' * 40}..{'2' * 40} 100644\n"
_EDIT_PATHS = "--- a/sample.txt\n+++ b/sample.txt\n"
_EDIT_BODY = "@@ -1 +1 @@\n-old\n+SYNTHETIC_PRIVATE_MARKER\n"


@pytest.mark.parametrize("patch", [
    _EDIT_HEADER + _EDIT_PATHS + _EDIT_BODY,
    _EDIT_HEADER + _EDIT_INDEX * 2 + _EDIT_PATHS + _EDIT_BODY,
    _EDIT_HEADER + _EDIT_PATHS + _EDIT_INDEX + _EDIT_BODY,
    _EDIT_HEADER + "new file mode 100644\ndeleted file mode 100644\n" + _EDIT_INDEX + _EDIT_PATHS + _EDIT_BODY,
    _EDIT_HEADER + "old mode 100644\n" + _EDIT_INDEX + _EDIT_PATHS + _EDIT_BODY,
    _EDIT_HEADER + "old mode PRIVATE_MARKER\n" + _EDIT_INDEX + _EDIT_PATHS + _EDIT_BODY,
    _EDIT_HEADER + f"index {'1' * 40}..{'2' * 64}\n" + _EDIT_PATHS + _EDIT_BODY,
    _EDIT_HEADER + "new file mode 100644\n" + f"index {'0' * 40}..{'2' * 40}\n"
    + "--- /dev/null\n+++ b/sample.txt\n" + _EDIT_BODY,
    _EDIT_HEADER + "deleted file mode 100644\n" + f"index {'1' * 40}..{'0' * 40}\n"
    + "--- a/sample.txt\n+++ /dev/null\n" + _EDIT_BODY,
    _EDIT_HEADER + _EDIT_INDEX + "--- /dev/null\n+++ /dev/null\n" + _EDIT_BODY,
    _EDIT_HEADER + _EDIT_INDEX + _EDIT_PATHS + "@@ -0,0 +0,0 @@\n",
    _EDIT_HEADER + _EDIT_INDEX + _EDIT_PATHS + _EDIT_BODY * 2,
    _EDIT_HEADER + _EDIT_INDEX + _EDIT_PATHS + "@@ -0,0 +1,2 @@\n+one\n\\ No newline at end of file\n+two\n",
])
def test_strict_rejects_inconsistent_native_file_state(patch):
    with pytest.raises(ValueError, match="^[a-z_]+$"):
        parse_diff(patch, strict=True)


def _native_git(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    hooks = tmp_path / "empty-hooks"
    hooks.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)

    def git(*args):
        return subprocess.run(
            ["git", "-c", f"core.hooksPath={hooks}", "-c", "commit.gpgsign=false",
             "-c", "core.autocrlf=false", "-c", "user.name=Fixture",
             "-c", "user.email=fixture@example.invalid", *args],
            cwd=repo, env=env, check=True, capture_output=True, timeout=10,
        ).stdout

    git("-c", "init.templateDir=", "init", "-q")
    return repo, git


def _native_patch(git):
    return git(
        "--no-replace-objects", "-c", "core.quotePath=true", "diff-tree",
        "-r", "-p", "-m", "--root", "--no-commit-id", "--no-color",
        "--no-ext-diff", "--no-textconv", "--no-renames", "--no-relative",
        "--src-prefix=a/", "--dst-prefix=b/", "--full-index", "--unified=0", "HEAD",
    ).decode("utf-8")


@pytest.mark.parametrize("filename", ["sample.txt", "with space.txt", "trailing ", 'quo"te.txt', "unicodé.txt"])
@pytest.mark.parametrize("empty", [False, True])
def test_native_root_patch_preserves_all_added_lines(tmp_path, filename, empty):
    repo, git = _native_git(tmp_path)
    contents = [] if empty else [
        "ordinary", "++SYNTHETIC_PRIVATE_MARKER", "++ marker", "a\x85marker", "a\u2028marker",
    ]
    (repo / filename).write_text("\n".join(contents) + ("\n" if contents else ""), encoding="utf-8")
    git("add", filename)
    git("commit", "-qm", "synthetic root")
    patch = _native_patch(git)
    expected = [(filename, i, line) for i, line in enumerate(contents, 1)]
    assert parse_diff(patch).added_lines == expected
    assert parse_diff(patch, strict=True).added_lines == expected
    assert parse_diff(patch, strict=True).file_paths == [filename]


def test_native_mode_deletion_and_type_transitions(tmp_path):
    repo, git = _native_git(tmp_path)
    text, empty = repo / "text", repo / "empty"
    text.write_text("old\n")
    empty.touch()

    def commit(expected):
        git("add", "-A")
        git("commit", "-qm", "synthetic transition")
        patch = _native_patch(git)
        assert parse_diff(patch, strict=True) == parse_diff(patch)
        assert parse_diff(patch, strict=True).added_lines == expected

    commit([("text", 1, "old")])
    text.write_text("new")
    text.chmod(0o755)
    commit([("text", 1, "new")])
    text.chmod(0o644)
    commit([])
    text.unlink()
    empty.unlink()
    commit([])
    text.symlink_to("synthetic-target")
    commit([("text", 1, "synthetic-target")])
    text.unlink()
    text.write_text("regular\n")
    commit([("text", 1, "regular")])
