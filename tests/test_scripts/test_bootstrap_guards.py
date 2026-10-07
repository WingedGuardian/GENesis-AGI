"""Bootstrap robustness guards (deploy-audit P8a — B1/B3/B5/B6/B7/B8/B9/S3).

These harden scripts/bootstrap.sh and scripts/setup-local-config.sh against
premature completion signalling, skipped reloads, cwd-dependent MCP scope,
unvalidated timezones, newline-less secret appends, hung dependency installs,
pipe-to-shell truncation, and dead config. bootstrap.sh runs the full install
(services, VNC, memory restore) and cannot be exercised in CI, so most locks are
extraction assertions on the shipped text; B7's subtle newline logic gets a
functional bash test.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap.sh"
UPDATE = REPO_ROOT / "scripts" / "update.sh"
INSTALL = REPO_ROOT / "scripts" / "install.sh"
CBM_INSTALLER = REPO_ROOT / "scripts" / "lib" / "cbm_installer.sh"
CBM_DISABLE_LIB = REPO_ROOT / "scripts" / "lib" / "cbm_disable_file.sh"
SETUP_LOCAL = REPO_ROOT / "scripts" / "setup-local-config.sh"


def _code(path: Path) -> str:
    """Script text minus comment-only lines (so assertions match real code)."""
    return "\n".join(ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#"))


def _recovery_env(**extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env.update(extra)
    return env


def _recovery_git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), *args],
        text=True,
        env=_recovery_env(),
        timeout=30,
    ).strip()


def _recovery_repo(tmp_path: Path) -> tuple[Path, str, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _recovery_git(root, "init", "-q", "-b", "main")
    _recovery_git(root, "config", "user.name", "recovery-test")
    _recovery_git(root, "config", "user.email", "recovery-test@example.test")
    files = {
        "changed.txt": "base changed\n",
        "untouched.txt": "base untouched\n",
        "local.txt": "base local\n",
    }
    for name, content in files.items():
        (root / name).write_text(content)
    _recovery_git(root, "add", "--all")
    _recovery_git(root, "commit", "-qm", "common ancestor")
    common = _recovery_git(root, "rev-parse", "HEAD")
    rollback_file = root / "local.txt"
    rollback_file.write_text("rollback branch state\n")
    _recovery_git(root, "add", "--", "local.txt")
    _recovery_git(root, "commit", "-qm", "pre-update checkout")
    rollback = _recovery_git(root, "rev-parse", "HEAD")
    _recovery_git(root, "tag", "rollback", rollback)
    return root, common, rollback


def _recovery_commit_file(root: Path, name: str, content: str, message: str) -> str:
    (root / name).parent.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(content)
    _recovery_git(root, "add", "--", name)
    _recovery_git(root, "commit", "-qm", message)
    return _recovery_git(root, "rev-parse", "HEAD")


def _crash_recovery_source() -> str:
    text = BOOTSTRAP.read_text()
    begin = text.index("# BEGIN crash-recovery")
    end = text.index("# END crash-recovery", begin) + len("# END crash-recovery")
    return text[begin:end]


def _write_recovery_state(home: Path, **state) -> Path:
    raw = state.pop("raw", None)
    state = {
        "phase": "merging",
        "rollback_tag": "rollback",
        "original_branch": "main",
        "own_head": "",
        "deploy_head": "",
        "pid": 2_147_483_647,
        **state,
    }
    if state["original_branch"] is None:
        state.pop("original_branch")
    state_path = home / ".genesis" / "update_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(raw if raw is not None else json.dumps(state))
    return state_path


def _run_crash_recovery(
    root: Path,
    home: Path,
    *,
    backup_root: Path | None = None,
) -> subprocess.CompletedProcess:
    script = (
        '#!/usr/bin/env bash\nset -euo pipefail\nSCRIPT_DIR="${RECOVERY_SCRIPTS:?}"\n'
        'GENESIS_ROOT="${RECOVERY_ROOT:?}"\nUPDATE_STATE="$HOME/.genesis/update_state.json"\n'
        '. "$SCRIPT_DIR/lib/deploy_marker.sh"\n. "$SCRIPT_DIR/lib/deploy_checkout.sh"\n'
        '. "$SCRIPT_DIR/lib/deploy_recovery.sh"\nEPHEMERAL_BACKUP_ROOT="${TEST_BACKUP_ROOT:?}"\n'
        + _crash_recovery_source()
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env=_recovery_env(
            HOME=str(home),
            RECOVERY_SCRIPTS=str(REPO_ROOT / "scripts"),
            RECOVERY_ROOT=str(root),
            TEST_BACKUP_ROOT=str(
                backup_root or home / ".genesis" / "premerge-backups" / "test-run"
            ),
        ),
    )


def _recover(tmp_path: Path, root: Path, *, backup_root: Path | None = None, **state):
    home = tmp_path / "home"
    state_path = _write_recovery_state(home, **state)
    return _run_crash_recovery(root, home, backup_root=backup_root), state_path


# ── B1: setup-complete marker written only at the END ────────────────


def test_b1_setup_complete_marker_written_at_end():
    text = BOOTSTRAP.read_text()
    marker = 'touch "$HOME/.genesis/setup-complete"'
    assert text.count(marker) == 1, "the marker must be written exactly once"
    touch_at = text.index(marker)
    init_at = text.index("Initializing runtime state")
    complete_at = text.index("=== Bootstrap complete ===")
    # No longer written in the early runtime-state block …
    assert touch_at > init_at, "marker must not be written in the early runtime-state block"
    # … it sits right before the completion banner (so a crashed bootstrap leaves
    # the box looking un-onboarded — the marker gates the onboarding prompt).
    assert 0 < complete_at - touch_at < 300, "marker must be written immediately before completion"


# ── B3: daemon-reload runs unconditionally before the enable loop ────


def test_b3_daemon_reload_not_gated_on_services_updated():
    text = BOOTSTRAP.read_text()
    echo = 'echo "  systemd daemon reloaded (units changed)"'
    echo_at = text.index(echo)
    window = text[max(0, echo_at - 300) : echo_at]
    # The reload itself is unconditional now …
    assert "systemctl --user daemon-reload 2>/dev/null || true" in window
    # … and precedes the SERVICES_UPDATED gate (which now guards only the echo).
    assert 'if [[ "$SERVICES_UPDATED" = "1" ]]; then' in window
    assert window.index("daemon-reload") < window.index('if [[ "$SERVICES_UPDATED"')


# ── B5: serena project-scope register runs from the repo root ────────


def test_b5_serena_register_runs_from_repo_root():
    code = _code(BOOTSTRAP)
    # `-s project` is cwd-keyed; the register must be wrapped in a cd-subshell so
    # it always writes .mcp.json to the repo, never the caller's cwd.
    assert '( cd "$GENESIS_ROOT" && _register_serena "$GENESIS_ROOT"' in code


# ── B6: timezone validated, but only when enumerable ────────────────


def test_b6_timezone_validated_when_enumerable():
    code = _code(BOOTSTRAP)
    assert "_tz_list=$(timedatectl list-timezones 2>/dev/null || true)" in code
    assert 'grep -qxF "$GENESIS_TIMEZONE"' in code
    # Validation is GUARDED on a non-empty list, so an image that can't enumerate
    # timezones is never force-reset to UTC.
    assert 'if [[ -n "$_tz_list" ]] &&' in code
    assert 'GENESIS_TIMEZONE="UTC"' in code  # the fallback on a genuinely invalid tz


# ── B7: trailing-newline guard before the secrets append ────────────


def test_b7_newline_guard_present():
    code = _code(BOOTSTRAP)
    assert '-n "$(tail -c1 "$SECRETS_FILE")"' in code


def test_b7_newline_guard_functional(tmp_path):
    """A newline is inserted ONLY when the file lacks a trailing one — so the new
    key always lands on its own line and never concatenates onto the last one.
    Runs under `set -euo pipefail` to prove the `[[ … ]] &&` guard is set-e-safe."""
    guard = (
        "set -euo pipefail; "
        '[[ -s "$SECRETS_FILE" && -n "$(tail -c1 "$SECRETS_FILE")" ]] && printf \'\\n\' >> "$SECRETS_FILE"; '
        "printf 'GENESIS_TIMEZONE=UTC\\n' >> \"$SECRETS_FILE\""
    )
    # No trailing newline → guard inserts one.
    f1 = tmp_path / "no_nl.env"
    f1.write_text("EXISTING=1")
    subprocess.run(["bash", "-c", guard], env={**os.environ, "SECRETS_FILE": str(f1)}, check=True)
    assert f1.read_text() == "EXISTING=1\nGENESIS_TIMEZONE=UTC\n"
    # Already has a trailing newline → no extra blank line.
    f2 = tmp_path / "with_nl.env"
    f2.write_text("EXISTING=1\n")
    subprocess.run(["bash", "-c", guard], env={**os.environ, "SECRETS_FILE": str(f2)}, check=True)
    assert f2.read_text() == "EXISTING=1\nGENESIS_TIMEZONE=UTC\n"


# ── B8: dependency installs are timeout-bounded + retried ───────────


def test_b8_skillspector_install_is_bounded():
    code = _code(BOOTSTRAP)
    assert "timeout 300 git clone --depth 1 https://github.com/NVIDIA/SkillSpector.git" in code
    assert "_ss_clone || { sleep 2; _ss_clone; }" in code  # one retry rides out a blip
    assert 'timeout 300 "$SKILLSPECTOR_DIR/.venv/bin/pip" install' in code
    # A partial clone (mid-transfer failure leaves a stub .git) must be cleared
    # before each attempt and must not wedge future runs: rm -rf before cloning,
    # and gate on a completion marker (pyproject.toml/setup.py), not bare .git.
    assert 'rm -rf "$SKILLSPECTOR_DIR"; timeout 300 git clone' in code
    assert '! -f "$SKILLSPECTOR_DIR/pyproject.toml"' in code
    assert '"$SKILLSPECTOR_DIR/.git"' not in code  # the wedge-prone bare-.git gate is gone


# ── B9: remote installers download-to-temp, never pipe to a shell ───


def test_b9_no_pipe_to_shell():
    code = _code(BOOTSTRAP)
    # The truncation-prone pipe-to-shell forms are gone …
    assert "install.sh | bash" not in code
    assert "uv/install.sh | sh" not in code
    # … replaced by download-to-file then execute-from-file (full download first).
    assert 'curl -LsSf https://astral.sh/uv/install.sh -o "$_uv_installer"' in code
    assert 'sh "$_uv_installer"' in code
    # The codebase-memory installer moved to scripts/lib/cbm_installer.sh, shared
    # with install.sh; bootstrap must reach it through that one site rather than
    # growing a second copy of the fetch.
    assert "raw.githubusercontent.com/DeusData/codebase-memory-mcp" not in code
    assert '. "$SCRIPT_DIR/lib/cbm_installer.sh"' in code


_CBM_PIN_RE = re.compile(r'GENESIS_CBM_INSTALLER_COMMIT="(?P<commit>[0-9a-f]{40})"')
_CBM_DIGEST_RE = re.compile(r'GENESIS_CBM_INSTALLER_SHA256="(?P<digest>[0-9a-f]{64})"')


def test_b9_cbm_installer_is_pinned_and_verified_from_one_site():
    """`main` is mutable third-party code, so the installer is pinned to a
    reviewed commit and carries a repository-owned digest.

    The pin and the digest have to move together, and when they lived inline in
    two scripts nothing bound them: bumping the commit in both while leaving the
    digest stale in both passed every test and turned the install into a
    permanent no-op behind a "non-critical" warning. One site removes the
    question rather than policing it.

    This is the CHEAP half — presence, a pinned URL, and stderr not discarded.
    Whether the digest actually GATES execution is control flow, which no
    character offset can see, so it is settled by running the thing:
    ``test_b9_cbm_installer_is_not_executed_unverified``.
    """
    code = _code(CBM_INSTALLER)
    assert _CBM_PIN_RE.search(code), "cbm installer is not pinned to a commit"
    assert _CBM_DIGEST_RE.search(code), "cbm installer has no committed sha256"
    assert (
        "raw.githubusercontent.com/DeusData/codebase-memory-mcp/${GENESIS_CBM_INSTALLER_COMMIT}"
        in code
    ), "the fetch must build its URL from the pin, so the two cannot disagree"
    # `-fsSL` carries `-S`, so curl explains its own failure — unless the fetch
    # discards stderr, which is what turned a mistyped-but-40-hex commit (a 404,
    # forever) into the same "download failed" line as a transient blip.
    lines = code.splitlines()
    first = next(i for i, ln in enumerate(lines) if "curl -fsSL" in ln)
    last = next(i for i, ln in enumerate(lines) if i >= first and '-o "$installer"' in ln)
    fetch = "\n".join(lines[first : last + 1])
    assert "2>/dev/null" not in fetch, f"the fetch must not discard curl's error:\n{fetch}"
    exec_line = next(ln for ln in code.splitlines() if 'bash "$installer"' in ln)
    # Upstream reports a checksum mismatch, an unexpected archive member and a
    # non-running binary on STDERR. Discarding it renders a release-integrity
    # failure and "not available today" identically — which is how a rejected
    # argument stayed invisible until the installer was read.
    assert "2>/dev/null" not in exec_line, (
        "the installer's stderr carries its integrity errors; do not discard it — "
        f"{exec_line.strip()!r}"
    )
    # Both install paths reach the tool through this one site.
    for script in (BOOTSTRAP, INSTALL):
        assert '. "$SCRIPT_DIR/lib/cbm_installer.sh"' in _code(script), (
            f"{script.name}: does not source the shared cbm installer"
        )


def _run_cbm_install(tmp_path: Path, payload: str, digest: str | None, label: str):
    """Call the SHIPPED `genesis_cbm_install` with `curl` and `bash` stubbed.

    `curl` writes ``payload`` to the requested path instead of fetching, and
    `bash` records the argv it was handed instead of installing anything. The
    real `sha256sum` does the checking. Returns ``(rc, argv)`` where argv is
    None when the installer was never executed.
    """
    work = tmp_path / label
    (work / "stub").mkdir(parents=True)
    lib = work / "cbm_installer.sh"
    body = CBM_INSTALLER.read_text()
    if digest is not None:  # re-point the committed digest at this payload
        body = _CBM_DIGEST_RE.sub(f'GENESIS_CBM_INSTALLER_SHA256="{digest}"', body)
    lib.write_text(body)
    # The installer sources its kill-switch resolver as a sibling file; the
    # sandboxed copy needs it beside it or every call refuses as unresolvable.
    (work / "cbm_disable_file.sh").write_text(CBM_DISABLE_LIB.read_text())
    payload_file = work / "payload"
    payload_file.write_text(payload)
    argv_file = work / "argv"
    (work / "stub" / "curl").write_text(
        "#!/bin/sh\nout=''\n"
        'while [ $# -gt 0 ]; do\n  if [ "$1" = "-o" ]; then out="$2"; fi\n  shift\ndone\n'
        '[ -n "$out" ] || exit 1\ncat "$PAYLOAD_FILE" > "$out"\n'
    )
    (work / "stub" / "bash").write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$ARGV_FILE"\nexit 0\n')
    for stub in (work / "stub").iterdir():
        stub.chmod(0o755)
    # `set -euo pipefail` is what both real callers run under.
    proc = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f'set -euo pipefail; . "{lib}"; rc=0; genesis_cbm_install || rc=$?; echo "rc=$rc"',
        ],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{work / 'stub'}:/usr/bin:/bin",
            "HOME": str(work),
            "TMPDIR": str(work),  # never the session's own temp
            "PAYLOAD_FILE": str(payload_file),
            "ARGV_FILE": str(argv_file),
        },
    )
    assert "rc=" in proc.stdout, proc.stdout + proc.stderr
    rc = int(proc.stdout.strip().rsplit("rc=", 1)[1])
    argv = argv_file.read_text().split() if argv_file.exists() else None
    leftovers = [p.name for p in work.iterdir() if p.name.startswith("tmp")]
    assert not leftovers, f"the downloaded installer was left behind: {leftovers}"
    return rc, argv


def test_b9_cbm_installer_is_not_executed_unverified(tmp_path):
    """A digest mismatch must stop the installer REACHING bash.

    No textual guard can establish this. A maintainer wanting a distinct warning
    for a mismatch writes the check as its own statement with `|| echo …` above
    an unchanged `bash "$installer"`: every ordering assertion still holds,
    `bash -n` is clean, and an unverified third-party script runs. So the real
    function is called here with `curl` and `bash` stubbed and the real
    sha256sum deciding — a question about control flow, not character offsets.

    The matching case is the control that moves: same call, same payload, with
    the committed digest re-pointed at that payload. The installer must then run
    with EXACTLY the argv the pinned upstream parser accepts. Containment is the
    wrong relation for that — appending a flag leaves any substring intact, and
    `--ui` exits 2 before the installer does any work, which is what made this a
    silent no-op.
    """
    payload = "#!/bin/sh\n# not the reviewed installer\n"
    rc, argv = _run_cbm_install(tmp_path, payload, digest=None, label="mismatch")
    assert argv is None, "an installer failing its digest check still reached bash"
    # A DISTINCT code, not the download failure's. Nothing offline can bind the
    # committed digest to the bytes upstream actually serves, so a bump that
    # moves the commit and forgets the digest stays possible — but a commit SHA
    # is content-addressed, so a mismatch can only mean the repository's own two
    # constants disagree. Folding that into the same "non-critical" warning as a
    # network blip is what would make a wrong digest a permanent silent no-op on
    # every machine; rc 3 is how it announces itself instead.
    assert rc == 3, f"a digest mismatch must be distinguishable from a network failure, got rc {rc}"

    matching = hashlib.sha256(payload.encode()).hexdigest()
    rc, argv = _run_cbm_install(tmp_path, payload, digest=matching, label="match")
    assert argv is not None, "a VERIFIED installer was not executed"
    assert rc == 0, f"a successful install must report rc 0, got {rc}"
    assert argv[1:] == ["--skip-config"], (
        "the pinned installer accepts only --dir/--clients/--skip-config/--help; "
        f"anything else hits its `-*)` case and exits 2 before doing any work. Got: {argv[1:]}"
    )


# ── S3: dead REPO_EXAMPLE var + phantom migration comment removed ───


def test_s3_dead_repo_example_removed():
    cfg = SETUP_LOCAL.read_text()
    assert "REPO_EXAMPLE" not in cfg
    assert "genesis.yaml.example" not in cfg
    assert "Migrate from repo YAML" not in cfg  # the misleading comment is corrected


# ── OfficeCLI: provisioned binary for deliverable-builder (external, optional) ──
# The render backend for high-fidelity .xlsx/.pptx. Security-critical: a pinned
# version + a COMMITTED literal SHA256 (not a fetched SHA256SUMS, which is TOFU).


def test_officecli_pins_committed_literal_sha256_both_arches():
    """BLOCKER guard: literal 64-hex SHA256 pins for x64 AND arm64 live in the
    script (repo-reviewed), so verification can't be defeated by a swapped
    release. A fetched SHA256SUMS would be trust-on-first-use."""
    code = _code(BOOTSTRAP)
    assert re.search(r'OCLI_SHA256_x64="[0-9a-f]{64}"', code)
    assert re.search(r'OCLI_SHA256_arm64="[0-9a-f]{64}"', code)
    # ...and it must actually be checked, refusing (deleting) on mismatch.
    assert "sha256sum -c -" in code
    assert 'rm -f "$OCLI_BIN"' in code
    assert "checksum mismatch" in code.lower()


def test_officecli_arch_detection_x64_arm64_and_skip():
    code = _code(BOOTSTRAP)
    assert 'x86_64)        OCLI_ARCH="x64"' in code or 'x86_64) OCLI_ARCH="x64"' in code
    assert 'aarch64|arm64) OCLI_ARCH="arm64"' in code
    # unsupported arch → empty → skip branch (never fatal)
    assert 'OCLI_ARCH=""' in code
    assert "unsupported arch" in code


def test_officecli_guard_is_checksum_based_not_presence_or_version_only():
    """The idempotency guard trusts the on-disk binary ONLY if its hash matches
    the committed pin — which re-verifies an existing binary (anti-tamper) AND
    re-downloads on a pin bump (the new hash won't match the old binary). NOT a
    presence-only `-x` skip and NOT a spoofable `--version` check."""
    code = _code(BOOTSTRAP)
    assert "_ocli_verify()" in code
    assert "if _ocli_verify; then" in code  # skip only when the hash matches
    # anti-tamper: the guard re-hashes; it does NOT trust a bare --version string.
    assert '"$OCLI_BIN" --version' not in code
    # verify runs both on the existing binary AND the freshly-downloaded one.
    assert code.count("_ocli_verify") >= 3


def test_officecli_curl_primary_gh_fallback():
    """curl (no auth needed for a public asset) must be tried BEFORE gh, so a
    fresh install with no gh login still provisions."""
    code = _code(BOOTSTRAP)
    curl_i = code.find('curl -fSL "$OCLI_URL"')
    gh_i = code.find("gh release download")
    assert curl_i != -1 and gh_i != -1
    assert curl_i < gh_i  # curl primary, gh fallback


def test_officecli_nonfatal_and_bounded():
    code = _code(BOOTSTRAP)
    assert "WARNING: OfficeCLI download failed" in code  # non-fatal echo, never exit 1
    assert "timeout 300 curl" in code  # bounded fetch
    assert "sleep 2" in code  # one retry


def test_officecli_arch_and_checksum_logic_functional(tmp_path):
    """Functional: arch mapping + checksum accept/refuse behave correctly."""
    script = r"""
    for m in x86_64 aarch64 arm64 riscv64; do
      case "$m" in x86_64) A="x64";; aarch64|arm64) A="arm64";; *) A="";; esac
      echo "$m=$A"
    done
    f="$1"; good=$(sha256sum "$f" | awk '{print $1}')
    echo "good  $f" | sed "s/^good/$good/" | sha256sum -c - >/dev/null 2>&1 && echo "good=accept" || echo "good=reject"
    echo "0000000000000000000000000000000000000000000000000000000000000000  $f" | sha256sum -c - >/dev/null 2>&1 && echo "bad=accept" || echo "bad=reject"
    """
    f = tmp_path / "bin"
    f.write_text("payload")
    out = subprocess.run(
        ["bash", "-c", script, "bash", str(f)], capture_output=True, text=True, check=True
    ).stdout
    assert "x86_64=x64" in out
    assert "aarch64=arm64" in out and "arm64=arm64" in out
    assert "riscv64=" in out  # unsupported → empty
    assert "good=accept" in out
    assert "bad=reject" in out  # checksum mismatch refused


# ── install_pkg: errexit must not swallow the failure diagnostics ────


def _install_pkg_source() -> str:
    """The real install_pkg body, extracted from the shipped script."""
    lines = BOOTSTRAP.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("install_pkg() {"))
    end = next(i for i, ln in enumerate(lines[start:], start) if ln == "}")
    return "\n".join(lines[start : end + 1])


def _run_install_pkg(tmp_path, sudo_body: str, pkg_mgr: str = "apt"):
    """Call install_pkg as a BARE statement under `set -euo pipefail`.

    Bare is the whole point: every current call site uses `|| …`, which disables
    errexit inside the function and hides the bug. It must also run in its OWN
    process — wrapping the call in `|| echo` to capture output would itself
    suppress errexit and silently invalidate the test.
    """
    script = tmp_path / "harness.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"PKG_MGR={pkg_mgr}\n"
        f"sudo() {{ {sudo_body} }}\n"
        f"{_install_pkg_source()}\n"
        "install_pkg somepkg\n"
        'echo "NEXT_LINE_REACHED"\n'
    )
    return subprocess.run(["bash", str(script)], capture_output=True, text=True)


def test_install_pkg_reports_the_real_error_on_failure(tmp_path):
    """The diagnostic block exists FOR the failure path and must run on it."""
    r = _run_install_pkg(tmp_path, 'printf "E: Unable to locate package\\n"; return 100;')
    assert "install failed (exit 100)" in r.stdout, f"diagnostic lost: {r.stdout!r}"
    assert "E: Unable to locate package" in r.stdout
    assert r.returncode == 100, "a failed install must still propagate non-zero"


def test_install_pkg_reports_when_the_installer_printed_nothing(tmp_path):
    r = _run_install_pkg(tmp_path, "return 7;")
    assert "install failed (exit 7): no output" in r.stdout, r.stdout
    assert r.returncode == 7


def test_install_pkg_survives_blank_only_installer_output(tmp_path):
    """`set -o pipefail` is on, so `grep -v '^\\s*$'` finding nothing exits 1 and
    would abort the diagnostic assignment itself — losing the `no output`
    fallback written for exactly this case."""
    r = _run_install_pkg(tmp_path, 'printf "\\n   \\n"; return 100;')
    assert "install failed (exit 100): no output" in r.stdout, r.stdout
    assert r.returncode == 100


def test_install_pkg_success_path_is_unchanged(tmp_path):
    r = _run_install_pkg(tmp_path, 'printf "done\\n"; return 0;')
    assert "NEXT_LINE_REACHED" in r.stdout
    assert "install failed" not in r.stdout
    assert r.returncode == 0


def test_install_pkg_captures_status_without_a_bare_assignment():
    """Structural guard on the shape itself. Under `set -e`, `out=$(cmd)` inherits
    the substitution's exit status and fires errexit, so a following `rc=$?` is
    unreachable on failure. shellcheck has no check for this at any severity
    (verified against -o all), which is why it is pinned here."""
    body = _install_pkg_source()
    # Every capture in the function guards its status. Matched on the guard, not
    # on exact flags, so an unrelated apt-flag change does not break this test
    # under a misleading name.
    # Comment lines excluded: the explanatory comment above the fix quotes the
    # very shape being asserted on, and would otherwise be counted as a capture.
    captures = [
        ln for ln in body.splitlines() if "output=$(" in ln and not ln.lstrip().startswith("#")
    ]
    assert len(captures) == 2, f"expected both package-manager branches, got {captures}"
    for ln in captures:
        assert ln.rstrip().endswith("|| rc=$?"), f"unguarded capture: {ln.strip()}"
    # A bare `rc=$?` on its own line is the unreachable form — matched at ANY
    # indentation, so re-indenting the function cannot silently disarm this.
    bare = [ln for ln in body.splitlines() if ln.strip() == "rc=$?"]
    assert not bare, "bare `rc=$?` after the capture is unreachable on failure"


def test_install_pkg_dnf_branch_reports_failures_too(tmp_path):
    """The non-apt branch is a separate capture and needs its own guard —
    a string assertion alone would not catch the two branches diverging."""
    r = _run_install_pkg(
        tmp_path, 'printf "Error: No match for argument\\n"; return 1;', pkg_mgr="dnf"
    )
    assert "install failed (exit 1)" in r.stdout, r.stdout
    assert "No match for argument" in r.stdout
    assert r.returncode == 1


def _direct_run(tmp_path: Path, label: str, env: dict[str, str]):
    """Run the shared installer as a script, with `curl` recording its calls."""
    work = tmp_path / label
    (work / "stub").mkdir(parents=True)
    called = work / "curl-called"
    (work / "stub" / "curl").write_text(f'#!/bin/sh\n: > "{called}"\nexit 7\n')
    (work / "stub" / "curl").chmod(0o755)
    proc = subprocess.run(
        ["/bin/bash", str(CBM_INSTALLER)],
        capture_output=True,
        text=True,
        env={"PATH": f"{work / 'stub'}:/usr/bin:/bin", "TMPDIR": str(work), **env},
    )
    return proc, called.exists()


def test_b9_direct_run_honours_the_kill_switch(tmp_path):
    """The launcher sends operators here, so this path must refuse on its own.

    Asserting the kill-switch string appears in the file is not a test of it:
    that survives inverting the check to `[ ! -e ]` — installing exactly when
    the machine says not to — and survives moving the check below the install.
    So the script is run.

    An unresolvable path fails CLOSED. With HOME unset the default used to
    collapse to `/.genesis/…` — absolute, but not this machine's switch — so the
    check came back false and the install proceeded. The launcher edited in the
    same change refuses on that exact condition; this now matches it.
    """
    home = tmp_path / "home" / ".genesis"
    home.mkdir(parents=True)
    (home / "codebase-memory-mcp.disabled").touch()

    proc, fetched = _direct_run(tmp_path, "switched-on", {"HOME": str(tmp_path / "home")})
    assert proc.returncode == 4, proc.stderr
    assert not fetched, "the kill switch is set and the installer was fetched anyway"
    assert "kill switch is active" in proc.stderr

    # Control that moves: same script, no switch file — it must get as far as
    # the download (stubbed to fail, hence rc 1).
    clear = tmp_path / "clear"
    (clear / ".genesis").mkdir(parents=True)
    proc, fetched = _direct_run(tmp_path, "switched-off", {"HOME": str(clear)})
    assert proc.returncode == 1, proc.stderr
    assert fetched, "with no kill switch the installer must actually be fetched"

    # HOME unset, and a relative override: both unresolvable, both refuse.
    proc, fetched = _direct_run(tmp_path, "no-home", {})
    assert proc.returncode == 4 and not fetched, proc.stderr
    proc, fetched = _direct_run(
        tmp_path, "relative", {"HOME": str(clear), "CODEBASE_MEMORY_MCP_DISABLE_FILE": "rel/path"}
    )
    assert proc.returncode == 4 and not fetched, proc.stderr


def _cbm_outcome_block(script: Path) -> str:
    """The caller's whole outcome path: the source, the CALL, and the `case`.

    Anchored on the source line rather than on `case`, because the wire between
    them — `genesis_cbm_install || _cbm_rc=$?` — is the part most easily broken.
    A slice that starts at `case` and injects `_cbm_rc` itself leaves that
    assignment outside everything it executes.
    """
    lines = script.read_text().splitlines()
    # Anchored on the kill-switch guard's `if [ -z "$_cbm_disable" ]` — the
    # if/elif/else encloses the source line, the call wire and the `case`, so
    # slicing from any inner line leaves an orphaned `fi`; `_cbm_disable` is
    # pre-set by the caller to a nonexistent path so the else branch runs.
    start = next(i for i, ln in enumerate(lines) if ln.strip() == 'if [ -z "$_cbm_disable" ]; then')
    end = next(i for i, ln in enumerate(lines) if i > start and ln.strip() == "esac")
    end = next(i for i, ln in enumerate(lines) if i > end and ln.strip() == "fi")
    return "\n".join(lines[start : end + 1]) + "\n"


def test_b9_callers_render_a_digest_mismatch_distinctly(tmp_path):
    """rc 3 exists so a wrong pin cannot hide inside a network-failure message.

    That only holds if the CALLERS say something different for it, and if the
    return actually reaches them. Both are exercised together: the real source
    line, the real call and the real `case` are run against a stub library that
    returns the code under test, so the `|| _cbm_rc=$?` wire is inside the
    harness rather than bypassed by injecting `_cbm_rc` directly. MEASURED with
    that wire replaced by `|| true`: install.sh rendered a pin/digest mismatch
    as `+ codebase-memory-mcp installed/upgraded`.
    """
    for script in (BOOTSTRAP, INSTALL):
        block = _cbm_outcome_block(script)
        stub_root = tmp_path / script.stem
        (stub_root / "lib").mkdir(parents=True)
        (stub_root / "lib" / "cbm_installer.sh").write_text(
            'genesis_cbm_install() { return "${FAKE_CBM_RC}"; }\n'
        )
        rendered = {}
        for rc in ("0", "1", "2", "3"):
            proc = subprocess.run(
                [
                    "/bin/bash",
                    "-c",
                    f'set -euo pipefail\nSCRIPT_DIR="{stub_root}"\n'
                    f'_cbm_disable="{stub_root}/no-sentinel-present"\n{block}',
                ],
                capture_output=True,
                text=True,
                env={"PATH": "/usr/bin:/bin", "FAKE_CBM_RC": rc},
            )
            assert proc.returncode == 0, proc.stderr
            rendered[rc] = proc.stdout.strip()
        assert "digest" in rendered["3"].lower(), (
            f"{script.name}: a digest mismatch is not named — {rendered['3']!r}"
        )
        for other in ("0", "1", "2"):
            assert rendered["3"] != rendered[other], (
                f"{script.name}: a wrong pin renders identically to outcome {other} "
                f"— {rendered['3']!r}"
            )


# ── CDPATH: `cd` SEARCHES it, and echoes what it resolves ────────────
#
# With CDPATH exported and a RELATIVE entrypoint, `cd` searches CDPATH for the
# directory, so a `$(cd … && pwd)` capture can (a) collect an extra echoed line
# and (b) resolve into a DIFFERENT checkout entirely. (b) is the dangerous one:
# silencing the echo alone turns a loud failure into a silently wrong root. The
# fix is to clear CDPATH inside the capture — and only running the shipped line
# proves it is still there. Two arms, because the benign one cannot see (b):
#   local — CDPATH=".": the directory is found locally; only the echo can bite.
#   decoy — CDPATH=<other checkout>: a decoy holding the same relative path must
#           NOT win. Without the CDPATH clear, the capture resolves there.

_CD_CAPTURES = [
    # (script, the variable the capture assigns, lines that must run first)
    ("bootstrap.sh", "SCRIPT_DIR", ""),
    ("cc-slot.sh", "_CC_SLOT_DIR", '_CC_SLOT_SCRIPT="${BASH_SOURCE[0]}"'),
]


@pytest.mark.parametrize("hostile", [False, True], ids=["local-cdpath", "decoy-cdpath"])
@pytest.mark.parametrize(("script", "var", "prelude"), _CD_CAPTURES)
def test_root_resolution_survives_cdpath(script, var, prelude, hostile, tmp_path):
    text = (REPO_ROOT / "scripts" / script).read_text()
    # The UNINDENTED assignment: cc-slot.sh has a second copy inside its symlink
    # loop, and this is the one that runs on every invocation. Matched on shape
    # (an unindented `var="$(… cd …)"`) rather than on the current spelling, so a
    # different CDPATH remedy does not read as "capture not found".
    pattern = re.compile(rf'^{var}="\$\(.*\bcd\b.*\)"$')
    line = next(
        (
            ln.strip()
            for ln in text.splitlines()
            if not ln.startswith((" ", "\t")) and pattern.match(ln.strip())
        ),
        None,
    )
    assert line, f"no unconditional {var} capture found in {script}"

    checkout = tmp_path / "checkout"
    entry = checkout / "scripts" / script
    entry.parent.mkdir(parents=True)
    entry.write_text(f'set -u\n{prelude}\n{line}\nprintf %s "${{{var}}}"\n')

    if hostile:
        decoy = tmp_path / "decoy"
        (decoy / "scripts").mkdir(parents=True)
        cdpath = str(decoy)
    else:
        cdpath = "."

    proc = subprocess.run(
        ["bash", f"scripts/{script}"],  # relative, so CDPATH is consulted
        cwd=str(checkout),
        env={"PATH": "/usr/bin:/bin", "CDPATH": cdpath, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    expected = str(entry.parent.resolve())
    assert "\n" not in proc.stdout, (
        f"{script}: the capture holds more than one line — {proc.stdout!r}"
    )
    assert proc.stdout == expected, (
        f"{script}: resolved {proc.stdout!r}, expected {expected!r} (CDPATH={cdpath})"
    )


def _assert_recovery_refused(proc: subprocess.CompletedProcess, state_path: Path) -> None:
    output = proc.stdout + proc.stderr
    assert proc.returncode == 1 and "REFUSE:" in output, output
    guidance = (
        "Current branch/HEAD:",
        "scripts/bootstrap.sh",
        "scripts/update.sh --post-merge",
        "update_state.json",
    )
    assert all(line in output for line in guidance), output
    assert state_path.exists(), "a refusal must retain update state"


def _assert_recovered(
    proc: subprocess.CompletedProcess, state_path: Path, root: Path, rollback: str
) -> None:
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _recovery_git(root, "rev-parse", "HEAD") == rollback
    assert not state_path.exists()


def test_crash_recovery_sources_shared_libraries_after_the_live_guard():
    text = BOOTSTRAP.read_text()
    assert (
        text.index('bootstrap_refuse_if_server_live "$@"')
        < text.index('. "$SCRIPT_DIR/lib/deploy_marker.sh"')
        < text.index('. "$SCRIPT_DIR/lib/deploy_checkout.sh"')
        < text.index('. "$SCRIPT_DIR/lib/deploy_recovery.sh"')
    )
    assert "reset --hard" not in text
    assert "# BEGIN crash-recovery" in text and "# END crash-recovery" in text


@pytest.mark.parametrize(
    ("original_branch", "services_stopped"),
    [
        ('feature/"snow\\man-λ', ["genesis-server", 'odd"name']),
        ("main", []),
    ],
    ids=["escaped-values", "no-stopped-services"],
)
def test_update_state_persists_the_recovery_ownership_inputs(
    tmp_path, original_branch, services_stopped
):
    text = UPDATE.read_text()
    start = text.index("_write_state() {")
    end = text.index("\n}\n\n# Clear this run's deploy state files", start) + 2
    state_path = tmp_path / "home" / ".genesis" / "update_state.json"
    script = (
        'set -euo pipefail; STATE_FILE="$1"; STARTED_AT="2026-01-01T00:00:00Z"; '
        'ROLLBACK_TAG=rollback; OLD_TAG=old; OLD_COMMIT=abc; ORIGINAL_BRANCH="$2"; '
        "UPDATE_OWN_HEAD=1111111111111111111111111111111111111111; "
        "DEPLOY_HEAD=2222222222222222222222222222222222222222\n"
        'WERE_RUNNING=("${@:3}")\n' + text[start:end] + '\n_write_state "merging"\n'
    )
    argv = [
        "bash",
        "-c",
        script,
        "write-state-test",
        str(state_path),
        original_branch,
        *services_stopped,
    ]
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "HOME": str(tmp_path / "home")},
    )
    assert proc.returncode == 0, proc.stderr
    state = json.loads(state_path.read_text())
    assert isinstance(state.pop("pid"), int)
    timestamp = state.pop("timestamp")
    assert isinstance(timestamp, str) and timestamp
    assert state == dict(
        phase="merging",
        rollback_tag="rollback",
        original_branch=original_branch,
        own_head="1" * 40,
        deploy_head="2" * 40,
        old_tag="old",
        old_commit="abc",
        started_at="2026-01-01T00:00:00Z",
        services_stopped=services_stopped,
    )


@pytest.mark.parametrize(
    "case",
    [
        "truncated",
        "control-character",
        "S3",
        "S5",
        "S6",
        "S10",
        "S14",
        "fetching-at-deploy-head",
        "empty-tag",
        "unresolvable-tag",
    ],
)
def test_unsafe_recovery_cases_refuse_without_moving_unowned_code(tmp_path, case):
    root, _, rollback = _recovery_repo(tmp_path)
    expected_head = rollback
    expected_branch = "main"
    state = {}
    preserved = {}

    if case == "truncated":
        state["raw"] = '{"phase":"merging","pid":2147483647,'
    elif case == "control-character":
        state.update(phase="merging\x01", own_head=rollback)
    elif case in {"S3", "S5", "S6", "S10", "S14", "fetching-at-deploy-head"}:
        deploy = _recovery_commit_file(root, "changed.txt", "deployed\n", "deploy")
        expected_head = deploy
        state.update(own_head=rollback, deploy_head=deploy)
        if case == "fetching-at-deploy-head":
            state["phase"] = "fetching"
        if case == "S3":
            (root / "changed.txt").write_text("operator edit\n")
            preserved["changed.txt"] = "operator edit\n"
        elif case == "S5":
            expected_head = _recovery_commit_file(
                root, "followup.txt", "someone else's commit\n", "followup"
            )
        elif case == "S6":
            _recovery_git(root, "branch", "other")
            _recovery_git(root, "checkout", "-q", "other")
            expected_branch = "other"
        elif case == "S10":
            state.update(original_branch=None, own_head="", deploy_head="")
        else:
            _recovery_git(root, "update-index", "--skip-worktree", "changed.txt")
            (root / "changed.txt").write_text("skip-worktree operator edit\n")
            preserved["changed.txt"] = "skip-worktree operator edit\n"
    else:
        state.update(rollback_tag="" if case == "empty-tag" else "missing-tag")
        state["own_head"] = rollback

    local = root / "untouched.txt"
    local.write_text("operator edit\n")
    preserved["untouched.txt"] = "operator edit\n"
    proc, state_path = _recover(tmp_path, root, **state)

    _assert_recovery_refused(proc, state_path)
    output = proc.stdout + proc.stderr
    if case in {"truncated", "control-character"}:
        assert "state is unreadable" in output
    if case == "fetching-at-deploy-head":
        assert (
            "the checkout moved while the update was in phase 'fetching', "
            "before it could have merged"
        ) in output
    if case in {"empty-tag", "unresolvable-tag"}:
        assert "rollback tag" in output
    assert _recovery_git(root, "rev-parse", "HEAD") == expected_head
    assert _recovery_git(root, "symbolic-ref", "--short", "HEAD") == expected_branch
    for path, content in preserved.items():
        assert (root / path).read_text() == content


@pytest.mark.parametrize(
    ("two_parent", "staged", "polluted_git_env"),
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (False, False, True),
    ],
    ids=[
        "S2-fast-forward",
        "S4-two-parent-merge",
        "S13-staged-edit",
        "S2-fast-forward-inherited-git-env",
    ],
)
def test_crashed_owned_update_rolls_back_and_keeps_untouched_edit(
    tmp_path, monkeypatch, two_parent, staged, polluted_git_env
):
    root, common, rollback = _recovery_repo(tmp_path)
    if two_parent:
        _recovery_git(root, "checkout", "-qb", "incoming", common)
        deploy = _recovery_commit_file(root, "changed.txt", "deployed\n", "incoming update")
        _recovery_git(root, "checkout", "-q", "main")
        _recovery_git(root, "merge", "--no-ff", "--no-edit", "incoming")
        assert _recovery_git(root, "rev-parse", "HEAD^1") == rollback
        assert _recovery_git(root, "rev-parse", "HEAD^2") == deploy
    else:
        deploy = _recovery_commit_file(root, "changed.txt", "deployed\n", "deploy")
    # update.sh records the adopted merge head (a fast-forward's deploy commit,
    # or its own two-parent merge) the moment it adopts it.
    merged = _recovery_git(root, "rev-parse", "HEAD")
    local = root / "untouched.txt"
    local.write_text("staged operator edit\n" if staged else "operator edit during recovery\n")
    if staged:
        _recovery_git(root, "add", "--", "untouched.txt")
    if polluted_git_env:
        decoy_parent = tmp_path / "decoy"
        decoy_parent.mkdir()
        decoy, _, _ = _recovery_repo(decoy_parent)
        (decoy / "untouched.txt").write_text("decoy staged edit\n")
        _recovery_git(decoy, "add", "--", "untouched.txt")
        decoy_head = _recovery_git(decoy, "rev-parse", "HEAD")
        decoy_index = decoy / ".git" / "index"
        decoy_index_before = decoy_index.read_bytes()
        monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
        monkeypatch.setenv("GIT_INDEX_FILE", str(decoy_index))
    proc, state_path = _recover(tmp_path, root, own_head=merged, deploy_head=deploy)

    _assert_recovered(proc, state_path, root, rollback)
    if polluted_git_env:
        assert _recovery_git(decoy, "rev-parse", "HEAD") == decoy_head
        assert decoy_index.read_bytes() == decoy_index_before
    assert (root / "changed.txt").read_text() == "base changed\n"
    if staged:
        assert _recovery_git(root, "show", ":untouched.txt") == "staged operator edit"
    else:
        assert local.read_text() == "operator edit during recovery\n"


@pytest.mark.parametrize(
    "corrupt_marker", [False, True], ids=["own-merge", "unparsable-merge-head"]
)
def test_crash_recovery_refuses_every_in_progress_merge(tmp_path, corrupt_marker):
    root, rollback, deploy = _merge_in_progress(tmp_path)
    if corrupt_marker:
        (root / ".git" / "MERGE_HEAD").write_text("not a commit\n")
    head_before = _recovery_git(root, "rev-parse", "HEAD")
    operator_content = "OPERATOR RESOLUTION\n"
    backup_root = tmp_path / "home" / ".genesis" / "premerge-backups" / "known"
    proc, state_path = _recover(
        tmp_path, root, backup_root=backup_root, own_head=rollback, deploy_head=deploy
    )

    _assert_recovery_refused(proc, state_path)
    assert "merge --abort" in proc.stdout + proc.stderr
    assert (root / ".git" / "MERGE_HEAD").exists()
    assert _recovery_git(root, "rev-parse", "HEAD") == head_before
    assert (root / "local.txt").read_text() == operator_content
    assert _recovery_git(root, "show", ":local.txt") == operator_content.rstrip("\n")
    assert not list(backup_root.parent.glob("*/merge-abort"))


@pytest.mark.parametrize(
    ("case", "phase", "original_branch", "staged"),
    [
        ("S1", "fetching", "main", False),
        ("S9", "merging", "main", False),
        ("S12", "merging", "main", True),
        ("old-format", "merging", None, False),
        ("live-pid", "merging", "main", False),
    ],
)
def test_recovery_warns_at_tag_or_skips_a_live_process(
    tmp_path, case, phase, original_branch, staged
):
    update_code = _code(UPDATE)
    assert '"$$"' in update_code
    assert 'GENESIS_BOOTSTRAP_ALLOW_LIVE=1 "$GENESIS_ROOT/scripts/bootstrap.sh"' in update_code

    root, _, rollback = _recovery_repo(tmp_path)
    local = root / "untouched.txt"
    local.write_text("operator edit\n")
    if staged:
        _recovery_git(root, "add", "--", "untouched.txt")
    deploy = (
        _recovery_commit_file(root, "changed.txt", "deployed\n", "deploy")
        if case == "live-pid"
        else ""
    )
    proc, state_path = _recover(
        tmp_path,
        root,
        phase=phase,
        original_branch=original_branch,
        own_head=rollback if case in {"S9", "S12"} else "",
        deploy_head=deploy,
        pid=os.getpid() if case == "live-pid" else 2_147_483_647,
    )

    if case == "live-pid":
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "still running" in proc.stdout
        assert state_path.exists()
        assert _recovery_git(root, "rev-parse", "HEAD") == deploy
        return
    if phase == "merging":
        # Refuse a dirty checkout at the rollback tag after an interrupted merge.
        _assert_recovery_refused(proc, state_path)
        assert "tracked files changed during the merge" in proc.stdout + proc.stderr
        assert _recovery_git(root, "rev-parse", "HEAD") == rollback
        assert local.read_text() == "operator edit\n"
        return
    _assert_recovered(proc, state_path, root, rollback)
    assert (
        "may be an interrupted merge's files or someone's edits; nothing was reset" in proc.stdout
    )
    assert local.read_text() == "operator edit\n"


@pytest.mark.parametrize("phase", ["merging", "bootstrap"])
def test_a_merge_killed_writing_only_an_ephemeral_path_is_refused(tmp_path, phase):
    """A fast-forward killed while writing AGENTS.md leaves HEAD at the tag with only
    that file changed. The deployability predicate skips the ephemeral allowlist, so
    the merging-phase check reads every tracked path instead: it refuses and keeps
    the state. In a later phase the merge had finished, so the same edit is an
    indexer's and recovery proceeds without touching it."""
    root, _, _ = _recovery_repo(tmp_path)
    rollback = _recovery_commit_file(root, "AGENTS.md", "index v1\n", "generated index")
    _recovery_git(root, "tag", "-f", "rollback", rollback)
    (root / "AGENTS.md").write_text("index v2, half writ")
    proc, state_path = _recover(tmp_path, root, phase=phase, own_head="")
    if phase == "merging":
        _assert_recovery_refused(proc, state_path)
        assert "tracked files changed during the merge" in proc.stdout + proc.stderr
        assert "AGENTS.md" in proc.stdout + proc.stderr
    else:
        _assert_recovered(proc, state_path, root, rollback)
    assert _recovery_git(root, "rev-parse", "HEAD") == rollback
    assert (root / "AGENTS.md").read_text() == "index v2, half writ"


def _merge_in_progress(tmp_path: Path) -> tuple[Path, str, str]:
    root, common, rollback = _recovery_repo(tmp_path)
    _recovery_git(root, "checkout", "-qb", "incoming", common)
    deploy = _recovery_commit_file(root, "local.txt", "incoming\n", "incoming")
    _recovery_git(root, "checkout", "-q", "main")
    merge = subprocess.run(
        ["git", "-C", str(root), "merge", "--no-ff", "--no-edit", "incoming"],
        capture_output=True,
        env=_recovery_env(),
        timeout=30,
    )
    assert merge.returncode != 0, "the fixture needs a conflicted merge"
    (root / "local.txt").write_text("OPERATOR RESOLUTION\n")
    _recovery_git(root, "add", "--", "local.txt")
    return root, rollback, deploy


@pytest.mark.parametrize("phase", ["migrations", "health_check"])
def test_a_crash_after_migrations_may_have_run_leaves_code_and_database(tmp_path, phase):
    """The state cannot say whether migrations ran or which snapshot matches, so
    rolling the code back here could put old code on a migrated schema. Recovery
    refuses and leaves the merged code, the database and the state file."""
    root, _, rollback = _recovery_repo(tmp_path)
    deploy = _recovery_commit_file(root, "changed.txt", "deployed\n", "deploy")
    db = root / "data" / "genesis.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_text("migrated schema\n")
    (root / "data" / "genesis.db.pre-update").write_text("old schema\n")
    proc, state_path = _recover(tmp_path, root, phase=phase, own_head=deploy, deploy_head=deploy)
    _assert_recovery_refused(proc, state_path)
    assert "after its database migrations may have run" in proc.stdout + proc.stderr
    assert _recovery_git(root, "rev-parse", "HEAD") == deploy
    assert db.read_text() == "migrated schema\n"


def test_a_checkout_at_the_deploy_head_is_not_taken_as_ours_without_the_record(tmp_path):
    """Ownership is the exact head update.sh recorded. A checkout that merely sits
    at the fetched DEPLOY_HEAD (another session's pull of the same commit) while
    the record still names the pre-update commit is left alone."""
    root, _, rollback = _recovery_repo(tmp_path)
    deploy = _recovery_commit_file(root, "changed.txt", "deployed\n", "someone's pull")
    proc, state_path = _recover(tmp_path, root, own_head=rollback, deploy_head=deploy)
    _assert_recovery_refused(proc, state_path)
    assert "not at the merge this update recorded as its own" in proc.stdout + proc.stderr
    assert _recovery_git(root, "rev-parse", "HEAD") == deploy


def test_update_records_its_own_head_the_moment_it_adopts_the_merge():
    """Bootstrap's recovery trusts only the recorded own_head, so update.sh must
    write the state right after adopting the merge result, not at the next phase."""
    code = _code(UPDATE)
    adopt = code.index('UPDATE_OWN_HEAD="$_merged_head"')
    record = code.index('_write_state "merging"', adopt)
    between = code[adopt:record]
    assert "\nfi" not in between, "the state write must sit inside the adoption branch"
    assert code.count('_write_state "merging"') == 2
