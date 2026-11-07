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


def _child_env(**env_extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.pop(ur.OVERRIDE_ENV, None)
    # Run THIS tree's module: the editable install points `genesis` at the main
    # checkout, so from a linked worktree the child would otherwise import a
    # different copy (or none, before this module exists on main).
    src = str(REPO / "src")
    env["PYTHONPATH"] = os.pathsep.join(p for p in (src, env.get("PYTHONPATH", "")) if p)
    env.update(GENESIS_GITHUB_PUBLIC_REPO=PUBLIC, **env_extra)
    return env


def _cli(root: Path, *args: str, **env_extra: str) -> subprocess.CompletedProcess:
    env = _child_env(**env_extra)
    return subprocess.run(
        [sys.executable, "-m", "genesis.util.update_remote", str(root), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=str(REPO),
    )


def test_cli_runs_this_trees_module():
    """The CLI tests must exercise the module beside this test file, not the
    copy an editable install resolves (the main checkout, from a worktree)."""
    probe = subprocess.run(
        [sys.executable, "-c", "import genesis.util.update_remote as m; print(m.__file__)"],
        capture_output=True,
        text=True,
        timeout=60,
        env=_child_env(),
        cwd=str(REPO),
    )
    assert (
        Path(probe.stdout.strip()).resolve()
        == (REPO / "src" / "genesis" / "util" / "update_remote.py").resolve()
    ), probe


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


# ── Round 1 (#2726): remotes by name, and update.sh's resolution block ───────


def test_a_fetch_url_with_whitespace_is_read_whole(tmp_path):
    """`git remote -v` splits such a URL across fields; the remote must not be
    dropped (which would fall back to origin and could pin the wrong source)."""
    root = tmp_path / "spaced"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "remote", "add", "origin", PRIVATE)
    _git(root, "remote", "add", "public", "/srv/My Repos/GENesis-AGI.git")
    remotes = ur.fetch_remotes(root)
    assert remotes["public"] == "/srv/My Repos/GENesis-AGI.git"
    assert remotes["origin"] == PRIVATE


def test_an_uppercase_git_suffix_still_names_the_public_repo():
    assert ur.repo_name("/srv/mirrors/GENesis-AGI.GIT") == "genesis-agi"
    remotes = {"origin": PRIVATE, "public": "/srv/mirrors/GENesis-AGI.GIT"}
    with pytest.raises(ur.UpdateRemoteError):  # a candidate, so not a silent origin fallback
        ur.select(remotes, PUBLIC)


def test_a_remote_named_like_an_option_does_not_break_resolution(tmp_path):
    """An unrelated remote named `--all` must not make every update path fail."""
    root = tmp_path / "dashed"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "remote", "add", "origin", UPSTREAM)
    _git(root, "remote", "add", "--", "--all", CONTRIB)
    remotes = ur.fetch_remotes(root)
    assert remotes == {"origin": UPSTREAM, "--all": CONTRIB}
    assert ur.select(remotes, PUBLIC) == ("origin", "origin")


def test_the_degraded_path_refuses_a_pin_that_is_not_a_plain_name(tmp_path):
    """The same plain_name rule as the helper: such a name would reach
    `git fetch` as an option."""
    root = tmp_path / "dashed_pin"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "remote", "add", "origin", PRIVATE)
    _git(root, "remote", "add", "--", "--all", UPSTREAM)
    _git(root, "config", "--local", ur.PIN_KEY, "--all")
    r = _run_block(tmp_path, root, helper_rc=1)
    assert r.returncode == 3, r
    assert "is not a plain remote name" in r.stdout


@pytest.mark.parametrize("pick", ["a..b", "a."])
def test_the_degraded_path_refuses_a_pin_that_cannot_form_a_ref(tmp_path, pick):
    root = tmp_path / "badref"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "remote", "add", "origin", UPSTREAM)
    _git(root, "config", "--local", ur.PIN_KEY, pick)
    r = _run_block(tmp_path, root, helper_rc=1)
    assert r.returncode == 3, r
    assert "is not a plain remote name" in r.stdout


@pytest.mark.parametrize(
    ("name", "plain"),
    [
        ("origin", True),
        ("upstream-2", True),
        ("my.fork_x", True),
        ("--all", False),
        ("-x", False),
        (".hidden", False),
        ("a b", False),
        ("foo\u0085bar", False),
        ("a/b", False),
        ("x.lock", False),
        ("a..b", False),
        ("a.", False),
        ("v1.2", True),
        ("", False),
    ],
)
def test_plain_name(name, plain):
    assert ur.plain_name(name) is plain


@pytest.mark.parametrize("label", ["override", "pinned"])
def test_an_override_or_pin_that_is_not_a_plain_name_refuses(label):
    remotes = {"origin": UPSTREAM, "--all": CONTRIB}
    with pytest.raises(ur.UpdateRemoteError, match="not a plain remote name"):
        ur.select(remotes, PUBLIC, **{label if label == "override" else "pinned": "--all"})


def test_a_remote_that_is_not_a_plain_name_naming_the_public_repo_refuses():
    """A `--all` remote naming the public repo beside a private origin may BE the
    public remote: never chosen, and never a reason to fall back to the private
    origin either (round 4: the fallback would deploy the private repository)."""
    with pytest.raises(ur.UpdateRemoteError, match="not a plain remote name"):
        ur.select({"origin": PRIVATE, "--all": UPSTREAM}, PUBLIC)


def test_origin_naming_the_public_repo_still_wins_over_an_unusable_remote():
    assert ur.select({"origin": UPSTREAM, "--all": CONTRIB}, PUBLIC) == ("origin", "origin")


def test_an_unusable_remote_naming_another_repo_does_not_block_the_fallback():
    assert ur.select({"origin": PRIVATE, "--all": "/srv/other.git"}, PUBLIC) == (
        "origin",
        "fallback",
    )


def test_a_url_ending_in_whitespace_keeps_it(tmp_path):
    """Only git's record LF is removed; a private path ending in a space must not
    be read as one naming the public repository."""
    root = tmp_path / "trailing"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "remote", "add", "origin", "/private/GENesis-AGI.git ")
    remotes = ur.fetch_remotes(root)
    assert remotes["origin"] == "/private/GENesis-AGI.git "
    assert ur.repo_name(remotes["origin"]) != "genesis-agi"


@pytest.mark.parametrize(
    ("url", "name"),
    [
        ("/srv/mirror:GENesis-AGI.git", "mirror:genesis-agi"),
        ("./mirror:GENesis-AGI.git", "mirror:genesis-agi"),
        ("git@host:owner/GENesis-AGI.git", "genesis-agi"),
        ("host:GENesis-AGI.git", "genesis-agi"),
        ("ssh://git@host:22/owner/GENesis-AGI.git", "genesis-agi"),
    ],
)
def test_a_colon_is_a_host_separator_only_in_scp_form(url, name):
    assert ur.repo_name(url) == name


def test_a_remote_name_with_a_unicode_separator_is_read_whole(tmp_path):
    """git ends each name with LF only; str.splitlines() would split on NEL."""
    root = tmp_path / "nel"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "remote", "add", "origin", UPSTREAM)
    _git(root, "remote", "add", "foo\u0085bar", CONTRIB)
    remotes = ur.fetch_remotes(root)
    assert remotes == {"origin": UPSTREAM, "foo\u0085bar": CONTRIB}
    assert ur.select(remotes, PUBLIC) == ("origin", "origin")


def test_the_callers_git_dir_does_not_redirect_the_probes(tmp_path, monkeypatch):
    target = tmp_path / "target"
    other = tmp_path / "other"
    for root, url in ((target, UPSTREAM), (other, PRIVATE)):
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        _git(root, "remote", "add", "origin", url)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    assert ur.fetch_remotes(target) == {"origin": UPSTREAM}


def test_callers_read_the_fully_qualified_tracking_ref():
    """`<remote>/main` resolves refs/heads/<remote>/main first, so a remote named
    `heads` (or a local branch `<remote>/main`) would shadow it."""
    assert ur.tracking_ref("heads") == "refs/remotes/heads/main"
    for rel in (
        "src/genesis/dashboard/routes/updates.py",
        "src/genesis/learning/signals/genesis_version.py",
    ):
        text = (REPO / rel).read_text()
        assert '{remote}/main"' not in text and '_update_remote)}/main"' not in text, rel
        assert "tracking_ref(" in text, rel


def test_a_remote_path_that_is_not_utf8_is_read_and_round_trips(tmp_path):
    root = tmp_path / "bytes"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "remote", "add", "origin", PRIVATE)
    raw = os.fsencode(str(tmp_path)) + b"/\xff/GENesis-AGI.git"
    subprocess.run(
        [b"git", b"-C", os.fsencode(str(root)), b"remote", b"add", b"public", raw], check=True
    )
    remotes = ur.fetch_remotes(root)
    assert os.fsencode(remotes["public"]) == raw
    assert ur.repo_name(remotes["public"]) == "genesis-agi"


def _resolution_block() -> str:
    text = (REPO / "scripts" / "update.sh").read_text()
    start = text.index("# ── Resolve upstream remote")
    end = text.index("unset GENESIS_UPDATE_REMOTE\n") + len("unset GENESIS_UPDATE_REMOTE\n")
    return text[start:end]


def _run_block(
    tmp_path, root: Path, *, post_merge=False, helper_rc=1, env_extra=None, helper_body=None
):
    """Run update.sh's resolution block alone, with a fake venv python that
    exits ``helper_rc`` (1 = the helper could not run)."""
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    py = venv / "bin" / "python"
    py.write_text(helper_body or f"#!/bin/sh\nexit {helper_rc}\n")
    py.chmod(0o755)
    script = (
        # The mode update.sh runs under, so a -u or -e regression fails here too.
        "set -Eeuo pipefail\n"
        f'POST_MERGE={"true" if post_merge else "false"}\n'
        f'VENV_DIR="{venv}"\nGENESIS_ROOT="{root}"\n'
        + _resolution_block()
        + 'echo "RESULT remote=$UPDATE_REMOTE how=$_UR_HOW override=${GENESIS_UPDATE_REMOTE-unset}"\n'
    )
    env = _child_env(**(env_extra or {}))
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=60, env=env
    )


def _result(proc) -> str:
    return next(line for line in proc.stdout.splitlines() if line.startswith("RESULT"))


def test_post_merge_does_not_resolve_and_cannot_be_stranded(tmp_path, checkout):
    # A refusal (exit 2) must not stop --post-merge, which never fetches.
    r = _run_block(tmp_path, checkout, post_merge=True, helper_rc=2)
    assert r.returncode == 0, r
    assert _result(r) == "RESULT remote= how= override=unset"


def test_a_helper_that_cannot_run_still_honours_the_pin(tmp_path, checkout):
    _git(checkout, "config", "--local", ur.PIN_KEY, "private")
    r = _run_block(tmp_path, checkout, helper_rc=1)
    assert r.returncode == 0, r
    assert _result(r).startswith("RESULT remote=private how=pinned"), r.stdout


def test_a_helper_that_cannot_run_refuses_a_pin_naming_no_remote(tmp_path, checkout):
    _git(checkout, "config", "--local", ur.PIN_KEY, "gone")
    r = _run_block(tmp_path, checkout, helper_rc=1)
    assert r.returncode == 3, r
    assert "is not a remote of this checkout" in r.stdout


def test_a_helper_that_cannot_run_uses_origin_only_when_it_is_the_only_remote(tmp_path):
    root = tmp_path / "solo"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "remote", "add", "origin", UPSTREAM)
    r = _run_block(tmp_path, root, helper_rc=1)
    assert r.returncode == 0, r
    assert _result(r).startswith("RESULT remote=origin how=fallback"), r.stdout


def test_a_helper_that_cannot_run_refuses_with_other_remotes_and_no_pin(tmp_path):
    """The private-origin setup: the rule would refuse, so the degraded path must
    not install origin's (the private fork's) main instead."""
    root = tmp_path / "private_origin"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "remote", "add", "origin", PRIVATE)
    _git(root, "remote", "add", "public", UPSTREAM)
    r = _run_block(tmp_path, root, helper_rc=1)
    assert r.returncode == 3, r
    assert "cannot" in r.stdout and "be chosen safely" in r.stdout


def test_a_helper_that_cannot_run_still_honours_the_override(tmp_path):
    root = tmp_path / "private_origin2"
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "remote", "add", "origin", PRIVATE)
    _git(root, "remote", "add", "public", UPSTREAM)
    r = _run_block(tmp_path, root, helper_rc=1, env_extra={ur.OVERRIDE_ENV: "public"})
    assert r.returncode == 0, r
    assert _result(r).startswith("RESULT remote=public how=override"), r.stdout


def test_a_refusal_is_exit_3_not_a_script_error(tmp_path, checkout):
    r = _run_block(tmp_path, checkout, helper_rc=2)
    assert r.returncode == 3, r


def test_the_supervisor_prompts_never_let_a_session_choose_the_source():
    text = (REPO / "src" / "genesis" / "dashboard" / "routes" / "updates.py").read_text()
    flat = " ".join(text.split())
    assert "If exit 3 (the update source is not chosen)" in text
    # Tier 1 (exit 3), tier 2 (script errors) and tier 3 all forbid it.
    assert flat.count("Never set or change the git config key genesis.updateRemote") >= 3
    assert "git merge origin/main" not in text, "resolvers merge the exact conflicting commit"
    sh = (REPO / "scripts" / "update.sh").read_text()
    assert '"deploy_head": os.environ.get("UC_DEPLOY_HEAD"' in sh


def test_the_one_run_override_is_dropped_after_resolution(tmp_path, checkout):
    """The helper must SEE the override (it is the one deciding), and nothing
    after the block may inherit it."""
    helper = (
        "#!/bin/sh\n"
        'echo "$GENESIS_UPDATE_REMOTE"\n'
        'echo "override:$GENESIS_UPDATE_REMOTE"\n'
        "exit 0\n"
    )
    r = _run_block(
        tmp_path, checkout, env_extra={ur.OVERRIDE_ENV: "private"}, helper_body=helper
    )
    assert r.returncode == 0, r
    assert "Update remote: private (override:private)" in r.stdout, r.stdout
    assert _result(r).endswith("override=unset"), r.stdout


def test_the_dashboard_check_uses_the_shared_rule():
    text = (REPO / "src" / "genesis" / "dashboard" / "routes" / "updates.py").read_text()
    check = text[text.index("def update_check"):]
    check = check[: check.index("\n@blueprint")] if "\n@blueprint" in check else check
    assert "update_remote(" in check
    assert '"origin"' not in check, "the manual check must not hard-code origin"


def test_the_collector_runs_the_git_probes_off_the_event_loop():
    """_update_remote shells out to git (up to 30 s per call); the async
    collector must not call it on the shared awareness loop."""
    text = (REPO / "src" / "genesis" / "learning" / "signals" / "genesis_version.py").read_text()
    import re

    calls = len(re.findall(r"(?<!def )\b_update_remote\(\)", text))
    threaded = text.count("asyncio.to_thread(_update_remote)")
    assert threaded == 3, threaded
    assert calls == 0, "every collector call goes through asyncio.to_thread"
