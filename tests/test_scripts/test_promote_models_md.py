"""Tests for scripts/promote_models_md.sh — overlay → tracked-reference copy.

Runs the REAL script against a throwaway repo + overlay in tmp_path, driving
the tracked doc location via the script's own layout and the overlay location
via GENESIS_OUTPUT_DIR. Covers: happy-path copy, the missing-overlay guard
(non-zero exit + guidance), and the identical-content no-op branch.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "promote_models_md.sh"


def _fake_repo(tmp_path: Path) -> Path:
    """A tmp dir shaped like the repo, with the script reachable at scripts/."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "docs" / "reference").mkdir(parents=True)
    # The script resolves REPO_ROOT from its own location, so copy it in.
    dst = repo / "scripts" / "promote_models_md.sh"
    dst.write_text(_SCRIPT.read_text())
    dst.chmod(0o755)
    return repo


def _run(repo: Path, output_dir: Path):
    env = dict(os.environ)
    env["GENESIS_OUTPUT_DIR"] = str(output_dir)
    return subprocess.run(
        ["bash", str(repo / "scripts" / "promote_models_md.sh")],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def test_copies_overlay_onto_tracked(tmp_path):
    repo = _fake_repo(tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "models.md").write_text("OVERLAY v2\n")
    tracked = repo / "docs" / "reference" / "models.md"
    tracked.write_text("OLD TRACKED\n")

    result = _run(repo, output_dir)

    assert result.returncode == 0, result.stderr
    assert tracked.read_text() == "OVERLAY v2\n"


def test_missing_overlay_exits_nonzero(tmp_path):
    repo = _fake_repo(tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()  # no models.md inside

    result = _run(repo, output_dir)

    assert result.returncode != 0
    assert "No overlay" in result.stderr


def test_identical_content_is_noop(tmp_path):
    repo = _fake_repo(tmp_path)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "models.md").write_text("SAME\n")
    tracked = repo / "docs" / "reference" / "models.md"
    tracked.write_text("SAME\n")

    result = _run(repo, output_dir)

    assert result.returncode == 0, result.stderr
    assert "already identical" in result.stdout


def test_unset_home_and_no_output_dir_does_not_abort(tmp_path):
    """Regression: with HOME unset AND GENESIS_OUTPUT_DIR unset, the $HOME
    default must not abort under `set -u`. A getent shim redirects HOME to a tmp
    home; the script should resolve it and reach the normal missing-overlay exit
    (not a "HOME: unbound variable" crash)."""
    repo = _fake_repo(tmp_path)
    fake_home = tmp_path / "pwhome"
    fake_home.mkdir()
    shim_bin = tmp_path / "bin"
    shim_bin.mkdir()
    getent = shim_bin / "getent"
    getent.write_text('#!/bin/sh\necho "u:x:$(id -u):$(id -g)::${FAKE_HOME}:/bin/sh"\n')
    getent.chmod(0o755)

    env = {
        "PATH": f"{shim_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
        "FAKE_HOME": str(fake_home),
    }  # no HOME, no GENESIS_OUTPUT_DIR
    result = subprocess.run(
        ["/bin/bash", str(repo / "scripts" / "promote_models_md.sh")],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )

    assert "unbound variable" not in result.stderr
    # Resolved HOME to the passwd home; overlay absent there → guarded exit 1.
    assert result.returncode == 1
    assert str(fake_home / ".genesis" / "output" / "models.md") in result.stderr
