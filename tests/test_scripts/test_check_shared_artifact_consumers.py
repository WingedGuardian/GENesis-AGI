"""Tests for scripts/check_shared_artifact_consumers.py — shared-artifact drift guard.

The guard parses the fenced ``yaml shared-artifact`` blocks in
docs/architecture/shared-artifacts.md and, for each registered artifact, diffs the
DECLARED consumer set (``readers``) against the ACTUAL set of code files under
src/ and scripts/ that reference any of the artifact's ``match_literals`` (fixed
substrings). A consumer that appears in the code but not the registry, or a
declared reader that no longer references the artifact, is a hard error — that is
the drift that let store_cc_token.sh's header go stale when login_health.py added
a second consumer (2026-08-19).

Tests use synthetic registries and source trees under tmp_path; no test depends on
the real repo layout.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "check_shared_artifact_consumers",
    _REPO_ROOT / "scripts" / "check_shared_artifact_consumers.py",
)
csac = importlib.util.module_from_spec(_spec)
sys.modules["check_shared_artifact_consumers"] = csac  # @dataclass __module__ resolves here
_spec.loader.exec_module(csac)


# One well-formed entry: the token file, both literals, four declared readers, the
# loader-home + writer allowlisted. A second, untagged yaml block that must be ignored.
REGISTRY_ONE = """# Shared artifacts

## CC OAuth token

```yaml shared-artifact
artifact: cc_oauth_token.env
documented_in: scripts/store_cc_token.sh
match_literals: [cc_oauth_token.env, load_cc_oauth_token]
readers:
  - src/genesis/cc/login_health.py
  - src/genesis/guardian/diagnosis.py
allowlist:
  - src/genesis/guardian/credential_bridge.py
```

```yaml
not_a_registry_block: ignored because it has no shared-artifact tag
```
"""


def _write(base: Path, rel: str, text: str) -> None:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


# --- parse_registry ---


def test_parse_registry_reads_tagged_blocks_only():
    entries, errors = csac.parse_registry(REGISTRY_ONE)
    assert errors == []
    assert len(entries) == 1
    e = entries[0]
    assert e.artifact == "cc_oauth_token.env"
    assert e.documented_in == "scripts/store_cc_token.sh"
    assert e.match_literals == ["cc_oauth_token.env", "load_cc_oauth_token"]
    assert e.readers == ["src/genesis/cc/login_health.py", "src/genesis/guardian/diagnosis.py"]
    assert e.allowlist == ["src/genesis/guardian/credential_bridge.py"]


def test_parse_registry_flags_missing_required_fields():
    for raw in (
        "```yaml shared-artifact\ndocumented_in: d\nmatch_literals: [x]\nreaders: []\n```\n",  # no artifact
        "```yaml shared-artifact\nartifact: a\ndocumented_in: d\nreaders: []\n```\n",  # no match_literals
        "```yaml shared-artifact\nartifact: a\ndocumented_in: d\nmatch_literals: [x]\n```\n",  # no readers key
        "```yaml shared-artifact\nartifact: a\nmatch_literals: [x]\nreaders: []\n```\n",  # no documented_in
    ):
        _, errors = csac.parse_registry(raw)
        assert len(errors) == 1, raw


def test_parse_registry_flags_empty_match_literals():
    raw = "```yaml shared-artifact\nartifact: a\ndocumented_in: d\nmatch_literals: []\nreaders: []\n```\n"
    _, errors = csac.parse_registry(raw)
    assert len(errors) == 1


def test_parse_registry_flags_empty_literal_string():
    # an empty-string literal would match every file — reject it
    raw = "```yaml shared-artifact\nartifact: a\ndocumented_in: d\nmatch_literals: ['', x]\nreaders: []\n```\n"
    _, errors = csac.parse_registry(raw)
    assert len(errors) == 1


# --- actual_consumers (the fixed-string scan) ---


def test_actual_consumers_finds_literal_matches_minus_excluded(tmp_path):
    _write(tmp_path, "src/pkg/reader_a.py", "path = 'cc_oauth_token.env'\n")
    _write(tmp_path, "src/pkg/loader_user.py", "from x import load_cc_oauth_token\n")
    _write(tmp_path, "src/pkg/unrelated.py", "print('nothing here')\n")
    _write(tmp_path, "scripts/writer.sh", "TOKEN=cc_oauth_token.env\n")
    got = csac.actual_consumers(
        tmp_path,
        ["src", "scripts"],
        ["cc_oauth_token.env", "load_cc_oauth_token"],
        excluded={"scripts/writer.sh"},  # the writer is excluded
    )
    assert got == {"src/pkg/reader_a.py", "src/pkg/loader_user.py"}


def test_actual_consumers_matches_fixed_string_not_regex(tmp_path):
    # A literal with regex metacharacters must match verbatim, not as a pattern.
    _write(tmp_path, "src/pkg/a.py", "x = 'a.b.env'\n")
    _write(tmp_path, "src/pkg/b.py", "x = 'axbyenv'\n")  # would match the regex a.b.env, must NOT
    got = csac.actual_consumers(tmp_path, ["src"], ["a.b.env"], excluded=set())
    assert got == {"src/pkg/a.py"}


def test_actual_consumers_skips_unreadable_gracefully(tmp_path):
    _write(tmp_path, "src/pkg/a.py", "cc_oauth_token.env\n")
    # a directory that looks like a file target is simply walked, not read; ensure no crash
    (tmp_path / "src" / "pkg" / "__pycache__").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__pycache__" / "junk.pyc").write_bytes(
        b"\x00\x01cc_oauth_token.env"
    )
    got = csac.actual_consumers(tmp_path, ["src"], ["cc_oauth_token.env"], excluded=set())
    assert "src/pkg/a.py" in got


# --- check_consumers (both-direction diff) ---


def test_flags_undeclared_direct_consumer_reconstructs_incident(tmp_path):
    """The exact 2026-08-19 shape: a NEW direct reader (login_health) appears in code
    but the registry (frozen at the pre-incident reader set) does not list it."""
    _write(
        tmp_path,
        "docs/architecture/shared-artifacts.md",
        "```yaml shared-artifact\nartifact: cc_oauth_token.env\n"
        "documented_in: scripts/store_cc_token.sh\n"
        "match_literals: [cc_oauth_token.env, load_cc_oauth_token]\n"
        "readers:\n  - src/genesis/guardian/diagnosis.py\n"  # pre-incident: only the host consumer
        "allowlist:\n  - src/genesis/guardian/credential_bridge.py\n```\n",
    )
    _write(tmp_path, "src/genesis/guardian/diagnosis.py", "from x import load_cc_oauth_token\n")
    _write(tmp_path, "src/genesis/guardian/credential_bridge.py", "P = 'cc_oauth_token.env'\n")
    _write(
        tmp_path,
        "scripts/store_cc_token.sh",
        "F='cc_oauth_token.env'  # authoritative list: docs/architecture/shared-artifacts.md\n",
    )
    # the new consumer the header never learned about:
    _write(
        tmp_path, "src/genesis/cc/login_health.py", "T = Path('~/.genesis/cc_oauth_token.env')\n"
    )

    rc, out = _run_main_over(tmp_path)
    assert rc == 1
    assert "::error::" in out
    assert "src/genesis/cc/login_health.py" in out


def test_transitive_consumer_via_loader_symbol_is_caught(tmp_path):
    """A consumer that reaches the file ONLY through the loader function (no filename
    literal) must still be caught — this is why match_literals carries the loader symbol.
    Locks the two-literal design: with the filename literal alone this would be missed."""
    _write(
        tmp_path,
        "docs/architecture/shared-artifacts.md",
        "```yaml shared-artifact\nartifact: cc_oauth_token.env\n"
        "documented_in: scripts/store_cc_token.sh\n"
        "match_literals: [cc_oauth_token.env, load_cc_oauth_token]\n"
        "readers:\n  - src/genesis/cc/login_health.py\n"
        "allowlist:\n  - src/genesis/guardian/credential_bridge.py\n```\n",
    )
    _write(tmp_path, "src/genesis/cc/login_health.py", "T = '~/.genesis/cc_oauth_token.env'\n")
    _write(
        tmp_path, "src/genesis/guardian/credential_bridge.py", "def load_cc_oauth_token(): ...\n"
    )
    _write(
        tmp_path,
        "scripts/store_cc_token.sh",
        "F='cc_oauth_token.env'  # authoritative list: docs/architecture/shared-artifacts.md\n",
    )
    # transitive consumer: references ONLY the loader symbol, never the filename
    _write(
        tmp_path,
        "src/genesis/guardian/diagnosis.py",
        "from genesis.guardian.credential_bridge import load_cc_oauth_token\n",
    )

    rc, out = _run_main_over(tmp_path)
    assert rc == 1
    assert "src/genesis/guardian/diagnosis.py" in out


def test_flags_stale_declared_reader(tmp_path):
    """A declared reader that no longer references the artifact (removed or gone) is an error."""
    _write(
        tmp_path,
        "docs/architecture/shared-artifacts.md",
        "```yaml shared-artifact\nartifact: cc_oauth_token.env\n"
        "documented_in: scripts/store_cc_token.sh\n"
        "match_literals: [cc_oauth_token.env]\n"
        "readers:\n  - src/genesis/cc/login_health.py\n  - src/genesis/gone/removed.py\n"
        "allowlist: []\n```\n",
    )
    _write(tmp_path, "src/genesis/cc/login_health.py", "T = 'cc_oauth_token.env'\n")
    _write(
        tmp_path,
        "scripts/store_cc_token.sh",
        "F='cc_oauth_token.env'  # authoritative list: docs/architecture/shared-artifacts.md\n",
    )
    # src/genesis/gone/removed.py does not exist → stale declared reader

    rc, out = _run_main_over(tmp_path)
    assert rc == 1
    assert "src/genesis/gone/removed.py" in out


def test_clean_when_declared_matches_actual(tmp_path):
    _write(
        tmp_path,
        "docs/architecture/shared-artifacts.md",
        "```yaml shared-artifact\nartifact: cc_oauth_token.env\n"
        "documented_in: scripts/store_cc_token.sh\n"
        "match_literals: [cc_oauth_token.env, load_cc_oauth_token]\n"
        "readers:\n  - src/genesis/cc/login_health.py\n  - src/genesis/guardian/diagnosis.py\n"
        "allowlist:\n  - src/genesis/guardian/credential_bridge.py\n```\n",
    )
    _write(tmp_path, "src/genesis/cc/login_health.py", "T = 'cc_oauth_token.env'\n")
    _write(tmp_path, "src/genesis/guardian/diagnosis.py", "from x import load_cc_oauth_token\n")
    _write(
        tmp_path,
        "src/genesis/guardian/credential_bridge.py",
        "P='cc_oauth_token.env'\ndef load_cc_oauth_token(): ...\n",
    )  # allowlisted loader home
    _write(
        tmp_path,
        "scripts/store_cc_token.sh",
        "F='cc_oauth_token.env'  # authoritative list: docs/architecture/shared-artifacts.md\n",
    )  # documented_in

    rc, out = _run_main_over(tmp_path)
    assert rc == 0, out
    assert "CLEAN" in out


def test_guard_self_is_excluded(tmp_path):
    """The guard script itself references the literals in its own docstring examples;
    as the enforcement mechanism it must never count as a consumer."""
    _write(
        tmp_path,
        "docs/architecture/shared-artifacts.md",
        "```yaml shared-artifact\nartifact: cc_oauth_token.env\n"
        "documented_in: scripts/store_cc_token.sh\n"
        "match_literals: [cc_oauth_token.env]\n"
        "readers:\n  - src/genesis/cc/login_health.py\n"
        "allowlist: []\n```\n",
    )
    _write(tmp_path, "src/genesis/cc/login_health.py", "T = 'cc_oauth_token.env'\n")
    _write(
        tmp_path,
        "scripts/store_cc_token.sh",
        "F='cc_oauth_token.env'  # authoritative list: docs/architecture/shared-artifacts.md\n",
    )
    # the guard's own file, carrying the literal in an example — must be self-excluded
    _write(
        tmp_path, "scripts/check_shared_artifact_consumers.py", "EXAMPLE = 'cc_oauth_token.env'\n"
    )

    rc, out = _run_main_over(tmp_path)
    assert rc == 0, out
    assert "check_shared_artifact_consumers.py" not in out


def test_flags_documented_in_missing_registry_pointer(tmp_path):
    """documented_in must route readers to the registry; a prose-only doc that never
    references it is flagged — otherwise the parallel prose list drifts unchecked."""
    _write(
        tmp_path,
        "docs/architecture/shared-artifacts.md",
        "```yaml shared-artifact\nartifact: cc_oauth_token.env\n"
        "documented_in: scripts/store_cc_token.sh\n"
        "match_literals: [cc_oauth_token.env]\n"
        "readers:\n  - src/genesis/cc/login_health.py\n"
        "allowlist: []\n```\n",
    )
    _write(tmp_path, "src/genesis/cc/login_health.py", "T = 'cc_oauth_token.env'\n")
    # documented_in exists but never points to the registry
    _write(tmp_path, "scripts/store_cc_token.sh", "F='cc_oauth_token.env'  # no pointer\n")
    rc, out = _run_main_over(tmp_path)
    assert rc == 1
    assert "authoritative registry" in out


def test_flags_documented_in_missing_file(tmp_path):
    _write(
        tmp_path,
        "docs/architecture/shared-artifacts.md",
        "```yaml shared-artifact\nartifact: cc_oauth_token.env\n"
        "documented_in: scripts/nonexistent.sh\n"
        "match_literals: [cc_oauth_token.env]\n"
        "readers:\n  - src/genesis/cc/login_health.py\n"
        "allowlist: []\n```\n",
    )
    _write(tmp_path, "src/genesis/cc/login_health.py", "T = 'cc_oauth_token.env'\n")
    rc, out = _run_main_over(tmp_path)
    assert rc == 1
    assert "does not exist" in out


def test_empty_registry_fails_closed(tmp_path):
    """A registry that parses to zero blocks (emptied) must fail closed, not CLEAN."""
    _write(
        tmp_path,
        "docs/architecture/shared-artifacts.md",
        "# prose only, no shared-artifact blocks\n",
    )
    rc, out = _run_main_over(tmp_path)
    assert rc == 1
    assert "no 'yaml shared-artifact' blocks" in out


def test_unclosed_fence_fails_closed(tmp_path):
    """A malformed/unclosed fence matches no block (0 entries, 0 errors) → fail closed."""
    _write(
        tmp_path,
        "docs/architecture/shared-artifacts.md",
        "```yaml shared-artifact\nartifact: x\ndocumented_in: d\nmatch_literals: [x]\nreaders: []\n",
    )  # no closing ``` fence
    rc, out = _run_main_over(tmp_path)
    assert rc == 1


def _registry_and_writer(tmp_path, match_literals="[cc_oauth_token.env]"):
    _write(
        tmp_path,
        "docs/architecture/shared-artifacts.md",
        "```yaml shared-artifact\nartifact: cc_oauth_token.env\n"
        "documented_in: scripts/store_cc_token.sh\n"
        f"match_literals: {match_literals}\nreaders: []\nallowlist: []\n```\n",
    )
    _write(
        tmp_path,
        "scripts/store_cc_token.sh",
        "F=cc_oauth_token.env  # docs/architecture/shared-artifacts.md\n",
    )


def test_symlinked_dir_consumer_is_seen(tmp_path):
    """A consumer reachable only through a symlinked directory under a scan root
    must still be caught (rglob does not descend into symlinked dirs → fail-open)."""
    _registry_and_writer(tmp_path)
    ext = tmp_path / "external"
    ext.mkdir()
    (ext / "hidden_consumer.py").write_text("P = 'cc_oauth_token.env'\n")
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "linkdir").symlink_to(ext, target_is_directory=True)
    rc, out = _run_main_over(tmp_path)
    assert rc == 1
    assert "hidden_consumer.py" in out


def test_unreadable_dir_fails_closed(tmp_path):
    """A directory the scan cannot read must fail closed (it could hide an undeclared
    consumer), not be silently skipped."""
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    _registry_and_writer(tmp_path)
    secret = tmp_path / "src" / "secret"
    secret.mkdir(parents=True)
    (secret / "hidden.py").write_text("cc_oauth_token.env\n")
    secret.chmod(0o000)
    try:
        rc, out = _run_main_over(tmp_path)
    finally:
        secret.chmod(0o755)  # restore so tmp cleanup can remove it
    assert rc == 1
    assert "fails closed" in out


def test_duplicate_top_level_key_errors():
    """A duplicate top-level key (a merge-conflict artifact) must error, not let
    safe_load's last-wins silently narrow the literal set."""
    raw = (
        "```yaml shared-artifact\nartifact: a\ndocumented_in: d\n"
        "match_literals: [cc_oauth_token.env, load_cc_oauth_token]\n"
        "match_literals: [load_cc_oauth_token]\nreaders: []\n```\n"
    )
    _, errors = csac.parse_registry(raw)
    assert len(errors) == 1
    assert "duplicate" in errors[0].lower()


def test_nonstring_literal_errors():
    raw = (
        "```yaml shared-artifact\nartifact: a\ndocumented_in: d\n"
        "match_literals: [[a, b], c]\nreaders: []\n```\n"
    )
    _, errors = csac.parse_registry(raw)
    assert len(errors) == 1
    assert "string" in errors[0].lower()


# --- integration harness: run main() with REGISTRY_PATH + SCAN_ROOTS pointed at tmp_path ---


def _run_main_over(base: Path):
    """Run the real main() against a synthetic tree by chdir'ing into it.

    main() resolves REGISTRY_PATH and SCAN_ROOTS relative to cwd, so chdir is all
    that's needed — no patching (and no recursion risk from stubbing the scanner).
    """
    import contextlib
    import io

    buf = io.StringIO()
    cwd = os.getcwd()
    try:
        os.chdir(base)
        with contextlib.redirect_stdout(buf):
            rc = csac.main()
    finally:
        os.chdir(cwd)
    return rc, buf.getvalue()
