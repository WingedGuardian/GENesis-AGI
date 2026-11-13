"""Tests for the `locate` self-retrieval MCP tool (Track 5.1).

Logic is tested against ``_impl_locate`` directly (fast, infra-free); one test
asserts registration on the genesis-memory FastMCP server. Filesystem isolation
via ``tmp_path`` + env-var overrides (the env.py helpers honor env vars).

The rg-backed content path and its pure-Python fallback are asserted to produce
identical results, so the suite passes whether or not ``rg`` is on PATH (CI-safe).
"""

from __future__ import annotations

import os
import shutil
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
    # The extra content-search names (secrets, key, ...) spare only files
    # whose bytes are on the public main (through CI's leak scan), so code
    # like runtime/init/secrets.py stays searchable.
    monkeypatch.setenv("SECRETS_PATH", str(roots["tmp"] / "absent.env"))
    repo = roots["repo"]
    _touch(repo / "secrets.py", content="def load_secrets():  # FAKE_TOKEN loader\n")
    _touch(repo / "secrets.yaml", content="FAKE_TOKEN: not-a-real-value\n")
    _git_commit(repo, "secrets.py")
    _publish(repo, monkeypatch)
    r = await _impl_locate(scope="repo", within="0", contains="FAKE_TOKEN")
    assert _names(r) == {"secrets.py"}
    assert r["content_skipped_credential_files"] == 1


def _git_commit(repo: Path, *paths: str) -> None:
    import subprocess

    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    if not (repo / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "add", "--", *paths], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "t"], cwd=repo, check=True, env=env)


async def test_a_tracked_file_edited_since_head_is_not_exempt(roots, monkeypatch):
    # Tracking says nothing about the working-tree content: a committed
    # secrets.yaml that later gains a credential line is not public, so the
    # extra name rules apply to it again until the edit is committed.
    monkeypatch.setenv("SECRETS_PATH", str(roots["tmp"] / "absent.env"))
    repo = roots["repo"]
    _touch(repo / "secrets.yaml", content="# FAKE_TOKEN goes here\n")
    _git_commit(repo, "secrets.yaml")
    _touch(repo / "secrets.yaml", content="# FAKE_TOKEN goes here\nFAKE_TOKEN: not-a-real-value\n")
    _touch(repo / "notes.md", content="FAKE_TOKEN rotation\n")
    r = await _impl_locate(scope="repo", within="0", contains="FAKE_TOKEN")
    assert _names(r) == {"notes.md"}
    assert "not-a-real-value" not in str(r)
    assert r["content_skipped_credential_files"] == 1


@pytest.mark.parametrize("sep", ["\n", "\r"])
async def test_a_secret_bearing_file_with_a_line_break_in_its_name_is_skipped(
    roots, monkeypatch, sep
):
    # rg -l prints one path per line; a name holding a line break came back
    # as two fragments, neither equal to the file, so the file was searched.
    if shutil.which("rg") is None:
        pytest.skip("rg not on PATH")
    _secrets_file(roots, monkeypatch, "FAKE_API_KEY=not-a-real-value-123\n")
    _touch(roots["repo"] / f"notes{sep}copy.txt", content="probe not-a-real-value-123\n")
    _touch(roots["repo"] / "notes.md", content="probe\n")
    r = await _impl_locate(scope="repo", within="0", contains="probe")
    assert _names(r) == {"notes.md"}
    assert "not-a-real-value-123" not in str(r)
    assert r["content_skipped_credential_files"] == 1


@pytest.mark.parametrize("have_rg", [True, False])
async def test_a_file_that_gains_a_secret_after_screening_is_dropped_whole(
    roots, monkeypatch, have_rg
):
    # The value screen and the content search read the file separately; a
    # write in between must not put a secret into the returned matches.
    from genesis.mcp.memory import locate

    _no_rg(monkeypatch, have_rg)
    _secrets_file(roots, monkeypatch, "FAKE_API_KEY=not-a-real-value-123\n")
    log = _touch(roots["repo"] / "application.log", content="probe start\n")
    _touch(roots["repo"] / "notes.md", content="probe\n")
    screen = locate._files_holding_secrets

    def screen_then_write(*args):
        found = screen(*args)
        with log.open("a") as fh:
            fh.write("probe token not-a-real-value-123\n")
        return found

    monkeypatch.setattr(locate, "_files_holding_secrets", screen_then_write)
    r = await _impl_locate(scope="repo", within="0", contains="probe")
    assert _names(r) == {"notes.md"}
    assert "not-a-real-value-123" not in str(r)
    assert r["content_skipped_credential_files"] == 1


async def test_the_response_says_when_the_value_screen_is_off(roots, monkeypatch):
    # A missing secrets file turns the value screen off; the caller is told in
    # the response, not only in a log line printed once per process.
    monkeypatch.setenv("SECRETS_PATH", str(roots["tmp"] / "absent.env"))
    _touch(roots["repo"] / "notes.md", content="probe\n")
    off = await _impl_locate(scope="repo", within="0", contains="probe")
    assert off["content_value_screen"].startswith("off")
    assert "value screen off" in off["summary"]
    _secrets_file(roots, monkeypatch, "FAKE_API_KEY=not-a-real-value-123\n")
    on = await _impl_locate(scope="repo", within="0", contains="probe")
    assert on["content_value_screen"] == "on"
    assert "value screen off" not in on["summary"]
    assert (await _impl_locate(scope="repo", within="0"))["content_value_screen"] is None


@pytest.mark.parametrize("word", ["PAT", "DSN", "JWT", "HMAC", "SALT"])
async def test_more_key_words_mark_a_value_as_secret(roots, monkeypatch, word):
    # A short or URL-shaped value is a secret only when its key names one.
    value = {"DSN": "https://fakepub@errors.example.invalid/7"}.get(word, "fake-short-val")
    _secrets_file(roots, monkeypatch, f"FAKE_SERVICE_{word}={value}\n")
    _touch(roots["repo"] / "copy.md", content=f"probe {value}\n")
    _touch(roots["repo"] / "notes.md", content="probe\n")
    r = await _impl_locate(scope="repo", within="0", contains="probe")
    assert _names(r) == {"notes.md"}
    assert r["content_skipped_credential_files"] == 1


def _shaped_line() -> str:
    # Credential-shaped for the shared output scanner (an env-style secret
    # assignment), assembled at runtime and in no secrets file.
    return "FAKE_SERVICE_" + "TOKEN=" + "not-a-real-value-789"


@pytest.mark.parametrize("have_rg", [True, False])
async def test_a_credential_shaped_line_skips_an_untracked_file_whatever_the_pattern(
    roots, monkeypatch, have_rg
):
    # The shape screen reads every line before the search, so a pattern that
    # misses the shaped line skips the file exactly as one that hits it does.
    _no_rg(monkeypatch, have_rg)
    _secrets_file(roots, monkeypatch, "FAKE_API_KEY=not-a-real-value-123\n")
    _touch(roots["repo"] / "draft.md", content=f"probe here\n{_shaped_line()}\n")
    _touch(roots["repo"] / "notes.md", content="probe here\n")
    miss = await _impl_locate(scope="repo", within="0", contains="here")
    hit = await _impl_locate(scope="repo", within="0", contains="not-a-real-value-789")
    assert _names(miss) == {"notes.md"}
    assert hit["results"] == []
    assert "not-a-real-value-789" not in str(miss) + str(hit["results"])
    assert miss["content_skipped_credential_files"] == hit["content_skipped_credential_files"] == 1


async def test_a_tracked_unchanged_file_with_a_credential_shape_is_still_searched(
    roots, monkeypatch
):
    # A file whose bytes equal the public main's blob is already public (and
    # passed its leak scan), so the shape screen exempts it; an edit ends it.
    _secrets_file(roots, monkeypatch, "FAKE_API_KEY=not-a-real-value-123\n")
    repo = roots["repo"]
    fixture = _touch(repo / "fixture.py", content=f"# probe\n# {_shaped_line()}\n")
    _git_commit(repo, "fixture.py")
    _publish(repo, monkeypatch)
    r = await _impl_locate(scope="repo", within="0", contains="probe")
    assert _names(r) == {"fixture.py"}
    assert r["content_skipped_credential_files"] == 0
    fixture.write_text(fixture.read_text() + "# edited\n")
    r = await _impl_locate(scope="repo", within="0", contains="probe")
    assert r["results"] == []
    assert r["content_skipped_credential_files"] == 1


_PUBLIC_URL = "https://github.com/fakeowner/FakeRepo.git"


def _publish(repo: Path, monkeypatch, url: str = _PUBLIC_URL) -> None:
    """Make HEAD the public repo's remote-tracking main, locally (no fetch)."""
    monkeypatch.setenv("GENESIS_GITHUB_USER", "fakeowner")
    monkeypatch.setenv("GENESIS_GITHUB_PUBLIC_REPO", "FakeRepo")
    _git(repo, "remote", "add", "origin", url)
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")


def _split_key(sep: str) -> str:
    # An OpenAI-style key shape broken by a control character the shared
    # scanner's normalisation deletes; assembled at runtime, not a real key.
    return "sk-" + "A" * 10 + sep + "B" * 12


@pytest.mark.parametrize("have_rg", [True, False])
@pytest.mark.parametrize(
    "content",
    [
        pytest.param(lambda: f"probe line {_split_key(chr(12))}\n", id="formfeed-split-key"),
        pytest.param(lambda: f"probe line {_split_key(chr(13))}\n", id="cr-split-key"),
        pytest.param(
            lambda: "FAKE_API_" + "KEY=\nprobe-not-a-real-value\n", id="multiline-assignment"
        ),
    ],
)
async def test_a_credential_shape_across_a_line_break_skips_the_file(
    roots, monkeypatch, have_rg, content
):
    # The scanner sees the whole text; a shape it finds only across a line
    # break (or a deleted control character) skips the file all the same.
    _no_rg(monkeypatch, have_rg)
    _secrets_file(roots, monkeypatch, "FAKE_API_KEY=not-a-real-value-123\n")
    _touch(roots["repo"] / "draft.txt", content=content())
    _touch(roots["repo"] / "notes.md", content="probe\n")
    r = await _impl_locate(scope="repo", within="0", contains="probe")
    assert _names(r) == {"notes.md"}
    assert "B" * 12 not in str(r) and "probe-not-a-real-value" not in str(r)
    assert r["content_skipped_credential_files"] == 1


def _git(repo: Path, *args: str) -> None:
    import subprocess

    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    subprocess.run(["git", *args], cwd=repo, check=True, env=env)


@pytest.mark.parametrize("how", ["clean-filter", "assume-unchanged", "skip-worktree"])
async def test_the_exemption_compares_the_bytes_on_disk_with_head(roots, monkeypatch, how):
    # A clean filter, assume-unchanged or skip-worktree make a file look
    # unchanged to git diff while its bytes on disk differ from HEAD; only a
    # byte-for-byte match with the HEAD blob is exempt.
    _secrets_file(roots, monkeypatch, "FAKE_API_KEY=not-a-real-value-123\n")
    repo = roots["repo"]
    target = repo / "settings.txt"
    _git(repo, "init", "-q")
    if how == "clean-filter":
        _git(repo, "config", "filter.fake.clean", "cat >/dev/null; echo ENCBLOB")
        _touch(repo / ".gitattributes", content="settings.txt filter=fake\n")
        _touch(target, content=f"probe\n{_shaped_line()}\n")
        _git(repo, "add", ".gitattributes", "settings.txt")
        _git(repo, "commit", "-q", "-m", "t")
        _publish(repo, monkeypatch)
    else:
        _touch(target, content="probe\n")
        _git(repo, "add", "settings.txt")
        _git(repo, "commit", "-q", "-m", "t")
        _publish(repo, monkeypatch)
        _git(repo, "update-index", f"--{how}", "settings.txt")
        target.write_text(f"probe\n{_shaped_line()}\n")
    _touch(repo / "notes.md", content="probe\n")
    r = await _impl_locate(scope="repo", within="0", contains="probe")
    assert "settings.txt" not in _names(r)
    assert "not-a-real-value-789" not in str(r)
    assert r["content_skipped_credential_files"] == 1


async def test_the_shape_verdict_is_cached_until_the_file_changes(roots, monkeypatch):
    from genesis.mcp.memory import locate

    _secrets_file(roots, monkeypatch, "FAKE_API_KEY=not-a-real-value-123\n")
    monkeypatch.setattr(locate, "_shape_cache", {})
    calls = []
    real = locate._has_credential_shape

    def counting(text):
        # Whole-file scans only; the rg backstop also scans each returned line.
        if "filler" in text:
            calls.append(text)
        return real(text)

    monkeypatch.setattr(locate, "_has_credential_shape", counting)
    note = _touch(roots["repo"] / "notes.md", content="probe\nfiller\n")
    await _impl_locate(scope="repo", within="0", contains="probe")
    first = len(calls)
    assert first >= 1
    await _impl_locate(scope="repo", within="0", contains="probe")
    assert len(calls) == first  # unchanged file: answered from the cache
    note.write_text("probe again\nfiller\n")
    os.utime(note, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
    await _impl_locate(scope="repo", within="0", contains="probe")
    assert len(calls) > first


async def test_files_the_shape_budget_cannot_reach_are_skipped_and_said(roots, monkeypatch):
    # Fails closed: a file not screened within the budget is never searched.
    from genesis.mcp.memory import locate

    _secrets_file(roots, monkeypatch, "FAKE_API_KEY=not-a-real-value-123\n")
    monkeypatch.setattr(locate, "_shape_cache", {})
    monkeypatch.setattr(locate, "_SHAPE_SCREEN_BUDGET_S", 0.0)
    _touch(roots["repo"] / "notes.md", content="probe\n")
    r = await _impl_locate(scope="repo", within="0", contains="probe")
    assert r["results"] == []
    assert r["content_skipped_credential_files"] == 1
    assert "not screened" in r["summary"]


async def test_the_content_search_runs_off_the_event_loop(roots, monkeypatch):
    import threading

    from genesis.mcp.memory import locate

    seen = []
    real = locate._content_match

    def recording(*args):
        seen.append(threading.current_thread() is threading.main_thread())
        return real(*args)

    monkeypatch.setattr(locate, "_content_match", recording)
    monkeypatch.setenv("SECRETS_PATH", str(roots["tmp"] / "absent.env"))
    _touch(roots["repo"] / "notes.md", content="probe\n")
    await _impl_locate(scope="repo", within="0", contains="probe")
    assert seen == [False]


async def test_a_secrets_file_with_no_qualifying_values_reports_the_screen_off(roots, monkeypatch):
    _secrets_file(roots, monkeypatch, "SHORT_FLAG=on\n")
    _touch(roots["repo"] / "notes.md", content="probe\n")
    r = await _impl_locate(scope="repo", within="0", contains="probe")
    assert r["content_value_screen"] == "off: no qualifying values"


async def test_a_locally_committed_credential_shape_is_screened(roots, monkeypatch):
    # Committed here but never in the public main: never through CI's leak
    # scan, so the shape screen applies and the file is skipped.
    _secrets_file(roots, monkeypatch, "FAKE_API_KEY=not-a-real-value-123\n")
    repo = roots["repo"]
    _touch(repo / "notes.md", content="probe\n")
    _git_commit(repo, "notes.md")
    _publish(repo, monkeypatch)
    _touch(repo / "local.py", content=f"# probe\n# {_shaped_line()}\n")
    _git_commit(repo, "local.py")
    r = await _impl_locate(scope="repo", within="0", contains="probe")
    assert _names(r) == {"notes.md"}
    assert "not-a-real-value-789" not in str(r)
    assert r["content_skipped_credential_files"] == 1


@pytest.mark.parametrize("remote", ["none", "private-origin", "no-main-ref"])
async def test_without_a_public_main_ref_nothing_is_exempt(roots, monkeypatch, remote):
    _secrets_file(roots, monkeypatch, "FAKE_API_KEY=not-a-real-value-123\n")
    monkeypatch.setenv("GENESIS_GITHUB_USER", "fakeowner")
    monkeypatch.setenv("GENESIS_GITHUB_PUBLIC_REPO", "FakeRepo")
    repo = roots["repo"]
    _touch(repo / "fixture.py", content=f"# probe\n# {_shaped_line()}\n")
    _git_commit(repo, "fixture.py")
    if remote == "private-origin":
        _publish(repo, monkeypatch, url="https://github.com/fakeowner/PrivateFork.git")
    elif remote == "no-main-ref":
        _git(repo, "remote", "add", "origin", _PUBLIC_URL)
    r = await _impl_locate(scope="repo", within="0", contains="probe")
    assert r["results"] == []
    assert r["content_skipped_credential_files"] == 1


@pytest.mark.parametrize("public", [False, True])
async def test_the_name_rule_spares_only_files_on_the_public_main(roots, monkeypatch, public):
    # A secrets.yaml committed here but never public is skipped; one whose
    # bytes equal the public main's blob is searchable.
    monkeypatch.setenv("SECRETS_PATH", str(roots["tmp"] / "absent.env"))
    repo = roots["repo"]
    _touch(repo / "notes.md", content="probe\n")
    _git_commit(repo, "notes.md")
    if public:
        _touch(repo / "secrets.yaml", content="probe: settings live here\n")
        _git_commit(repo, "secrets.yaml")
        _publish(repo, monkeypatch)
    else:
        _publish(repo, monkeypatch)
        dsn = "postgres://fakeuser:" + "fakepw99" + "@db.example.invalid/x"
        _touch(repo / "secrets.yaml", content=f"probe_dsn: {dsn}\n")
        _git_commit(repo, "secrets.yaml")
    r = await _impl_locate(scope="repo", within="0", contains="probe")
    if public:
        assert _names(r) == {"notes.md", "secrets.yaml"}
        assert r["content_skipped_credential_files"] == 0
    else:
        assert _names(r) == {"notes.md"}
        assert "fakepw99" not in str(r)
        assert r["content_skipped_credential_files"] == 1


def test_the_secrets_file_is_read_fresh_after_a_same_size_rewrite(roots, monkeypatch):
    # No cache: a rewrite of the same length with its mtime put back still
    # yields the new value, not the one from before.
    from genesis.mcp.memory import locate

    path = _secrets_file(roots, monkeypatch, "FAKE_API_KEY=not-a-real-value-123\n")
    first, _ = locate._secret_values()
    assert "not-a-real-value-123" in first
    st = os.stat(path)
    path.write_text("FAKE_API_KEY=not-a-real-value-456\n")
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    second, _ = locate._secret_values()
    assert "not-a-real-value-456" in second
    assert "not-a-real-value-123" not in second


def test_a_shape_verdict_is_keyed_by_the_bytes_judged():
    # A cached verdict can never describe other bytes, whatever path or file
    # identity they arrive under.
    from genesis.mcp.memory import locate

    clean = b"probe\n" + b"x" * len(_shaped_line()) + b"\n"
    shaped = f"probe\n{_shaped_line()}\n".encode()
    assert locate._shape_verdict(clean, clean.decode()) is False
    assert locate._shape_verdict(shaped, shaped.decode()) is True
    assert locate._shape_verdict(clean, clean.decode()) is False


@pytest.mark.parametrize("url", [
    "https://example.invalid?next=mailto:op" + "erator@example.invalid",
    "https://example.invalid#frag:op" + "erator@example.invalid",
])
def test_a_url_query_or_fragment_is_not_read_as_a_password(roots, monkeypatch, url):
    from genesis.mcp.memory import locate

    _secrets_file(roots, monkeypatch, f"SERVICE_URL={url}\n")
    values, _ = locate._secret_values()
    assert "operator" not in values


@pytest.mark.parametrize("url,password", [
    ("redis://:pa#ss1234@db.example.invalid:6379/0", "pa#ss1234"),
    ("postgres://u:pa?ss1234@db.example.invalid/db", "pa?ss1234"),
])
def test_a_url_password_holding_a_delimiter_is_still_screened(roots, monkeypatch, url, password):
    from genesis.mcp.memory import locate

    _secrets_file(roots, monkeypatch, f"CACHE_URL={url}\n")
    values, _ = locate._secret_values()
    assert password in values


@pytest.mark.parametrize("have_rg", [True, False])
async def test_a_final_newline_does_not_add_a_line(roots, monkeypatch, have_rg):
    _no_rg(monkeypatch, have_rg)
    monkeypatch.setenv("SECRETS_PATH", str(roots["tmp"] / "absent.env"))
    _touch(roots["repo"] / "notes.md", content="probe\n")
    r = await _impl_locate(scope="repo", within="0", contains="^$", regex=True)
    assert r["results"] == []


async def test_a_same_size_rewrite_with_a_restored_mtime_is_screened_again(roots, monkeypatch):
    # The shape verdict is keyed on the change time too: os.utime can put the
    # mtime back, never the ctime.
    from genesis.mcp.memory import locate

    _secrets_file(roots, monkeypatch, "FAKE_API_KEY=not-a-real-value-123\n")
    monkeypatch.setattr(locate, "_shape_cache", {})
    shaped = _shaped_line()
    draft = _touch(roots["repo"] / "draft.md", content=f"probe\n{'x' * len(shaped)}\n")
    _touch(roots["repo"] / "notes.md", content="probe\n")
    clean = await _impl_locate(scope="repo", within="0", contains="probe")
    assert _names(clean) == {"draft.md", "notes.md"}
    before = draft.stat()
    draft.write_text(f"probe\n{shaped}\n")
    os.utime(draft, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert draft.stat().st_size == before.st_size
    r = await _impl_locate(scope="repo", within="0", contains="probe")
    assert _names(r) == {"notes.md"}
    assert "not-a-real-value-789" not in str(r)


@pytest.mark.parametrize("have_rg", [True, False])
async def test_a_public_file_edited_after_its_exemption_is_screened(roots, monkeypatch, have_rg):
    # The exemption is the public blob id, not the path: a file rewritten with
    # a credential after the exemption was computed is judged on its new bytes.
    from genesis.mcp.memory import locate

    _no_rg(monkeypatch, have_rg)
    _secrets_file(roots, monkeypatch, "FAKE_API_KEY=not-a-real-value-123\n")
    repo = roots["repo"]
    fixture = _touch(repo / "fixture.md", content="probe\n")
    _git_commit(repo, "fixture.md")
    _publish(repo, monkeypatch)
    real = locate._tracked_files

    def then_edit(records):
        out = real(records)
        assert str(fixture) in out[1]
        fixture.write_text(f"probe\n{_shaped_line()}\n")
        return out

    monkeypatch.setattr(locate, "_tracked_files", then_edit)
    _touch(repo / "notes.md", content="probe\n")
    r = await _impl_locate(scope="repo", within="0", contains="probe")
    assert _names(r) == {"notes.md"}
    assert "not-a-real-value-789" not in str(r)
    assert r["content_skipped_credential_files"] == 1


def test_rg_matches_only_the_screened_bytes_on_stdin(roots, monkeypatch):
    # rg reads a file separately from the screen, so nothing it read from disk
    # is returned: each listed file is read once, screened, and rg matches
    # again on exactly those bytes, given on stdin (keeping rg's linear-time
    # engine). A stale exemption never spares bytes that are not the blob.
    import json as _json
    import subprocess as _subprocess

    from genesis.mcp.memory import locate

    clean = _touch(roots["repo"] / "clean.md", content="probe one\n")
    edited = _touch(roots["repo"] / "edited.md", content=f"probe\n{_shaped_line()}\n")
    unlisted = _touch(roots["repo"] / "unlisted.md", content="probe two\n")
    stale_oid = locate._blob_oid(b"probe\n", b"0" * 40)
    stdin_seen: list[bytes] = []

    def fake_rg(cmd, **kw):
        if "-l" in cmd:
            out = (str(clean) + "\0" + str(edited) + "\0").encode()
            return _subprocess.CompletedProcess(cmd, 0, out, b"")
        assert cmd[-1] == "-"
        stdin_seen.append(kw["input"])
        evt = {"type": "match", "data": {"line_number": 1, "lines": {"text": "IGNORED\n"}}}
        return _subprocess.CompletedProcess(cmd, 0, (_json.dumps(evt) + "\n").encode(), b"")

    monkeypatch.setattr(locate.subprocess, "run", fake_rg)
    records = [{"path": p, "name": p.name} for p in (clean, edited, unlisted)]
    matched, dropped = locate._content_match_rg(
        records, "probe", False, frozenset(), {str(edited): stale_oid}, set(),
    )
    assert stdin_seen == [b"probe one\n"]
    assert [m["path"] for m in matched] == [clean]
    assert matched[0]["matches"] == [{"line": 1, "text": "probe one"}]
    assert dropped == 1
    assert "not-a-real-value-789" not in str(matched)


def test_a_file_changed_during_its_read_is_dropped_and_not_cached(roots, monkeypatch):
    # The identity is taken before and after the read; a write in between
    # drops the file and leaves no verdict cached for the new bytes.
    from genesis.mcp.memory import locate

    monkeypatch.setattr(locate, "_shape_cache", {})
    note = _touch(roots["repo"] / "notes.md", content="probe\n")
    real_fstat = os.fstat
    calls = []

    def racing_fstat(fd):
        calls.append(fd)
        if len(calls) == 2:
            with open(note, "a") as fh:
                fh.write(f"{_shaped_line()}\n")
        return real_fstat(fd)

    monkeypatch.setattr(locate.os, "fstat", racing_fstat)
    assert locate._screened_text(str(note), frozenset(), {}, set()) == (None, True)
    monkeypatch.setattr(locate.os, "fstat", real_fstat)
    assert locate._shape_cache == {}
    assert locate._screened_text(str(note), frozenset(), {}, set()) == (None, True)


@pytest.mark.parametrize("have_rg", [True, False])
async def test_a_name_spared_public_file_edited_later_is_dropped(roots, monkeypatch, have_rg):
    # secrets.yaml is searchable only because its bytes were the public blob;
    # edited after that check, it is dropped even with no credential shape.
    from genesis.mcp.memory import locate

    _no_rg(monkeypatch, have_rg)
    monkeypatch.setenv("SECRETS_PATH", str(roots["tmp"] / "absent.env"))
    repo = roots["repo"]
    spared = _touch(repo / "secrets.yaml", content="probe: settings live here\n")
    _touch(repo / "notes.md", content="probe\n")
    _git_commit(repo, "secrets.yaml", "notes.md")
    _publish(repo, monkeypatch)
    real = locate._tracked_files

    def then_edit(records):
        out = real(records)
        spared.write_text("probe: hunter2-but-no-shape\n")
        return out

    monkeypatch.setattr(locate, "_tracked_files", then_edit)
    r = await _impl_locate(scope="repo", within="0", contains="probe")
    assert _names(r) == {"notes.md"}
    assert "hunter2" not in str(r)
    assert r["content_skipped_credential_files"] == 1


@pytest.mark.parametrize("have_rg", [True, False])
@pytest.mark.parametrize("contains", ["probe", "zzzzz"])
async def test_non_utf8_and_bom_files_count_the_same_for_every_pattern(
    roots, monkeypatch, have_rg, contains,
):
    _no_rg(monkeypatch, have_rg)
    monkeypatch.setenv("SECRETS_PATH", str(roots["tmp"] / "absent.env"))
    (roots["repo"] / "bom.txt").write_bytes(b"\xef\xbb\xbfprobe here\n")
    (roots["repo"] / "latin.txt").write_bytes(b"probe caf\xe9\n")
    r = await _impl_locate(scope="repo", within="0", contains=contains)
    assert r["content_skipped_credential_files"] == 0
    assert _names(r) == ({"bom.txt", "latin.txt"} if contains == "probe" else set())


@pytest.mark.parametrize("have_rg", [True, False])
@pytest.mark.parametrize("name", ["service-account.json", "my-secret-notes.txt", "service-account.json~"])
async def test_the_dashboards_credential_names_are_never_searched(roots, monkeypatch, have_rg, name):
    _no_rg(monkeypatch, have_rg)
    monkeypatch.setenv("SECRETS_PATH", str(roots["tmp"] / "absent.env"))
    _touch(roots["repo"] / name, content='probe {"private_key": "x"}\n')
    _touch(roots["repo"] / "notes.md", content="probe\n")
    r = await _impl_locate(scope="repo", within="0", contains="probe")
    assert _names(r) == {"notes.md"}
    assert r["content_skipped_credential_files"] == 1


def test_locate_and_the_dashboard_share_the_exact_credential_names():
    from genesis.dashboard.routes import files
    from genesis.security.credential_names import CREDENTIAL_FILE_NAMES, names_a_secret

    assert files._BLOCKED_NAMES is CREDENTIAL_FILE_NAMES
    assert files._names_a_secret is names_a_secret
    assert "service-account.json" in CREDENTIAL_FILE_NAMES


async def test_files_over_the_content_limit_are_never_hashed_for_the_exemption(roots, monkeypatch):
    from genesis.mcp.memory import locate

    monkeypatch.setenv("SECRETS_PATH", str(roots["tmp"] / "absent.env"))
    repo = roots["repo"]
    big = _touch(repo / "big.txt", content="probe\n" + "x" * (locate._MAX_CONTENT_BYTES + 1))
    _touch(repo / "notes.md", content="probe\n")
    _git_commit(repo, "big.txt", "notes.md")
    _publish(repo, monkeypatch)
    seen: list[str] = []
    real = locate._tracked_files

    def recording(records):
        seen.extend(str(rec["path"]) for rec in records)
        return real(records)

    monkeypatch.setattr(locate, "_tracked_files", recording)
    r = await _impl_locate(scope="repo", within="0", contains="probe")
    assert _names(r) == {"notes.md"}
    assert str(big) not in seen
    assert str(repo / "notes.md") in seen


def test_a_file_replaced_after_the_walk_is_dropped(roots, monkeypatch):
    # Name, place and inode were judged at walk time; a different file at the
    # same path when it is read is not the file that was judged.
    from genesis.mcp.memory import locate

    note = _touch(roots["repo"] / "notes.md", content="probe\n")
    st = os.stat(note)
    assert locate._screened_text(str(note), frozenset(), {}, set(), (st.st_dev, st.st_ino))[1] is False
    assert locate._screened_text(str(note), frozenset(), {}, set(), (st.st_dev, st.st_ino + 1)) == (
        None, True,
    )


def test_a_nul_past_the_sniff_window_makes_the_file_binary(roots):
    from genesis.mcp.memory import locate

    late_nul = roots["repo"] / "late.txt"
    late_nul.write_bytes(b"x" * 9000 + b"\nprobe a\n\x00\nprobe b\n")
    assert locate._screened_text(str(late_nul), frozenset(), {}, set()) == (None, False)
