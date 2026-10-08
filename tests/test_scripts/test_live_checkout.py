"""scripts/lib/live_checkout.py: the verdict the deploy refusals consult on `live`.

Driven through its command line, as deploy_code_only.sh and the dashboard routes
run it (`python3 -I -S <file> <root>`), against scratch repositories and a
private HOME holding the deploy manifest.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "live_checkout.py"

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="git + POSIX paths")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture()
def world(tmp_path):
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    root = tmp_path / "root"
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    _git(
        root,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "seed",
    )
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", "-b", "main", str(other)], check=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "PYTHON"))}
    env["HOME"] = str(home)
    return {"home": home, "root": root, "other": other, "env": env, "tmp": tmp_path}


def _manifest(w, payload) -> None:
    path = w["home"] / ".genesis" / "deploy_manifest.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))


def _bound(repo: Path) -> dict:
    return {"version": 2, "repo": str((repo / ".git").resolve()), "candidates": []}


def _verdict(w, root: Path | None = None, *extra: str) -> tuple[int, str]:
    args = [str(root or w["root"]), *extra] if root is not False else list(extra)
    p = subprocess.run(
        [sys.executable, "-I", "-S", str(SCRIPT), *args],
        env=w["env"],
        capture_output=True,
        text=True,
    )
    return p.returncode, p.stdout.strip()


def test_live_and_bound_here_is_live(world):
    _git(world["root"], "checkout", "-qb", "live")
    _manifest(world, _bound(world["root"]))
    assert _verdict(world) == (0, "live")


def test_a_symlinked_spelling_of_this_repository_is_still_this_repository(world):
    _git(world["root"], "checkout", "-qb", "live")
    link = world["tmp"] / "link"
    link.symlink_to(world["root"])
    _manifest(world, {"version": 2, "repo": str(link / ".git"), "candidates": []})
    assert _verdict(world) == (0, "live")


@pytest.mark.parametrize(
    ("case", "branch", "manifest"),
    [
        ("main, no manifest", None, None),
        ("main, manifest bound here", None, "here"),
        # Never consulted off `live`, so a broken manifest changes nothing on main.
        ("main, malformed manifest", None, "{not json"),
        ("live, no manifest", "live", None),
        ("live, manifest bound to another repository", "live", "other"),
        ("a branch merely containing live", "live-x", "here"),
    ],
)
def test_other(world, case, branch, manifest):
    if branch:
        _git(world["root"], "checkout", "-qb", branch)
    if manifest == "here":
        _manifest(world, _bound(world["root"]))
    elif manifest == "other":
        _manifest(world, _bound(world["other"]))
    elif manifest is not None:
        _manifest(world, manifest)
    assert _verdict(world) == (1, "other"), case


def test_a_detached_head_is_not_live(world):
    _git(world["root"], "checkout", "-q", "--detach")
    _manifest(world, _bound(world["root"]))
    assert _verdict(world) == (1, "other")


@pytest.mark.parametrize(
    ("case", "payload"),
    [
        ("malformed JSON", "{not json"),
        ("not an object", json.dumps([1, 2])),
        ("no repo key", json.dumps({"version": 2, "candidates": []})),
        ("repo is relative", json.dumps({"version": 2, "repo": ".git", "candidates": []})),
        ("repo is a work tree, not a git dir", "WORKTREE"),
        ("repo does not exist", json.dumps({"version": 2, "repo": "/nonexistent/.git"})),
        ("repo is not a string", json.dumps({"version": 2, "repo": 7})),
    ],
)
def test_on_live_a_manifest_that_cannot_answer_is_unreadable(world, case, payload):
    _git(world["root"], "checkout", "-qb", "live")
    if payload == "WORKTREE":
        payload = json.dumps({"version": 2, "repo": str(world["root"]), "candidates": []})
    _manifest(world, payload)
    assert _verdict(world) == (2, "unreadable"), case


def test_on_live_an_unreadable_manifest_file_is_unreadable(world):
    _git(world["root"], "checkout", "-qb", "live")
    # A directory where the file goes: it exists, and cannot be read as JSON.
    (world["home"] / ".genesis" / "deploy_manifest.json").mkdir()
    assert _verdict(world) == (2, "unreadable")


def test_a_directory_git_cannot_read_is_unreadable(world):
    plain = world["tmp"] / "plain"
    plain.mkdir()
    # A path that does not exist: git cannot answer for it on any host (a real
    # directory could sit inside some enclosing repository, and the module drops
    # GIT_CEILING_DIRECTORIES with the rest of GIT_*).
    assert _verdict(world, plain / "absent") == (2, "unreadable")


def test_a_wrong_argument_count_is_unreadable(world):
    assert _verdict(world, False)[0] == 2


def test_a_tag_named_live_does_not_hide_the_branch(world):
    """`symbolic-ref --short` prints heads/live when a tag shares the name."""
    _git(world["root"], "tag", "live")
    _git(world["root"], "checkout", "-qb", "live")
    _manifest(world, _bound(world["root"]))
    assert _verdict(world) == (0, "live")


def test_an_inherited_git_dir_does_not_redirect_the_answer(world):
    """GIT_DIR from the caller names another repository; the verdict is still
    about <root>."""
    _git(world["root"], "checkout", "-qb", "live")
    _manifest(world, _bound(world["root"]))
    world["env"]["GIT_DIR"] = str(world["other"] / ".git")
    assert _verdict(world) == (0, "live")


SHELL_LIB = SCRIPT.parent / "deploy_checkout.sh"


@pytest.mark.parametrize(
    ("case", "text", "want"),
    [
        ("the real module", None, 0),
        ("a copy that does not parse", "def (\n", 2),
        ("a copy that dies in an uncaught exception", "raise SystemExit('x')\n", 2),
        ("a copy that prints the wrong word for its code", "print('other')\n", 2),
        ("no copy read at all", "", 2),
    ],
)
def test_the_shell_wrapper_acts_only_on_an_agreeing_verdict(world, case, text, want):
    """genesis_live_checkout runs the text the caller read; python exits 1 on a
    SyntaxError or an uncaught exception, which must never read as `other`."""
    _git(world["root"], "checkout", "-qb", "live")
    _manifest(world, _bound(world["root"]))
    code = SCRIPT.read_text() if text is None else text
    p = subprocess.run(
        [
            "bash",
            "-c",
            '. "$1"; genesis_live_checkout "$2"',
            "_",
            str(SHELL_LIB),
            str(world["root"]),
        ],
        env={**world["env"], "_LIVE_CHECKOUT_PY": code},
        capture_output=True,
        text=True,
    )
    assert p.returncode == want, (case, p.stdout, p.stderr)
