"""Tests for the `locate` self-retrieval MCP tool (Track 5.1).

Logic is tested against ``_impl_locate`` directly (fast, infra-free); one test
asserts registration on the genesis-memory FastMCP server. Filesystem isolation
via ``tmp_path`` + env-var overrides (the env.py helpers honor env vars).

The rg-backed content path and its pure-Python fallback are asserted to produce
identical results, so the suite passes whether or not ``rg`` is on PATH (CI-safe).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from genesis.mcp.memory.locate import _humanize_age, _impl_locate, _parse_window


def _touch(p: Path, content: str = "", age_s: float = 0.0) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    if age_s:
        t = time.time() - age_s
        os.utime(p, (t, t))
    return p


def _names(result: dict) -> set[str]:
    return {r["name"] for r in result["results"]}


@pytest.fixture
def roots(tmp_path, monkeypatch):
    plans = tmp_path / "plans"
    output = tmp_path / "output"
    repo = tmp_path / "repo"
    for d in (plans, output, repo):
        d.mkdir()
    monkeypatch.setenv("GENESIS_PLANS_DIR", str(plans))
    monkeypatch.setenv("GENESIS_OUTPUT_DIR", str(output))
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(repo))
    return {"plans": plans, "output": output, "repo": repo, "tmp": tmp_path}


# ── window parsing ────────────────────────────────────────────────────────────


def test_parse_window_units():
    assert _parse_window("24h") == 24 * 3600
    assert _parse_window("12h") == 12 * 3600
    assert _parse_window("7d") == 7 * 86400
    assert _parse_window("30d") == 30 * 86400
    assert _parse_window("5") == 5 * 86400  # bare int = days


def test_parse_window_no_limit():
    for token in ("0", "", "all", "any"):
        assert _parse_window(token) is None


def test_parse_window_invalid_raises():
    with pytest.raises(ValueError):
        _parse_window("not-a-window")


def test_parse_window_negative_raises():
    with pytest.raises(ValueError):
        _parse_window("-5d")


# ── recency ───────────────────────────────────────────────────────────────────


async def test_recency_window_boundary(roots):
    _touch(roots["plans"] / "new.md", age_s=2 * 3600)
    _touch(roots["plans"] / "mid.md", age_s=23 * 3600)
    _touch(roots["plans"] / "old.md", age_s=30 * 3600)
    r = await _impl_locate(scope="plans", within="24h")
    assert _names(r) == {"new.md", "mid.md"}


async def test_within_zero_no_filter(roots):
    _touch(roots["plans"] / "old.md", age_s=30 * 3600)
    r = await _impl_locate(scope="plans", within="0")
    assert "old.md" in _names(r)


# ── type / glob ───────────────────────────────────────────────────────────────


async def test_file_type_filters(roots):
    _touch(roots["plans"] / "a.py")
    _touch(roots["plans"] / "b.md")
    assert _names(await _impl_locate(scope="plans", within="0", file_type="code")) == {"a.py"}
    assert _names(await _impl_locate(scope="plans", within="0", file_type="noncode")) == {"b.md"}
    assert _names(await _impl_locate(scope="plans", within="0", file_type="any")) == {"a.py", "b.md"}


async def test_name_glob_case_insensitive(roots):
    _touch(roots["plans"] / "voice-roadmap.md")
    _touch(roots["plans"] / "notes.md")
    assert _names(await _impl_locate(scope="plans", within="0", name="*roadmap*")) == {"voice-roadmap.md"}
    assert _names(await _impl_locate(scope="plans", within="0", name="*ROADMAP*.MD")) == {"voice-roadmap.md"}


# ── scope mapping ─────────────────────────────────────────────────────────────


async def test_scope_docs_maps_plans_and_output(roots):
    _touch(roots["plans"] / "p.md")
    _touch(roots["output"] / "o.md")
    _touch(roots["repo"] / "r.md")
    r = await _impl_locate(scope="docs", within="0")
    assert _names(r) == {"p.md", "o.md"}
    labels = {x["name"]: x["scope"] for x in r["results"]}
    assert labels["p.md"] == "plans"
    assert labels["o.md"] == "output"


async def test_scope_repo_is_optin(roots):
    _touch(roots["repo"] / "r.md")
    assert "r.md" not in _names(await _impl_locate(scope="docs", within="0"))
    rep = await _impl_locate(scope="repo", within="0")
    assert "r.md" in _names(rep)
    assert rep["results"][0]["scope"] == "repo"


# ── pruning ───────────────────────────────────────────────────────────────────


async def test_prune_dirs(roots):
    repo = roots["repo"]
    _touch(repo / "node_modules" / "x.md")
    _touch(repo / ".git" / "y.md")
    _touch(repo / ".claude" / "worktrees" / "wt" / "z.md")  # nested worktree -> pruned
    _touch(repo / "keep.md")
    r = await _impl_locate(scope="repo", within="0")
    assert _names(r) == {"keep.md"}


# ── content match (rg + fallback parity) ──────────────────────────────────────


async def test_content_match_rg_present(roots):
    _touch(roots["plans"] / "hit.md", content="alpha needle one\nbeta two\n")
    _touch(roots["plans"] / "miss.md", content="nothing here\n")
    r = await _impl_locate(scope="plans", within="0", contains="needle")
    assert _names(r) == {"hit.md"}
    matches = r["results"][0]["matches"]
    assert matches and matches[0]["line"] == 1 and "needle" in matches[0]["text"]


async def test_content_match_fallback_when_rg_absent(roots, monkeypatch):
    monkeypatch.setattr("genesis.mcp.memory.locate._have_rg", lambda: False)
    _touch(roots["plans"] / "hit.md", content="alpha needle one\nbeta two\n")
    _touch(roots["plans"] / "miss.md", content="nothing here\n")
    r = await _impl_locate(scope="plans", within="0", contains="needle")
    assert _names(r) == {"hit.md"}
    matches = r["results"][0]["matches"]
    assert matches and matches[0]["line"] == 1 and "needle" in matches[0]["text"]


async def test_content_invalid_regex(roots):
    _touch(roots["plans"] / "f.md", content="x")
    r = await _impl_locate(scope="plans", within="0", contains="(", regex=True)
    assert r["error"]
    assert r["results"] == []


async def test_content_skips_binary_and_oversize(roots):
    _touch(roots["plans"] / "big.md", content="needle " + ("x" * 2_000_000))  # > 1 MiB
    (roots["plans"] / "bin.md").write_bytes(b"needle\x00\x01rest")
    _touch(roots["plans"] / "ok.md", content="needle here\n")
    r = await _impl_locate(scope="plans", within="0", contains="needle")
    assert _names(r) == {"ok.md"}


# ── truncation / ordering ─────────────────────────────────────────────────────


async def test_truncation_signal(roots):
    for i in range(60):
        _touch(roots["plans"] / f"f{i:02d}.md", age_s=i)
    r = await _impl_locate(scope="plans", within="0", limit=10)
    assert r["count"] == 10
    assert r["total_matched"] == 60
    assert r["truncated"] is True
    assert len(r["results"]) == 10


async def test_scan_truncation_signal(roots, monkeypatch):
    monkeypatch.setattr("genesis.mcp.memory.locate._MAX_FILES_SCANNED", 3)
    for i in range(10):
        _touch(roots["plans"] / f"f{i}.md")
    r = await _impl_locate(scope="plans", within="0")
    assert r["scan_truncated"] is True
    assert "incomplete" in r["summary"].lower()


async def test_no_scan_truncation_normally(roots):
    _touch(roots["plans"] / "a.md")
    r = await _impl_locate(scope="plans", within="0")
    assert r["scan_truncated"] is False


async def test_limit_zero_returns_all(roots):
    for i in range(5):
        _touch(roots["plans"] / f"f{i}.md")
    r = await _impl_locate(scope="plans", within="0", limit=0)
    assert r["count"] == 5
    assert r["truncated"] is False


async def test_ordering_recency_desc(roots):
    _touch(roots["plans"] / "oldest.md", age_s=300)
    _touch(roots["plans"] / "middle.md", age_s=200)
    _touch(roots["plans"] / "newest.md", age_s=100)
    r = await _impl_locate(scope="plans", within="0")
    mtimes = [x["mtime"] for x in r["results"]]
    assert mtimes == sorted(mtimes, reverse=True)
    assert r["results"][0]["name"] == "newest.md"


# ── empty / error / safety ────────────────────────────────────────────────────


async def test_empty_no_match_is_clean(roots):
    _touch(roots["plans"] / "a.md")
    r = await _impl_locate(scope="plans", within="0", name="*nonexistent*")
    assert r["count"] == 0
    assert r["results"] == []
    assert r["error"] is None
    assert "no files" in r["summary"].lower()


async def test_nonexistent_root_skipped_not_raised(roots, monkeypatch):
    monkeypatch.setenv("GENESIS_OUTPUT_DIR", str(roots["tmp"] / "does_not_exist"))
    _touch(roots["plans"] / "p.md")
    r = await _impl_locate(scope="docs", within="0")
    assert any("does_not_exist" in s for s in r["skipped_roots"])
    assert "p.md" in _names(r)


async def test_symlink_dir_not_followed(roots):
    sub = roots["plans"] / "sub"
    sub.mkdir()
    _touch(sub / "real.md")
    (sub / "loop").symlink_to(roots["plans"])  # back-reference; must not loop
    r = await _impl_locate(scope="plans", within="0")
    assert "real.md" in _names(r)


async def test_permission_denied_dir_continues(roots):
    if os.geteuid() == 0:
        pytest.skip("running as root — chmod 000 is ineffective")
    locked = roots["plans"] / "locked"
    locked.mkdir()
    _touch(locked / "hidden.md")
    _touch(roots["plans"] / "visible.md")
    os.chmod(locked, 0o000)
    try:
        r = await _impl_locate(scope="plans", within="0")
        assert "visible.md" in _names(r)
    finally:
        os.chmod(locked, 0o755)


# ── registration / helpers ────────────────────────────────────────────────────


async def test_locate_registered_on_memory_server():
    from genesis.mcp.memory_mcp import mcp

    tools = await mcp.get_tools()
    assert "locate" in tools


def test_humanize_age():
    now = time.time()
    assert _humanize_age(now, now) == "just now"
    assert _humanize_age(now - 7200, now) == "2h ago"
    assert _humanize_age(now - 3 * 86400, now) == "3d ago"
