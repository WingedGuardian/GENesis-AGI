"""scripts/lib/managed_etc.sh genesis_etc_put, driven directly.

The callers' own suites (test_memory_resilience.py, test_network_resilience.py,
test_bootstrap_etc_dropins.py) cover the paths through each writer; these cases
pin the helper's edges that no caller reaches today.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "scripts" / "lib" / "managed_etc.sh"
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap.sh"

_SUDO_STUB = '#!/bin/bash\nexec "$@"\n'


def _put(tmp_path: Path, path: Path, content: str, extra: str = "") -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "sudo").write_text(_SUDO_STUB)
    (bin_dir / "sudo").chmod(0o755)
    script = (
        f'set -euo pipefail; . "{LIB}"; {extra}\n'
        f'genesis_etc_put "{path}" "$CONTENT" "" dropin\n'
        'printf "%s|%s\\n" "$GENESIS_ETC_RESULT" "$GENESIS_ETC_BACKUP"'
    )
    r = subprocess.run(
        ["/bin/bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path), "CONTENT": content},
    )
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout.strip().splitlines()[-1]


def test_content_ending_in_a_newline_still_migrates_a_legacy_file(tmp_path):
    """The old writers wrote `printf '%s\\n' "$content"`; content that itself ends
    in a newline must still read as Genesis's own, not as a hand edit."""
    target = tmp_path / "etc" / "x.conf"
    target.parent.mkdir()
    target.write_text("[X]\nY=1\n")
    assert _put(tmp_path, target, "[X]\nY=1\n").startswith("migrated|")


def test_a_stale_backup_path_never_survives_into_the_next_call(tmp_path):
    target = tmp_path / "etc" / "x.conf"
    out = _put(tmp_path, target, "[X]\nY=1", extra="GENESIS_ETC_BACKUP=/stale/path")
    assert out == "written|"


def test_bootstrap_counts_kept_etc_files_apart_from_units():
    text = BOOTSTRAP.read_text()
    assert "grep -c '^kept-etc ' \"$GENESIS_DEPLOY_SUMMARY\"" in text
    assert "/etc file(s) KEPT, not overwritten" in text
    # The units' count must not swallow the /etc lines.
    assert "grep -c '^kept ' \"$GENESIS_DEPLOY_SUMMARY\"" in text


def test_an_edited_file_is_kept_unless_its_path_is_taken(tmp_path):
    target = tmp_path / "etc" / "x.conf"
    target.parent.mkdir()
    target.write_text("[X]\nMine=1\n")
    assert _put(tmp_path, target, "[X]\nY=1") == "kept-edited|"
    assert target.read_text() == "[X]\nMine=1\n"
    backups = tmp_path / "backups"
    out = _put(
        tmp_path,
        target,
        "[X]\nY=1",
        extra=f'GENESIS_TAKE_TEMPLATES="{target}"; GENESIS_MU_BACKUP_DIR="{backups}"',
    )
    saved = backups / str(target).lstrip("/")
    assert out == f"taken|{saved}"
    assert saved.read_text() == "[X]\nMine=1\n"
    assert saved.stat().st_mode & 0o777 == 0o600
    assert target.read_text().startswith("[X]\nY=1\n# genesis-managed v1 sha256=")


def test_a_supervised_update_session_never_takes_an_etc_file(tmp_path):
    """managed_units.sh refuses every take under GENESIS_UPDATE_TIER=1; the /etc
    writers are one more door and must refuse too, `etc:*` and full path alike."""
    target = tmp_path / "etc" / "x.conf"
    target.parent.mkdir()
    target.write_text("[X]\nMine=1\n")
    for take in ("etc:*", str(target)):
        out = _put(
            tmp_path,
            target,
            "[X]\nY=1",
            extra=f'GENESIS_UPDATE_TIER=1; GENESIS_TAKE_TEMPLATES="{take}"',
        )
        assert out == "kept-edited|"
        assert target.read_text() == "[X]\nMine=1\n"


def test_a_symlink_is_kept_and_never_written_through(tmp_path):
    target = tmp_path / "etc" / "x.conf"
    target.parent.mkdir()
    real = tmp_path / "real.conf"
    real.write_text("mine\n")
    target.symlink_to(real)
    out = _put(tmp_path, target, "[X]\nY=1", extra="GENESIS_TAKE_TEMPLATES='etc:*'")
    assert out == "kept-nonregular|"
    assert target.is_symlink() and real.read_text() == "mine\n"


# A sudo that logs every call and fails the ones the case names: the inspection
# (`test` probes, or one `/bin/sh -c` classification), `cat`, or nothing.
_FLAKY_SUDO = """#!/bin/bash
echo "$*" >> "$SUDO_LOG"
case "$1" in
    test|/bin/sh) [ -n "${FAIL_INSPECT:-}" ] && exit 1 ;;
    cat) [ -n "${FAIL_CAT:-}" ] && exit 1 ;;
esac
exec "$@"
"""


def _put_logged(tmp_path: Path, path: Path, content: str, env_extra: dict, extra: str = ""):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "sudo").write_text(_FLAKY_SUDO)
    (bin_dir / "sudo").chmod(0o755)
    summary = tmp_path / "summary.txt"
    log = tmp_path / "sudo.log"
    script = (
        f'set -euo pipefail; . "{LIB}"; GENESIS_ETC_SUMMARY_OK=1; {extra}\n'
        f'genesis_etc_put "{path}" "$CONTENT" "" dropin\n'
        'printf "%s\\n" "$GENESIS_ETC_RESULT"'
    )
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "CONTENT": content,
        "SUDO_LOG": str(log),
        "GENESIS_DEPLOY_SUMMARY": str(summary),
        **env_extra,
    }
    r = subprocess.run(
        ["/bin/bash", "-c", script], capture_output=True, text=True, timeout=30, env=env
    )
    assert r.returncode == 0, r.stdout + r.stderr
    calls = log.read_text() if log.exists() else ""
    lines = summary.read_text().splitlines() if summary.exists() else []
    return r.stdout.strip().splitlines()[-1], r.stdout, calls, lines


def test_a_sudo_that_cannot_inspect_never_writes_an_edited_file(tmp_path):
    """A failing sudo must not read as "missing": that falls through to the write
    and overwrites a hand edit with no backup."""
    target = tmp_path / "etc" / "x.conf"
    target.parent.mkdir()
    target.write_text("[X]\nMine=1\n")
    result, out, calls, lines = _put_logged(tmp_path, target, "[X]\nY=1", {"FAIL_INSPECT": "1"})
    assert target.read_text() == "[X]\nMine=1\n"
    assert not any(c.startswith("tee") for c in calls.splitlines())
    assert result == "kept-nonregular"
    assert f"kept-etc {target}: could not be inspected" in lines
    assert "left as it is" in out


def test_a_sudo_that_cannot_read_never_writes(tmp_path):
    target = tmp_path / "etc" / "x.conf"
    target.parent.mkdir()
    target.write_text("[X]\nMine=1\n")
    result, _, calls, lines = _put_logged(tmp_path, target, "[X]\nY=1", {"FAIL_CAT": "1"})
    assert target.read_text() == "[X]\nMine=1\n"
    assert not any(c.startswith("tee") for c in calls.splitlines())
    assert result == "kept-nonregular"
    assert f"kept-etc {target}: could not be inspected" in lines


def test_a_take_whose_backup_fails_is_named_in_the_summary(tmp_path):
    target = tmp_path / "etc" / "x.conf"
    target.parent.mkdir()
    target.write_text("[X]\nMine=1\n")
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")  # mkdir -p under a regular file fails
    result, _, _, lines = _put_logged(
        tmp_path,
        target,
        "[X]\nY=1",
        {},
        extra=f'GENESIS_TAKE_TEMPLATES="etc:*"; GENESIS_MU_BACKUP_DIR="{blocker}"',
    )
    assert result == "kept-edited"
    assert target.read_text() == "[X]\nMine=1\n"
    assert f"kept-etc {target}: take requested but the backup failed; not overwritten" in lines


def test_a_path_with_whitespace_is_not_offered_a_take_it_cannot_have(tmp_path):
    """The take list is space-separated, so such a path can never be taken."""
    target = tmp_path / "etc dir" / "x.conf"
    target.parent.mkdir()
    target.write_text("[X]\nMine=1\n")
    result, out, _, lines = _put_logged(tmp_path, target, "[X]\nY=1", {})
    assert result == "kept-edited"
    assert "--take-template" not in out
    assert not any("--take-template" in line for line in lines)
    assert any(line.startswith(f"kept-etc {target}: edited by hand") for line in lines)
