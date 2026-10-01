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
# A minimal PATH with the system python3. The hook runs outside any repository
# (cwd=tmp_path), so it finds no Genesis venv and uses this python3.
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
        cwd=tmp_path,
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


def _prepush_lines(msg: str, fingerprints: str | bytes, tmp_path: Path) -> set[int]:
    fp = tmp_path / "prepush-fingerprints.txt"
    if isinstance(fingerprints, bytes):
        fp.write_bytes(fingerprints)
    else:
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
    # Lines that used to stop a reader outright, beside one ordinary fingerprint:
    # both readers must still flag the ordinary one.
    "repeat-count-overflow": (f"x{{4294967296}}\n{GOOD}", f"fix: ok\n\n{GOOD}\n"),
    "nesting-recursion": ("(" * 1500 + "a" + ")" * 1500 + f"\n{GOOD}", f"fix: ok\n\n{GOOD}\n"),
    "non-utf8-line": (b"# caf\xe9\n\xff\xfe x\n" + GOOD.encode() + b"\n", f"fix: ok\n\n{GOOD}\n"),
}


@pytest.mark.parametrize("case", _PARITY_CASES, ids=list(_PARITY_CASES))
def test_the_hook_flags_the_lines_the_prepush_reader_flags(case, tmp_path):
    fps, msg = _PARITY_CASES[case]
    want = _prepush_lines(msg, fps, tmp_path)
    assert want, "control: the pre-push reader flags something in every case"
    r = _run(msg, fps, tmp_path)
    assert _blocked_lines(r) == want, r.stdout + r.stderr
    assert r.returncode == 1, r.stdout + r.stderr


def test_a_stray_backslash_warning_setting_does_not_weaken_a_pattern(tmp_path):
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


@pytest.mark.parametrize(
    "bad_line",
    ["x{4294967296}", "(" * 1500 + "a" + ")" * 1500],
    ids=["repeat-count-overflow", "nesting-recursion"],
)
def test_a_line_that_breaks_the_compiler_does_not_switch_the_rest_off(bad_line, tmp_path):
    """re.compile raises OverflowError or RecursionError, not re.error, on these.
    They used to end the reader, so every fingerprint went unchecked."""
    r = _run(f"fix: {GOOD}\n", f"{GOOD}\n{bad_line}\n", tmp_path)
    assert r.returncode == 1 and _blocked_lines(r) == {1}, r.stdout + r.stderr
    assert "cannot compile" in r.stdout and "(line 2)" in r.stdout, r.stdout


def test_a_non_utf8_comment_line_is_still_reported(tmp_path):
    """A non-UTF-8 byte anywhere in the file is worth fixing, comments included,
    so the hook names such a line even though it is not a pattern."""
    fps = b"# caf\xe9 notes\n" + GOOD.encode() + b"\n"
    r = _run(f"fix: {GOOD}\n", fps, tmp_path)
    assert r.returncode == 1 and _blocked_lines(r) == {1}, r.stdout + r.stderr
    assert "not all UTF-8" in r.stdout and "(line 1)" in r.stdout, r.stdout


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
            cwd=tmp_path,
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
    for tool in ("bash", "grep", "sed", "sort", "cut", "tr", "cat", "head", "git"):
        for d in ("/usr/bin", "/bin"):
            if os.path.exists(f"{d}/{tool}"):
                (b / tool).symlink_to(f"{d}/{tool}")
                break
    if python3 is not None:
        (b / "python3").write_text(python3)
        (b / "python3").chmod(0o755)
    return str(b)


def test_the_genesis_venv_interpreter_is_preferred(tmp_path):
    """The pre-push review runs under the Genesis venv, and a regex can compile
    under one Python version and not another, so the hook uses that venv when
    the repository has one, even with no python3 on PATH."""
    repo = tmp_path / "repo"
    (repo / ".venv" / "bin").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    marker = tmp_path / "venv_used"
    venv_python = repo / ".venv" / "bin" / "python"
    venv_python.write_text(f'#!/bin/sh\n/usr/bin/touch {marker}\nexec /usr/bin/python3 "$@"\n')
    venv_python.chmod(0o755)
    path = _bin_without_python(tmp_path)
    fp = tmp_path / "fingerprints.txt"
    fp.write_text(f"{GOOD}\n")
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(f"fix: {GOOD}\n")
    r = subprocess.run(
        ["/bin/bash", str(HOOK), str(msg)],
        capture_output=True,
        text=True,
        cwd=repo,
        env={
            "PATH": path,
            "LC_ALL": "C.UTF-8",
            "HOME": str(tmp_path),
            "GENESIS_RELEASE_FINGERPRINTS": str(fp),
        },
    )
    assert r.returncode == 1 and "BLOCKED" in r.stdout, r.stdout + r.stderr
    assert "could not run" not in r.stdout, r.stdout
    assert marker.exists(), "the venv interpreter was not the one that ran"


@pytest.mark.parametrize(
    "python3",
    [None, "#!/bin/sh\nexit 1\n"],
    ids=["python3-absent", "python3-failing"],
)
def test_without_a_usable_python_the_check_says_it_did_not_run(python3, tmp_path):
    """There is no grep fallback: grep is another regex dialect. A checkout with
    this hook installed has built a Python venv, so this is a broken install, and
    the hook says loudly that nothing was checked rather than blocking."""
    path = _bin_without_python(tmp_path, python3)
    r = _run(f"fix: ok\n\n{GOOD}\n", f"{GOOD}\n", tmp_path, path=path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "check could not run" in r.stdout and "NOT checked" in r.stdout, r.stdout
    assert PATH_MARK not in r.stdout + r.stderr


def test_a_linked_worktree_uses_the_main_checkouts_venv(tmp_path):
    """Commits are made from linked worktrees, which have no .venv of their own:
    the hook finds the main checkout's through git's common directory."""
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    ident = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run(
        ["git", "-C", str(main), *ident, "commit", "-q", "--allow-empty", "-m", "base"], check=True
    )
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(wt)], check=True)
    (main / "scripts").mkdir()
    (main / ".venv" / "bin").mkdir(parents=True)
    marker = tmp_path / "venv_used"
    venv_python = main / ".venv" / "bin" / "python"
    venv_python.write_text(f'#!/bin/sh\n/usr/bin/touch {marker}\nexec /usr/bin/python3 "$@"\n')
    venv_python.chmod(0o755)
    path = _bin_without_python(tmp_path)
    fp = tmp_path / "fingerprints.txt"
    fp.write_text(f"{GOOD}\n")
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(f"fix: {GOOD}\n")
    r = subprocess.run(
        ["/bin/bash", str(HOOK), str(msg)],
        capture_output=True,
        text=True,
        cwd=wt,
        env={
            "PATH": path,
            "LC_ALL": "C.UTF-8",
            "HOME": str(tmp_path),
            "GENESIS_RELEASE_FINGERPRINTS": str(fp),
        },
    )
    assert r.returncode == 1 and "BLOCKED" in r.stdout, r.stdout + r.stderr
    assert marker.exists(), "the main checkout's venv interpreter was not the one that ran"


def _two_venv_worktree(tmp_path, dev_local, main_has_scripts=True):
    """A main checkout and a linked worktree, each with its own venv whose
    interpreter records that it ran; the hook runs from the worktree."""
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    ident = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run(
        ["git", "-C", str(main), *ident, "commit", "-q", "--allow-empty", "-m", "base"], check=True
    )
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(wt)], check=True)
    if main_has_scripts:
        (main / "scripts").mkdir()
    for root, name in ((main, "main_used"), (wt, "wt_used")):
        (root / ".venv" / "bin").mkdir(parents=True)
        py = root / ".venv" / "bin" / "python"
        py.write_text(f'#!/bin/sh\n/usr/bin/touch {tmp_path / name}\nexec /usr/bin/python3 "$@"\n')
        py.chmod(0o755)
    fp = tmp_path / "fingerprints.txt"
    fp.write_text(f"{GOOD}\n")
    msg = tmp_path / "COMMIT_EDITMSG"
    msg.write_text(f"fix: {GOOD}\n")
    env = {
        "PATH": _bin_without_python(tmp_path),
        "LC_ALL": "C.UTF-8",
        "HOME": str(tmp_path),
        "GENESIS_RELEASE_FINGERPRINTS": str(fp),
    }
    if dev_local:
        env["GENESIS_HOOK_DEV_LOCAL"] = "1"
    return subprocess.run(
        ["/bin/bash", str(HOOK), str(msg)], capture_output=True, text=True, cwd=wt, env=env
    )


def test_a_worktree_with_its_own_venv_still_uses_the_main_checkouts(tmp_path):
    """The same order as .claude/hooks/genesis-hook, which runs the pre-push
    review: the main checkout's venv before this checkout's, so both checks read
    the fingerprints under the same Python even when the worktree's is older."""
    r = _two_venv_worktree(tmp_path, dev_local=False)
    assert r.returncode == 1 and "BLOCKED" in r.stdout, r.stdout + r.stderr
    assert (tmp_path / "main_used").exists() and not (tmp_path / "wt_used").exists()


def test_dev_local_uses_the_worktrees_venv_first(tmp_path):
    """GENESIS_HOOK_DEV_LOCAL=1 is the launcher's switch to a worktree's own hook
    tree and venv; the commit hook follows it the same way."""
    r = _two_venv_worktree(tmp_path, dev_local=True)
    assert r.returncode == 1 and "BLOCKED" in r.stdout, r.stdout + r.stderr
    assert (tmp_path / "wt_used").exists() and not (tmp_path / "main_used").exists()


def test_a_main_root_that_is_not_a_genesis_checkout_is_not_trusted(tmp_path):
    """genesis-hook trusts the main checkout only when it has a scripts/ directory
    (a --separate-git-dir clone's common dir has no checkout beside it); the
    commit hook applies the same test, so it falls through to this checkout's venv."""
    r = _two_venv_worktree(tmp_path, dev_local=False, main_has_scripts=False)
    assert r.returncode == 1 and "BLOCKED" in r.stdout, r.stdout + r.stderr
    assert (tmp_path / "wt_used").exists() and not (tmp_path / "main_used").exists()
