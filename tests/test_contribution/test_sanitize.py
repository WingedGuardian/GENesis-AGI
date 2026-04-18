"""Unit tests for genesis.contribution.sanitize.

External scanners (detect-secrets, gitleaks) are mocked via
shutil.which → None so tests run deterministically regardless of
what's installed on the host.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from genesis.contribution import sanitize
from genesis.contribution.findings import FindingKind, Severity


@pytest.fixture(autouse=True)
def no_external_scanners(monkeypatch):
    """Default: pretend detect-secrets exists (as a stub) and gitleaks
    is absent. After P1-1 the sanitizer fails closed if detect-secrets
    is missing, so the default "clean" test fixtures need to simulate
    its presence. Tests that want to exercise the missing-binary code
    path override `shutil.which` inside their own body.

    Also stubs out subprocess.run so the stub binary is never actually
    executed — the default behavior is "returncode=0, stdout empty"
    which means detect-secrets reports nothing and the scan is clean.
    """
    original_which = sanitize.shutil.which
    original_run = sanitize.subprocess.run

    def mock_which(name):
        if name == "detect-secrets":
            return "/fake/detect-secrets"
        if name in ("gitleaks", "betterleaks"):
            return None
        return original_which(name)

    def mock_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "detect-secrets":
            import subprocess as _sp
            return _sp.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr="",
            )
        return original_run(cmd, *args, **kwargs)

    monkeypatch.setattr(sanitize.shutil, "which", mock_which)
    monkeypatch.setattr(sanitize.subprocess, "run", mock_run)


@pytest.fixture
def clean_diff():
    """A small, clean bug-fix diff."""
    return (
        "diff --git a/src/parser.py b/src/parser.py\n"
        "--- a/src/parser.py\n"
        "+++ b/src/parser.py\n"
        "@@ -10,6 +10,8 @@\n"
        " def tokenize(text):\n"
        "+    if not text:\n"
        "+        return []\n"
        "     return text.split()\n"
    )


def test_clean_diff_ok(clean_diff):
    r = sanitize.scan_diff(clean_diff)
    assert r.ok is True
    assert r.blocking() == []
    assert "portability" in r.scanners_run


def test_parse_diff_added_lines_only(clean_diff):
    p = sanitize.parse_diff(clean_diff)
    assert p.file_paths == ["src/parser.py"]
    texts = [t for _, _, t in p.added_lines]
    assert any("if not text" in t for t in texts)
    assert not p.is_binary


def test_forbidden_path_blocks():
    diff = (
        "diff --git a/src/genesis/identity/USER.md b/src/genesis/identity/USER.md\n"
        "--- a/src/genesis/identity/USER.md\n"
        "+++ b/src/genesis/identity/USER.md\n"
        "@@ -1,1 +1,1 @@\n"
        "+hello\n"
    )
    r = sanitize.scan_diff(diff)
    assert r.ok is False
    kinds = [f.kind for f in r.blocking()]
    assert FindingKind.FORBIDDEN_PATH in kinds


def test_forbidden_secrets_env_blocks():
    diff = (
        "diff --git a/secrets.env b/secrets.env\n"
        "--- a/secrets.env\n+++ b/secrets.env\n@@ -1 +1 @@\n+FOO=bar\n"
    )
    r = sanitize.scan_diff(diff)
    assert r.ok is False


def test_forbidden_research_profiles_blocks():
    diff = (
        "diff --git a/config/research-profiles/jay.yaml b/config/research-profiles/jay.yaml\n"
        "--- a/config/research-profiles/jay.yaml\n"
        "+++ b/config/research-profiles/jay.yaml\n"
        "@@ -1 +1 @@\n+topic: foo\n"
    )
    r = sanitize.scan_diff(diff)
    assert r.ok is False


def test_binary_file_blocks():
    diff = (
        "diff --git a/img.png b/img.png\n"
        "Binary files a/img.png and b/img.png differ\n"
    )
    r = sanitize.scan_diff(diff)
    assert r.ok is False
    assert any(f.kind == FindingKind.BINARY for f in r.blocking())


def test_size_cap_blocks():
    huge = "+" + ("x" * (sanitize.MAX_DIFF_BYTES + 100)) + "\n"
    diff = (
        "diff --git a/big.txt b/big.txt\n"
        "--- a/big.txt\n+++ b/big.txt\n@@ -1 +1 @@\n" + huge
    )
    r = sanitize.scan_diff(diff)
    assert r.ok is False
    assert any(f.kind == FindingKind.SIZE for f in r.blocking())


def test_portability_ip_blocks():
    diff = (
        "diff --git a/config.py b/config.py\n"
        "--- a/config.py\n+++ b/config.py\n@@ -1 +1 @@\n"
        "+OLLAMA_URL = 'http://10.176.34.199:11434'\n"
    )
    r = sanitize.scan_diff(diff)
    assert r.ok is False
    assert any(f.kind == FindingKind.PORTABILITY for f in r.blocking())


def test_portability_home_path_blocks():
    diff = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n"
        "+path = '/home/ubuntu/genesis/data/genesis.db'\n"
    )
    r = sanitize.scan_diff(diff)
    assert r.ok is False


def test_portability_timezone_blocks():
    diff = (
        "diff --git a/t.py b/t.py\n"
        "--- a/t.py\n+++ b/t.py\n@@ -1 +1 @@\n+TZ = 'America/New_York'\n"
    )
    r = sanitize.scan_diff(diff)
    assert r.ok is False


def test_portability_only_scans_added_lines():
    """A portability hit on a REMOVED line should not block."""
    diff = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n"
        "-TZ = 'America/New_York'\n"
        "+TZ = 'UTC'\n"
    )
    r = sanitize.scan_diff(diff)
    assert r.ok is True


def test_email_blocks_personal():
    diff = (
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n"
        "+Contact: jay@somedomain.org\n"
    )
    r = sanitize.scan_diff(diff)
    assert r.ok is False
    assert any(f.kind == FindingKind.EMAIL for f in r.blocking())


def test_email_allows_noreply():
    diff = (
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n"
        "+Contact: noreply@anthropic.com\n"
    )
    r = sanitize.scan_diff(diff)
    assert r.ok is True


def test_email_allows_example_tld():
    diff = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n+u@example.com\n"
    )
    r = sanitize.scan_diff(diff)
    assert r.ok is True


def test_fingerprint_blocks(tmp_path, clean_diff):
    fp = tmp_path / "fingerprints.txt"
    # "text" appears in the added line `    if not text:` — match added lines only
    fp.write_text("not text\n# comment\n\n")
    r = sanitize.scan_diff(clean_diff, fingerprint_file=fp)
    assert r.ok is False
    assert any(f.kind == FindingKind.FINGERPRINT for f in r.blocking())


def test_fingerprint_missing_file_skips(clean_diff, tmp_path):
    r = sanitize.scan_diff(
        clean_diff,
        fingerprint_file=tmp_path / "does-not-exist.txt",
    )
    assert r.ok is True


def test_empty_diff_ok():
    r = sanitize.scan_diff("")
    assert r.ok is True


def test_deletion_only_diff_ok():
    diff = (
        "diff --git a/old.py b/old.py\n"
        "--- a/old.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-print('gone')\n-pass\n"
    )
    r = sanitize.scan_diff(diff)
    assert r.ok is True


def test_multi_file_diff_all_scanned():
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n+ok_line\n"
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n"
        "+IP = '10.176.34.199'\n"
    )
    r = sanitize.scan_diff(diff)
    assert r.ok is False
    blocking = r.blocking()
    assert any(f.file == "b.py" for f in blocking)


def test_wingedguardian_blocks():
    diff = (
        "diff --git a/x.md b/x.md\n"
        "--- a/x.md\n+++ b/x.md\n@@ -1 +1 @@\n+see WingedGuardian/Genesis for more\n"
    )
    r = sanitize.scan_diff(diff)
    assert r.ok is False


def test_wingedguardian_public_allowed():
    # The public repo name (GENesis-AGI) shouldn't match the private regex
    diff = (
        "diff --git a/x.md b/x.md\n"
        "--- a/x.md\n+++ b/x.md\n@@ -1 +1 @@\n+see WingedGuardian/GENesis-AGI\n"
    )
    r = sanitize.scan_diff(diff)
    # The portability regex specifically matches private repo names
    assert r.ok is True


def test_protected_paths_yaml_loaded(tmp_path, clean_diff):
    """Loading globs from a yaml file should extend the default list."""
    yaml_path = tmp_path / "protected.yaml"
    yaml_path.write_text(
        "contribution_forbidden:\n"
        "  - pattern: 'src/parser.py'\n"
        "    reason: 'test block'\n"
    )
    r = sanitize.scan_diff(clean_diff, protected_paths_yaml=yaml_path)
    assert r.ok is False
    assert any(f.kind == FindingKind.FORBIDDEN_PATH for f in r.blocking())


def test_protected_paths_missing_yaml_uses_defaults(clean_diff):
    r = sanitize.scan_diff(clean_diff, protected_paths_yaml=None)
    # clean_diff touches src/parser.py which is NOT in the default list
    assert r.ok is True


def test_parse_diff_dev_null_target():
    """+++ /dev/null means the file is being deleted; shouldn't fail."""
    diff = (
        "diff --git a/x.py b/x.py\n--- a/x.py\n+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-old\n"
    )
    p = sanitize.parse_diff(diff)
    assert p.added_lines == []


def test_findings_include_severity():
    diff = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n+ip=10.176.34.199\n"
    )
    r = sanitize.scan_diff(diff)
    assert all(f.severity == Severity.BLOCK for f in r.blocking())


def test_detect_secrets_runs_when_available(clean_diff, monkeypatch):
    """If detect-secrets is on PATH, the scan runs (mocked to return nothing)."""
    monkeypatch.setattr(sanitize.shutil, "which", lambda name: "/fake/detect-secrets"
                        if name == "detect-secrets" else None)

    class FakeProc:
        returncode = 0
        stdout = "HexHighEntropyString: False\n"
        stderr = ""

    def fake_run(*args, **kwargs):
        return FakeProc()

    with patch("genesis.contribution.sanitize.subprocess.run", side_effect=fake_run):
        r = sanitize.scan_diff(clean_diff)
    assert r.ok is True
    assert "detect-secrets" in r.scanners_run


def test_detect_secrets_missing_is_blocking_p1_1(clean_diff, monkeypatch):
    """P1-1 regression: missing detect-secrets binary must fail CLOSED,
    not silently skip. This is the sanitizer's required floor."""
    monkeypatch.setattr(sanitize.shutil, "which", lambda name: None)
    r = sanitize.scan_diff(clean_diff)
    assert r.ok is False
    blocking = r.blocking()
    assert any(
        f.scanner == "detect-secrets" and f.detail == "missing_binary"
        for f in blocking
    ), f"expected missing_binary block, got: {blocking}"


def test_rename_only_forbidden_path_blocks_codex_p1():
    """Codex review P1 regression: git mv of a forbidden file emits a
    rename-only diff (no +++ header), which previously bypassed the
    forbidden-path check entirely."""
    diff = (
        "diff --git a/src/genesis/identity/USER.md b/docs/user_copy.md\n"
        "similarity index 100%\n"
        "rename from src/genesis/identity/USER.md\n"
        "rename to docs/user_copy.md\n"
    )
    parsed = sanitize.parse_diff(diff)
    assert "src/genesis/identity/USER.md" in parsed.file_paths
    assert "docs/user_copy.md" in parsed.file_paths
    r = sanitize.scan_diff(diff)
    assert r.ok is False
    assert any(f.kind == FindingKind.FORBIDDEN_PATH for f in r.blocking())


def test_mode_only_diff_tracks_paths_codex_p1():
    """Codex review P1 regression: chmod-only commits produce only
    `old mode`/`new mode` headers. The `diff --git` line is now parsed
    to capture the path."""
    diff = (
        "diff --git a/secrets.env b/secrets.env\n"
        "old mode 100644\n"
        "new mode 100755\n"
    )
    parsed = sanitize.parse_diff(diff)
    assert "secrets.env" in parsed.file_paths
    r = sanitize.scan_diff(diff)
    assert r.ok is False
    assert any(f.kind == FindingKind.FORBIDDEN_PATH for f in r.blocking())


def test_diff_git_line_captures_both_a_and_b_paths():
    diff = (
        "diff --git a/renamed-from.py b/renamed-to.py\n"
        "similarity index 95%\n"
        "rename from renamed-from.py\n"
        "rename to renamed-to.py\n"
    )
    parsed = sanitize.parse_diff(diff)
    assert "renamed-from.py" in parsed.file_paths
    assert "renamed-to.py" in parsed.file_paths


def test_quoted_path_forbidden_still_blocks_p1_2(clean_diff):
    """P1-2 regression: git's C-style quoted paths must not bypass the
    forbidden-path check. A diff touching USER.md via a quoted header
    (which git emits for filenames with non-ASCII chars) would previously
    slip past the glob matcher because of the leading `"`."""
    diff = (
        'diff --git "a/src/genesis/identity/USER.md" "b/src/genesis/identity/USER.md"\n'
        '--- "a/src/genesis/identity/USER.md"\n'
        '+++ "b/src/genesis/identity/USER.md"\n'
        "@@ -1 +1 @@\n"
        "+hello\n"
    )
    r = sanitize.scan_diff(diff)
    assert r.ok is False
    assert any(f.kind == FindingKind.FORBIDDEN_PATH for f in r.blocking())


def test_quoted_path_with_special_chars_normalized():
    """Paths with escape sequences should round-trip through
    _normalize_diff_path without smuggling a leading quote into the
    file name."""
    normalized = sanitize._normalize_diff_path('"b/src/foo bar.py"')
    assert normalized == "src/foo bar.py"


def test_gitignored_path_blocks():
    """Diff touching a gitignored path should be blocked when repo_root is set."""
    diff = (
        "diff --git a/src/genesis/modules/automaton_supervisor/module.py "
        "b/src/genesis/modules/automaton_supervisor/module.py\n"
        "--- a/src/genesis/modules/automaton_supervisor/module.py\n"
        "+++ b/src/genesis/modules/automaton_supervisor/module.py\n"
        "@@ -1 +1 @@\n+fix\n"
    )
    from pathlib import Path

    r = sanitize.scan_diff(diff, repo_root=Path.cwd())
    assert r.ok is False
    assert any(f.kind == FindingKind.GITIGNORED_PATH for f in r.blocking())
    assert "gitignored_paths" in r.scanners_run


def test_gitignored_scanner_skips_without_repo_root():
    """Without repo_root, the gitignored-paths scanner should not run."""
    diff = (
        "diff --git a/src/genesis/modules/automaton_supervisor/module.py "
        "b/src/genesis/modules/automaton_supervisor/module.py\n"
        "--- a/src/genesis/modules/automaton_supervisor/module.py\n"
        "+++ b/src/genesis/modules/automaton_supervisor/module.py\n"
        "@@ -1 +1 @@\n+fix\n"
    )
    r = sanitize.scan_diff(diff)
    assert "gitignored_paths" not in r.scanners_run


def test_non_gitignored_path_not_blocked():
    """A normal tracked file should not trigger the gitignored-paths scanner."""
    diff = (
        "diff --git a/src/genesis/routing/cost_tracker.py "
        "b/src/genesis/routing/cost_tracker.py\n"
        "--- a/src/genesis/routing/cost_tracker.py\n"
        "+++ b/src/genesis/routing/cost_tracker.py\n"
        "@@ -1 +1 @@\n+fix\n"
    )
    from pathlib import Path

    r = sanitize.scan_diff(diff, repo_root=Path.cwd())
    assert "gitignored_paths" in r.scanners_run
    gitignored_findings = [f for f in r.findings if f.kind == FindingKind.GITIGNORED_PATH]
    assert gitignored_findings == []


def test_gitignored_scanner_degrades_on_failure(monkeypatch):
    """If git check-ignore fails, the scanner should return empty, not crash."""
    import subprocess as _sp

    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n+fix\n"
    )

    original_run = sanitize.subprocess.run

    def mock_run(cmd, *args, **kwargs):
        if cmd and cmd[0] == "git" and "check-ignore" in cmd:
            raise _sp.TimeoutExpired(cmd, 10)
        return original_run(cmd, *args, **kwargs)

    monkeypatch.setattr(sanitize.subprocess, "run", mock_run)
    from pathlib import Path

    r = sanitize.scan_diff(diff, repo_root=Path.cwd())
    # Scanner ran but produced no findings due to timeout
    gitignored_findings = [f for f in r.findings if f.kind == FindingKind.GITIGNORED_PATH]
    assert gitignored_findings == []


def test_detect_secrets_positive_blocks(clean_diff, monkeypatch):
    monkeypatch.setattr(sanitize.shutil, "which", lambda name: "/fake/detect-secrets"
                        if name == "detect-secrets" else None)

    call_count = [0]

    class FakeProc:
        returncode = 0
        stdout = "HexHighEntropyString: True\n"
        stderr = ""

    def fake_run(*args, **kwargs):
        call_count[0] += 1
        return FakeProc()

    with patch("genesis.contribution.sanitize.subprocess.run", side_effect=fake_run):
        r = sanitize.scan_diff(clean_diff)
    assert r.ok is False
    assert any(f.scanner == "detect-secrets" for f in r.blocking())
