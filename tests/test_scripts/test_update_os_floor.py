"""update.sh refuses below the supported OS floor before it fetches or merges.

Off the floor (glibc 2.38 on x86_64 or aarch64) the merged code's bootstrap
refuses, so an update that merged first would leave code nothing activates.
These run the real ``os-floor`` block of scripts/update.sh against the real
lib, with getconf, uname and git stubbed on PATH: a refusal must exit before
any git command runs.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_scripts._update_source import update_text

REPO = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="bash")


def _block(name: str) -> str:
    text = update_text()
    begin = text.index(f"# BEGIN {name}")
    return text[begin : text.index(f"# END {name}", begin)]


def _stubs(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in {
        "getconf": '[ "$1" = GNU_LIBC_VERSION ] || exit 1\nprintf "%s\\n" "${FAKE_GLIBC-glibc 2.39}"\n',
        "uname": '[ "$1" = -m ] || exec /bin/uname "$@"\nprintf "%s\\n" "${FAKE_ARCH-x86_64}"\n',
        "git": 'printf "%s\\n" "$*" >> "$GIT_LOG"\n',
    }.items():
        stub = bin_dir / name
        stub.write_text("#!/bin/bash\n" + body)
        stub.chmod(0o755)
    return bin_dir


def _run(tmp_path: Path, *, scripts: Path = REPO / "scripts", post_merge=False, **fake):
    bin_dir = _stubs(tmp_path)
    git_log = tmp_path / "git.log"
    script = (
        "set -Eeuo pipefail\n"
        f'SCRIPT_DIR="{scripts}"\nPOST_MERGE={"true" if post_merge else "false"}\n'
        + _block("os-floor")
        # What update.sh does next: resolve the remote, fetch, merge.
        + 'git fetch origin\ngit merge origin/main\necho "REACHED_FETCH"\n'
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "GENESIS_"))}
    env.update(PATH=f"{bin_dir}:/usr/bin:/bin", GIT_LOG=str(git_log), HOME=str(tmp_path))
    env.update(fake)
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    calls = git_log.read_text().splitlines() if git_log.exists() else []
    return result, calls


@pytest.mark.parametrize(
    ("fake", "says"),
    [
        ({"FAKE_GLIBC": "glibc 2.35"}, "this OS has glibc 2.35"),
        ({"FAKE_GLIBC": ""}, "this OS has no glibc"),
        ({"FAKE_ARCH": "riscv64"}, "this machine is riscv64"),
    ],
    ids=["old-glibc", "no-glibc", "arch"],
)
def test_below_the_floor_refuses_before_any_git_operation(tmp_path, fake, says):
    result, calls = _run(tmp_path, **fake)
    assert result.returncode == 5, result.stdout + result.stderr
    assert calls == [], "ran git before refusing"
    assert "REFUSED: " in result.stdout and says in result.stdout
    assert "Nothing was changed." in result.stdout
    assert "REACHED_FETCH" not in result.stdout


def test_on_the_floor_it_goes_on_to_the_fetch(tmp_path):
    """Negative control: the stubs and the block pass a supported machine."""
    result, calls = _run(tmp_path, FAKE_ARCH="aarch64")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "REACHED_FETCH" in result.stdout
    assert calls == ["fetch origin", "merge origin/main"]


def test_a_post_merge_is_not_refused(tmp_path):
    """--post-merge finishes a merge that already happened."""
    result, _ = _run(tmp_path, post_merge=True, FAKE_GLIBC="glibc 2.31")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "REACHED_FETCH" in result.stdout


def test_a_checkout_without_the_lib_is_not_checked(tmp_path):
    scripts = tmp_path / "old-scripts"
    (scripts / "lib").mkdir(parents=True)
    result, _ = _run(tmp_path, scripts=scripts, FAKE_GLIBC="glibc 2.31")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "REACHED_FETCH" in result.stdout


def test_the_floor_is_checked_after_the_lock_and_before_any_fetch_or_tag():
    text = update_text()
    floor = text.index("# BEGIN os-floor")
    assert text.index('flock -n "$_UPDATE_LOCK_FD"') < floor
    assert text.index("# END prior-state-guard") < floor
    assert floor < text.index("_UR_OUT=$(")  # the remote is resolved (and pinned) after
    assert floor < text.index('ROLLBACK_TAG="pre-update-')
    assert floor < text.index('_write_state "fetching"')
    assert floor < text.index("trap _on_err ERR")
