"""The install-fingerprint layer of scripts/hooks/commit-msg, when a pattern in
the fingerprint file is one grep -E cannot use.

The hook greps the file with grep -E. Two failures were silent before:
  * a pattern grep -E REJECTS (an unbalanced paren) made the whole -f read exit
    2, which the hook took as "no match", so EVERY fingerprint went dark;
  * a pattern grep -E only WARNS about (a PCRE lookbehind, "(?<!...)") never
    matched anything, and the warning was the only sign.

The generator writes patterns valid in both grep -E and Python's re; a hand
edit need not. So each line is tried alone, unusable ones are left out (the
rest keep working) and named by line number, never by value: the pattern is
the private value.
"""

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "scripts" / "hooks" / "commit-msg"

GOOD = "ULTRA_PRIVATE_TOKEN_123"
LOOKBEHIND = r"(?<![0-9.])\.shh\b"
UNBALANCED = "SECRET_HOST_(9"


def _run(msg: str, fingerprints: str, tmp_path: Path) -> subprocess.CompletedProcess:
    fp = tmp_path / "fingerprints.txt"
    fp.write_text(fingerprints)
    f = tmp_path / "COMMIT_EDITMSG"
    f.write_text(msg)
    return subprocess.run(
        ["bash", str(HOOK), str(f)],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            # As a real `git commit` runs it: the user's UTF-8 locale.
            "LC_ALL": "C.UTF-8",
            "HOME": str(tmp_path),
            "GENESIS_RELEASE_FINGERPRINTS": str(fp),
        },
    )


def test_a_rejected_pattern_no_longer_disables_every_fingerprint(tmp_path):
    fps = f"# install fingerprints\n{GOOD}\n{UNBALANCED}\n"
    r = _run(f"fix: a subject\n\nmentions {GOOD} here\n", fps, tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "BLOCKED: Commit message contains an install-specific private identifier" in r.stdout
    assert "line 3" in r.stdout and "cannot use" in r.stdout, r.stdout
    assert UNBALANCED not in r.stdout + r.stderr, "the unusable pattern is a private value"


def test_a_pcre_lookbehind_is_named_and_the_rest_still_work(tmp_path):
    fps = f"{LOOKBEHIND}\n# a comment\n{GOOD}\n"
    r = _run(f"fix: a subject\n\nmentions {GOOD} here\n", fps, tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "line 1)" in r.stdout and "cannot use" in r.stdout, r.stdout
    assert LOOKBEHIND not in r.stdout + r.stderr
    assert "warning" not in r.stderr, "grep's own warning should not leak through"


def test_an_unusable_pattern_alone_warns_and_does_not_block(tmp_path):
    r = _run("fix: an ordinary subject\n", f"{LOOKBEHIND}\n", tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "cannot use" in r.stdout, r.stdout


def test_a_clean_file_says_nothing_and_still_blocks(tmp_path):
    fps = f"# comment\n\n{GOOD}\n"
    clean = _run("fix: an ordinary subject\n", fps, tmp_path)
    assert clean.returncode == 0 and "cannot use" not in clean.stdout, clean.stdout
    hit = _run(f"fix: {GOOD}\n", fps, tmp_path)
    assert hit.returncode == 1 and "cannot use" not in hit.stdout, hit.stdout


def test_a_comment_line_is_not_a_pattern(tmp_path):
    r = _run("fix: see # ordinary notes\n", f"  # ordinary notes\n{GOOD}\n", tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_last_line_without_a_newline_is_still_read(tmp_path):
    r = _run(f"fix: {GOOD}\n", f"# comment\n{GOOD}", tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr


# Parity with the pre-push reader of the same file
# (src/genesis/contribution/sanitize.py, _check_fingerprints): it strips each line
# and matches a line Python cannot compile as literal text. The commit hook is the
# only reader that blocks, so it must not protect less.
def test_a_rejected_pattern_still_blocks_its_literal_text(tmp_path):
    r = _run(f"fix: a subject\n\nmentions {UNBALANCED} here\n", f"{UNBALANCED}\n", tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "BLOCKED" in r.stdout and "cannot use" in r.stdout, r.stdout
    assert UNBALANCED not in r.stdout + r.stderr


def test_a_crlf_fingerprint_file_still_blocks(tmp_path):
    r = _run(f"fix: {GOOD}\n", f"# comment\r\n{GOOD}\r\n", tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "cannot use" not in r.stdout, r.stdout


def test_surrounding_whitespace_on_a_line_is_trimmed(tmp_path):
    r = _run(f"fix: x{GOOD}y\n", f"   {GOOD}\t \n", tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
