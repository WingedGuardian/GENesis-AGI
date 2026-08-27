"""REAL-binary integration tests for the contribution secret scanners.

These deliberately do NOT mock detect-secrets / gitleaks (unlike test_sanitize.py's
``no_external_scanners`` fixture). They guard against the 2026-08 regression where
BOTH secret layers were silently dead while the mocked tests stayed green:

- ``_run_detect_secrets`` matched ``val == "true"`` but real detect-secrets output
  is ``True  (unverified)`` / ``True  (4.872)`` — the entropy/status suffix meant it
  parsed ZERO findings (the "required floor" caught nothing).
- ``_run_gitleaks`` passed ``--no-git`` alongside ``--pipe``; that combination makes
  gitleaks scan nothing from stdin (verified: even the default ``github-pat`` rule
  never fires). Removing ``--no-git`` restores the scan; ``-c .gitleaks.toml`` adds
  the genesis rules (the config extends defaults via ``useDefault = true``).

detect-secrets is a declared core dependency, so its test runs on CI. gitleaks is an
optional external binary → skipif-guarded.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from genesis.contribution import sanitize
from genesis.contribution.findings import FindingKind, Severity

# A github PAT (default-rule secret for both scanners) + a 12-digit account id near
# a keyword (the genesis-aws-account-id custom rule, gitleaks -c only). Synthetic.
_SECRET_DIFF = (
    "diff --git a/config.py b/config.py\n"
    "--- a/config.py\n"
    "+++ b/config.py\n"
    "@@ -0,0 +1,2 @@\n"
    '+github_token = "ghp_1a2B3c4D5e6F7g8H9i0J1k2L3m4N5o6P7q8R"\n'
    '+account_id = "123456789012"\n'
)


@pytest.mark.skipif(
    sanitize._resolve_detect_secrets() is None, reason="detect-secrets not installed"
)
def test_detect_secrets_real_binary_flags_suffixed_true():
    """The REAL detect-secrets '<plugin> : True  (suffix)' output must parse as a hit.

    RED against the ``== "true"`` parser (the suffix makes it miss every finding).
    """
    parsed = sanitize.parse_diff(_SECRET_DIFF)
    ran, hits = sanitize._run_detect_secrets(parsed)
    assert ran
    assert any(h.kind == FindingKind.SECRET and h.severity == Severity.BLOCK for h in hits), (
        "detect-secrets must BLOCK on the github token (real output is 'True  (unverified)')"
    )


@pytest.mark.skipif(
    sanitize._resolve_detect_secrets() is None,
    reason="detect-secrets not installed in the test env",
)
def test_detect_secrets_floor_survives_path_without_venv_bin(monkeypatch):
    """Regression (2026-08-27): the required secret-scan floor fail-closed BLOCKED
    with detail='missing_binary' whenever the running process's PATH lacked the venv
    bin — e.g. a CC-spawned MCP child inherits CC's PATH, not the server unit's
    ``PATH=<venv>/bin:...`` — even though detect-secrets is a declared core dependency
    installed next to ``sys.executable``. A bare ``shutil.which('detect-secrets')``
    returned None and blocked EVERY contribution proposal on every install.

    With PATH stripped so bare resolution fails, the floor must STILL resolve
    detect-secrets (via the interpreter's bin dir) and actually scan — blocking the
    token, never emitting ``missing_binary``.
    """
    monkeypatch.setenv("PATH", "/nonexistent-dir")
    assert shutil.which("detect-secrets") is None  # precondition: bare PATH now fails

    ran, hits = sanitize._run_detect_secrets(sanitize.parse_diff(_SECRET_DIFF))
    assert ran
    assert not any((h.detail or "") == "missing_binary" for h in hits), (
        "floor must resolve detect-secrets via the venv, not bare PATH"
    )
    assert any(h.kind == FindingKind.SECRET and h.severity == Severity.BLOCK for h in hits), (
        "the floor must still catch the github token with the venv-relative resolve"
    )


def test_resolve_detect_secrets_prefers_interpreter_bin(monkeypatch):
    """The resolver returns an executable even when PATH is empty (installed as a
    core dep next to sys.executable)."""
    monkeypatch.setenv("PATH", "")
    resolved = sanitize._resolve_detect_secrets()
    assert resolved is not None
    assert Path(resolved).is_file() and os.access(resolved, os.X_OK)


@pytest.mark.skipif(
    shutil.which("gitleaks") is None and shutil.which("betterleaks") is None,
    reason="gitleaks not installed",
)
def test_gitleaks_real_binary_scans_stdin():
    """REAL gitleaks must actually scan the piped diff and flag the token.

    RED against ``--no-git --pipe`` (that combo scans nothing from stdin).
    """
    ran, hits = sanitize._run_gitleaks(sanitize.parse_diff(_SECRET_DIFF).added_lines)
    assert ran
    assert any(h.kind == FindingKind.SECRET and h.severity == Severity.BLOCK for h in hits), (
        "gitleaks must flag the github token via --pipe (default rule)"
    )


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks not installed")
def test_gitleaks_loads_repo_config_for_genesis_rules():
    """With -c <repo>/.gitleaks.toml the custom genesis-aws-account-id rule fires.

    Proves the config is actually loaded (extends defaults), not just default rules.
    """
    ran, hits = sanitize._run_gitleaks(sanitize.parse_diff(_SECRET_DIFF).added_lines)
    assert ran
    assert any(
        "account" in (h.detail or "").lower() or "123456789012" in (h.detail or "") for h in hits
    ), "the genesis-aws-account-id rule (only in .gitleaks.toml) must fire with -c"


@pytest.mark.skipif(
    sanitize._resolve_detect_secrets() is None, reason="detect-secrets not installed"
)
def test_scan_diff_end_to_end_blocks_secret():
    """Full scan_diff over a secret-bearing diff must NOT be ok (the floor blocks it)."""
    result = sanitize.scan_diff(_SECRET_DIFF)
    assert result.ok is False
    assert any(f.kind == FindingKind.SECRET for f in result.blocking())


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks not installed")
def test_gitleaks_ignores_secret_on_a_removed_line():
    """gitleaks scans only ADDED lines — a secret on a REMOVED line must NOT be
    flagged (a contribution that DELETES a secret is a good change, not a block).
    RED against the old code, which fed the whole diff to gitleaks."""
    removal_diff = (
        "diff --git a/config.py b/config.py\n"
        "--- a/config.py\n"
        "+++ b/config.py\n"
        "@@ -1 +1 @@\n"
        '-github_token = "ghp_1a2B3c4D5e6F7g8H9i0J1k2L3m4N5o6P7q8R"\n'
        "+cleaned = true\n"
    )
    ran, hits = sanitize._run_gitleaks(sanitize.parse_diff(removal_diff).added_lines)
    assert ran
    assert hits == [], "a secret on a removed line must not be flagged"


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks not installed")
def test_gitleaks_attributes_finding_to_correct_file_and_line():
    """The reported (file, line) must be the TRUE source location of the secret.
    gitleaks' line numbers over header-less --pipe content are unreliable (0-based,
    version-dependent), so attribution is by content-match. A multi-file diff with
    the secret on the 2nd added line of the 2nd file catches off-by-one AND
    file-boundary errors."""
    diff = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -0,0 +1 @@\n"
        "+harmless = 1\n"
        "diff --git a/b.py b/b.py\n"
        "--- a/b.py\n"
        "+++ b/b.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+keep = 2\n"
        '+token = "ghp_1a2B3c4D5e6F7g8H9i0J1k2L3m4N5o6P7q8R"\n'
    )
    ran, hits = sanitize._run_gitleaks(sanitize.parse_diff(diff).added_lines)
    assert ran
    secret_hits = [h for h in hits if h.kind == FindingKind.SECRET]
    assert secret_hits, "the github token must be flagged"
    h = secret_hits[0]
    assert h.file == "b.py", f"wrong file attribution: {h.file}"
    assert h.line == 2, f"wrong line attribution: {h.line} (token is on b.py line 2)"


# ── Install-agnostic argv guards (run on CI without the gitleaks binary) ──
# These spy on subprocess.run so the gitleaks invocation itself is locked even
# where the real binary is absent (the real-binary tests above skip on CI). They
# also fake `git show HEAD:.gitleaks.toml` so the config-PINNING path is exercised.
def _capture_gitleaks_argv(monkeypatch, repo_dir, *, committed_config: str | None):
    """Run _run_gitleaks with a fake gitleaks binary and a fake `git show`,
    capturing the gitleaks argv (and the content it materialized for -c).

    ``committed_config``: what `git show HEAD:.gitleaks.toml` returns — a string
    (the committed config) or None (git show fails → no committed config).
    """
    import subprocess as _sp

    captured: dict = {}
    monkeypatch.setattr(
        sanitize.shutil,
        "which",
        lambda name: "/fake/gitleaks" if name == "gitleaks" else None,
    )
    monkeypatch.setattr("genesis.env.repo_root", lambda: repo_dir)

    def _fake_run(cmd, *a, **k):
        cmd = list(cmd)
        # The config-pinning call: `git -C <root> show HEAD:.gitleaks.toml`.
        if cmd[:2] == ["git", "-C"] and "show" in cmd:
            if committed_config is None:
                return _sp.CompletedProcess(
                    args=cmd, returncode=1, stdout="", stderr="fatal: bad object"
                )
            return _sp.CompletedProcess(args=cmd, returncode=0, stdout=committed_config, stderr="")
        # The gitleaks call.
        captured["cmd"] = cmd
        if "-c" in cmd:
            # Read the materialized config BEFORE _run_gitleaks' finally unlinks it.
            captured["config_content"] = Path(cmd[cmd.index("-c") + 1]).read_text()
        return _sp.CompletedProcess(args=cmd, returncode=0, stdout="[]", stderr="")

    monkeypatch.setattr(sanitize.subprocess, "run", _fake_run)
    sanitize._run_gitleaks(sanitize.parse_diff(_SECRET_DIFF).added_lines)
    return captured


def test_gitleaks_argv_no_nogit_and_pins_committed_config(monkeypatch, tmp_path):
    """gitleaks invoked WITHOUT --no-git (it nullifies --pipe), WITH --pipe, and
    WITH -c pointing at a temp file materialized from the COMMITTED .gitleaks.toml
    (`git show HEAD:`) — NOT the mutable working-tree path."""
    cap = _capture_gitleaks_argv(
        monkeypatch, tmp_path, committed_config="[extend]\nuseDefault = true\n# pinned\n"
    )
    cmd = cap["cmd"]
    assert "--no-git" not in cmd, "--no-git must NOT be combined with --pipe"
    assert "--pipe" in cmd
    assert "-c" in cmd
    # The config is the COMMITTED content, and NOT read straight from the working tree.
    assert cmd[cmd.index("-c") + 1] != str(tmp_path / ".gitleaks.toml"), (
        "config must be pinned to a git-show materialization, not the mutable working tree"
    )
    assert cap["config_content"] == "[extend]\nuseDefault = true\n# pinned\n"


def test_gitleaks_argv_skips_config_when_not_committed(monkeypatch, tmp_path):
    """git show fails (no committed .gitleaks.toml) → -c is omitted (graceful skip;
    default rules still apply), and --no-git is still absent."""
    cap = _capture_gitleaks_argv(monkeypatch, tmp_path, committed_config=None)
    cmd = cap["cmd"]
    assert "-c" not in cmd
    assert "--no-git" not in cmd
    assert "--pipe" in cmd


def test_gitleaks_config_error_warns_not_silent_clean(monkeypatch):
    """A gitleaks config/runtime error (exit 1 with no report) must surface a WARN,
    NOT report a silent clean scan. This is the CRITICAL fail-open the hardening
    closes: exit 1 is ALSO the "leaks found" code, so `if not stdout: return True, []`
    silently treated a broken config as clean. Uses the REAL gitleaks binary with a
    deliberately broken pinned config."""
    if shutil.which("gitleaks") is None:
        pytest.skip("gitleaks not installed")
    import tempfile

    fd, bad = tempfile.mkstemp(suffix=".gitleaks.toml")
    os.write(fd, b"this is not valid toml {{{")
    os.close(fd)
    monkeypatch.setattr(sanitize, "_pinned_gitleaks_config", lambda: bad)
    ran, hits = sanitize._run_gitleaks(sanitize.parse_diff(_SECRET_DIFF).added_lines)
    assert ran is True
    warns = [h for h in hits if h.severity == Severity.WARN and h.scanner == "gitleaks"]
    assert warns, "a broken gitleaks config must WARN (visible), not report clean"
    assert not any(h.severity == Severity.BLOCK for h in hits), (
        "no bogus BLOCKs from an errored scan"
    )


@pytest.mark.skipif(shutil.which("gitleaks") is None, reason="gitleaks not installed")
def test_gitleaks_allowlist_path_cannot_hide_secret():
    """A secret attributed to a diff path that MATCHES .gitleaks.toml's
    `[allowlist] paths` (e.g. tests/) must still fire — in --pipe mode gitleaks
    never sees the file path, so path-based allowlisting is structurally inert and
    a contributor cannot dodge the scan by naming an allowlisted-looking file.
    Locks the security property the docstring asserts."""
    diff = (
        "diff --git a/tests/helper.py b/tests/helper.py\n"
        "--- a/tests/helper.py\n"
        "+++ b/tests/helper.py\n"
        "@@ -0,0 +1 @@\n"
        '+github_token = "ghp_1a2B3c4D5e6F7g8H9i0J1k2L3m4N5o6P7q8R"\n'
    )
    ran, hits = sanitize._run_gitleaks(sanitize.parse_diff(diff).added_lines)
    assert ran
    assert any(h.severity == Severity.BLOCK for h in hits), (
        "an allowlisted diff path must NOT suppress a gitleaks --pipe finding"
    )


@pytest.mark.skipif(
    sanitize._resolve_detect_secrets() is None, reason="detect-secrets not installed"
)
def test_detect_secrets_output_format_canary():
    """Canary against detect-secrets output-format DRIFT — the exact failure mode
    this PR fixed (a suffix on the verdict token silently voided the parser). If a
    future detect-secrets version changes `--string` output so `startswith("true")`
    stops matching, the required floor silently reverts to catching nothing. This
    runs the real binary on a known secret and asserts the parser still yields a hit,
    turning silent format drift into a loud break."""
    parsed = sanitize.parse_diff(
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -0,0 +1 @@\n"
        "+key = AKIAIOSFODNN7EXAMPLE\n"
    )
    ran, hits = sanitize._run_detect_secrets(parsed)
    assert ran
    assert any(h.kind == FindingKind.SECRET and h.severity == Severity.BLOCK for h in hits), (
        "detect-secrets --string output format drifted — the required floor is not parsing hits"
    )


def test_gitleaks_toml_is_contribution_forbidden():
    """A contribution that edits .gitleaks.toml must be BLOCKED — otherwise a merged
    edit could silently weaken secret detection for every future contribution. No
    scanner binary needed; this is a path-policy check."""
    diff = (
        "diff --git a/.gitleaks.toml b/.gitleaks.toml\n"
        "--- a/.gitleaks.toml\n"
        "+++ b/.gitleaks.toml\n"
        "@@ -1 +1 @@\n"
        "-useDefault = true\n"
        "+useDefault = false\n"
    )
    result = sanitize.scan_diff(diff)
    assert result.ok is False, ".gitleaks.toml edits must be blocked"
    assert any(
        f.kind == FindingKind.FORBIDDEN_PATH and ".gitleaks.toml" in (f.file or "")
        for f in result.blocking()
    ), "a .gitleaks.toml edit must raise a FORBIDDEN_PATH block"
