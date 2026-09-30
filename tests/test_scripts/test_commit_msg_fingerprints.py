"""The install-fingerprint layer of scripts/hooks/commit-msg.

The same fingerprint file is read by two checks: this hook at commit time, and
the pre-push review (src/genesis/contribution/sanitize.py, _check_fingerprints),
which reads each line as a Python regex and falls back to literal text when it
does not compile. The hook used to read the file with grep -E, a different
dialect, so the two disagreed: a Python-only construct (\\A, \\d, a lookbehind)
matched nothing at commit time, and one pattern grep rejected (an unbalanced
paren) made the whole file read as "no match". The hook now reads the file with
the same rules in Python, and these tests hold it to the pre-push reader's
verdict, line for line.

Every message about the file names line numbers only and never its path: the
patterns are the private values, and the path can name a user or a host.
"""

import os
import subprocess
from pathlib import Path

import pytest

from genesis.contribution.sanitize import _check_fingerprints, _ParsedDiff

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "scripts" / "hooks" / "commit-msg"

GOOD = "ULTRA_PRIVATE_TOKEN_123"
LOOKBEHIND = r"(?<![0-9.])\.shh\b"
UNBALANCED = "SECRET_HOST_(9"
# A directory name the output must never contain.
PATH_MARK = "PATHMARK_NOT_FOR_OUTPUT"
# What a real `git commit` has on a stock install: the system python3, no venv.
BASE_PATH = "/usr/bin:/bin:/usr/local/bin"


def _run(
    msg: str | bytes,
    fingerprints: str | bytes,
    tmp_path: Path,
    *,
    path: str = BASE_PATH,
    **extra: str,
) -> subprocess.CompletedProcess:
    d = tmp_path / PATH_MARK
    d.mkdir(exist_ok=True)
    fp = d / "fingerprints.txt"
    if isinstance(fingerprints, bytes):
        fp.write_bytes(fingerprints)
    else:
        fp.write_text(fingerprints)
    f = d / "COMMIT_EDITMSG"
    if isinstance(msg, bytes):
        f.write_bytes(msg)
    else:
        f.write_text(msg)
    return subprocess.run(
        ["/bin/bash", str(HOOK), str(f)],
        capture_output=True,
        text=True,
        env={
            "PATH": path,
            # As a real `git commit` runs it: the user's UTF-8 locale.
            "LC_ALL": "C.UTF-8",
            "HOME": str(tmp_path),
            "GENESIS_RELEASE_FINGERPRINTS": str(fp),
            **extra,
        },
    )


def _blocked_lines(r: subprocess.CompletedProcess) -> set[int]:
    if "install-specific private identifier" not in r.stdout:
        return set()
    return {int(ln.split()[1]) for ln in r.stdout.splitlines() if ln.startswith("  line ")}


def _prepush_lines(msg: str, fingerprints: str, tmp_path: Path) -> set[int]:
    fp = tmp_path / "prepush-fingerprints.txt"
    fp.write_text(fingerprints)
    lines = msg.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    parsed = _ParsedDiff(
        file_paths=["m"],
        added_lines=[("m", n, text) for n, text in enumerate(lines, 1)],
        is_binary=False,
        size_bytes=len(msg),
    )
    return {f.line for f in _check_fingerprints(parsed, fp)}


# Each case: fingerprint lines, then a message whose lines the two readers must
# flag identically. The comments say what grep -E used to do with the pattern.
_PARITY_CASES = {
    # \A: grep -E passes it silently and it matches nothing.
    "python-anchor": ("\\Afix: SECRETA", "fix: SECRETA x\n\nx SECRETA\n"),
    # \d: the same.
    "python-digit-class": (r"tok\d{3}", "fix: ok\n\ntok123\ntokabc\n"),
    # A lookbehind: grep -E warns and matches nothing.
    "lookbehind": (LOOKBEHIND, "fix: ok\n\nat x.shh here\n1.shh\n"),
    # An unbalanced paren: grep -E rejects the whole file.
    "unbalanced-paren": (f"{UNBALANCED}\n{GOOD}", f"fix: ok\n\n{UNBALANCED}\n{GOOD}\n"),
    # An escaped space: some grep builds warn about the stray backslash.
    "escaped-space": (r"private\ value", "fix: ok\n\na private value\n"),
    "hash-inside-a-pattern": ("a#b", "fix: ok\n\nsee a#b\n"),
    "inline-flag": ("(?i)casey", "fix: ok\n\nCASEY here\n"),
    "non-ascii": ("café_host", "fix: ok\n\non café_host\n"),
    "comment-and-trim": (
        f"  # ordinary notes\n\t{GOOD}  \n",
        f"fix: see # ordinary notes\n\n{GOOD}\n",
    ),
    "crlf-file": (f"# comment\r\n{GOOD}\r\n", f"fix: {GOOD}\n"),
    "no-trailing-newline": (f"# comment\n{GOOD}", f"fix: {GOOD}\n"),
}


@pytest.mark.parametrize("case", _PARITY_CASES, ids=list(_PARITY_CASES))
def test_the_hook_flags_the_lines_the_prepush_reader_flags(case, tmp_path):
    fps, msg = _PARITY_CASES[case]
    want = _prepush_lines(msg, fps, tmp_path)
    assert want, "control: the pre-push reader flags something in every case"
    r = _run(msg, fps, tmp_path)
    assert _blocked_lines(r) == want, r.stdout + r.stderr
    assert r.returncode == 1, r.stdout + r.stderr


def test_a_grep_warning_does_not_weaken_a_valid_pattern(tmp_path):
    """Debian's grep warns about a stray backslash when this variable is set,
    and still matches. Round 1 took any warning as "unusable" and fell back to
    searching for the pattern's own text, backslash included."""
    r = _run(
        "fix: ok\n\na private value\n",
        r"private\ value" + "\n",
        tmp_path,
        DEB_GREP_ENABLE_STRAY_BACKSLASH_WARN="1",
    )
    assert r.returncode == 1 and _blocked_lines(r) == {3}, r.stdout + r.stderr


def test_a_clean_message_passes_quietly(tmp_path):
    r = _run("fix: an ordinary subject\n", f"# comment\n\n{GOOD}\n{LOOKBEHIND}\n", tmp_path)
    assert r.returncode == 0 and r.stdout.strip() == "", r.stdout + r.stderr


def test_a_pattern_python_cannot_compile_is_named_by_line_only(tmp_path):
    r = _run("fix: an ordinary subject\n", f"# c\n{GOOD}\n{UNBALANCED}\n", tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "cannot compile" in r.stdout and "(line 3)" in r.stdout, r.stdout
    assert UNBALANCED not in r.stdout + r.stderr, "the pattern is a private value"
    assert PATH_MARK not in r.stdout + r.stderr, "the file's path is never printed"


def test_a_non_utf8_line_does_not_switch_the_other_fingerprints_off(tmp_path):
    """One byte that is not UTF-8, anywhere in the file, used to stop the reader
    before any pattern compiled, so every fingerprint went unchecked."""
    fps = GOOD.encode() + b"\n\xff\xfe garbage line\n"
    r = _run(f"fix: ok\n\nmentions {GOOD}\n", fps, tmp_path)
    assert r.returncode == 1 and _blocked_lines(r) == {3}, r.stdout + r.stderr
    assert "not all UTF-8" in r.stdout and "(line 2)" in r.stdout, r.stdout
    assert PATH_MARK not in r.stdout + r.stderr


def test_a_non_utf8_fingerprint_matches_byte_for_byte(tmp_path):
    """A Latin-1 byte in a fingerprint is kept as itself, in the patterns and the
    message alike. Control: the same text with a different byte passes."""
    fps = b"caf\xe9_host\n"
    hit = _run(b"fix: on caf\xe9_host\n", fps, tmp_path)
    assert hit.returncode == 1 and _blocked_lines(hit) == {1}, hit.stdout + hit.stderr
    miss = _run(b"fix: on cafe_host\n", fps, tmp_path)
    assert miss.returncode == 0, miss.stdout + miss.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
def test_an_unreadable_file_warns_without_blocking_or_naming_it(tmp_path):
    r0 = _run("fix: warm-up\n", f"{GOOD}\n", tmp_path)
    assert r0.returncode == 0, r0.stdout + r0.stderr
    fp = tmp_path / PATH_MARK / "fingerprints.txt"
    fp.chmod(0)
    try:
        msg = tmp_path / PATH_MARK / "COMMIT_EDITMSG"
        msg.write_text(f"fix: {GOOD}\n")
        r = subprocess.run(
            ["/bin/bash", str(HOOK), str(msg)],
            capture_output=True,
            text=True,
            env={
                "PATH": BASE_PATH,
                "LC_ALL": "C.UTF-8",
                "HOME": str(tmp_path),
                "GENESIS_RELEASE_FINGERPRINTS": str(fp),
            },
        )
    finally:
        fp.chmod(0o600)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "cannot be read" in r.stdout, r.stdout
    assert PATH_MARK not in r.stdout + r.stderr


@pytest.mark.parametrize("route", ["SHELLOPTS", "BASH_ENV"])
def test_inherited_errexit_does_not_reject_a_clean_commit(route, tmp_path):
    """git passes its environment to the hook, so errexit and pipefail can
    arrive through an exported SHELLOPTS, or a BASH_ENV file that bash runs
    before the hook's first line."""
    if route == "SHELLOPTS":
        extra = {"SHELLOPTS": "errexit:pipefail"}
    else:
        benv = tmp_path / "bash_env"
        benv.write_text("set -eo pipefail\n")
        extra = {"BASH_ENV": str(benv)}
    fps = f"{GOOD}\n{LOOKBEHIND}\n{UNBALANCED}\n"
    clean = _run("fix: an ordinary subject\n", fps, tmp_path, **extra)
    assert clean.returncode == 0, clean.stdout + clean.stderr
    hit = _run(f"fix: {GOOD}\n", fps, tmp_path, **extra)
    assert hit.returncode == 1 and "BLOCKED" in hit.stdout, hit.stdout + hit.stderr


def test_the_environment_cannot_shadow_the_regex_module(tmp_path):
    """A PYTHONPATH entry comes before the stdlib on sys.path, so a planted re.py
    would replace the matcher; the hook runs Python isolated (-I)."""
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "re.py").write_text(
        "class error(Exception): pass\n"
        "class _P:\n    def search(self, s): return None\n"
        "def compile(p): return _P()\n"
        "def escape(p): return p\n"
    )
    r = _run(f"fix: {GOOD}\n", f"{GOOD}\n", tmp_path, PYTHONPATH=str(shadow))
    assert r.returncode == 1 and "BLOCKED" in r.stdout, r.stdout + r.stderr


def _bin_without_python(tmp_path: Path, python3: str | None = None) -> str:
    """A PATH holding only what the hook needs besides python3, and, when
    given, a stand-in python3."""
    b = tmp_path / "bin"
    b.mkdir()
    for tool in ("bash", "grep", "sed", "sort", "cut", "tr", "cat", "head"):
        for d in ("/usr/bin", "/bin"):
            if os.path.exists(f"{d}/{tool}"):
                (b / tool).symlink_to(f"{d}/{tool}")
                break
    if python3 is not None:
        (b / "python3").write_text(python3)
        (b / "python3").chmod(0o755)
    return str(b)


@pytest.mark.parametrize(
    "python3",
    [None, "#!/bin/sh\nexit 1\n"],
    ids=["python3-absent", "python3-failing"],
)
def test_without_a_working_python3_grep_reads_them_one_at_a_time(python3, tmp_path):
    path = _bin_without_python(tmp_path, python3)
    # No newline after the last line: the loop must still read it.
    fps = f"{UNBALANCED}\n{GOOD}"
    r = _run(f"fix: ok\n\n{GOOD}\n", fps, tmp_path, path=path)
    assert "python3 is not available" in r.stdout, r.stdout
    # The pattern grep rejects does not take the one after it down with it...
    assert r.returncode == 1 and _blocked_lines(r) == {3}, r.stdout + r.stderr
    # ...and is itself matched as literal text.
    lit = _run(f"fix: ok\n\n{UNBALANCED}\n", fps, tmp_path, path=path)
    assert lit.returncode == 1 and _blocked_lines(lit) == {3}, lit.stdout + lit.stderr
    clean = _run("fix: an ordinary subject\n", fps, tmp_path, path=path)
    assert clean.returncode == 0, clean.stdout + clean.stderr
    assert PATH_MARK not in r.stdout + r.stderr + lit.stdout + clean.stdout


def test_without_python3_a_line_grep_reads_differently_is_named(tmp_path):
    """grep cannot emulate Python's dialect: MEASURED on GNU grep 3.11, \\d and a
    "(?" group match nothing where Python matches. The degraded path names such
    lines rather than trusting them, and \\s, which grep reads the same way, is
    not named."""
    path = _bin_without_python(tmp_path)
    fps = "acct-\\d{4}\n(?<!public-)hostx\nx\\sbeta\n" + GOOD + "\n"
    r = _run(f"fix: ok\n\n{GOOD}\n", fps, tmp_path, path=path)
    assert r.returncode == 1 and _blocked_lines(r) == {3}, r.stdout + r.stderr
    assert "may match nothing: line 1 2." in r.stdout, r.stdout
