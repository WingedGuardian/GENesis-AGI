"""genesis.util.update_remote: which git remote an install takes its updates from.

The update source decides what code scripts/update.sh installs and runs. These
tests pin that adding a remote (a contributor's fork, fetched to review their PR)
can never become it: the old rule took the FIRST remote, alphabetically, whose
URL contained the repo name.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from genesis.util import update_remote as ur

REPO = Path(__file__).resolve().parents[2]
PUBLIC = "GENesis-AGI"
UPSTREAM = "https://github.com/owner/GENesis-AGI.git"
CONTRIB = "https://github.com/contributor/GENesis-AGI.git"
PRIVATE = "https://github.com/owner/GENesis.git"


@pytest.mark.parametrize(
    ("url", "name"),
    [
        ("https://github.com/owner/GENesis-AGI.git", "genesis-agi"),
        ("https://github.com/owner/GENesis-AGI", "genesis-agi"),
        ("https://github.com/owner/GENesis-AGI/", "genesis-agi"),
        ("git@github.com:owner/GENesis-AGI.git", "genesis-agi"),
        ("ssh://git@github.com/owner/GENesis-AGI.git", "genesis-agi"),
        ("/srv/mirrors/GENesis-AGI.git", "genesis-agi"),
        ("git@host:GENesis-AGI.git", "genesis-agi"),
        ("https://github.com/owner/GENesis-AGI-tools.git", "genesis-agi-tools"),
        ("https://github.com/owner/GENesis.git", "genesis"),
    ],
)
def test_repo_name_is_the_last_path_component(url, name):
    assert ur.repo_name(url) == name


def test_a_contributor_remote_that_sorts_first_does_not_win():
    """The defect: a contributor's remote whose name sorts before `origin`, with a
    URL that contains the repo name too. origin is what the install was cloned
    from."""
    remotes = {"acontributor": CONTRIB, "origin": UPSTREAM, "private": PRIVATE}
    assert ur.select(remotes, PUBLIC) == ("origin", "origin")


def test_a_repo_whose_name_merely_contains_the_public_name_is_not_a_candidate():
    remotes = {"origin": PRIVATE, "a": "https://github.com/x/GENesis-AGI-tools.git"}
    assert ur.select(remotes, PUBLIC) == ("origin", "fallback")


def test_a_lone_non_origin_candidate_refuses_until_pinned():
    """The private-origin setup: origin is the private repo and another remote is
    the public one. A fork fetched for review carries the public name too, so a
    lone match is not evidence; it refuses until the operator pins it (security
    review, round 1: accepting it let a review fetch become the update source)."""
    with pytest.raises(ur.UpdateRemoteError) as exc:
        ur.select({"origin": PRIVATE, "public": UPSTREAM}, PUBLIC)
    assert ur.PIN_KEY in str(exc.value)
    assert ur.select({"origin": PRIVATE, "public": UPSTREAM}, PUBLIC, pinned="public") == (
        "public",
        "pinned",
    )


def test_two_candidates_and_neither_is_origin_refuses():
    remotes = {"origin": PRIVATE, "public": UPSTREAM, "zcontrib": CONTRIB}
    with pytest.raises(ur.UpdateRemoteError) as exc:
        ur.select(remotes, PUBLIC)
    msg = str(exc.value)
    assert "public" in msg and "zcontrib" in msg
    assert ur.PIN_KEY in msg, "the refusal names the setting that resolves it"


def test_no_candidate_falls_back_to_origin_as_before():
    assert ur.select({"origin": PRIVATE}, PUBLIC) == ("origin", "fallback")


def test_a_pin_wins_over_a_later_remote():
    remotes = {"acontributor": CONTRIB, "origin": UPSTREAM}
    assert ur.select(remotes, PUBLIC, pinned="origin") == ("origin", "pinned")


def test_a_pin_or_override_naming_no_remote_refuses():
    with pytest.raises(ur.UpdateRemoteError):
        ur.select({"origin": UPSTREAM}, PUBLIC, pinned="gone")
    with pytest.raises(ur.UpdateRemoteError):
        ur.select({"origin": UPSTREAM}, PUBLIC, override="gone")


def test_the_override_wins_over_the_pin():
    remotes = {"origin": UPSTREAM, "public": UPSTREAM}
    assert ur.select(remotes, PUBLIC, override="public", pinned="origin") == ("public", "override")


# ── The command line, against a real scratch repository ─────────────────────


def _git(cwd: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def _cli(root: Path, *args: str, **env_extra: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.pop(ur.OVERRIDE_ENV, None)
    env.update(GENESIS_GITHUB_PUBLIC_REPO=PUBLIC, **env_extra)
    return subprocess.run(
        [sys.executable, "-m", "genesis.util.update_remote", str(root), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=str(REPO),
    )


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "remote", "add", "origin", UPSTREAM)
    _git(root, "remote", "add", "private", PRIVATE)
    return root


def test_cli_pins_on_first_use_and_a_later_contributor_remote_cannot_move_it(checkout):
    r = _cli(checkout, "--pin")
    assert (r.returncode, r.stdout.splitlines()[0]) == (0, "origin"), r
    assert _git(checkout, "config", "--get", ur.PIN_KEY) == "origin"
    # Reviewing a contributor's PR adds their fork; it sorts before origin.
    _git(checkout, "remote", "add", "acontributor", CONTRIB)
    r = _cli(checkout, "--pin")
    assert (r.returncode, r.stdout.splitlines()[0]) == (0, "origin"), r


def test_cli_reports_how_the_remote_was_chosen(checkout):
    r = _cli(checkout, "--pin")
    assert r.stdout.splitlines() == ["origin", "origin"], r
    r = _cli(checkout, "--pin")
    assert r.stdout.splitlines() == ["origin", "pinned"], r


def test_a_review_fetch_cannot_become_the_source_in_the_private_origin_setup(tmp_path):
    """The security review's reproduction: origin is private, a fork fetched for a
    review is the only remote naming the public repo, then the real public remote
    is added. Nothing may pin the fork, and the operator's pin then holds."""
    root = tmp_path / "private_origin"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "remote", "add", "origin", PRIVATE)
    _git(root, "remote", "add", "prreview", CONTRIB)
    r = _cli(root, "--pin")
    assert r.returncode == 2 and "prreview" in r.stdout, r
    _git(root, "remote", "add", "public", UPSTREAM)
    r = _cli(root, "--pin")
    assert r.returncode == 2, r
    rc = subprocess.run(
        ["git", "-C", str(root), "config", "--get", ur.PIN_KEY], capture_output=True
    ).returncode
    assert rc == 1, "neither refusal pinned anything"
    _git(root, "config", ur.PIN_KEY, "public")
    _git(root, "remote", "add", "another", "https://github.com/someone/GENesis-AGI.git")
    r = _cli(root, "--pin")
    assert r.stdout.splitlines() == ["public", "pinned"], r


def test_a_pin_in_global_config_is_not_this_checkouts_pin(checkout, tmp_path):
    """The pin is read with --local: a global or system entry must not act as one."""
    _git(checkout, "remote", "add", "acontributor", CONTRIB)
    gcfg = tmp_path / "gitconfig"
    gcfg.write_text("[genesis]\n\tupdateRemote = acontributor\n")
    r = _cli(checkout, "--pin", GIT_CONFIG_GLOBAL=str(gcfg))
    assert r.stdout.splitlines() == ["origin", "origin"], r


def test_cli_without_pin_does_not_write_config(checkout):
    r = _cli(checkout)
    assert (r.returncode, r.stdout.splitlines()[0]) == (0, "origin"), r
    rc = subprocess.run(
        ["git", "-C", str(checkout), "config", "--get", ur.PIN_KEY], capture_output=True
    ).returncode
    assert rc == 1, "a read-only caller must never pin"


def test_cli_refusal_exits_2_with_the_message_on_stdout(tmp_path):
    root = tmp_path / "ambiguous"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "remote", "add", "origin", PRIVATE)
    _git(root, "remote", "add", "public", UPSTREAM)
    _git(root, "remote", "add", "zcontrib", CONTRIB)
    r = _cli(root, "--pin")
    assert r.returncode == 2, r
    assert "zcontrib" in r.stdout and ur.PIN_KEY in r.stdout, r.stdout
    rc = subprocess.run(
        ["git", "-C", str(root), "config", "--get", ur.PIN_KEY], capture_output=True
    ).returncode
    assert rc == 1, "a refusal pins nothing"


def test_cli_override_env(checkout):
    _git(checkout, "remote", "add", "public", UPSTREAM)
    r = _cli(checkout, "--pin", **{ur.OVERRIDE_ENV: "public"})
    assert (r.returncode, r.stdout.splitlines()) == (0, ["public", "override"]), r
    rc = subprocess.run(
        ["git", "-C", str(checkout), "config", "--get", ur.PIN_KEY], capture_output=True
    ).returncode
    assert rc == 1, "an override is per-run; it is not pinned"


# ── update.sh calls the shared rule, not a name match of its own ─────────────


def test_update_sh_resolves_its_remote_through_the_shared_rule():
    text = (REPO / "scripts" / "update.sh").read_text()
    assert "-m genesis.util.update_remote" in text
    assert "--pin" in text
    assert "remote -v" not in text, "update.sh must not pick a remote by name match itself"


def test_the_version_collector_uses_the_shared_rule():
    text = (REPO / "src" / "genesis" / "learning" / "signals" / "genesis_version.py").read_text()
    assert "update_remote" in text
    assert '"remote", "-v"' not in text
