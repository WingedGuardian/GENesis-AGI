"""Fail-closed version gate for the GitNexus MCP launcher."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = REPO_ROOT / ".claude" / "mcp" / "run-gitnexus"


def _fake_gitnexus(tmp_path: Path, version: str) -> tuple[Path, Path]:
    binary = tmp_path / "gitnexus"
    log = tmp_path / "args.log"
    binary.write_text(
        "#!/usr/bin/env bash\n"
        f'if [ "${{1:-}}" = "--version" ]; then echo "{version}"; exit 0; fi\n'
        f'printf "%s\\n" "$*" > "{log}"\n'
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary, log


def _run(tmp_path: Path, version: str, *, node_version: str = "v22.22.2"):
    binary, log = _fake_gitnexus(tmp_path, version)
    node = tmp_path / "node"
    node.write_text(f"#!/bin/sh\necho {node_version}\n")
    node.chmod(node.stat().st_mode | stat.S_IXUSR)
    env = {
        **os.environ,
        "GITNEXUS_BIN": str(binary),
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
    }
    result = subprocess.run(
        [str(LAUNCHER), "mcp"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, log


def test_launcher_executes_only_the_pinned_binary(tmp_path):
    result, log = _run(tmp_path, "1.6.12")
    assert result.returncode == 0, result.stderr
    assert log.read_text() == "mcp\n"


def test_launcher_refuses_a_stale_binary(tmp_path):
    result, log = _run(tmp_path, "1.6.8")
    assert result.returncode != 0
    assert "refusing unpinned GitNexus 1.6.8; expected 1.6.12" in result.stderr
    assert not log.exists()


def test_launcher_refuses_an_unsupported_node_runtime(tmp_path):
    result, log = _run(tmp_path, "1.6.12", node_version="v23.11.1")
    assert result.returncode != 0
    assert "does not support Node v23.11.1" in result.stderr
    assert not log.exists()


def test_launcher_resolves_gitnexus_from_npm_prefix_outside_path(tmp_path):
    path_bin = tmp_path / "path-bin"
    prefix_bin = tmp_path / "npm-prefix" / "bin"
    path_bin.mkdir()
    prefix_bin.mkdir(parents=True)
    binary, log = _fake_gitnexus(prefix_bin, "1.6.12")
    node = path_bin / "node"
    node.write_text("#!/bin/sh\necho v22.22.2\n")
    node.chmod(0o755)
    npm = path_bin / "npm"
    npm.write_text(f'#!/bin/sh\necho "{tmp_path / "npm-prefix"}"\n')
    npm.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{path_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
    }
    env.pop("GITNEXUS_BIN", None)
    result = subprocess.run(
        [str(LAUNCHER), "status"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert binary == prefix_bin / "gitnexus"
    assert log.read_text() == "status\n"


def _npm_install(root: Path, version: str) -> Path:
    """An npm-shaped install: a bin symlink into a package with a manifest."""
    pkg = root / "lib" / "node_modules" / "gitnexus"
    (pkg / "dist" / "cli").mkdir(parents=True)
    entry = pkg / "dist" / "cli" / "index.js"
    entry.write_text("#!/usr/bin/env node\n// entry\n")
    entry.chmod(entry.stat().st_mode | stat.S_IXUSR)
    (pkg / "package.json").write_text(json.dumps({"name": "gitnexus", "version": version}) + "\n")
    binary = root / "bin" / "gitnexus"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.symlink_to(entry)
    return binary


def test_launcher_surfaces_why_the_resolver_refused(tmp_path):
    """The resolver's own diagnostic must reach the operator.

    It refuses for several different reasons and only it knows which. The
    launcher used to send that text to a temp file and print "GitNexus is not
    installed" whenever the file came back empty — which told an operator with
    two conflicting installs to go and install what they already had twice.

    It now simply does not redirect. There is nothing left that needs the text
    as a value, and the temp file cost a predictable fallback path (pid-only
    when mktemp was unavailable) that a local attacker could pre-create as a
    symlink so the redirection truncated a file of their choosing.
    """
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    # Two installations at DIFFERENT versions — a conflict the resolver refuses.
    _npm_install(home / ".local", "1.6.12")
    second = _npm_install(tmp_path / "other", "1.6.8")
    path_dir = tmp_path / "pathbin"
    path_dir.mkdir()
    (path_dir / "gitnexus").symlink_to(second)
    node = path_dir / "node"
    node.write_text("#!/bin/sh\necho v22.22.2\n")
    node.chmod(node.stat().st_mode | stat.S_IXUSR)

    result = subprocess.run(
        [str(LAUNCHER), "mcp"],
        env={"HOME": str(home), "PATH": f"{path_dir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "conflicting installations" in result.stderr, result.stderr
    assert "1.6.12" in result.stderr and "1.6.8" in result.stderr, result.stderr


def test_launcher_refuses_a_candidate_whose_version_cannot_be_read(tmp_path):
    """A shadow scan that cannot read a candidate must refuse, not pick one.

    `first_version` tracked initialization by emptiness, so an unreadable first
    candidate left it empty, the SECOND candidate initialized it, the conflict
    check never compared against the first — and the resolver returned the
    first anyway. MEASURED before the fix: two candidates, the first with no
    readable version, resolved successfully to that unreadable binary.
    """
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    unreadable = home / ".local" / "bin" / "gitnexus"  # no manifest, prints nothing
    unreadable.write_text("#!/bin/sh\nexit 0\n")
    unreadable.chmod(unreadable.stat().st_mode | stat.S_IXUSR)
    readable = _npm_install(tmp_path / "other", "1.6.12")
    path_dir = tmp_path / "pathbin"
    path_dir.mkdir()
    (path_dir / "gitnexus").symlink_to(readable)
    node = path_dir / "node"
    node.write_text("#!/bin/sh\necho v22.22.2\n")
    node.chmod(node.stat().st_mode | stat.S_IXUSR)

    result = subprocess.run(
        [str(LAUNCHER), "mcp"],
        env={"HOME": str(home), "PATH": f"{path_dir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "cannot determine the version" in result.stderr, result.stderr
    assert str(unreadable) in result.stderr, result.stderr

    # …and the RESOLVER's own contract, not just the launcher's exit code.
    # Every other caller — `genesis_gitnexus_ensure_pin`, code_intel_index.sh —
    # reads the return value, not this message. A softened guard that prints the
    # diagnostic and then hands back `candidates[0]` anyway satisfies everything
    # above while restoring the exact defect, so the refusal is asserted here.
    helper = REPO_ROOT / "scripts" / "lib" / "gitnexus_version.sh"
    direct = subprocess.run(
        ["bash", "-c", f'source "{helper}"; genesis_gitnexus_resolve_binary; echo "rc=$?"'],
        env={"HOME": str(home), "PATH": f"{path_dir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    # rc 2, not 1: "installations exist but cannot be told apart", which
    # `genesis_gitnexus_ensure_pin` must not answer by installing another one.
    assert "rc=2" in direct.stdout, f"the resolver did not refuse: {direct.stdout!r}"
    assert direct.stdout.replace("rc=2", "").strip() == "", (
        f"the resolver returned a path it could not read: {direct.stdout!r}"
    )


def test_ensure_pin_does_not_install_over_an_ambiguous_machine(tmp_path):
    """A machine with installations it cannot tell apart is rc 3, not an install.

    `genesis_gitnexus_ensure_pin` documents rc 3 for "an installed version whose
    output cannot be classified safely". When the resolver began refusing an
    unreadable candidate it returned 1 — indistinguishable from "nothing is
    installed" — so ensure_pin fell through and ran `npm install -g`. That
    neither resolves an ambiguity between the copies already present nor is safe
    to guess at, and the resolver refuses again immediately afterwards. The
    refusal is rc 2 now, and only rc 1 means install.

    The control moves in both directions: an EMPTY machine must still install.
    """
    helper = REPO_ROOT / "scripts" / "lib" / "gitnexus_version.sh"

    def _run(home: Path, path_dir: Path):
        npm_log = tmp_path / f"npm-{home.name}.log"
        npm = path_dir / "npm"
        npm.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{npm_log}"\nexit 0\n')
        npm.chmod(npm.stat().st_mode | stat.S_IXUSR)
        proc = subprocess.run(
            ["bash", "-c", f'source "{helper}"; genesis_gitnexus_ensure_pin; echo "rc=$?"'],
            env={"HOME": str(home), "PATH": f"{path_dir}:/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=False,
        )
        installs = npm_log.read_text().splitlines() if npm_log.exists() else []
        return proc, [ln for ln in installs if "install" in ln]

    # Ambiguous: two installations, the first unreadable.
    ambiguous = tmp_path / "ambiguous"
    (ambiguous / ".local" / "bin").mkdir(parents=True)
    unreadable = ambiguous / ".local" / "bin" / "gitnexus"
    unreadable.write_text("#!/bin/sh\nexit 0\n")
    unreadable.chmod(unreadable.stat().st_mode | stat.S_IXUSR)
    amb_path = tmp_path / "ambiguous-bin"
    amb_path.mkdir()
    (amb_path / "gitnexus").symlink_to(_npm_install(tmp_path / "second", "1.6.12"))
    proc, installs = _run(ambiguous, amb_path)
    assert "rc=3" in proc.stdout, proc.stdout + proc.stderr
    assert not installs, f"installed on top of an ambiguous machine: {installs}"

    # Control: nothing installed anywhere — this one MUST install.
    empty = tmp_path / "empty"
    (empty / ".genesis").mkdir(parents=True)
    empty_path = tmp_path / "empty-bin"
    empty_path.mkdir()
    proc, installs = _run(empty, empty_path)
    assert installs, f"an empty machine was not installed to: {proc.stdout!r}"
    assert any("gitnexus@" in ln for ln in installs), installs
