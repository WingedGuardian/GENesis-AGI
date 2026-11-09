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

    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert "locate" in tools


def test_humanize_age():
    now = time.time()
    assert _humanize_age(now, now) == "just now"
    assert _humanize_age(now - 7200, now) == "2h ago"
    assert _humanize_age(now - 3 * 86400, now) == "3d ago"


_CREDENTIAL_NAMES = (
    "secrets.env",
    "secrets.env.bak-1",
    ".env",
    ".env.local",
    "prod.env",
    "id_ed25519",
    "API_TOKEN",
    ".credentials.json",
    "deploy.pem",
)


@pytest.mark.parametrize("have_rg", [True, False])
async def test_credential_files_are_findable_by_name_but_never_content_searched(
    roots, monkeypatch, have_rg
):
    # #4129: a content match returns matching lines, which in these files are
    # the credentials themselves.
    if not have_rg:
        monkeypatch.setattr("genesis.mcp.memory.locate._have_rg", lambda: False)
    monkeypatch.setenv("SECRETS_PATH", str(roots["tmp"] / "elsewhere" / "absent.env"))
    for name in _CREDENTIAL_NAMES:
        _touch(roots["repo"] / name, content="FAKE_TOKEN=not-a-real-value\n")
    _touch(roots["repo"] / "creds" / "service_creds.env", content="FAKE_TOKEN=not-a-real-value\n")
    _touch(roots["repo"] / ".config" / "gh" / "hosts.yml", content="FAKE_TOKEN: not-a-real-value\n")
    _touch(roots["repo"] / "notes.md", content="rotate FAKE_TOKEN monthly\n")
    r = await _impl_locate(scope="repo", within="0", contains="FAKE_TOKEN")
    assert _names(r) == {"notes.md"}
    assert "not-a-real-value" not in str(r)
    # Counted, so a skipped file never reads as "no match" (incl. creds/ and .config/).
    assert r["content_skipped_credential_files"] == len(_CREDENTIAL_NAMES) + 2
    assert "not content-searched" in r["summary"]
    assert "secrets.env" in _names(await _impl_locate(scope="repo", within="0", name="secrets.env"))
    assert "hosts.yml" in _names(await _impl_locate(scope="repo", within="0", name="hosts.yml"))


async def test_the_configured_secrets_file_is_skipped_under_any_name(roots, monkeypatch):
    # SECRETS_PATH can point anywhere; a hard link under a neutral name is the
    # same file, caught by inode rather than by name.
    real = _touch(roots["tmp"] / "vault" / "keys.txt", content="FAKE_TOKEN=not-a-real-value\n")
    os.link(real, roots["repo"] / "notes-copy.txt")
    monkeypatch.setenv("SECRETS_PATH", str(real))
    r = await _impl_locate(scope="repo", within="0", contains="FAKE_TOKEN")
    assert r["results"] == []
    assert r["content_skipped_credential_files"] == 1


def test_dashboard_and_locate_share_one_credential_predicate():
    from genesis.dashboard.routes import files
    from genesis.security.credential_names import is_credential_component

    assert files._is_credential_component is is_credential_component


def _secrets_file(roots, monkeypatch, content: str) -> Path:
    path = _touch(roots["tmp"] / "vault" / "secrets.env", content=content)
    monkeypatch.setenv("SECRETS_PATH", str(path))
    return path


def _no_rg(monkeypatch, have_rg: bool) -> None:
    if not have_rg:
        monkeypatch.setattr("genesis.mcp.memory.locate._have_rg", lambda: False)


@pytest.mark.parametrize("have_rg", [True, False])
async def test_a_file_carrying_a_secret_value_is_never_searched(roots, monkeypatch, have_rg):
    # A copy under a neutral name, or a value pasted into a note, passes every
    # name and inode rule; the secret VALUE in its content gives it away, and
    # the whole file is skipped, not just the line.
    _no_rg(monkeypatch, have_rg)
    _secrets_file(roots, monkeypatch, 'FAKE_TOKEN="not-a-real-value-123"\nSHORT_FLAG=on\n')
    _touch(roots["repo"] / "keys-copy.txt", content="FAKE_TOKEN=not-a-real-value-123\n")
    _touch(roots["repo"] / "pasted.md", content="FAKE_TOKEN rotated\nvalue not-a-real-value-123\n")
    _touch(roots["repo"] / "notes.md", content="FAKE_TOKEN is rotated monthly\n")
    r = await _impl_locate(scope="repo", within="0", contains="FAKE_TOKEN")
    assert _names(r) == {"notes.md"}
    assert "not-a-real-value-123" not in str(r)
    assert r["content_skipped_credential_files"] == 2


async def test_the_skip_count_does_not_depend_on_the_pattern(roots, monkeypatch):
    # Hiding only matching lines let a regex recover a secret one character at
    # a time from whether a line was withheld. The file-level skip answers the
    # same for every pattern.
    _secrets_file(roots, monkeypatch, "FAKE_API_KEY=Zq8fakeRandomish9ValueX\n")
    _touch(roots["repo"] / "copy.txt", content="key Zq8fakeRandomish9ValueX\n")
    _touch(roots["repo"] / "notes.md", content="key rotation notes\n")
    hit = await _impl_locate(scope="repo", within="0", contains="^key Zq8f", regex=True)
    miss = await _impl_locate(scope="repo", within="0", contains="^key Zq8X", regex=True)
    assert hit["results"] == miss["results"] == []
    assert hit["content_skipped_credential_files"] == miss["content_skipped_credential_files"] == 1
    assert hit["summary"].replace("Zq8f", "") == miss["summary"].replace("Zq8X", "")


async def test_secret_selection_reads_like_the_runtime_and_spares_ordinary_settings(
    roots, monkeypatch
):
    # python-dotenv semantics (inline comment, export, quotes); secret-named
    # keys from 6 characters; URL passwords; random-looking values under
    # neutral keys. Ordinary settings, short values and look-alike key names
    # (AUTHOR, BYPASS) never mark a file.
    _secrets_file(
        roots,
        monkeypatch,
        "FAKE_API_KEY=not-a-real-value-123 # rotated\n"
        "export FAKE_SIGNING_SECRET='also-not-real-456'\n"
        "FAKE_DB_PASSWORD=pw6fak\n"
        "FAKE_NAS_PASS=abcd\n"
        "FAKE_DB_URL=postgres://fakeuser:fakepw99@db.example.invalid/x\n"
        "NEUTRAL_NAME=Zq7fakeRandomish9Value\n"
        "OLLAMA_URL=http://ollama.example.invalid:11434\n"
        "USER_TIMEZONE=Etc/Fake_Timezone\n"
        "WHISPER_MODEL=large-v3-turbo-fake\n"
        "GIT_AUTHOR_NAME=Fake Author Name\n"
        "BYPASS_MODE=plain-words-mode\n",
    )
    marked = {
        "a.md": "not-a-real-value-123",
        "b.md": "also-not-real-456",
        "c.md": "pw6fak",
        "d.md": "fakepw99",
        "e.md": "Zq7fakeRandomish9Value",
    }
    spared = {
        "f.md": "abcd",
        "g.md": "http://ollama.example.invalid:11434",
        "h.md": "Etc/Fake_Timezone",
        "i.md": "large-v3-turbo-fake",
        "j.md": "Fake Author Name",
        "k.md": "plain-words-mode",
    }
    for name, value in {**marked, **spared}.items():
        _touch(roots["repo"] / name, content=f"probe {value}\n")
    r = await _impl_locate(scope="repo", within="0", contains="probe")
    assert _names(r) == set(spared)
    assert r["content_skipped_credential_files"] == len(marked)


@pytest.mark.parametrize("have_rg", [True, False])
@pytest.mark.parametrize("newline", ["\n", "\\n"])
async def test_a_partial_copy_of_a_multiline_secret_is_caught(
    roots, monkeypatch, have_rg, newline
):
    # A PEM key stored across lines, or on one line with literal \n escapes,
    # never occurs whole in a copy; each body line marks the file, the armor
    # lines do not.
    _no_rg(monkeypatch, have_rg)
    key = newline.join(
        [
            "-----BEGIN FAKE KEY-----",
            "fakeKeyBodyLineOne-not-real",
            "fakeKeyBodyLineTwo-not-real",
            "-----END FAKE KEY-----",
        ]
    )
    # dotenv expands an escaped newline only inside double quotes; single quotes keep it literal.
    quote = '"' if newline == "\n" else "'"
    _secrets_file(roots, monkeypatch, f"FAKE_PRIVATE_KEY={quote}{key}{quote}\n")
    _touch(roots["repo"] / "partial.txt", content="probe\nfakeKeyBodyLineTwo-not-real\n")
    _touch(roots["repo"] / "armor.md", content="probe -----BEGIN FAKE KEY-----\n")
    r = await _impl_locate(scope="repo", within="0", contains="probe")
    assert _names(r) == {"armor.md"}
    assert r["content_skipped_credential_files"] == 1


@pytest.mark.parametrize(
    "path",
    [
        "secrets.yaml",
        "client_secret.json",
        "api_key.txt",
        "gcp-key.json",
        "prod.secret",
        "terraform.tfvars",
        ".s3cfg",
        "kubeconfig",
        "auth.json",
        "secrets.env~",
        ".secrets.env.swp",
        ".ssh/known_hosts",
        ".aws/config",
    ],
)
async def test_content_search_skips_more_credential_names_than_the_dashboard(
    roots, monkeypatch, path
):
    monkeypatch.setenv("SECRETS_PATH", str(roots["tmp"] / "absent.env"))
    _touch(roots["repo"] / path, content="FAKE_TOKEN=not-a-real-value\n")
    _touch(roots["repo"] / "notes.md", content="FAKE_TOKEN rotation\n")
    r = await _impl_locate(scope="repo", within="0", contains="FAKE_TOKEN")
    assert _names(r) == {"notes.md"}
    assert r["content_skipped_credential_files"] == 1


async def test_a_missing_secrets_file_is_logged_once(roots, monkeypatch, caplog):
    import logging

    from genesis.mcp.memory import locate

    monkeypatch.setattr(locate, "_warned_secrets", set())
    monkeypatch.setenv("SECRETS_PATH", str(roots["tmp"] / "absent.env"))
    _touch(roots["repo"] / "notes.md", content="probe\n")
    with caplog.at_level(logging.WARNING):
        await _impl_locate(scope="repo", within="0", contains="probe")
        await _impl_locate(scope="repo", within="0", contains="probe")
    assert caplog.text.count("copies of credentials are not detected by value") == 1


async def test_a_tracked_source_file_named_for_secrets_is_still_searched(roots, monkeypatch):
    # The extra content-search names (secrets, key, ...) apply to untracked
    # files only: git-tracked files are public and leak-scanned, and code like
    # runtime/init/secrets.py must stay searchable.
    import subprocess

    monkeypatch.setenv("SECRETS_PATH", str(roots["tmp"] / "absent.env"))
    repo = roots["repo"]
    _touch(repo / "secrets.py", content="def load_secrets():  # FAKE_TOKEN loader\n")
    _touch(repo / "secrets.yaml", content="FAKE_TOKEN: not-a-real-value\n")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "add", "secrets.py"], cwd=repo, check=True, env=env)
    r = await _impl_locate(scope="repo", within="0", contains="FAKE_TOKEN")
    assert _names(r) == {"secrets.py"}
    assert r["content_skipped_credential_files"] == 1
