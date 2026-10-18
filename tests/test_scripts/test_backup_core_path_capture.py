"""Real path transport and overlap safety, including trailing LF names."""

import os
import subprocess
from pathlib import Path

import pytest

HELPER = Path(__file__).parents[2] / "scripts/lib/backup_core_paths.sh"


@pytest.mark.parametrize("suffix", ["plain", "embedded\nname", "trailing\n", "double\n\n",
                                   "space ", "dot.", "unicode-日"])
@pytest.mark.parametrize("relationship", ["equal", "descendant", "ancestor", "disjoint"])
def test_exact_realpath_and_overlap(tmp_path, suffix, relationship):
    core = tmp_path / suffix
    core.mkdir()
    targets = {"equal": core, "descendant": core / "child", "ancestor": tmp_path,
               "disjoint": tmp_path / (suffix + "-sibling")}
    target = targets[relationship]
    env = {**os.environ, "HOME": str(tmp_path / "home"), "GENESIS_DIR": str(core),
           "TRANSCRIPT_DIR": str(tmp_path / "transcripts"),
           "_SCRIPT_DIR": str(HELPER.parents[1])}
    script = '''source "$1"
backup_capture_path captured realpath -m -- "$2" || exit "$?"
printf '%s\\0' "$captured"
if backup_core_overlap "$captured" >/dev/null; then printf 0; else printf %s "$?"; fi
'''
    result = subprocess.run(["bash", "-c", script, "test", str(HELPER), str(target)],
                            env=env, capture_output=True, check=True)
    captured, status = result.stdout.split(b"\0")
    assert captured == os.fsencode(target.resolve())
    assert int(status) == (1 if relationship == "disjoint" else 0)


@pytest.mark.parametrize("status", [1, 2, 75, 130])
def test_capture_failure_preserves_status_and_clears_output(status):
    script = '''source "$1"
producer() { printf 'stale\\n'; return "$2"; }
captured=prior
if backup_capture_path captured producer placeholder "$2"; then exit 99; else rc=$?; fi
[ "$rc" = "$2" ] && [ -z "$captured" ]
'''
    subprocess.run(["bash", "-c", script, "test", str(HELPER), str(status)], check=True)


@pytest.mark.parametrize("output", ["", "missing-formatter", "\n"])
def test_malformed_success_does_not_authorize_path(output):
    script = '''source "$1"
producer() { printf %s "$2"; }
captured=prior
if backup_capture_path captured producer placeholder "$2"; then exit 99; else rc=$?; fi
[ "$rc" = 2 ] && [ -z "$captured" ]
'''
    subprocess.run(["bash", "-c", script, "test", str(HELPER), output], check=True)


def test_core_inspection_failure_is_not_disjoint(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path / "home"), "GENESIS_DIR": str(tmp_path / "core"),
           "TRANSCRIPT_DIR": str(tmp_path / "transcripts"),
           "_SCRIPT_DIR": str(HELPER.parents[1])}
    script = '''source "$1"
realpath() { return 75; }
if backup_core_overlap "$2"; then exit 99; else rc=$?; fi
[ "$rc" = 2 ]
'''
    subprocess.run(["bash", "-c", script, "test", str(HELPER), str(tmp_path / "outside")],
                   env=env, check=True)


@pytest.mark.parametrize("left,right,status", [
    ("/", "/bounded/core", 0), ("/bounded/core", "/", 0), ("/", "/", 0),
    ("/bounded/core", "/bounded/core/child", 0),
    ("/bounded/core/child", "/bounded/core", 0),
    ("/bounded/one", "/bounded/two", 1), ("/bounded/a\n", "/bounded/a", 1),
    ("/bounded/a\n", "/bounded/a\n/child", 0),
    ("", "/bounded/core", 2), ("relative", "/bounded/core", 2),
    ("/bounded/core", "relative", 2),
])
def test_shared_overlap_root_boundary_and_invalid_paths(left, right, status):
    script = '''source "$1"
if backup_paths_overlap "$2" "$3"; then printf 0; else printf %s "$?"; fi
'''
    proc = subprocess.run(["bash", "-c", script, "test", str(HELPER), left, right],
                          capture_output=True, check=True)
    assert int(proc.stdout) == status


@pytest.mark.parametrize("protected_root", [False, True])
def test_core_overlap_recognizes_filesystem_root_in_both_directions(tmp_path, protected_root):
    env = {**os.environ, "HOME": str(tmp_path / "home"),
           "GENESIS_DIR": "/" if protected_root else str(tmp_path / "core"),
           "TRANSCRIPT_DIR": str(tmp_path / "transcripts"),
           "_SCRIPT_DIR": str(HELPER.parents[1])}
    candidate = str(tmp_path / "outside") if protected_root else "/"
    subprocess.run(["bash", "-c", 'source "$1"; backup_core_overlap "$2" >/dev/null',
                    "test", str(HELPER), candidate], env=env, check=True)
