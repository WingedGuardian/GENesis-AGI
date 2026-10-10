"""update.sh's managed-units preflight: refuse over a hand-edited unit BEFORE the stop.

Runs the real ``# BEGIN/END managed-units-preflight`` block of scripts/update.sh
against a scratch repository and HOME, with the real checker
(scripts/lib/managed_units.py) committed into that repository, as update.sh reads
it from git.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
UPDATE = REPO / "scripts" / "update.sh"
CHECKER = REPO / "scripts" / "lib" / "managed_units.py"
TEMPLATE = "[Service]\nExecStart=__VENV__/bin/python -m demo\nRestart=on-failure\n"


def _block() -> str:
    text = UPDATE.read_text()
    start = text.index("# BEGIN managed-units-preflight")
    end = text.index("# END managed-units-preflight")
    return text[start:end]


def _git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


@pytest.fixture
def world(tmp_path: Path):
    repo, home = tmp_path / "repo", tmp_path / "home"
    (repo / "scripts" / "systemd").mkdir(parents=True)
    (repo / "scripts" / "lib").mkdir(parents=True)
    (repo / "scripts" / "lib" / "managed_units.py").write_text(CHECKER.read_text())
    (repo / "scripts" / "systemd" / "demo.service.template").write_text(TEMPLATE)
    _git(repo, "init", "-q", "-b", "trunk")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    units = home / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    (units / "demo.service").write_text(TEMPLATE.replace("__VENV__", "/opt/v"))
    (home / ".genesis").mkdir()
    return repo, home, units


def _run(
    repo: Path,
    home: Path,
    *,
    deploy_head: str,
    post_merge: bool = False,
    take: str = "",
    reused: bool = False,
) -> subprocess.CompletedProcess:
    tag = "pre-update-test"
    _git(repo, "tag", "-f", tag)
    state = home / ".genesis" / "update_state.json"
    script = f"""
set -Eeuo pipefail
GENESIS_ROOT={str(repo)!r}; HOME={str(home)!r}
DEPLOY_HEAD={deploy_head!r}; POST_MERGE={"true" if post_merge else "false"}
GENESIS_TAKE_TEMPLATES={take!r}; ROLLBACK_TAG={tag!r}
_ROLLBACK_TAG_REUSED={"true" if reused else "false"}; STATE_FILE={str(state)!r}
_clear_deploy_state() {{ rm -f "$STATE_FILE"; echo CLEARED; }}
{_block()}
echo PASSED_PREFLIGHT
"""
    state.write_text('{"phase": "merging"}')
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def _tag_exists(repo: Path) -> bool:
    return bool(_git(repo, "tag", "-l", "pre-update-test"))


def _new_commit(repo: Path) -> str:
    (repo / "scripts" / "systemd" / "demo.service.template").write_text(
        TEMPLATE.replace("on-failure", "always")
    )
    _git(repo, "commit", "-qam", "incoming")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "reset", "-q", "--hard", "HEAD~1")
    return head


def test_an_unedited_unit_passes_even_when_the_incoming_template_changed(world):
    repo, home, _ = world
    r = _run(repo, home, deploy_head=_new_commit(repo))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "PASSED_PREFLIGHT" in r.stdout


def test_a_hand_edited_unit_refuses_before_anything_stops(world):
    repo, home, units = world
    (units / "demo.service").write_text(
        TEMPLATE.replace("__VENV__", "/opt/v") + "Environment=X=1\n"
    )
    r = _run(repo, home, deploy_head=_new_commit(repo))
    assert r.returncode == 4, r.stdout + r.stderr
    assert "REFUSED: demo.service" in r.stdout and "--take-template" in r.stdout
    assert "PASSED_PREFLIGHT" not in r.stdout
    assert not _tag_exists(repo) and "CLEARED" in r.stdout


def test_take_template_lets_the_update_through(world):
    repo, home, units = world
    (units / "demo.service").write_text("[Service]\nExecStart=/mine\n")
    r = _run(repo, home, deploy_head=_new_commit(repo), take="demo.service")
    assert r.returncode == 0 and "PASSED_PREFLIGHT" in r.stdout


def test_nothing_to_merge_still_refuses(world):
    """A run with nothing new can still activate templates a code-only deploy
    pulled in (the tier-2 check further down update.sh), so no exception."""
    repo, home, units = world
    (units / "demo.service").write_text("[Service]\nExecStart=/mine\n")
    r = _run(repo, home, deploy_head=_git(repo, "rev-parse", "HEAD"))
    assert r.returncode == 4 and "PASSED_PREFLIGHT" not in r.stdout


@pytest.mark.parametrize("reused", [True, False])
def test_a_post_merge_refusal_keeps_the_state_file_and_a_reused_tag(world, reused):
    repo, home, units = world
    (units / "demo.service").write_text("[Service]\nExecStart=/mine\n")
    r = _run(repo, home, deploy_head="", post_merge=True, reused=reused)
    assert r.returncode == 4, r.stdout + r.stderr
    assert (home / ".genesis" / "update_state.json").exists(), (
        "the unfinished-update state must survive"
    )
    assert "CLEARED" not in r.stdout
    assert _tag_exists(repo) is reused
    assert "NOT activated" in r.stdout


def test_a_checker_error_refuses_with_exit_1_and_changes_nothing(world):
    repo, home, _ = world
    r = _run(repo, home, deploy_head="0" * 40)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "could not check" in r.stdout
    assert "PASSED_PREFLIGHT" not in r.stdout and not _tag_exists(repo)


def test_nothing_from_the_incoming_tree_executes_before_the_merge(world):
    """The checker that runs is this checkout's: the fetched commit is data only.
    An incoming managed_units.py that would refuse (or do anything else) never runs."""
    repo, home, _ = world
    lib = repo / "scripts" / "lib" / "managed_units.py"
    lib.write_text("import sys\nprint('  REFUSED: incoming-checker')\nsys.exit(4)\n")
    _git(repo, "commit", "-qam", "incoming checker")
    incoming = _git(repo, "rev-parse", "HEAD")
    _git(repo, "reset", "-q", "--hard", "HEAD~1")
    r = _run(repo, home, deploy_head=incoming)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "incoming-checker" not in r.stdout and "PASSED_PREFLIGHT" in r.stdout


@pytest.mark.parametrize("flag", [["--take-template", "a.service"], ["--take-templates"]])
def test_a_supervised_session_may_never_take_a_template(flag):
    text = UPDATE.read_text()
    start = text.index("# ── Flag parsing")
    end = text.index("export GENESIS_TAKE_TEMPLATES")
    script = "set -euo pipefail\n" + text[start:end] + "\necho PARSED\n"
    r = subprocess.run(
        ["bash", "-c", script, "update.sh", *flag],
        capture_output=True, text=True, env={**os.environ, "GENESIS_UPDATE_TIER": "1"},
    )
    assert r.returncode == 1 and "PARSED" not in r.stdout
    assert "supervised update session" in r.stderr


def test_the_preflight_runs_before_the_stop_and_the_state_write():
    text = UPDATE.read_text()
    pre = text.index("# BEGIN managed-units-preflight")
    assert pre > text.index('DEPLOY_HEAD="$(git -C "$GENESIS_ROOT" rev-parse --verify -q')
    assert pre < text.index('\n_write_state "fetching"')
    assert pre < text.index('echo "--- Stopping services for update ---"')


@pytest.mark.parametrize(
    "args",
    [["--bogus"], ["--take-template"], ["--take-template", ""], ["--take-template", "--post-merge"]],
)
def test_bad_arguments_refuse_before_anything(args, tmp_path):
    text = UPDATE.read_text()
    start = text.index("# ── Flag parsing")
    end = text.index("export GENESIS_TAKE_TEMPLATES")
    script = "set -euo pipefail\n" + text[start:end] + "\necho PARSED\n"
    r = subprocess.run(["bash", "-c", script, "update.sh", *args], capture_output=True, text=True)
    assert r.returncode == 1 and "PARSED" not in r.stdout


def test_flags_build_the_take_list():
    text = UPDATE.read_text()
    start = text.index("# ── Flag parsing")
    end = text.index("export GENESIS_TAKE_TEMPLATES")
    script = (
        "set -euo pipefail\n"
        + text[start:end]
        + '\necho "[$POST_MERGE][$GENESIS_TAKE_TEMPLATES]"\n'
    )
    r = subprocess.run(
        [
            "bash",
            "-c",
            script,
            "update.sh",
            "--take-template",
            "a.service",
            "--post-merge",
            "--take-template",
            "b.timer",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "GENESIS_TAKE_TEMPLATES": "inherited.service"},
    )
    assert r.stdout.strip() == "[true][a.service b.timer]", r.stderr
