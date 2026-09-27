from __future__ import annotations

import ast
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from string import Formatter

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE = REPO_ROOT / "scripts" / "update.sh"

_INHERITED_PREFIXES = ("GENESIS_", "GIT_")


def _clean_env(**overrides: str) -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items() if not key.startswith(_INHERITED_PREFIXES)
    }
    env.update(overrides)
    return env


def _block(text: str, marker: str) -> str:
    match = re.search(
        rf"# BEGIN {re.escape(marker)}.*?\n(.*?)# END {re.escape(marker)}",
        text,
        re.DOTALL,
    )
    assert match, f"missing {marker} block"
    return match.group(1)


def _function(text: str, name: str) -> str:
    start = text.index(f"{name}() {{")
    next_function = re.search(r"\n[A-Za-z_][A-Za-z0-9_]*\(\) \{", text[start + 1 :])
    assert next_function, f"missing function boundary after {name}"
    return text[start : start + 1 + next_function.start()]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=_clean_env(),
    ).stdout.strip()


def _merged_repo(tmp_path: Path) -> tuple[Path, str, str]:
    remote = tmp_path / "remote"
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "remote", "add", "origin", str(remote))
    (repo / "file.txt").write_text("base\n")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "origin", "main")
    _git(repo, "checkout", "-b", "incoming")
    (repo / "file.txt").write_text("incoming\n")
    _git(repo, "commit", "-am", "incoming")
    incoming = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "origin", "incoming:main")
    _git(repo, "checkout", "main")
    _git(repo, "merge", "--no-ff", "incoming", "-m", "merge incoming")
    return repo, base, incoming


def _unrelated_merge_repo(tmp_path: Path) -> tuple[Path, str, str, str]:
    repo, base, incoming = _merged_repo(tmp_path)
    _git(repo, "reset", "--hard", "HEAD^1")
    _git(repo, "checkout", "-b", "feature", base)
    (repo / "feature.txt").write_text("feature\n")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "feature")
    feature = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    _git(repo, "merge", "--no-ff", "feature", "-m", "merge feature")
    return repo, base, feature, incoming


def _json_reader_stub() -> str:
    return """_read_json_field() {
    python3 - "$1" "$2" <<'PY'
import json, sys
try:
    print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))
except Exception:
    pass
PY
}
"""


def _run_post_merge_block(
    repo: Path,
    home: Path,
    *,
    state: dict[str, object] | None,
    conflict: dict[str, object] | None,
) -> subprocess.CompletedProcess[str]:
    state_file = home / ".genesis" / "update_state.json"
    conflict_file = home / ".genesis" / "update_conflicts.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    if state is not None:
        state_file.write_text(json.dumps(state))
    if conflict is not None:
        conflict_file.write_text(json.dumps(conflict))
    script = (
        "set -euo pipefail\n"
        f'GENESIS_ROOT="{repo}"\n'
        f'STATE_FILE="{state_file}"\n'
        f'CONFLICT_FILE="{conflict_file}"\n'
        "POST_MERGE=true\nDEPLOY_BRANCH=main\nUPDATE_REMOTE=origin\n"
        "OLD_TAG=old\nOLD_COMMIT=old\n"
        + _json_reader_stub()
        + _block(UPDATE.read_text(), "post-merge-target-recovery")
        + 'printf \'%s\\n%s\\n\' "$DEPLOY_HEAD" "$ROLLBACK_TAG"\n'
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=_clean_env(HOME=str(home)),
    )


def test_write_state_json_encodes_deploy_fields_without_venv(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    script = (
        "set -euo pipefail\n"
        f'HOME="{tmp_path / "home"}"\n'
        f'VENV_DIR="{tmp_path / "missing-venv"}"\n'
        f'STATE_FILE="{state}"\n'
        "ROLLBACK_TAG='pre-update-\"x'\n"
        "OLD_TAG='v\"old'\n"
        "OLD_COMMIT='abc\"def'\n"
        "DEPLOY_BRANCH='release/\"x'\n"
        "DEPLOY_HEAD='feed\"beef'\n"
        "STARTED_AT='start\"ed'\n"
        'WERE_RUNNING=("svc-a" \'svc "b"\')\n'
        + _function(UPDATE.read_text(), "_read_json_field")
        + "\n"
        + _function(UPDATE.read_text(), "_write_state")
        + '_write_state "fetching"\n'
        '_read_json_field "$STATE_FILE" deploy_branch\n'
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=_clean_env(),
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(state.read_text())
    assert data["phase"] == "fetching"
    assert data["rollback_tag"] == 'pre-update-"x'
    assert data["old_tag"] == 'v"old'
    assert data["old_commit"] == 'abc"def'
    assert data["deploy_branch"] == 'release/"x'
    assert data["deploy_head"] == 'feed"beef'
    assert data["started_at"] == 'start"ed'
    assert data["services_stopped"] == ["svc-a", 'svc "b"']


def test_update_history_uses_python312_when_system_python_is_old(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    db_path = repo / "data" / "genesis.db"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE update_history ("
        "id TEXT, old_tag TEXT, new_tag TEXT, old_commit TEXT, new_commit TEXT, "
        "status TEXT, rollback_tag TEXT, failure_reason TEXT, "
        "degraded_subsystems TEXT, started_at TEXT, completed_at TEXT)"
    )
    con.commit()
    con.close()

    # Use the interpreter RUNNING this suite, not `which("python3.12")`.
    #
    # `_record_update_history` imports `genesis`, so the selected interpreter has
    # to be one that can. `which` answers with whatever PATH happens to hold:
    # with the venv on PATH it finds `.venv/bin/python3.12` (imports fine), and
    # without it `/usr/bin/python3.12` (no `genesis`, so the helper warns, no-ops,
    # and this test fails on the row assertion while still exiting 0). That made
    # the verdict a property of the caller's PATH rather than of `update.sh` —
    # green in CI and for a reviewer with the venv activated, red for one invoking
    # the venv binary directly. `sys.executable` is the interpreter that imported
    # this module, so it can always import the package under test.
    python312 = sys.executable
    assert sys.version_info >= (3, 12), (
        f"this test shims python3.12 to {python312}, which is "
        f"{sys.version_info.major}.{sys.version_info.minor}"
    )
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    (shim_dir / "python3").write_text("#!/bin/sh\nexit 3\n")
    (shim_dir / "python3").chmod(0o755)
    (shim_dir / "python3.12").write_text(f'#!/bin/sh\nexec {python312} "$@"\n')
    (shim_dir / "python3.12").chmod(0o755)

    script = (
        "set -u\n"
        f'GENESIS_ROOT="{repo}"\n'
        f'VENV_DIR="{tmp_path / "missing-venv"}"\n'
        "OLD_TAG=old\nOLD_COMMIT=old\nNEW_TAG=new\nNEW_COMMIT=new\n"
        "ROLLBACK_TAG=rollback\nSTARTED_AT=started\n"
        + _function(UPDATE.read_text(), "_metadata_python")
        + "\n"
        + _function(UPDATE.read_text(), "_record_update_history")
        + '\n_record_update_history "success" "" ""\n'
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=_clean_env(
            HOME=str(tmp_path / "home"),
            PATH=f"{shim_dir}:{os.environ['PATH']}",
        ),
    )

    assert result.returncode == 0, result.stderr
    con = sqlite3.connect(db_path)
    row = con.execute("SELECT status, old_commit, new_commit FROM update_history").fetchone()
    con.close()
    assert row == ("success", "old", "new")


def test_metadata_python_honors_a_per_writer_version_floor(tmp_path: Path) -> None:
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    python312 = shutil.which("python3.12") or shutil.which("python3")
    assert python312 is not None
    fake311 = shim_dir / "python3"
    fake311.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"(3, 12)"*) exit 1;;\n'
        '  *"(3, 11)"*) exit 0;;\n'
        "esac\n"
        f'exec {python312} "$@"\n'
    )
    fake311.chmod(0o755)

    script = (
        "set -u\n"
        f'VENV_DIR="{tmp_path / "missing-venv"}"\n'
        + _function(UPDATE.read_text(), "_metadata_python")
        + "\n"
        "if _metadata_python 12 >/dev/null; then echo unexpected-312; fi\n"
        "_metadata_python 11\n"
    )
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": str(shim_dir), "HOME": str(tmp_path / "home")},
    )

    assert result.returncode == 0, result.stderr
    assert "unexpected-312" not in result.stdout
    assert result.stdout.strip().splitlines() == [str(fake311)]


def test_update_resolves_branch_from_the_remote_it_fetches() -> None:
    text = UPDATE.read_text()
    remote = text.index('UPDATE_REMOTE="$(_detect_update_remote)"')
    resolve = text.index('genesis_resolve_deploy_branch "$GENESIS_ROOT" "$UPDATE_REMOTE"')
    assert remote < resolve
    # Both refspecs are fully qualified on the source side, so a tag sharing the
    # branch name cannot win git's disambiguation.
    assert '"+refs/heads/$DEPLOY_BRANCH:$DEPLOY_FETCH_REF"' in text
    assert '"+refs/heads/$DEPLOY_BRANCH:$DEPLOY_TRACKING_REF"' in text


def test_only_the_private_deploy_ref_is_a_fatal_fetch() -> None:
    """The tracking ref must not be able to fail an otherwise-good update.

    This previously asserted the two refspecs appeared in ONE `git fetch`, which
    pinned the defect: after a default-branch hierarchy change an existing
    refs/remotes/<remote>/release blocks refs/remotes/<remote>/release/v2, git
    exits non-zero, and bundled together that failed every update until someone
    ran `git remote prune` by hand -- while the deploy ref had already been
    fetched successfully.
    """
    text = UPDATE.read_text()
    fatal = text.index('"+refs/heads/$DEPLOY_BRANCH:$DEPLOY_FETCH_REF" || return')
    tracking = text.index('"+refs/heads/$DEPLOY_BRANCH:$DEPLOY_TRACKING_REF"')
    assert fatal < tracking, "the deploy ref must be fetched first, and alone"

    # The tracking refresh lives in its own function, and that function must not
    # be able to propagate a failure: `|| return` in the caller would reinstate
    # the bug, so the helper swallows and reports.
    helper = text.index("_refresh_deploy_tracking_ref() {")
    body = text[helper : text.index("\n}\n", helper)]
    assert ">/dev/null 2>&1" in body, "a failed refresh must not spam the deploy log"
    assert "remote prune" in body, "the note must name the operator's remedy"
    assert body.rstrip().endswith("return 0"), (
        "the helper must always succeed; a non-zero exit here is the defect"
    )
    assert "_refresh_deploy_tracking_ref\n" in text, "and it must actually be called"
    assert "_refresh_deploy_tracking_ref || " not in text, (
        "the call must not be chained to anything that could make it fatal"
    )


def test_detect_update_remote_matches_the_repo_basename(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "remote", "add", "origin", "https://example.test/GENesis-AGI-backup.git")
    _git(repo, "remote", "add", "upstream", "https://example.test/GENesis-AGI.git")
    text = UPDATE.read_text()
    function = text[
        text.index("_detect_update_remote() {") : text.index(
            'UPDATE_REMOTE="$(_detect_update_remote)"'
        )
    ]
    env = _clean_env(
        HOME=str(tmp_path / "home"),
        GENESIS_GITHUB_PUBLIC_REPO="GENesis-AGI",
    )
    result = subprocess.run(
        ["bash", "-c", f'GENESIS_ROOT="{repo}"\n{function}\n_detect_update_remote'],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "upstream"


def _run_detect_update_remote(
    repo: Path, home: Path, **env: str
) -> subprocess.CompletedProcess[str]:
    """Run the real `_detect_update_remote` with the real config helper sourced."""
    text = UPDATE.read_text()
    function = text[
        text.index("_detect_update_remote() {") : text.index(
            'UPDATE_REMOTE="$(_detect_update_remote)"'
        )
    ]
    lib = REPO_ROOT / "scripts" / "lib" / "deploy_checkout.sh"
    return subprocess.run(
        ["bash", "-c", f'. "{lib}"\nGENESIS_ROOT="{repo}"\n{function}\n_detect_update_remote'],
        capture_output=True,
        text=True,
        env=_clean_env(HOME=str(home), VENV_DIR="", **env),
    )


def test_detect_update_remote_recognises_a_url_containing_whitespace(tmp_path: Path) -> None:
    """A legal local-path remote with a space must still be recognised.

    The previous parse split `git remote -v` on whitespace, so `/x/a b/GENesis-AGI.git`
    became `/x/a` plus a stray field, matched nothing, and fell through to `origin` —
    deploying from the wrong remote with no error. Enumerating remotes by name and
    asking git for each URL has no field boundary to get wrong.
    """
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "remote", "add", "origin", "https://example.test/GENesis-AGI-backup.git")
    _git(repo, "remote", "add", "public", str(tmp_path / "a b" / "GENesis-AGI.git"))

    result = _run_detect_update_remote(
        repo, tmp_path / "home", GENESIS_GITHUB_PUBLIC_REPO="GENesis-AGI"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "public"


def test_detect_update_remote_refuses_an_unreadable_config(tmp_path: Path) -> None:
    """A config that exists but cannot be parsed REFUSES; an absent one does not.

    Guessing the default repo name when the configured one cannot be read selects
    a fetch remote the operator did not choose. Positive control first: with NO
    config file the default applies and detection succeeds, so the refusal below
    is caused by the malformed file and not by the harness.
    """
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "remote", "add", "origin", "https://example.test/GENesis-AGI.git")
    home = tmp_path / "home"
    (home / ".genesis" / "config").mkdir(parents=True)

    absent = _run_detect_update_remote(repo, home)
    assert absent.returncode == 0, absent.stderr
    assert absent.stdout.strip() == "origin"

    (home / ".genesis" / "config" / "genesis.yaml").write_text("github: [unclosed\n")
    malformed = _run_detect_update_remote(repo, home)
    assert malformed.returncode != 0
    assert "refusing rather than guessing" in malformed.stderr
    assert malformed.stdout.strip() == ""


def test_private_fetch_ref_and_tracking_ref_survive_narrow_refspec(tmp_path: Path) -> None:
    remote = tmp_path / "remote"
    work = tmp_path / "work"
    _git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    _git(tmp_path, "clone", str(remote), str(work))
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    (work / "file.txt").write_text("base\n")
    _git(work, "add", "file.txt")
    _git(work, "commit", "-m", "base")
    base = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", "main")

    _git(work, "remote", "set-branches", "origin", "other")
    _git(work, "update-ref", "-d", "refs/remotes/origin/main")
    (work / "file.txt").write_text("incoming\n")
    _git(work, "commit", "-am", "incoming")
    incoming = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", "main")

    _git(
        work,
        "fetch",
        "origin",
        "+refs/heads/main:refs/genesis-update-head",
        "+refs/heads/main:refs/remotes/origin/main",
    )

    assert _git(work, "rev-parse", "refs/genesis-update-head") == incoming
    assert _git(work, "rev-parse", "refs/remotes/origin/main") == incoming
    _git(work, "push", "--force", "origin", f"{base}:main")
    _git(
        work,
        "fetch",
        "origin",
        "+refs/heads/main:refs/genesis-update-head",
        "+refs/heads/main:refs/remotes/origin/main",
    )
    assert _git(work, "rev-parse", "refs/genesis-update-head") == base
    assert _git(work, "rev-parse", "refs/remotes/origin/main") == base


def test_update_merges_and_verifies_fetched_remote_head() -> None:
    text = UPDATE.read_text()
    assert 'DEPLOY_FETCH_REF="refs/genesis-update-head"' in text
    assert 'DEPLOY_HEAD=$(git -C "$GENESIS_ROOT" rev-parse "$DEPLOY_FETCH_REF")' in text
    assert 'merge "$DEPLOY_FETCH_REF" --no-edit' in text
    verify = text.index('merge-base --is-ancestor "$DEPLOY_FETCH_REF" HEAD')
    restart = text.index("--- Restarting services ---")
    success = text.index('_record_update_history "success"', verify)
    assert verify < restart
    assert verify < success


def test_record_update_history_uses_bootstrap_safe_python(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "data").mkdir(parents=True)
    (repo / "src").symlink_to(REPO_ROOT / "src", target_is_directory=True)
    db = repo / "data" / "genesis.db"
    subprocess.run(
        [
            "python3",
            "-c",
            "import sqlite3,sys; con=sqlite3.connect(sys.argv[1]); "
            "con.execute('CREATE TABLE update_history (id TEXT, old_tag TEXT, new_tag TEXT, "
            "old_commit TEXT, new_commit TEXT, status TEXT, rollback_tag TEXT, "
            "failure_reason TEXT, degraded_subsystems TEXT, started_at TEXT, completed_at TEXT)'); "
            "con.commit()",
            str(db),
        ],
        check=True,
    )
    script = (
        "set -euo pipefail\n"
        f'GENESIS_ROOT="{repo}"\nVENV_DIR="{tmp_path / "missing-venv"}"\n'
        "OLD_TAG=old\nNEW_TAG=new\nOLD_COMMIT=oldsha\nNEW_COMMIT=newsha\n"
        'ROLLBACK_TAG=tag\nSTARTED_AT=start\nPRE_UPDATE_DEGRADED="backup:process_exit"\n'
        + _function(UPDATE.read_text(), "_metadata_python")
        + "\n"
        + _function(UPDATE.read_text(), "_record_update_history")
        + '\n_record_update_history success "" "container_cc_sync"\n'
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=_clean_env(),
    )

    assert result.returncode == 0, result.stderr
    row = subprocess.run(
        [
            "python3",
            "-c",
            "import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute("
            "'select degraded_subsystems from update_history').fetchone()[0])",
            str(db),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert row == "container_cc_sync,backup:process_exit"


def test_tier2_baseline_resolves_abbreviated_commit_objects(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "file.txt").write_text("base\n")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "update.sh").write_text("# changed\n")
    _git(repo, "add", "scripts/update.sh")
    _git(repo, "commit", "-m", "tier2 shadow")
    _git(repo, "branch", base[:8])
    _git(repo, "reset", "--hard", base)
    (repo / "other.txt").write_text("other\n")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-m", "unrelated")
    (repo / "data").mkdir()
    db = repo / "data" / "genesis.db"
    subprocess.run(
        [
            "python3",
            "-c",
            "import sqlite3,sys; con=sqlite3.connect(sys.argv[1]); "
            "con.execute('CREATE TABLE update_history (new_commit TEXT, status TEXT, completed_at TEXT)'); "
            'con.execute(\'INSERT INTO update_history VALUES (?, "success", "now")\', (sys.argv[2],)); '
            "con.commit()",
            str(db),
            base[:8],
        ],
        check=True,
    )
    script = (
        "set -euo pipefail\n"
        f'GENESIS_ROOT="{repo}"\nVENV_DIR="{tmp_path / "missing-venv"}"\n'
        + _function(UPDATE.read_text(), "_resolve_commit_object")
        + "\n"
        + _block(UPDATE.read_text(), "tier2-baseline-check")
        + "\nif _tier2_pending_since_baseline; then echo pending; else echo clean; fi\n"
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=_clean_env(),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "clean"


def test_modified_ephemeral_file_is_backed_up_before_clear(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "AGENTS.md").write_text("tracked\n")
    (repo / "config").mkdir()
    (repo / "config" / "procedure_triggers.yaml").write_text("tracked\n")
    _git(repo, "add", "AGENTS.md", "config")
    _git(repo, "commit", "-m", "initial")
    (repo / "AGENTS.md").write_text("staged work\n")
    _git(repo, "add", "AGENTS.md")
    (repo / "AGENTS.md").write_text("local work\n")
    # BOTH paths the loop owns are made dirty. With only AGENTS.md dirty, deleting
    # `config/procedure_triggers.yaml` from the loop failed nothing.
    (repo / "config" / "procedure_triggers.yaml").write_text("local triggers\n")

    script = f'set -euo pipefail\nGENESIS_ROOT="{repo}"\nHOME="{home}"\n' + _block(
        UPDATE.read_text(), "ephemeral-premerge-backup"
    )
    result = subprocess.run(
        ["bash", "-c", script],
        check=True,
        env=_clean_env(),
        capture_output=True,
        text=True,
    )
    backups = list((home / ".genesis" / "premerge-backups").glob("*/AGENTS.md"))
    assert len(backups) == 1
    assert (backups[0] / "current").read_text() == "local work\n"
    assert "local work" in (backups[0] / "worktree.patch").read_text()
    assert "staged work" in (backups[0] / "index.patch").read_text()
    assert (repo / "AGENTS.md").read_text() == "tracked\n"
    assert str(backups[0]) in result.stdout
    assert stat.S_IMODE(backups[0].parent.stat().st_mode) == 0o700

    triggers = list(
        (home / ".genesis" / "premerge-backups").glob("*/config/procedure_triggers.yaml")
    )
    assert len(triggers) == 1, "the second path the loop owns was not backed up"
    assert (triggers[0] / "current").read_text() == "local triggers\n"
    assert (repo / "config" / "procedure_triggers.yaml").read_text() == "tracked\n"
    assert str(triggers[0]) in result.stdout


def test_every_path_the_dirty_gate_excuses_is_backed_up_before_it_is_cleared() -> None:
    """`EPHEMERAL_DIRTY_RE` and the backup blocks must name the SAME set of paths.

    #2303's second defect was two individually-reasonable rules composing badly:
    the dirty gate EXCUSED a path, and a later step force-cleared it with no
    backup. Coverage is now split across four mechanisms — the new
    `ephemeral-premerge-backup` loop plus three older `*-premerge` blocks — and
    nothing tied them to the regex. A sixth alternative added to the regex would
    be excused and then either destroyed unbacked or left to abort the merge,
    which is the same defect with a wider surface.

    So derive the excused set FROM the regex and require it to equal the set the
    backup blocks name.
    """
    text = UPDATE.read_text()

    match = re.search(r"^EPHEMERAL_DIRTY_RE='([^']+)'", text, re.M)
    assert match, "EPHEMERAL_DIRTY_RE definition not found"
    excused = set()
    for alt in match.group(1).split("|"):
        path = alt.strip()
        assert path.endswith("$"), f"unanchored alternative {alt!r}"
        excused.add(path[:-1].replace("\\.", "."))
    assert len(excused) == 5, f"expected 5 excused paths, found {sorted(excused)}"

    backed_up = set()
    loop = _block(text, "ephemeral-premerge-backup")
    loop_match = re.search(r"for _eph in ([^;]+); do", loop)
    assert loop_match, "backup loop header not found"
    backed_up.update(loop_match.group(1).split())
    for marker, var in (
        ("settings-local-premerge", "SETTINGS_LOCAL"),
        ("serena-yml-premerge", "SERENA_YML"),
        ("user-md-premerge", "USER_MD"),
    ):
        block = _block(text, marker)
        assigned = re.search(rf'^{var}="([^"]+)"', block, re.M)
        assert assigned, f"{var} not assigned inside {marker}"
        assert "cp " in block and "$HOME/.genesis/" in block, (
            f"{marker} must copy the file OUTSIDE the repo before clearing it"
        )
        backed_up.add(assigned.group(1))

    assert excused == backed_up, (
        f"excused by the dirty gate but not backed up: {sorted(excused - backed_up)}; "
        f"backed up but not excused: {sorted(backed_up - excused)}"
    )


def test_every_success_record_uses_marker_capable_variable() -> None:
    """Every success writer must use the SAME predicate, not merely a
    marker-capable variable.

    #2145's criterion is worded "passes a variable that can hold
    `genesis-server-not-restarted`", and its PURPOSE is that no success row can
    name the new HEAD while nothing is serving it. Those are not the same test,
    and the gap between them shipped: the P6 site passed `_OPERATOR_STOP`, which
    is only ever true when `WERE_RUNNING` was ENTIRELY empty — and
    `genesis-bridge` populates that array too, so a bridge-up / server-down run
    satisfied the letter of the criterion and wrote a bare success row anyway.
    So assert the DERIVATION, not the variable name.

    Enumeration is deliberately spelling-agnostic. The previous version matched
    only `_record_update_history "success" "" "$x"`, so a site written
    `_record_update_history success …`, or with a non-empty second argument, or
    with the third omitted, was invisible — and `assert calls == [...]` passed
    regardless. A gate whose denominator is one spelling is a denylist.
    """
    text = UPDATE.read_text()

    # 1. Enumerate EVERY invocation, whatever the quoting or arity.
    invocations = re.findall(r"^[ \t]*_record_update_history[ \t]+(\S.*)$", text, re.M)
    assert len(invocations) >= 3, f"found only {len(invocations)}: {invocations}"

    success = [a for a in invocations if re.split(r"[ \t]+", a)[0].strip("\"'") == "success"]
    assert len(success) == 3, f"expected 3 success writers, got {len(success)}: {success}"

    # 2. Each passes a marker-capable third argument.
    markers = []
    for args in success:
        parts = re.findall(r'"[^"]*"|\S+', args)
        assert len(parts) == 3, f"unexpected arity in `{args}`: {parts}"
        markers.append(parts[2].strip('"'))
    assert markers == ["$_nd_degraded", "$_nd_degraded", "$_p6_degraded"], markers

    # 3. Each boolean fed to the helper answers "was genesis-server left
    #    un-restarted?" — and the two sites answer it DIFFERENTLY on purpose:
    #    - P6 verifies health and ROLLS BACK on failure, so reaching its success
    #      write already proves a restart worked; membership in WERE_RUNNING (was
    #      it meant to be restarted?) is the whole question there.
    #    - The no-delta path has NO health check, and its restart is
    #      `_start_genesis_server || true`, which reports success even when the
    #      unit never came back. So membership alone is not enough: the unit's
    #      state AFTER the attempt must be read too.
    #    `_OPERATOR_STOP` is disallowed at both: it answers "was the array
    #    empty?", a different question.
    feeders = re.findall(r'_success_degraded_subsystems[ \t]*\\?\s*"[^"]*"[ \t]+"([^"]+)"', text)
    assert len(feeders) == 2, f"expected 2 helper call sites, got: {feeders}"
    assert all("_OPERATOR_STOP" not in f for f in feeders), (
        f"{feeders}: `_OPERATOR_STOP` is true only for an ENTIRELY empty "
        "WERE_RUNNING, so it cannot answer whether genesis-server was left un-restarted"
    )
    assert sorted(f.lstrip("$").strip("{}") for f in feeders) == [
        "_nd_server_not_restarted",
        "_p6_server_not_restarted",
    ], feeders

    membership = r'\[\[ " \$\{WERE_RUNNING\[\*\]\} " == \*" genesis-server "\* \]\]'
    assert re.search(membership + r"[\s\\]*\|\|[ \t]*_p6_server_not_restarted=true", text), (
        "P6 must derive its boolean from WERE_RUNNING membership"
    )
    # No-delta: DEFAULT true, cleared only when the server was meant to be up AND
    # the unit is actually up after the restart attempt. A default of `false`
    # would re-open the failed-restart case.
    assert "_nd_server_not_restarted=true\n" in text
    assert re.search(
        r"if " + membership + r" && _server_unit_is_up; then\s*\n\s*_nd_server_not_restarted=false",
        text,
    ), "no-delta must clear the marker only after reading the unit's actual state"


def test_no_delta_path_never_records_success_over_an_unresolved_failure() -> None:
    """With the server down, a latest `failed`/`rolled_back` row must stay latest.

    P6's recovery detection reads only the NEWEST update_history status, and when
    last_update_failure.json was never written that row is the only signal it has.
    #2145 asked whether this branch should inherit the "a still-down server may be
    an unresolved failure whose artifact must survive" condition; writing a newer
    success row over it is exactly what that condition forbids.
    """
    text = UPDATE.read_text()
    start = text.index('elif [ -n "$_nd_base_degraded" ]')
    branch = text[start : text.index("Nothing to do.", start)]
    assert '_nd_last_status="$(_latest_update_status)"' in branch
    guard = branch[branch.index('case "$_nd_last_status" in') :]
    failed_arm = guard[guard.index("failed | rolled_back)") : guard.index(";;")]
    assert "_record_update_history" not in failed_arm, (
        "the failed/rolled_back arm must not write any update_history row"
    )
    # P6 reads the SAME status through the SAME reader, so the two cannot drift:
    # exactly two call sites (P6 and this branch), and the query exists once.
    assert re.findall(r"(\w+)=\"\$\(_latest_update_status\)\"", text) == [
        "_nd_last_status",
        "_last_status",
    ]
    assert text.count("SELECT status FROM update_history ORDER BY started_at DESC") == 1


def test_deploy_outcome_probes_behave(tmp_path: Path) -> None:
    """The two probes, driven for real rather than grepped.

    `_server_unit_is_up` is fed each state `systemctl is-active` can print through
    a PATH shim; an EMPTY answer (a D-Bus hiccup) must read as not-up, so the error
    lands on the side that reports a problem. `_latest_update_status` reads a real
    SQLite fixture, and an absent database reads as "" rather than failing.
    """
    block = _block(UPDATE.read_text(), "deploy-outcome-probes")
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()

    def unit_up(state: str) -> bool:
        (shim_dir / "systemctl").write_text(f"#!/bin/sh\nprintf '%s' '{state}'\nexit 3\n")
        (shim_dir / "systemctl").chmod(0o755)
        result = subprocess.run(
            ["bash", "-c", block + "\n_server_unit_is_up"],
            capture_output=True,
            text=True,
            env=_clean_env(PATH=f"{shim_dir}:{os.environ['PATH']}"),
        )
        return result.returncode == 0

    assert unit_up("active") and unit_up("activating") and unit_up("reloading")
    for state in ("inactive", "failed", "deactivating", ""):
        assert not unit_up(state), f"state {state!r} must not read as up"

    home = tmp_path / "home"
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    (venv / "bin" / "python").chmod(0o755)

    def latest() -> str:
        result = subprocess.run(
            ["bash", "-c", f'VENV_DIR="{venv}"\n' + block + "\n_latest_update_status"],
            capture_output=True,
            text=True,
            env=_clean_env(HOME=str(home)),
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    assert latest() == "", "no database must read as empty, not fail"
    db = home / "genesis" / "data" / "genesis.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE update_history (status TEXT, started_at TEXT)")
    con.execute("INSERT INTO update_history VALUES ('success', '2026-01-01T00:00:00')")
    con.execute("INSERT INTO update_history VALUES ('rolled_back', '2026-01-02T00:00:00')")
    con.commit()
    con.close()
    assert latest() == "rolled_back", "the NEWEST row, by started_at, must win"


def test_success_degraded_helper_marks_server_not_restarted() -> None:
    script = (
        "set -euo pipefail\n"
        + _block(UPDATE.read_text(), "success-degraded-subsystems")
        + '\n_success_degraded_subsystems "container_cc_sync" true\n'
        + '_success_degraded_subsystems "container_cc_sync" false\n'
    )
    result = subprocess.run(
        ["bash", "-c", script],
        check=True,
        env=_clean_env(),
        capture_output=True,
        text=True,
    )
    assert result.stdout.splitlines() == [
        "container_cc_sync,genesis-server-not-restarted",
        "container_cc_sync",
    ]


def test_post_merge_recovers_target_from_old_state_merge_parent(tmp_path: Path) -> None:
    repo, base, incoming = _merged_repo(tmp_path)
    home = tmp_path / "home"
    result = _run_post_merge_block(
        repo,
        home,
        state={"old_commit": base[:12], "rollback_tag": ""},
        conflict=None,
    )

    assert result.returncode == 0, result.stderr
    deploy_head, rollback_tag = result.stdout.splitlines()[-2:]
    assert deploy_head == incoming
    assert _git(repo, "rev-parse", f"{rollback_tag}^{{commit}}") == base


def test_post_merge_uses_recorded_fetch_head_without_network(tmp_path: Path) -> None:
    repo, base, incoming = _merged_repo(tmp_path)
    _git(repo, "update-ref", "refs/genesis-update-head", incoming)
    _git(repo, "remote", "rename", "origin", "gone")
    home = tmp_path / "home"

    result = _run_post_merge_block(
        repo,
        home,
        state={"old_commit": base, "rollback_tag": ""},
        conflict=None,
    )

    assert result.returncode == 0, result.stderr
    deploy_head, _rollback_tag = result.stdout.splitlines()[-2:]
    assert deploy_head == incoming


def test_post_merge_legacy_fetch_head_recovery_needs_no_network(tmp_path: Path) -> None:
    repo, base, incoming = _merged_repo(tmp_path)
    _git(repo, "fetch", "origin")
    _git(repo, "remote", "rename", "origin", "gone")
    home = tmp_path / "home"

    result = _run_post_merge_block(
        repo,
        home,
        state={"old_commit": base, "rollback_tag": ""},
        conflict=None,
    )

    assert result.returncode == 0, result.stderr
    deploy_head, _rollback_tag = result.stdout.splitlines()[-2:]
    assert deploy_head == incoming


def test_post_merge_recovers_target_from_conflict_target_commit(tmp_path: Path) -> None:
    repo, base, incoming = _merged_repo(tmp_path)
    home = tmp_path / "home"
    result = _run_post_merge_block(
        repo,
        home,
        state=None,
        conflict={"target_commit": incoming, "old_commit": base},
    )

    assert result.returncode == 0, result.stderr
    deploy_head, rollback_tag = result.stdout.splitlines()[-2:]
    assert deploy_head == incoming
    assert _git(repo, "rev-parse", f"{rollback_tag}^{{commit}}") == base


def test_post_merge_rejects_unrelated_merge_parent(tmp_path: Path) -> None:
    repo, base, _feature, _incoming = _unrelated_merge_repo(tmp_path)
    home = tmp_path / "home"
    result = _run_post_merge_block(
        repo,
        home,
        state={"old_commit": base[:12], "rollback_tag": ""},
        conflict=None,
    )

    assert result.returncode != 0
    assert "parent matching the deploy branch's fetched head" in result.stderr
    assert _git(repo, "tag", "--list", "pre-update-*") == ""


def test_saved_old_commit_cannot_be_shadowed_by_a_ref(tmp_path: Path) -> None:
    repo, base, incoming = _merged_repo(tmp_path)
    _git(repo, "branch", base[:12], incoming)
    home = tmp_path / "home"
    result = _run_post_merge_block(
        repo,
        home,
        state={"old_commit": base[:12], "rollback_tag": ""},
        conflict=None,
    )

    assert result.returncode == 0, result.stderr
    _deploy_head, rollback_tag = result.stdout.splitlines()[-2:]
    assert _git(repo, "rev-parse", f"{rollback_tag}^{{commit}}") == base


def test_post_merge_rejects_saved_rollback_tag_on_incoming_side(
    tmp_path: Path,
) -> None:
    repo, base, _incoming = _merged_repo(tmp_path)
    home = tmp_path / "home"
    _git(repo, "tag", "pre-update-saved", "HEAD")

    result = _run_post_merge_block(
        repo,
        home,
        state={"old_commit": base, "rollback_tag": "pre-update-saved"},
        conflict=None,
    )

    assert result.returncode != 0
    assert "does not point at this merge's pre-merge parent" in result.stderr


def test_post_merge_rejects_a_rollback_tag_at_an_older_ancestor(tmp_path: Path) -> None:
    """A tag on the pre-merge side but OLDER than the merge's first parent is refused.

    Ancestry admitted it: `--is-ancestor` asks "is the first an ancestor of the
    other?", so a tag at A passed for a history A -> B -> merge, and rolling back
    to it would discard B. The `old_commit` equality check cannot catch this
    because it is skipped when the saved state has no `old_commit` — which is
    exactly this fixture's state.
    """
    repo, base, _incoming = _merged_repo(tmp_path)
    home = tmp_path / "home"
    older = _git(repo, "rev-list", "--max-parents=0", "HEAD")
    assert older == base, "fixture: base is the root commit"
    # Make the tagged commit strictly OLDER than HEAD^1: rebuild so HEAD^1 is a
    # descendant of the tagged commit rather than the commit itself.
    _git(repo, "reset", "--hard", base)
    (repo / "later.txt").write_text("later\n")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-m", "later work on main")
    _git(repo, "merge", "--no-ff", "incoming", "-m", "merge incoming")
    assert _git(repo, "rev-parse", "HEAD^1") != base
    _git(repo, "tag", "pre-update-saved", base)

    result = _run_post_merge_block(
        repo,
        home,
        state={"rollback_tag": "pre-update-saved"},
        conflict=None,
    )

    assert result.returncode != 0, result.stdout
    assert "does not point at this merge's pre-merge parent" in result.stderr


def test_post_merge_rejects_wrong_head_before_creating_rollback_tag(
    tmp_path: Path,
) -> None:
    repo, base, incoming = _merged_repo(tmp_path)
    home = tmp_path / "home"
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-b", "main")
    _git(other, "config", "user.email", "test@example.com")
    _git(other, "config", "user.name", "Test")
    (other / "file.txt").write_text("other\n")
    _git(other, "add", "file.txt")
    _git(other, "commit", "-m", "other")
    wrong = _git(other, "rev-parse", "HEAD")
    _git(repo, "fetch", str(other), "main")
    fetched_wrong = _git(repo, "rev-parse", "FETCH_HEAD")

    result = _run_post_merge_block(
        repo,
        home,
        state={
            "deploy_branch": "main",
            "deploy_head": fetched_wrong or wrong,
            "old_commit": base,
        },
        conflict=None,
    )

    assert result.returncode != 0
    assert "does not contain the fetched deploy head" in result.stderr
    assert _git(repo, "tag", "--list", "pre-update-*") == ""


def test_conflict_context_carries_deploy_target() -> None:
    text = UPDATE.read_text()
    assert 'UC_DEPLOY_BRANCH="$DEPLOY_BRANCH"' in text
    assert 'UC_DEPLOY_HEAD="$DEPLOY_HEAD"' in text
    assert '"deploy_branch": os.environ.get("UC_DEPLOY_BRANCH", "")' in text
    assert '"deploy_head": os.environ.get("UC_DEPLOY_HEAD", "")' in text


def test_conflict_resolution_prompts_merge_saved_deploy_head() -> None:
    text = (REPO_ROOT / "src/genesis/dashboard/routes/updates.py").read_text()
    tier2 = text[text.index("_TIER2_PROMPT") : text.index("_TIER3_PROMPT")]
    tier3 = text[text.index("_TIER3_PROMPT") : text.index("# Files used")]
    for prompt in (tier2, tier3):
        assert "deploy_branch" in prompt
        assert "deploy_head" in prompt
        assert "target_commit" in prompt
        assert "checkout's deploy branch" in prompt
        assert "git merge <resolved-fetched-head> --no-edit" in prompt
        assert "git checkout <resolved-deploy-branch>" in prompt
        assert "git merge origin/main" not in prompt


def test_post_merge_recovers_target_from_durable_conflict_context() -> None:
    text = UPDATE.read_text()
    assert 'CONFLICT_FILE="$HOME/.genesis/update_conflicts.json"' in text
    assert '_uc_py="$(_metadata_python 11 || true)"' in text
    assert 'metadata_py="$(_metadata_python 12 || true)"' in text
    assert '"$_uc_py" - > "$HOME/.genesis/update_conflicts.json.tmp"' in text
    assert '"rollback_tag": os.environ.get("UC_ROLLBACK_TAG", "")' in text
    assert '_read_json_field "$CONFLICT_FILE" deploy_head' in text
    assert '_read_json_field "$CONFLICT_FILE" deploy_branch' in text
    assert '_read_json_field "$CONFLICT_FILE" target_commit' in text


def test_progress_gc_preserves_state_while_conflict_context_exists() -> None:
    text = (REPO_ROOT / "src/genesis/dashboard/routes/updates.py").read_text()
    assert "if stale and not _CONFLICT_FILE.is_file():" in text
    # Scope the kwarg assertion to the TIER-3 call. `conflict_file=_CONFLICT_FILE,`
    # appears three times (tier 1 and tier 2 are pre-existing), so an unscoped
    # `in text` would still pass with the tier-3 kwarg deleted — while
    # `_TIER3_PROMPT` carries the `{conflict_file}` placeholder, so that deletion
    # is a KeyError in `update_resolve()`'s escalation path at runtime.
    tier3 = text[
        text.index("tier3_prompt = _TIER3_PROMPT.format(") : text.index(
            "_spawn_detached_cc(tier3_prompt"
        )
    ]
    assert "conflict_file=_CONFLICT_FILE," in tier3


def test_every_conflict_prompt_is_formattable_with_its_own_call_site_kwargs() -> None:
    """Each `_TIERn_PROMPT`'s placeholders must equal its `.format()` kwargs.

    The conflict-prompt changes in `updates.py` are otherwise covered only by
    source-text greps, so adding a `{placeholder}` without the matching kwarg —
    or removing a kwarg a placeholder still needs — ships green and raises
    `KeyError` in `update_resolve()` the first time that tier escalates.

    Deliberately AST-parses the file at `REPO_ROOT` rather than importing the
    module: the venv resolves `genesis` to the primary checkout, so an
    import-based assertion in a linked worktree silently describes the wrong
    tree. `string.Formatter().parse` is used instead of a regex so escaped
    `{{`/`}}` in the prompt bodies are read exactly as `.format()` reads them.
    """
    source = (REPO_ROOT / "src/genesis/dashboard/routes/updates.py").read_text()
    tree = ast.parse(source)

    def is_prompt(name: str) -> bool:
        return name.startswith("_TIER") and name.endswith("_PROMPT")

    placeholders: dict[str, set[str]] = {}
    # A LIST of kwarg sets per prompt: every `.format()` call site is checked, so a
    # second call site cannot hide behind the first (a dict of single sets kept
    # only whichever call `ast.walk` visited last).
    call_sites: dict[str, list[set[str]]] = {}
    for node in ast.walk(tree):
        # `_TIERn_PROMPT = """..."""`
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and is_prompt(node.targets[0].id)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            placeholders[node.targets[0].id] = {
                field for _, field, _, _ in Formatter().parse(node.value.value) if field is not None
            }
        # `_TIERn_PROMPT.format(key=...)` ANYWHERE — assigned, passed as an
        # argument, or returned — not only on the right of an assignment.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format"
            and isinstance(node.func.value, ast.Name)
            and is_prompt(node.func.value.id)
        ):
            call_sites.setdefault(node.func.value.id, []).append(
                {kw.arg for kw in node.keywords if kw.arg is not None}
            )

    # Pin the denominator. Renaming a prompt at both its definition and its call
    # site would otherwise drop it out of coverage with nothing going red.
    assert sorted(placeholders) == ["_TIER1_PROMPT", "_TIER2_PROMPT", "_TIER3_PROMPT"], (
        f"expected exactly the three tier prompts, found {sorted(placeholders)}"
    )
    assert set(call_sites) == set(placeholders), (
        f"prompt constants {sorted(placeholders)} but format() call sites {sorted(call_sites)}"
    )
    for name, fields in sorted(placeholders.items()):
        # Positive control: prove the substitution actually runs, so the equality
        # below cannot pass vacuously on a prompt with no placeholders at all.
        assert fields, f"{name} has no placeholders — the comparison would be vacuous"
        for kwargs in call_sites[name]:
            assert fields == kwargs, (
                f"{name}: placeholders {sorted(fields)} != format() kwargs {sorted(kwargs)}"
            )


def test_noop_history_does_not_duplicate_pre_update_degraded() -> None:
    text = UPDATE.read_text()
    assert '"${HOST_CC_DEGRADED:-}" "$_nd_server_not_restarted"' in text
