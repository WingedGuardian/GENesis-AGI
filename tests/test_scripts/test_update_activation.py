"""update.sh activation: deploy the commit that was fetched, and lose no local edits (#2303).

Three properties, each driven against real scratch repositories through the
SHIPPED blocks of scripts/update.sh (extracted, never copied):

1. The fetched head is pinned from the tracking ref an explicit refspec wrote —
   not FETCH_HEAD, which any later fetch in the same checkout rewrites — and the
   merge takes that pinned commit.
2. A merge that reports success but did not bring the pinned head in is rolled
   back like any other merge failure.
3. Locally edited ephemeral files (which the merge step discards so an upstream
   change to them can apply) are backed up BEFORE the services stop, and are
   discarded only when a backup of their CURRENT content exists.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE = REPO_ROOT / "scripts" / "update.sh"
HYGIENE = REPO_ROOT / "scripts" / "disk_hygiene.sh"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def _env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "GENESIS_"))}
    env.update(_GIT_ENV)
    env["HOME"] = str(home)
    return env


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
        env=_env(cwd),
    ).stdout.strip()


def _block(marker: str) -> str:
    text = UPDATE.read_text()
    match = re.search(
        rf"# BEGIN {re.escape(marker)}[^\n]*\n(.*?)# END {re.escape(marker)}", text, re.DOTALL
    )
    assert match, f"missing {marker} block"
    return match.group(1)


def _fetch_block() -> str:
    """From `DEPLOY_HEAD=""` to the first column-0 `fi` after it: the whole
    fetch-and-pin step, verbatim."""
    text = UPDATE.read_text()
    start = text.index('DEPLOY_HEAD=""\n')
    return text[start : text.index("\nfi\n", start) + 4]


def _merge_assertion_block() -> str:
    """From the merge-result capture through the assertion: the capture decides
    UPDATE_OWN_HEAD, which the assertion compares against."""
    text = UPDATE.read_text()
    start = text.index('_merged_head="$(git -C "$GENESIS_ROOT" rev-parse')
    check = text.index('if ! git -C "$GENESIS_ROOT" merge-base --is-ancestor "$DEPLOY_HEAD" HEAD')
    return (
        'VALIDATED_HEAD="${VALIDATED_HEAD:-}"\nUPDATE_OWN_HEAD="$VALIDATED_HEAD"\n'
        + text[start : text.index("\nfi\n", check) + 4]
    )


_STUBS = """
ROLLBACK_TAG=test-rollback-tag
_clear_deploy_state() { echo CLEARED-STATE; }
_do_rollback() { echo "ROLLBACK: $1"; }
"""


def _run(script: str, home: Path, **extra_env: str) -> subprocess.CompletedProcess:
    env = _env(home)
    env.update(extra_env)
    return subprocess.run(
        ["bash", "-c", "set -Eeuo pipefail\n" + script],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


@pytest.fixture
def upstream_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    up = tmp_path / "upstream"
    _git(tmp_path, "init", "-q", "-b", "main", str(up))
    (up / "f").write_text("1\n")
    _git(up, "add", ".")
    _git(up, "commit", "-qm", "c1")
    _git(up, "branch", "feature")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(up), str(clone))
    (up / "f").write_text("2\n")
    _git(up, "commit", "-qam", "c2 on main")
    _git(up, "checkout", "-q", "feature")
    (up / "g").write_text("feature\n")
    _git(up, "add", ".")
    _git(up, "commit", "-qm", "feature work")
    _git(up, "checkout", "-q", "main")
    return up, clone


# ── 1. the pin ──────────────────────────────────────────────────────────────


def test_fetch_pins_the_upstream_head_from_the_tracking_ref(upstream_and_clone, tmp_path):
    up, clone = upstream_and_clone
    script = (
        f'GENESIS_ROOT="{clone}"\nUPDATE_REMOTE=origin\nPOST_MERGE=false\n'
        '. "'
        + str(REPO_ROOT / "scripts/lib/deploy_checkout.sh")
        + '"\n'
        + _STUBS
        + _fetch_block()
        + 'echo "PIN=$DEPLOY_HEAD"\n'
        # Another session fetches a DIFFERENT branch in the same checkout: that
        # rewrites FETCH_HEAD, and must not move what is deployed.
        + f'git -C "{clone}" fetch -q origin feature\n'
        + f'echo "FETCH_HEAD=$(git -C "{clone}" rev-parse FETCH_HEAD)"\n'
    )
    r = _run(script, tmp_path)
    assert r.returncode == 0, r.stderr
    pin = re.search(r"PIN=(\w+)", r.stdout).group(1)
    fetch_head = re.search(r"FETCH_HEAD=(\w+)", r.stdout).group(1)
    assert pin == _git(up, "rev-parse", "main")
    assert fetch_head == _git(up, "rev-parse", "feature")
    assert fetch_head != pin, "control: the concurrent fetch really did move FETCH_HEAD"


def test_a_tag_named_like_the_branch_cannot_hijack_the_pin(upstream_and_clone, tmp_path):
    up, clone = upstream_and_clone
    _git(clone, "tag", "main", "HEAD")  # an old commit, under the branch's name
    script = (
        f'GENESIS_ROOT="{clone}"\nUPDATE_REMOTE=origin\nPOST_MERGE=false\n'
        '. "'
        + str(REPO_ROOT / "scripts/lib/deploy_checkout.sh")
        + '"\n'
        + _STUBS
        + _fetch_block()
        + 'echo "PIN=$DEPLOY_HEAD"\n'
    )
    r = _run(script, tmp_path)
    assert r.returncode == 0, r.stderr
    assert re.search(r"PIN=(\w+)", r.stdout).group(1) == _git(up, "rev-parse", "main")


def test_a_failed_fetch_changes_nothing_and_never_rolls_back(tmp_path):
    clone = tmp_path / "clone"
    _git(tmp_path, "init", "-q", "-b", "main", str(clone))
    _git(clone, "commit", "-q", "--allow-empty", "-m", "c1")
    _git(clone, "remote", "add", "origin", str(tmp_path / "does-not-exist"))
    script = (
        f'GENESIS_ROOT="{clone}"\nUPDATE_REMOTE=origin\nPOST_MERGE=false\n'
        '. "' + str(REPO_ROOT / "scripts/lib/deploy_checkout.sh") + '"\n' + _STUBS + _fetch_block()
    )
    r = _run(script, tmp_path)
    assert r.returncode == 1
    assert "CLEARED-STATE" in r.stdout and "ROLLBACK" not in r.stdout


# ── 1b. an ignored local file the incoming side adds (#2589) ────────────────


@pytest.fixture
def ignored_collision(tmp_path: Path) -> tuple[Path, Path]:
    """Upstream starts tracking `local.env`, which the clone ignores and holds
    with its own contents. The clone has a local commit of its own, so the merge
    is a true 3-way merge — the case `--no-overwrite-ignore` does not cover."""
    up = tmp_path / "upstream"
    _git(tmp_path, "init", "-q", "-b", "main", str(up))
    (up / "f").write_text("1\n")
    _git(up, "add", ".")
    _git(up, "commit", "-qm", "c1")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(up), str(clone))
    (up / "local.env").write_text("UPSTREAM\n")
    _git(up, "add", "local.env")
    _git(up, "commit", "-qm", "track local.env")
    (clone / "x").write_text("local work\n")
    _git(clone, "add", "x")
    _git(clone, "commit", "-qm", "local diverge")
    (clone / ".git" / "info" / "exclude").write_text("local.env\n")
    return up, clone


def _fetch_script(clone: Path) -> str:
    return (
        f'GENESIS_ROOT="{clone}"\nUPDATE_REMOTE=origin\nPOST_MERGE=false\n'
        '. "' + str(REPO_ROOT / "scripts/lib/deploy_checkout.sh") + '"\n' + _STUBS + _fetch_block()
    )


def test_an_ignored_file_the_upstream_adds_is_refused_before_the_stop(ignored_collision, tmp_path):
    up, clone = ignored_collision
    (clone / "local.env").write_bytes(b"LOCAL-SECRET\n")
    head = _git(clone, "rev-parse", "HEAD")
    r = _run(_fetch_script(clone), tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "brings in files that already exist here" in r.stdout
    assert "local.env" in r.stdout
    assert "CLEARED-STATE" in r.stdout and "ROLLBACK" not in r.stdout
    assert (clone / "local.env").read_bytes() == b"LOCAL-SECRET\n"
    assert _git(clone, "rev-parse", "HEAD") == head


def test_the_same_diverged_range_without_the_local_file_is_admitted(ignored_collision, tmp_path):
    """Control: the refusal above is the collision, not the divergence."""
    up, clone = ignored_collision
    r = _run(_fetch_script(clone), tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "brings in files that already exist" not in r.stdout


def _late_scan_script(clone: Path) -> str:
    _git(clone, "fetch", "-q", "origin")
    deploy_head = _git(clone, "rev-parse", "origin/main")
    return (
        f'GENESIS_ROOT="{clone}"\nUPDATE_REMOTE=origin\nDEPLOY_BRANCH=main\n'
        f'DEPLOY_HEAD="{deploy_head}"\n'
        '. "'
        + str(REPO_ROOT / "scripts/lib/deploy_checkout.sh")
        + '"\n'
        + _STUBS
        + _block("late-collision-scan")
        + 'echo "MERGE-WOULD-RUN"\n'
    )


def test_a_file_that_appears_during_the_stop_is_refused_before_the_merge(
    ignored_collision, tmp_path
):
    """The pre-stop scan cannot see a file created during the stop; on a diverged
    (3-way) merge the flag does not protect it either. The scan runs again as the
    last step before the merge, and a hit rolls back with HEAD unmoved — so the
    rollback's reset leaves the ignored file exactly as it was."""
    up, clone = ignored_collision
    head = _git(clone, "rev-parse", "HEAD")
    (clone / "local.env").write_bytes(b"LOCAL-SECRET\n")  # created during the stop
    r = _run(_late_scan_script(clone), tmp_path)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "ROLLBACK: incoming files collide" in r.stdout and "local.env" in r.stdout
    assert "MERGE-WOULD-RUN" not in r.stdout
    assert (clone / "local.env").read_bytes() == b"LOCAL-SECRET\n"
    assert _git(clone, "rev-parse", "HEAD") == head


def test_the_late_scan_lets_a_clean_range_through(ignored_collision, tmp_path):
    """Control: the same diverged range with nothing in the way reaches the merge."""
    up, clone = ignored_collision
    r = _run(_late_scan_script(clone), tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "MERGE-WOULD-RUN" in r.stdout and "ROLLBACK" not in r.stdout


def test_the_late_scan_sits_between_the_clear_and_the_merge():
    text = UPDATE.read_text()
    clear = text.index("# END ephemeral-clear")
    scan = text.index("# BEGIN late-collision-scan")
    merge = text.index('merge --no-overwrite-ignore "$DEPLOY_HEAD" --no-edit')
    assert clear < scan < merge


def test_the_real_merge_line_refuses_an_ignored_file_on_a_fast_forward(tmp_path):
    """The shipped merge line, run on a fast-forward whose range adds a file that
    appeared locally, ignored, after the scan: git refuses and the file is kept.
    The control strips only the flag and shows git overwriting it, so the flag is
    what bites."""
    text = UPDATE.read_text()
    line = next(ln for ln in text.splitlines() if ln.startswith("MERGE_OUTPUT=$(git -C"))
    assert "--no-overwrite-ignore" in line

    def attempt(merge_line: str) -> tuple[str, bytes]:
        up = tmp_path / f"up{len(merge_line)}"
        _git(tmp_path, "init", "-q", "-b", "main", str(up))
        (up / "f").write_text("1\n")
        _git(up, "add", ".")
        _git(up, "commit", "-qm", "c1")
        clone = tmp_path / f"clone{len(merge_line)}"
        _git(tmp_path, "clone", "-q", str(up), str(clone))
        (up / "local.env").write_text("UPSTREAM\n")
        _git(up, "add", "local.env")
        _git(up, "commit", "-qm", "track local.env")
        _git(clone, "fetch", "-q", "origin")
        (clone / ".git" / "info" / "exclude").write_text("local.env\n")
        (clone / "local.env").write_bytes(b"LOCAL-SECRET\n")
        deploy_head = _git(clone, "rev-parse", "origin/main")
        script = (
            f'GENESIS_ROOT="{clone}"\nDEPLOY_HEAD="{deploy_head}"\nMERGE_RC=0\n'
            f'{merge_line}\necho "RC=$MERGE_RC"\n'
        )
        r = _run(script, tmp_path)
        return r.stdout, (clone / "local.env").read_bytes()

    out, kept = attempt(line)
    assert "RC=0" not in out, out
    assert kept == b"LOCAL-SECRET\n"
    out, lost = attempt(line.replace("--no-overwrite-ignore ", ""))
    assert "RC=0" in out, out
    assert lost == b"UPSTREAM\n", "control: without the flag git overwrites it"


def test_the_merge_takes_the_pinned_commit_and_nothing_names_the_moving_ref():
    text = UPDATE.read_text()
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert 'merge --no-overwrite-ignore "$DEPLOY_HEAD" --no-edit' in code
    assert not re.search(r'merge "\$UPDATE_REMOTE/', code), "merge must not name the moving ref"
    assert "FETCH_HEAD" not in code, "FETCH_HEAD is rewritten by every fetch"
    # The conflict record names the pinned target, too.
    assert "describe --tags --match 'v*' --abbrev=0 \"$DEPLOY_HEAD\"" in code
    assert 'rev-parse --short "$DEPLOY_HEAD"' in code


# ── 2. the merge assertion ──────────────────────────────────────────────────


def test_a_merge_that_did_not_bring_the_pin_in_is_rolled_back(upstream_and_clone, tmp_path):
    up, clone = upstream_and_clone
    upstream_main = _git(up, "rev-parse", "main")
    _git(clone, "fetch", "-q", "origin")
    base = (
        f'GENESIS_ROOT="{clone}"\nUPDATE_REMOTE=origin\nDEPLOY_BRANCH=main\n'
        "ORIGINAL_BRANCH=main\n" + _STUBS
    )
    # HEAD does NOT contain the pinned head (nothing was merged): rolled back.
    r = _run(base + f'DEPLOY_HEAD="{upstream_main}"\n' + _merge_assertion_block(), tmp_path)
    assert r.returncode == 1
    assert "ROLLBACK: merge did not bring in" in r.stdout
    # Control: after a real merge of the pin, the assertion passes.
    _git(clone, "merge", "-q", upstream_main)
    r = _run(base + f'DEPLOY_HEAD="{upstream_main}"\n' + _merge_assertion_block(), tmp_path)
    assert r.returncode == 0 and "ROLLBACK" not in r.stdout, r.stdout


def test_the_merge_assertion_sits_between_the_merge_and_the_new_head_record():
    text = UPDATE.read_text()
    merge = text.index('merge --no-overwrite-ignore "$DEPLOY_HEAD" --no-edit')
    check = text.index('merge-base --is-ancestor "$DEPLOY_HEAD" HEAD')
    new_tag = text.index("NEW_TAG=$(git -C")
    assert merge < check < new_tag


# ── 3. the ephemeral-file backup ────────────────────────────────────────────


@pytest.fixture
def dirty_checkout(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    _git(tmp_path, "init", "-q", "-b", "main", str(root))
    (root / "AGENTS.md").write_text("base\n")
    (root / "config").mkdir()
    (root / "config" / "procedure_triggers.yaml").write_text("a: 1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "init")
    # The incoming range (what DEPLOY_HEAD brings): it changes BOTH ephemeral
    # files, so a local edit to either must be cleared before the merge.
    _git(root, "checkout", "-q", "-b", "incoming")
    (root / "AGENTS.md").write_text("upstream stats\n")
    (root / "config" / "procedure_triggers.yaml").write_text("a: 9\n")
    _git(root, "commit", "-qam", "upstream changes both")
    _git(root, "checkout", "-q", "main")
    return root


def _deploy_head(root: Path) -> str:
    return f'DEPLOY_HEAD="{_git(root, "rev-parse", "incoming")}"\n'


def _backup_script(root: Path, extra: str = "") -> str:
    return (
        f'GENESIS_ROOT="{root}"\nPOST_MERGE=false\n'
        + _deploy_head(root)
        + _STUBS
        + _block("ephemeral-prestop-backup")
        + extra
    )


def test_local_edits_are_backed_up_before_the_stop_and_restorable(dirty_checkout, tmp_path):
    root = dirty_checkout
    home = tmp_path / "home"
    (root / "AGENTS.md").write_text("staged edit\n")
    _git(root, "add", "AGENTS.md")
    (root / "AGENTS.md").write_text("staged edit\nplus an unstaged one\n")
    r = _run(_backup_script(root, 'echo "ROOT=$EPHEMERAL_BACKUP_ROOT"\n'), home)
    assert r.returncode == 0, r.stderr
    backup_root = Path(re.search(r"ROOT=(\S+)", r.stdout).group(1))
    saved = backup_root / "AGENTS.md"
    assert f"Backed up local edits to AGENTS.md: {saved}" in r.stdout
    assert (saved / "current").read_text() == "staged edit\nplus an unstaged one\n"
    assert oct((backup_root).stat().st_mode & 0o777) == "0o700"
    # The file is untouched by the backup itself.
    assert (root / "AGENTS.md").read_text() == "staged edit\nplus an unstaged one\n"
    # The patches reproduce the edits on a clean tree.
    _git(root, "checkout", "-q", "HEAD", "--", "AGENTS.md")
    _git(root, "apply", str(saved / "worktree.patch"))
    assert (root / "AGENTS.md").read_text() == "staged edit\nplus an unstaged one\n"
    # A clean file is not backed up at all.
    assert not (backup_root / "config").exists()


def test_a_failed_backup_exits_before_anything_stops(dirty_checkout, tmp_path):
    root = dirty_checkout
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    (home / ".genesis" / "premerge-backups").write_text("a FILE where the dir must go\n")
    (root / "AGENTS.md").write_text("local\n")
    r = _run(_backup_script(root, "echo REACHED-THE-STOP\n"), home)
    assert r.returncode == 1
    assert "server NOT stopped, nothing changed" in r.stdout
    assert "CLEARED-STATE" in r.stdout and "REACHED-THE-STOP" not in r.stdout
    assert (root / "AGENTS.md").read_text() == "local\n"


def _clear_script(root: Path, between: str = "", deploy_head: str | None = None) -> str:
    head = _deploy_head(root) if deploy_head is None else f'DEPLOY_HEAD="{deploy_head}"\n'
    return (
        f'GENESIS_ROOT="{root}"\nPOST_MERGE=false\n'
        + head
        + _STUBS
        + _block("ephemeral-prestop-backup")
        + between
        + _block("ephemeral-clear")
        + 'echo "ROOT=$EPHEMERAL_BACKUP_ROOT"\n'
    )


def test_the_clear_discards_edits_only_after_a_backup_of_their_current_content(
    dirty_checkout, tmp_path
):
    root = dirty_checkout
    (root / "AGENTS.md").write_text("before the stop\n")
    # The file changes AFTER the pre-stop backup (an indexer rewrote it mid-stop).
    between = f'printf "during the stop\\n" > "{root}/AGENTS.md"\n'
    r = _run(_clear_script(root, between), tmp_path / "home")
    assert r.returncode == 0, r.stderr
    backup_root = Path(re.search(r"ROOT=(\S+)", r.stdout).group(1))
    assert (backup_root / "AGENTS.md" / "current").read_text() == "before the stop\n"
    assert (backup_root / "late" / "AGENTS.md" / "current").read_text() == "during the stop\n"
    assert (root / "AGENTS.md").read_text() == "base\n", "cleared once backed up"


def test_the_clear_leaves_a_file_it_could_not_back_up(dirty_checkout, tmp_path):
    root = dirty_checkout
    (root / "AGENTS.md").write_text("before the stop\n")
    between = (
        f'printf "during the stop\\n" > "{root}/AGENTS.md"\n'
        'printf "not a dir\\n" > "$EPHEMERAL_BACKUP_ROOT/late"\n'
    )
    r = _run(_clear_script(root, between), tmp_path / "home")
    assert r.returncode == 0, r.stderr
    assert "could not back up local edits to AGENTS.md — leaving it in place" in r.stdout
    assert (root / "AGENTS.md").read_text() == "during the stop\n", "never discarded unsaved"


def test_an_unchanged_backup_is_reused_and_the_file_is_cleared(dirty_checkout, tmp_path):
    root = dirty_checkout
    (root / "config" / "procedure_triggers.yaml").write_text("a: 2\n")
    r = _run(_clear_script(root), tmp_path / "home")
    assert r.returncode == 0, r.stderr
    backup_root = Path(re.search(r"ROOT=(\S+)", r.stdout).group(1))
    assert not (backup_root / "late").exists(), "no second backup when the first is current"
    assert (root / "config" / "procedure_triggers.yaml").read_text() == "a: 1\n"


def test_an_edit_the_incoming_range_does_not_touch_is_left_alone(dirty_checkout, tmp_path):
    """git keeps a local edit to a file the merge does not change, so it is NOT
    cleared. It IS still backed up before the stop, because a rollback
    (`reset --hard`) would discard it. Here the range touches only code.py."""
    root = dirty_checkout
    _git(root, "checkout", "-q", "-b", "code-only")
    (root / "code.py").write_text("x = 2\n")
    _git(root, "add", "code.py")
    _git(root, "commit", "-qm", "upstream touches only code")
    code_only = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", "main")
    (root / "AGENTS.md").write_text("an indexer's local rewrite\n")
    r = _run(_clear_script(root, deploy_head=code_only), tmp_path / "home")
    assert r.returncode == 0, r.stderr
    assert "cleared local edits" not in r.stdout, r.stdout
    assert (root / "AGENTS.md").read_text() == "an indexer's local rewrite\n"
    # Saved anyway: a rollback after the stop would reset it to HEAD.
    backup_root = Path(re.search(r"ROOT=(\S+)", r.stdout).group(1))
    assert (backup_root / "AGENTS.md" / "current").read_text() == "an indexer's local rewrite\n"
    assert not (backup_root / "late").exists(), "no late backup for a file that is not cleared"
    # And git really does merge past it with the edit intact (the premise).
    _git(root, "merge", "-q", "--no-edit", code_only)
    assert (root / "AGENTS.md").read_text() == "an indexer's local rewrite\n"


def test_a_staged_edit_the_range_does_not_touch_is_cleared_on_a_diverged_merge(
    dirty_checkout, tmp_path
):
    """A true 3-way merge refuses on ANY index change, touched or not, so a staged
    edit to an ephemeral file must be backed up and cleared even when the incoming
    range does not change that file — else the merge fails after the stop."""
    root = dirty_checkout
    _git(root, "checkout", "-q", "-b", "code-only")
    (root / "code.py").write_text("x = 2\n")
    _git(root, "add", "code.py")
    _git(root, "commit", "-qm", "upstream touches only code")
    code_only = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", "main")
    (root / "local.txt").write_text("local work\n")
    _git(root, "add", "local.txt")
    _git(root, "commit", "-qm", "local main diverges")
    (root / "AGENTS.md").write_text("a staged local rewrite\n")
    _git(root, "add", "AGENTS.md")
    # The premise, measured in place: with the staged edit, the 3-way merge refuses.
    probe = subprocess.run(
        ["git", "-C", str(root), "merge", "--no-edit", code_only],
        capture_output=True,
        text=True,
        env=_env(root),
    )
    assert probe.returncode != 0, "control: git refuses a 3-way merge over a staged edit"
    assert not (root / ".git" / "MERGE_HEAD").exists(), "the refusal left no merge in progress"
    r = _run(_clear_script(root, deploy_head=code_only), tmp_path / "home")
    assert r.returncode == 0, r.stderr
    assert "cleared local edits to ephemeral AGENTS.md" in r.stdout, r.stdout
    backup_root = Path(re.search(r"ROOT=(\S+)", r.stdout).group(1))
    assert (backup_root / "AGENTS.md" / "current").read_text() == "a staged local rewrite\n"
    # And the merge now goes through.
    _git(root, "merge", "-q", "--no-edit", code_only)


def test_a_staged_only_change_after_the_backup_forces_a_second_backup(dirty_checkout, tmp_path):
    """The currency check compares the INDEX patch too: staging the same content
    after the backup changes only index.patch, and must still be saved first."""
    root = dirty_checkout
    (root / "AGENTS.md").write_text("unstaged at backup time\n")
    between = f'git -C "{root}" add AGENTS.md\n'
    r = _run(_clear_script(root, between), tmp_path / "home")
    assert r.returncode == 0, r.stderr
    backup_root = Path(re.search(r"ROOT=(\S+)", r.stdout).group(1))
    assert (backup_root / "AGENTS.md" / "index.patch").read_text() == ""
    assert (
        "unstaged at backup time"
        in (backup_root / "late" / "AGENTS.md" / "index.patch").read_text()
    )
    assert (root / "AGENTS.md").read_text() == "base\n"


def test_a_branch_switched_before_the_merge_is_rolled_back(upstream_and_clone, tmp_path):
    """The deployable-checkout assertion ran on main; someone switched the checkout
    to another branch before the merge. The merge then lands on that branch and
    still contains the pinned head — so the head check alone would pass."""
    up, clone = upstream_and_clone
    upstream_main = _git(up, "rev-parse", "main")
    _git(clone, "fetch", "-q", "origin")
    _git(clone, "checkout", "-q", "-b", "feature")
    _git(clone, "merge", "-q", upstream_main)
    base = (
        f'GENESIS_ROOT="{clone}"\nUPDATE_REMOTE=origin\nDEPLOY_BRANCH=main\n'
        f'ORIGINAL_BRANCH=main\nDEPLOY_HEAD="{upstream_main}"\n' + _STUBS
    )
    r = _run(base + _merge_assertion_block(), tmp_path)
    assert r.returncode == 1
    assert "ROLLBACK: merge did not bring in" in r.stdout
    # Control: the same tree, started on this branch, passes.
    r = _run(
        base.replace("ORIGINAL_BRANCH=main", "ORIGINAL_BRANCH=feature") + _merge_assertion_block(),
        tmp_path,
    )
    assert r.returncode == 0, r.stdout


def test_backup_runs_before_the_stop_and_the_clear_after_it():
    text = UPDATE.read_text()
    fetch = text.index('timeout 120 git -C "$GENESIS_ROOT" fetch')
    backup = text.index("# BEGIN ephemeral-prestop-backup")
    marker = text.index('\n_write_state "fetching"')
    stop = text.index("--- Stopping services for update ---")
    clear = text.index("# BEGIN ephemeral-clear")
    merge = text.index('merge --no-overwrite-ignore "$DEPLOY_HEAD" --no-edit')
    assert fetch < backup < marker < stop < clear < merge


def test_every_cleared_path_is_one_the_dirty_gate_excuses():
    """The clear list and the gate's allowlist must agree: a path the gate refuses
    never reaches the clear, and a path the clear would discard must be excused."""
    text = UPDATE.read_text()
    paths = re.search(r"^EPHEMERAL_CLEAR_PATHS=\(([^)]*)\)", text, re.M).group(1).split()
    assert paths == ["AGENTS.md", "config/procedure_triggers.yaml"]
    assert text.count("EPHEMERAL_CLEAR_PATHS=(") == 1
    marker = (REPO_ROOT / "scripts" / "lib" / "deploy_marker.sh").read_text()
    regex = re.search(r"^EPHEMERAL_DIRTY_RE='(.*)'$", marker, re.M).group(1)
    for p in paths:
        assert re.search(regex, f" M {p}"), p


# ── retention for the backups ───────────────────────────────────────────────


# ── the rollback's reset keeps edits too ────────────────────────────────────


def _rollback_script(root: Path, post_merge: bool, between: str = "") -> str:
    return (
        f'GENESIS_ROOT="{root}"\nPOST_MERGE={"true" if post_merge else "false"}\n'
        + _deploy_head(root)
        + _STUBS
        + _block("ephemeral-prestop-backup")
        + 'echo "ROOT=$EPHEMERAL_BACKUP_ROOT"\n'
        + between
        + '_ephemeral_backup_before_reset "$EPHEMERAL_BACKUP_ROOT"\n'
    )


def test_a_post_merge_rollback_backs_up_edits_first(dirty_checkout, tmp_path):
    """--post-merge takes no pre-stop backup, so the rollback's reset would drop an
    edit made there with no copy. The rollback now saves it first."""
    root = dirty_checkout
    (root / "AGENTS.md").write_text("edited before a --post-merge run\n")
    r = _run(_rollback_script(root, post_merge=True), tmp_path / "home")
    assert r.returncode == 0, r.stderr
    backup_root = Path(re.search(r"ROOT=(\S+)", r.stdout).group(1))
    assert not (backup_root / "AGENTS.md").exists(), "control: post-merge took no pre-stop backup"
    saved = backup_root / "rollback" / "AGENTS.md" / "current"
    assert saved.read_text() == "edited before a --post-merge run\n", r.stdout


def test_an_edit_made_after_the_backup_is_saved_before_the_reset(dirty_checkout, tmp_path):
    root = dirty_checkout
    (root / "AGENTS.md").write_text("first local edit\n")
    later = f'printf "rewritten during the update\\n" > "{root}/AGENTS.md"\n'
    r = _run(_rollback_script(root, post_merge=False, between=later), tmp_path / "home")
    assert r.returncode == 0, r.stderr
    backup_root = Path(re.search(r"ROOT=(\S+)", r.stdout).group(1))
    assert (backup_root / "AGENTS.md" / "current").read_text() == "first local edit\n"
    assert (
        backup_root / "rollback" / "AGENTS.md" / "current"
    ).read_text() == "rewritten during the update\n"


def test_an_unchanged_edit_is_not_backed_up_twice(dirty_checkout, tmp_path):
    root = dirty_checkout
    (root / "AGENTS.md").write_text("one local edit\n")
    r = _run(_rollback_script(root, post_merge=False), tmp_path / "home")
    assert r.returncode == 0, r.stderr
    backup_root = Path(re.search(r"ROOT=(\S+)", r.stdout).group(1))
    assert (backup_root / "AGENTS.md" / "current").exists()
    assert not (backup_root / "rollback").exists(), r.stdout


def test_the_rollback_saves_edits_before_its_reset():
    text = UPDATE.read_text()
    start = text.index("_do_rollback() {")
    body = text[start : text.index("\n}\n", start)]
    body = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
    save = body.index('_ephemeral_backup_before_reset "$EPHEMERAL_BACKUP_ROOT"')
    reset = body.index('checkout -q --no-overwrite-ignore -B "$ORIGINAL_BRANCH" "$ROLLBACK_TAG"')
    assert save < reset


def test_disk_hygiene_prunes_old_backup_runs_only(tmp_path):
    text = HYGIENE.read_text()
    start = text.index('if ! _pmb_root="$(cd -P -- "$HOME/.genesis/premerge-backups"')
    stanza = (
        f'. "{REPO_ROOT / "scripts" / "lib" / "tmp_liveness.sh"}"\n'
        + text[start : text.index("\n    fi\n", start) + 8]
    )
    base = tmp_path / ".genesis" / "premerge-backups"
    old = base / "20260101T000000Z-1"
    new = base / "20260920T000000Z-2"
    for d in (old, new):
        (d / "AGENTS.md").mkdir(parents=True)
        (d / "AGENTS.md" / "current").write_text("x\n")
    t = time.time() - 60 * 86400
    os.utime(old, (t, t))
    r = subprocess.run(
        ["bash", "-c", stanza],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(tmp_path)},
    )
    assert r.returncode == 0, r.stderr
    assert not old.exists() and new.exists()
