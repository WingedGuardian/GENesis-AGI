"""CC single-copy enforcement (cc_shadow_scan / cc_ensure_local PATH-blind fix).

Four real shadow-copy incidents motivated scripts/lib/cc_version.sh's
``cc_shadow_scan``: an nvm-tree copy shadowing the pin in interactive shells,
a native-installer symlink in ~/.local/bin, leftover native version blobs,
and a user-prefix copy invisible to non-interactive shells (which also made
``cc_ensure_local`` reinstall CC on every update run).

Harness: sources the REAL lib with a fake $HOME, a minimal PATH (no sudo —
which is also the safety these tests assert for system-path candidates), and
``CC_PROBE_DIRS`` pointed at fake dirs so nothing depends on the machine's
actual Claude Code install.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LIB = _REPO_ROOT / "scripts" / "lib" / "cc_version.sh"

_TOOLS = ("bash", "sh", "env", "readlink", "rm", "grep", "dirname", "awk",
          "cat", "mkdir", "ln", "chmod", "timeout")


def _minimal_bin(tmp_path: Path) -> Path:
    """PATH dir with core tools but deliberately NO sudo and NO npm."""
    d = tmp_path / "minbin"
    d.mkdir(exist_ok=True)
    for tool in _TOOLS:
        for src_dir in ("/usr/bin", "/bin"):
            src = Path(src_dir) / tool
            if src.exists():
                (d / tool).symlink_to(src)
                break
    return d


def _write_exec(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _plant_npm_tree(prefix: Path, version: str = "2.1.150") -> Path:
    """An npm-style CC install: bin/claude → ../lib/node_modules/.../claude.exe."""
    pkg = prefix / "lib" / "node_modules" / "@anthropic-ai" / "claude-code"
    _write_exec(pkg / "bin" / "claude.exe",
                f'#!/usr/bin/env bash\necho "{version} (Claude Code)"\n')
    bin_link = prefix / "bin" / "claude"
    bin_link.parent.mkdir(parents=True, exist_ok=True)
    bin_link.symlink_to("../lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe")
    return bin_link


def _run(tmp_path: Path, script: str, *, extra_env: dict | None = None,
         canonical: Path | None = None) -> subprocess.CompletedProcess:
    """Source the real lib in a fake HOME and run `script`."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    minbin = _minimal_bin(tmp_path)
    path_dirs = [str(minbin)]
    if canonical is not None:
        path_dirs.insert(0, str(canonical.parent))
    harness = tmp_path / "harness.sh"
    harness.write_text(f'#!/usr/bin/env bash\n. "{_LIB}"\n{script}\n', encoding="utf-8")
    env = {"PATH": ":".join(path_dirs), "HOME": str(home),
           "CC_PROBE_DIRS": str(tmp_path / "probe-nowhere"),
           "CC_VERSION": "2.1.201",
           **(extra_env or {})}
    return subprocess.run(["bash", str(harness)], env=env,
                          capture_output=True, text=True, timeout=30)


def _canonical(tmp_path: Path, version: str = "2.1.201") -> Path:
    return _write_exec(tmp_path / "canon" / "claude",
                       f'#!/usr/bin/env bash\necho "{version} (Claude Code)"\n')


# ── cc_shadow_scan ────────────────────────────────────────────────────────


def test_nvm_shadow_removed_with_package_dir(tmp_path):
    canon = _canonical(tmp_path)
    home = tmp_path / "home"
    nvm_prefix = home / ".nvm" / "versions" / "node" / "v24.15.0"
    link = _plant_npm_tree(nvm_prefix)
    res = _run(tmp_path, "cc_shadow_scan", canonical=canon)
    assert res.returncode == 0, res.stderr
    assert not link.exists()
    assert not (nvm_prefix / "lib" / "node_modules" / "@anthropic-ai" / "claude-code").exists()
    assert "removing shadow copy" in res.stdout
    assert canon.exists()


def test_native_installer_artifacts_removed(tmp_path):
    canon = _canonical(tmp_path)
    home = tmp_path / "home"
    blob_dir = home / ".local" / "share" / "claude" / "versions"
    _write_exec(blob_dir / "2.1.170", "#!/usr/bin/env bash\n")
    native_link = home / ".local" / "bin" / "claude"
    native_link.parent.mkdir(parents=True)
    native_link.symlink_to(home / ".local" / "share" / "claude" / "versions" / "2.1.170")
    res = _run(tmp_path, "cc_shadow_scan", canonical=canon)
    assert res.returncode == 0, res.stderr
    assert not blob_dir.exists()
    assert not native_link.exists() and not native_link.is_symlink()


def test_unprovable_artifact_kept_and_warned(tmp_path):
    canon = _canonical(tmp_path)
    home = tmp_path / "home"
    impostor = _write_exec(home / ".npm-global" / "bin" / "claude",
                           "#!/usr/bin/env bash\necho not-claude\n")
    res = _run(tmp_path, "cc_shadow_scan", canonical=canon)
    assert res.returncode == 0, res.stderr
    assert impostor.exists()
    assert "not provably a claude-code install" in res.stderr


def test_canonical_on_a_scanned_surface_is_kept(tmp_path):
    # Canonical living AT a candidate path (~/.npm-global/bin) must survive.
    home = tmp_path / "home"
    link = _plant_npm_tree(home / ".npm-global", version="2.1.201")
    res = _run(tmp_path, "cc_shadow_scan", canonical=link)
    assert res.returncode == 0, res.stderr
    assert link.exists()
    assert "removing shadow copy" not in res.stdout


def test_system_path_shadow_skipped_without_sudo(tmp_path):
    # PATH has no sudo → a /usr/* candidate must be warned about, never rm'd.
    # (Also the safety net that keeps THIS test suite from touching the real
    # /usr/local/bin/claude on dev machines.) Snapshot BEFORE the scan so a
    # gate regression that deletes a real system copy fails loudly on dev
    # boxes; on CI (no /usr/*/claude) only the exit-0 half is meaningful.
    before = {p: (Path(p).exists() or Path(p).is_symlink())
              for p in ("/usr/local/bin/claude", "/usr/bin/claude")}
    canon = _canonical(tmp_path)
    res = _run(tmp_path, "cc_shadow_scan", canonical=canon)
    assert res.returncode == 0, res.stderr
    for p, existed in before.items():
        if existed:
            assert Path(p).exists() or Path(p).is_symlink(), \
                f"scan removed real system copy {p} despite no-sudo PATH"
            assert "needs sudo to remove — skipped" in res.stderr


def test_stale_path_first_copy_is_not_crowned_canonical(tmp_path):
    # THE inverted-incident case: a STALE copy wins the invoking shell's PATH
    # while the pinned copy sits in a probe dir. Canonical selection is
    # pin-verified, so the stale PATH-first copy must be removed and the
    # pinned copy kept — never the other way around.
    home = tmp_path / "home"
    stale = _plant_npm_tree(home / ".nvm" / "versions" / "node" / "v24.15.0",
                            version="2.1.150")
    pinned = _canonical(tmp_path)  # prints the pin
    res = _run(
        tmp_path, "cc_shadow_scan",
        canonical=stale,  # stale dir goes FIRST on PATH
        extra_env={"CC_PROBE_DIRS": str(pinned.parent)},
    )
    assert res.returncode == 0, res.stderr
    assert pinned.exists()
    assert not stale.exists()
    assert "removing shadow copy" in res.stdout


def test_no_pinned_copy_refuses_all_removal(tmp_path):
    # Fail-safe: ONLY stale copies exist (nothing at the pin) → refuse to
    # remove anything at all.
    home = tmp_path / "home"
    stale = _plant_npm_tree(home / ".nvm" / "versions" / "node" / "v24.15.0",
                            version="2.1.150")
    res = _run(tmp_path, "cc_shadow_scan", canonical=stale)
    assert res.returncode == 0, res.stderr
    assert stale.exists()
    assert "REFUSING to remove anything" in res.stderr


def test_native_canonical_is_protected(tmp_path):
    # Canonical IS a native install (~/.local/bin/claude → versions blob at
    # the pin): the blob sweep must keep the versions dir AND the link.
    home = tmp_path / "home"
    blob = _write_exec(home / ".local" / "share" / "claude" / "versions" / "2.1.201",
                       '#!/usr/bin/env bash\necho "2.1.201 (Claude Code)"\n')
    link = home / ".local" / "bin" / "claude"
    link.parent.mkdir(parents=True)
    link.symlink_to(blob)
    res = _run(tmp_path, "cc_shadow_scan", canonical=link)
    assert res.returncode == 0, res.stderr
    assert blob.exists() and link.exists()
    assert "keeping" in res.stderr


def test_stale_link_into_canonical_package_loses_link_only(tmp_path):
    # A leftover second symlink pointing at a DIFFERENT entry file of the
    # CANONICAL package must lose only the link — never the package.
    home = tmp_path / "home"
    canon_link = _plant_npm_tree(home / ".npm-global", version="2.1.201")
    pkg = home / ".npm-global" / "lib" / "node_modules" / "@anthropic-ai" / "claude-code"
    (pkg / "cli.js").write_text("// old entry\n", encoding="utf-8")
    stale_link = home / ".local" / "bin" / "claude"
    stale_link.parent.mkdir(parents=True)
    stale_link.symlink_to(pkg / "cli.js")
    res = _run(tmp_path, "cc_shadow_scan", canonical=canon_link)
    assert res.returncode == 0, res.stderr
    assert not stale_link.exists() and not stale_link.is_symlink()
    assert canon_link.exists() and pkg.is_dir()
    assert "CANONICAL package — removing the link only" in res.stdout


def test_migrate_installer_removes_launcher_and_package(tmp_path):
    canon = _canonical(tmp_path)
    home = tmp_path / "home"
    launcher = _write_exec(home / ".claude" / "local" / "claude",
                           "#!/usr/bin/env bash\n")
    pkg = home / ".claude" / "local" / "node_modules" / "@anthropic-ai" / "claude-code"
    pkg.mkdir(parents=True)
    (pkg / "package.json").write_text("{}", encoding="utf-8")
    res = _run(tmp_path, "cc_shadow_scan", canonical=canon)
    assert res.returncode == 0, res.stderr
    assert not launcher.exists()
    assert not pkg.exists()


def test_opt_out_env(tmp_path):
    canon = _canonical(tmp_path)
    home = tmp_path / "home"
    link = _plant_npm_tree(home / ".nvm" / "versions" / "node" / "v24.15.0")
    res = _run(tmp_path, "cc_shadow_scan", extra_env={"CC_SHADOW_SCAN": "0"},
               canonical=canon)
    assert res.returncode == 0, res.stderr
    assert link.exists()
    assert "disabled" in res.stdout


def test_alias_warning(tmp_path):
    canon = _canonical(tmp_path)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / ".bashrc").write_text("alias claude='~/somewhere/claude'\n", encoding="utf-8")
    res = _run(tmp_path, "cc_shadow_scan", canonical=canon)
    assert res.returncode == 0, res.stderr
    assert "alias" in res.stderr and ".bashrc" in res.stderr


def test_no_claude_anywhere_is_a_noop(tmp_path):
    res = _run(tmp_path, "cc_shadow_scan")  # no claude at all
    assert res.returncode == 0, res.stderr
    assert "REFUSING to remove anything" in res.stderr


# ── gateway mirror parity ─────────────────────────────────────────────────


def test_gateway_compact_scan_covers_same_user_surfaces():
    """guardian-gateway.sh's hermetic update-cc sweep must keep covering the
    same USER-dir shadow surfaces as cc_shadow_scan (it deliberately excludes
    system paths — the gateway never sudo-removes). A surface added to the lib
    but not the gateway would silently re-open host-side drift."""
    gateway = (_REPO_ROOT / "scripts" / "guardian-gateway.sh").read_text(encoding="utf-8")
    for surface in (
        '.nvm/versions/node/*/bin/claude',
        '.local/bin/claude',
        '.claude/local/claude',
        '.npm-global/bin/claude',
        '.local/share/claude/versions',
    ):
        assert surface in gateway, f"gateway update-cc sweep missing surface: {surface}"


# ── cc_ensure_local PATH-blind probe ──────────────────────────────────────


def test_ensure_local_finds_path_blind_install_at_pin(tmp_path):
    # claude NOT on PATH, but present at a probe dir AND at the pin →
    # "already at pin", no npm install attempted (the fake npm logs any call).
    blind = _write_exec(tmp_path / "blind" / "claude",
                        '#!/usr/bin/env bash\necho "2.1.201 (Claude Code)"\n')
    fake_npm = _write_exec(tmp_path / "npmbin" / "npm",
                           f'#!/usr/bin/env bash\necho "$*" >> "{tmp_path}/npm.log"\n')
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    minbin = _minimal_bin(tmp_path)
    harness = tmp_path / "harness.sh"
    harness.write_text(f'#!/usr/bin/env bash\n. "{_LIB}"\ncc_ensure_local\n',
                       encoding="utf-8")
    env = {"PATH": f"{fake_npm.parent}:{minbin}", "HOME": str(home),
           "CC_PROBE_DIRS": str(blind.parent), "CC_VERSION": "2.1.201"}
    res = subprocess.run(["bash", str(harness)], env=env,
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    assert "already at pin" in res.stdout
    assert "NOT on this shell's PATH" in res.stderr
    log = tmp_path / "npm.log"
    assert not log.exists() or "install" not in log.read_text()


def test_ensure_local_absent_everywhere_still_installs(tmp_path):
    # No claude on PATH or probe dirs → falls through to the install branch
    # (fake npm records the call and pretends success; verify then fails
    # non-fatally, which is fine — the assertion is the install ATTEMPT).
    # Prefix must be USER-writable — a system prefix would route through the
    # sudo guard (and this PATH has no sudo, by design).
    fake_npm = _write_exec(tmp_path / "npmbin" / "npm",
                           "#!/usr/bin/env bash\n"
                           f'echo "$*" >> "{tmp_path}/npm.log"\n'
                           f'if [ "$1" = "config" ]; then echo "{tmp_path}/home/.npm-global"; fi\n')
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    minbin = _minimal_bin(tmp_path)
    harness = tmp_path / "harness.sh"
    harness.write_text(f'#!/usr/bin/env bash\n. "{_LIB}"\ncc_ensure_local\n',
                       encoding="utf-8")
    env = {"PATH": f"{fake_npm.parent}:{minbin}", "HOME": str(home),
           "CC_PROBE_DIRS": str(tmp_path / "nowhere"), "CC_VERSION": "2.1.201"}
    res = subprocess.run(["bash", str(harness)], env=env,
                         capture_output=True, text=True, timeout=30)
    assert "not installed — installing pinned" in res.stdout
    log = (tmp_path / "npm.log").read_text()
    assert "install -g" in log and "@anthropic-ai/claude-code@2.1.201" in log
