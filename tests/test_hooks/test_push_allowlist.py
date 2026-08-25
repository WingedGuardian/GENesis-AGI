"""Unit tests for scripts/hooks/push_allowlist.py — the local push allowlist.

The allowlist caches "branch X is confirmed on remote (push-urls U)" so a
re-push is decided offline instead of via a network ls-remote. These tests pin
the security-relevant invariants: it never matches an unrecorded branch; an
empty/partial/relative url set never matches; a NEW push destination re-prompts
(subset, not intersection); credentials embedded in a url are never persisted;
stale/future entries expire; and every read/write fails OPEN (a corrupt file can
only cause a redundant prompt, never a phantom allow).

Test urls must be KEYABLE (scheme url, absolute path, or scp-like ssh) — a bare
token like ``url-a`` is a RELATIVE path and is deliberately non-keyable.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "hooks"))
import push_allowlist as pa  # noqa: E402

# Keyable urls (stable across worktrees): scheme urls, an absolute path, scp ssh.
A = "https://ex.com/a.git"
B = "https://ex.com/b.git"
C = "https://ex.com/c.git"
SCP = "git@github.com:o/r.git"
ABS = "/srv/git/a.git"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Redirect the allowlist state file under a tmp GENESIS_HOME (never ~/.genesis)."""
    gh = tmp_path / "genesis-home"
    gh.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("GENESIS_HOME", str(gh))
    return gh


def _state(home: Path) -> Path:
    return home / "pushed_branches.json"


# ── roundtrip + keying ────────────────────────────────────────────────


def test_record_then_is_recorded_roundtrip(home):
    pa.record({SCP}, "feat/x")
    assert pa.is_recorded({SCP}, "feat/x") is True


def test_absolute_path_and_scheme_urls_are_keyable(home):
    pa.record({ABS}, "feat/x")
    assert pa.is_recorded({ABS}, "feat/x") is True


def test_subset_matches_when_current_covered_by_recorded(home):
    """A push whose url set is a SUBSET of the recorded set matches."""
    pa.record({A, B}, "feat/x")
    assert pa.is_recorded({A}, "feat/x") is True
    assert pa.is_recorded({A, B}, "feat/x") is True


def test_added_push_url_reprompts(home):
    """SECURITY (#P2): git pushes to EVERY push url. A newly-added destination
    must NOT ride an old url's cache hit — require subset, not intersection."""
    pa.record({A}, "feat/x")
    assert pa.is_recorded({A, B}, "feat/x") is False  # B was never recorded → prompt


def test_disjoint_urls_do_not_match(home):
    """Same branch NAME, a different repo's url → NOT recorded (no conflation)."""
    pa.record({A}, "feat/x")
    assert pa.is_recorded({B}, "feat/x") is False


def test_empty_push_urls_never_matches_and_never_records(home):
    pa.record({A}, "feat/x")
    assert pa.is_recorded(set(), "feat/x") is False
    pa.record(set(), "feat/y")
    data = json.loads(_state(home).read_text())
    assert "feat/y" not in data["branches"]


def test_unrecorded_branch_not_recorded(home):
    pa.record({A}, "feat/x")
    assert pa.is_recorded({A}, "feat/never-pushed") is False


def test_empty_state_returns_false(home):
    assert pa.is_recorded({A}, "feat/x") is False


# ── #P2: relative local urls are non-keyable (cross-worktree ambiguity) ─


def test_relative_url_not_recorded(home):
    """A relative local push url (``../../remote.git``) resolves differently across
    worktrees, so it must NOT be cached — record no-ops."""
    pa.record({"../../remote.git"}, "feat/x")
    assert not _state(home).exists() or "feat/x" not in json.loads(_state(home).read_text()).get(
        "branches", {}
    )


def test_relative_url_never_matches(home):
    pa.record({ABS}, "feat/x")
    # A later push that resolves a relative url can't decide offline → False.
    assert pa.is_recorded({"../../remote.git"}, "feat/x") is False


def test_any_relative_url_in_set_blocks_offline_decision(home):
    """If ANY current push url is relative/ambiguous, the WHOLE push falls back
    (git pushes to every url, so one ambiguous destination taints the decision)."""
    pa.record({A}, "feat/x")
    assert pa.is_recorded({A, "./local.git"}, "feat/x") is False


# ── #P2: credentials in urls are never persisted ───────────────────────


def test_credentials_stripped_from_stored_url(home):
    pa.record({"https://user:SECRETPAT@ex.com/a.git"}, "feat/x")
    raw = _state(home).read_text()
    assert "SECRETPAT" not in raw
    assert "user:" not in raw
    data = json.loads(raw)
    assert data["branches"]["feat/x"]["urls"] == ["https://ex.com/a.git"]


def test_credential_url_matches_sanitized_form(home):
    """A push with an embedded credential matches a record of the same repo — both
    are sanitized to the credential-free form, so matching still works."""
    pa.record({"https://user:PAT@ex.com/a.git"}, "feat/x")
    assert pa.is_recorded({"https://ex.com/a.git"}, "feat/x") is True
    assert pa.is_recorded({"https://other:TOKEN@ex.com/a.git"}, "feat/x") is True


# ── #CRITICAL: scp-vs-local disambiguation (git's slash-before-colon rule) ──


def test_sanitize_url_classification_matrix():
    """``_sanitize_url`` must match git's real scp-vs-local rule and strip creds."""
    # scheme urls (canonical urlsplit): userinfo stripped, scheme+host lowercased
    # (host is case-insensitive; path case preserved), empty host rejected, @ in
    # path kept, multiple @ in authority collapsed to the host, IPv6 + port kept.
    assert pa._sanitize_url("https://user:PAT@host/x.git") == "https://host/x.git"
    assert pa._sanitize_url("HTTPS://Host/X") == "https://host/X"
    assert pa._sanitize_url("https://GitHub.com/O/R.git") == "https://github.com/O/R.git"
    assert pa._sanitize_url("https://host:443/x") == "https://host:443/x"
    assert pa._sanitize_url("https://[::1]:22/x") == "https://[::1]:22/x"
    assert pa._sanitize_url("ssh://user:pw@host:22/x") == "ssh://host:22/x"
    assert pa._sanitize_url("https://user@/x") is None
    assert pa._sanitize_url("https://a@b@host/x") == "https://host/x"
    assert pa._sanitize_url("https://host/a@b") == "https://host/a@b"
    # scp-like: ONLY when ':' has no '/' before it; userinfo stripped.
    assert pa._sanitize_url("git@github.com:o/r.git") == "github.com:o/r.git"
    assert pa._sanitize_url("host:path/x") == "host:path/x"
    # absolute local path — stable.
    assert pa._sanitize_url("/srv/git/a.git") == "/srv/git/a.git"
    # RELATIVE local path with a colon AFTER a slash → NOT scp → None (the CRITICAL).
    assert pa._sanitize_url("sub/dir:name") is None
    assert pa._sanitize_url("backups/2026-01-01T12:00:00/repo.git") is None
    assert pa._sanitize_url("../../remote.git") is None
    assert pa._sanitize_url("./local.git") is None
    assert pa._sanitize_url("~/repo.git") is None
    assert pa._sanitize_url("") is None
    assert pa._sanitize_url("   ") is None


def test_relative_path_with_colon_not_offline_allowed(home):
    """CRITICAL regression: a relative local path containing a colon (e.g. a
    timestamped backup dir) must NOT be treated as a stable scp key — otherwise a
    genuine first push to it could skip the prompt across worktrees."""
    ambiguous = "backups/2026-01-01T12:00:00/repo.git"
    pa.record({ambiguous}, "feat/x")  # non-keyable → must no-op
    assert not _state(home).exists() or "feat/x" not in json.loads(_state(home).read_text()).get(
        "branches", {}
    )
    assert pa.is_recorded({ambiguous}, "feat/x") is False


def test_scp_userinfo_stripped_from_stored_url(home):
    """An scp ``user@`` prefix is never persisted verbatim; matching still works."""
    pa.record({"git@github.com:o/r.git"}, "feat/x")
    data = json.loads(_state(home).read_text())
    assert data["branches"]["feat/x"]["urls"] == ["github.com:o/r.git"]
    assert pa.is_recorded({"git@github.com:o/r.git"}, "feat/x") is True


# ── REPLACE semantics (C2) ────────────────────────────────────────────


def test_rerecord_replaces_url_set(home):
    """A re-record REPLACES the url set (does not union) — a stale set-url --push
    ages out immediately rather than lingering in the trusted set."""
    pa.record({A}, "feat/x")
    pa.record({B}, "feat/x")
    data = json.loads(_state(home).read_text())
    assert data["branches"]["feat/x"]["urls"] == [B]
    assert pa.is_recorded({A}, "feat/x") is False
    assert pa.is_recorded({B}, "feat/x") is True


# ── freshness / prune ─────────────────────────────────────────────────


def _write_state(home: Path, branches: dict) -> None:
    _state(home).write_text(json.dumps({"version": 1, "branches": branches}))


def test_stale_entry_is_not_recorded(home):
    old = (datetime.now(UTC) - timedelta(days=pa.RETENTION_DAYS + 1)).isoformat()
    _write_state(home, {"feat/x": {"urls": [A], "ts": old}})
    assert pa.is_recorded({A}, "feat/x") is False


def test_fresh_entry_just_inside_window_is_recorded(home):
    recent = (datetime.now(UTC) - timedelta(days=pa.RETENTION_DAYS - 1)).isoformat()
    _write_state(home, {"feat/x": {"urls": [A], "ts": recent}})
    assert pa.is_recorded({A}, "feat/x") is True


def test_future_timestamp_is_not_fresh(home):
    """SECURITY (#P2): a FUTURE ts (clock skew / tampered state) must NOT read as
    fresh — its negative age would otherwise pass `< RETENTION_DAYS` forever."""
    future = (datetime.now(UTC) + timedelta(days=5)).isoformat()
    _write_state(home, {"feat/x": {"urls": [A], "ts": future}})
    assert pa.is_recorded({A}, "feat/x") is False


def test_stale_entries_pruned_on_write(home):
    old = (datetime.now(UTC) - timedelta(days=pa.RETENTION_DAYS + 5)).isoformat()
    _write_state(home, {"stale/one": {"urls": [C], "ts": old}})
    pa.record({B}, "fresh/two")
    data = json.loads(_state(home).read_text())
    assert "stale/one" not in data["branches"]
    assert "fresh/two" in data["branches"]


def test_missing_or_unparseable_ts_treated_as_stale(home):
    _write_state(home, {"a": {"urls": [A]}, "b": {"urls": [A], "ts": "not-a-date"}})
    assert pa.is_recorded({A}, "a") is False
    assert pa.is_recorded({A}, "b") is False


# ── fail-open on corruption ───────────────────────────────────────────


def test_corrupt_file_fails_open_to_false(home):
    _state(home).write_text("{ this is not json ]]")
    assert pa.is_recorded({A}, "feat/x") is False


def test_record_overwrites_corrupt_file(home):
    _state(home).write_text("garbage")
    pa.record({A}, "feat/x")  # must not raise; overwrites with a valid envelope
    assert pa.is_recorded({A}, "feat/x") is True


def test_non_dict_envelope_fails_open(home):
    _state(home).write_text(json.dumps([1, 2, 3]))
    assert pa.is_recorded({A}, "feat/x") is False


# ── placement / hygiene ───────────────────────────────────────────────


def test_state_lands_under_genesis_home(home):
    pa.record({A}, "feat/x")
    assert _state(home).exists()


def test_no_tmp_files_left_behind(home):
    pa.record({A}, "feat/x")
    leftovers = list(home.glob(".pushed_branches.*.tmp"))
    assert leftovers == [], f"temp files not cleaned: {leftovers}"
