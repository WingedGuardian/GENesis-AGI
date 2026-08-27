"""Unit tests for genesis.contribution.fingerprints (the install fingerprint
generator). All fixtures are synthetic — no real install values — so these run
identically on any clone / CI runner.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

from genesis.contribution import fingerprints as fp
from genesis.contribution import sanitize
from genesis.contribution.findings import FindingKind

# ERE / re-portability: constructs a generated pattern must never contain
# (GNU-grep extensions or PCRE-only syntax that break `grep -E` on other hosts).
_BANNED = (r"\d", r"\w", r"\s", r"\D", r"\W", r"\S", "(?=", "(?!", "(?<", "(?P")


def _fake_home(tmp_path: Path, *, genesis_cfg=None, guardian=None, ambient=None) -> Path:
    home = tmp_path / "home"
    (home / ".genesis" / "config").mkdir(parents=True)
    if genesis_cfg is not None:
        (home / ".genesis" / "config" / "genesis.yaml").write_text(yaml.safe_dump(genesis_cfg))
    if guardian is not None:
        (home / ".genesis" / "guardian_remote.yaml").write_text(yaml.safe_dump(guardian))
    if ambient is not None:
        (home / ".genesis" / "ambient_remote.yaml").write_text(yaml.safe_dump(ambient))
    return home


def _make_diff(added: str) -> str:
    return (
        f"diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,2 @@\n existing\n+{added}\n"
    )


@pytest.fixture(autouse=True)
def _stable_hostname(monkeypatch):
    # Force a stopword hostname so harvest() is deterministic regardless of the
    # runner's real hostname (which could otherwise add a pattern on CI).
    monkeypatch.setattr(fp.socket, "gethostname", lambda: "localhost")
    # Clear the effective-config overrides so config tests read the fake yaml,
    # not whatever the runner's shell exports (install-agnostic determinism).
    for var in ("OLLAMA_URL", "LM_STUDIO_URL", "USER_TIMEZONE"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _no_external_scanners(monkeypatch):
    """Stub detect-secrets present (returncode 0) and gitleaks absent so the
    round-trip scan_diff calls are deterministic on any runner (CI may lack the
    detect-secrets binary, whose missing-floor would otherwise BLOCK)."""
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
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
        return original_run(cmd, *args, **kwargs)

    monkeypatch.setattr(sanitize.shutil, "which", mock_which)
    monkeypatch.setattr(sanitize.subprocess, "run", mock_run)


def _harvest(home, **kw):
    return fp.harvest(home=home, repo_root=Path("/srv/proj"), run_ip6=False, **kw)


# ── harvest: config-derived patterns ─────────────────────────────────────────


def test_harvest_config_patterns(tmp_path):
    home = _fake_home(
        tmp_path,
        genesis_cfg={
            "network": {
                "ollama_url": "http://10.5.6.7:11434",
                "lm_studio_url": "http://192.168.9.4:1234/v1",
            },
            "timezone": "Europe/Berlin",
            "github": {
                "user": "AcmeOrg",
                "public_repo": "Acme-Public",
                "private_repo": "Acme-Private",
            },
        },
    )
    pats = {p for p, _ in _harvest(home)}
    assert r"\b10\.5\.6\." in pats  # /24 prefix, last octet dropped
    assert r"\b192\.168\.9\." in pats
    assert r"\bEurope/Berlin\b" in pats
    assert r"\bAcmeOrg/Acme\-Private\b" in pats
    # The bare github user is NEVER emitted (it appears in the public org name).
    assert not any(p.strip(r"\b") == "AcmeOrg" for p in pats)
    # CC project-dir slug is emitted (re.escape'd, so '-' becomes '\-').
    assert re.escape("-srv-proj") in pats
    assert any(c == "CC project-dir slug" for _, c in _harvest(home))


def test_harvest_env_override_precedence(tmp_path, monkeypatch):
    # Codex P1: an install using env/secrets overrides must be fingerprinted on
    # its EFFECTIVE values, not the stale yaml leaves.
    home = _fake_home(
        tmp_path,
        genesis_cfg={
            "network": {"ollama_url": "http://10.5.6.7:11434"},
            "timezone": "Europe/Berlin",
        },
    )
    monkeypatch.setenv("OLLAMA_URL", "http://198.51.100.9:11434")  # RFC 5737 doc IP
    monkeypatch.setenv("USER_TIMEZONE", "Asia/Tokyo")
    pats = {p for p, _ in _harvest(home)}
    # Subnet URLs stay env-first (env IS the runtime override for OLLAMA_URL).
    assert r"\b198\.51\.100\." in pats  # env override wins
    assert r"\b10\.5\.6\." not in pats  # stale yaml value not used
    # Timezone is now file-authoritative with env a deprecated fallback, so the
    # scrubber blocks the UNION of both possible sources — either could be the
    # effective runtime zone across installs, and missing the effective one leaks it.
    assert r"\bAsia/Tokyo\b" in pats  # env value blocked
    assert r"\bEurope/Berlin\b" in pats  # file value ALSO blocked (union)


def test_harvest_blocks_file_tz_when_env_unset(tmp_path):
    # USER_TIMEZONE cleared by the _stable_hostname autouse fixture → the file must still
    # be blocked (it is the effective runtime zone under file-first resolution).
    home = _fake_home(tmp_path, genesis_cfg={"timezone": "Europe/Berlin"})
    pats = {p for p, _ in _harvest(home)}
    assert r"\bEurope/Berlin\b" in pats


def test_harvest_tz_union_deterministic_order(tmp_path, monkeypatch):
    # P3 (Codex): when env and file differ, the two tz patterns must emit in a
    # STABLE order (env then file) — a set would iterate hash-dependently and
    # break harvest()'s stable-order contract.
    home = _fake_home(tmp_path, genesis_cfg={"timezone": "Europe/Berlin"})
    monkeypatch.setenv("USER_TIMEZONE", "Asia/Tokyo")
    tz_pats = [p for p, c in _harvest(home) if c == "install timezone"]
    assert tz_pats == [r"\bAsia/Tokyo\b", r"\bEurope/Berlin\b"]


def test_harvest_tz_union_no_utc_and_dedups(tmp_path, monkeypatch):
    # UTC on either side is never emitted; when both agree the pattern dedups.
    home = _fake_home(tmp_path, genesis_cfg={"timezone": "UTC"})
    monkeypatch.setenv("USER_TIMEZONE", "Europe/Berlin")
    pats = [p for p, c in _harvest(home) if c == "install timezone"]
    assert pats == [r"\bEurope/Berlin\b"]  # UTC file value dropped, only the real one
    # Both sources the same real zone → a single (deduped) pattern.
    home2 = _fake_home(tmp_path / "x", genesis_cfg={"timezone": "Europe/Berlin"})
    pats2 = [p for p, c in _harvest(home2) if c == "install timezone"]
    assert pats2 == [r"\bEurope/Berlin\b"]


def test_bounded_helper_edges():
    # Codex P2: \b only where the edge char is a word char.
    assert fp._bounded("abc") == r"\babc\b"
    assert fp._bounded("Priv-") == r"\bPriv\-"  # no trailing \b after '-'
    assert fp._bounded("/path") == r"/path\b"  # no leading \b before '/'


def test_bounded_trailing_punctuation_round_trip(tmp_path):
    # A private repo name ending in a non-word char must still block its value.
    home = _fake_home(
        tmp_path, genesis_cfg={"github": {"user": "AcmeOrg", "private_repo": "Priv-"}}
    )
    dest = tmp_path / "fp.txt"
    fp.generate(path=dest, home=home, repo_root=Path("/srv/proj"), run_ip6=False)
    r = sanitize.scan_diff(_make_diff("pushed to AcmeOrg/Priv- today"), fingerprint_file=dest)
    assert r.ok is False
    assert any(f.kind == FindingKind.FINGERPRINT for f in r.blocking())


def test_harvest_skips_localhost_and_utc(tmp_path):
    home = _fake_home(
        tmp_path,
        genesis_cfg={
            "network": {
                "ollama_url": "http://localhost:11434",
                "lm_studio_url": "http://127.0.0.1:1234",
            },
            "timezone": "UTC",
            "github": {"user": "Acme", "private_repo": ""},  # empty private_repo → no repo pattern
        },
    )
    comments = {c for _, c in _harvest(home)}
    assert "install timezone" not in comments
    assert "private repo name" not in comments
    assert not any("subnet" in c for c in comments)


def test_harvest_hostuser_stopword_and_short_dropped(tmp_path):
    home = _fake_home(
        tmp_path,
        guardian={"host_user": "genesis"},  # stopword → dropped
        ambient={"host_user": "ns1"},  # too short (<5) → dropped
    )
    pats = {p for p, _ in _harvest(home)}
    assert r"\bgenesis\b" not in pats
    assert r"\bns1\b" not in pats


def test_harvest_hostuser_specific_kept(tmp_path):
    home = _fake_home(tmp_path, guardian={"host_user": "zorrik", "host_ip": "10.9.9.2"})
    pats = {p for p, _ in _harvest(home)}
    assert r"\bzorrik\b" in pats
    assert r"\b10\.9\.9\." in pats


def test_harvest_missing_files_no_raise(tmp_path):
    home = tmp_path / "empty_home"
    home.mkdir()
    # No .genesis at all — must not raise; still yields the $HOME-path + slug patterns.
    pats = fp.harvest(home=home, repo_root=Path("/srv/proj"), run_ip6=False)
    assert isinstance(pats, list)
    assert any(c == "home directory path" for _, c in pats)


# ── harvest: IPv6 ULA via mocked `ip -6 addr` ────────────────────────────────


def test_harvest_ula_prefixes(monkeypatch, tmp_path):
    fake = "1: lo\n    inet6 ::1/128 scope host\n2: eth0\n    inet6 fd12:3456:789a::5/64 scope global\n    inet6 fe80::1/64 scope link\n"
    monkeypatch.setattr(
        fp.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=fake, stderr=""),
    )
    home = _fake_home(tmp_path)
    pats = {p for p, _ in fp.harvest(home=home, repo_root=Path("/srv/proj"), run_ip6=True)}
    assert r"fd12:3456" in pats  # first two hextets of the ULA
    assert not any("fe80" in p for p in pats)  # link-local excluded
    assert not any("::1" in p for p in pats)  # loopback excluded


def test_harvest_ula_ip_absent_no_raise(monkeypatch, tmp_path):
    def _boom(*a, **k):
        raise FileNotFoundError("ip")

    monkeypatch.setattr(fp.subprocess, "run", _boom)
    home = _fake_home(tmp_path)
    pats = fp.harvest(home=home, repo_root=Path("/srv/proj"), run_ip6=True)
    assert not any("ULA" in c for _, c in pats)  # degraded, no crash


# ── render: marker preservation ──────────────────────────────────────────────


def test_render_preserves_handedits(tmp_path):
    block1 = fp.build_block([(r"\bhostA\b", "host")])
    existing = fp.render("", block1)
    # add a hand-edited line after the block
    existing += "\\bsynthetictoken\\b\n"
    block2 = fp.build_block([(r"\bhostB\b", "host")])  # different generated content
    out = fp.render(existing, block2)
    assert r"\bhostB\b" in out  # new generated content spliced in
    assert r"\bhostA\b" not in out  # old generated content replaced
    assert r"\bsynthetictoken\b" in out  # hand-edit preserved


def test_render_legacy_markerless_preserved(tmp_path):
    legacy = "\\bsynthetictoken\\b\n\\b999999[0-9]\\b\n"
    block = fp.build_block([(r"\bhostA\b", "host")])
    out = fp.render(legacy, block)
    assert r"\bsynthetictoken\b" in out
    assert "999999" in out
    assert fp.BEGIN_MARKER in out
    assert fp.END_MARKER in out


# ── generate: atomic write, mode, empty harvest ──────────────────────────────


def test_generate_writes_mode_0600(tmp_path):
    dest = tmp_path / "out" / "release-fingerprints.txt"
    path, n = fp.generate(
        path=dest,
        home=_fake_home(tmp_path, guardian={"host_user": "zorrik"}),
        repo_root=Path("/srv/proj"),
        run_ip6=False,
    )
    assert path == dest
    assert dest.is_file()
    assert (dest.stat().st_mode & 0o777) == 0o600
    assert n >= 1


def test_generate_empty_harvest_valid_file(tmp_path):
    # No config, no ip6, home with no derivable tokens beyond path/slug.
    dest = tmp_path / "fp.txt"
    fp.generate(path=dest, home=tmp_path / "eh", repo_root=Path("/x"), run_ip6=False)
    # File is valid for _check_fingerprints (only comments + a couple patterns → no crash).
    r = sanitize.scan_diff(_make_diff("nothing private here"), fingerprint_file=dest)
    assert r.ok is True


# ── ERE contract ─────────────────────────────────────────────────────────────


def test_ere_contract_no_pcre_constructs(tmp_path):
    home = _fake_home(
        tmp_path,
        genesis_cfg={
            "network": {
                "ollama_url": "http://10.5.6.7:11434",
                "lm_studio_url": "http://192.168.9.4:1234",
            },
            "timezone": "America/Sao_Paulo",
            "github": {"user": "AcmeOrg", "private_repo": "Priv-Repo"},
        },
        guardian={"host_user": "zorrik", "host_ip": "10.9.9.2"},
    )
    for pat, _ in _harvest(home):
        re.compile(pat)  # must compile under Python re
        for banned in _BANNED:
            assert banned not in pat, f"pattern {pat!r} contains non-ERE construct {banned!r}"


# ── round-trip through the real sanitizer ────────────────────────────────────


@pytest.mark.parametrize(
    "cfg_kw, probe",
    [
        ({"guardian": {"host_user": "zorrik"}}, "deploying to zorrik now"),
        (
            {"genesis_cfg": {"network": {"ollama_url": "http://10.5.6.7:11434"}}},
            "endpoint 10.5.6.42 responded",
        ),
        (
            {"genesis_cfg": {"timezone": "Europe/Berlin"}},
            "tz set to Europe/Berlin",
        ),
        (
            {"genesis_cfg": {"github": {"user": "AcmeOrg", "private_repo": "Priv-Repo"}}},
            "pushed to AcmeOrg/Priv-Repo",
        ),
    ],
)
def test_round_trip_blocks_source_values(tmp_path, cfg_kw, probe):
    home = _fake_home(tmp_path, **cfg_kw)
    dest = tmp_path / "fp.txt"
    fp.generate(path=dest, home=home, repo_root=Path("/srv/proj"), run_ip6=False)
    r = sanitize.scan_diff(_make_diff(probe), fingerprint_file=dest)
    assert r.ok is False
    assert any(f.kind == FindingKind.FINGERPRINT for f in r.blocking())


def test_comment_lines_do_not_become_patterns(tmp_path):
    # A generated comment ("# host user (...)") must be skipped, not compiled.
    home = _fake_home(tmp_path, guardian={"host_user": "zorrik"})
    dest = tmp_path / "fp.txt"
    fp.generate(path=dest, home=home, repo_root=Path("/srv/proj"), run_ip6=False)
    # A diff line that contains a word from a comment ("host") but no real
    # pattern must NOT block.
    r = sanitize.scan_diff(_make_diff("the host is fine"), fingerprint_file=dest)
    assert r.ok is True


# ── render: edge cases (reviewer finding #3) ─────────────────────────────────


def test_render_begin_without_end():
    block = fp.build_block([(r"\bhostA\b", "host")])
    existing = fp.BEGIN_MARKER + "\n\\bstray\\b\n"  # orphan BEGIN, no END
    out = fp.render(existing, block)
    assert r"\bstray\b" in out  # preserved (falls to the no-pair branch)
    assert out.count(fp.END_MARKER) == 1  # exactly one END, from the fresh block


def test_render_duplicate_marker_pairs():
    existing = (
        fp.build_block([(r"\bold\b", "x")])
        + "\n"
        + fp.build_block([(r"\bsecond\b", "y")])
        + "\n\\bkeep\\b\n"
    )
    out = fp.render(existing, fp.build_block([(r"\bfresh\b", "z")]))
    assert r"\bfresh\b" in out  # first pair replaced with new content
    assert r"\bold\b" not in out  # first block's content gone
    assert r"\bsecond\b" in out  # only the FIRST pair is replaced; 2nd preserved
    assert r"\bkeep\b" in out  # trailing hand-edit preserved


# ── sync_secret / _resolve_public_slug (reviewer findings #1, #2) ────────────


def test_resolve_public_slug_pins_cwd(monkeypatch):
    seen = {}

    def fake_run(cmd, *a, **k):
        seen["cwd"] = k.get("cwd")
        return subprocess.CompletedProcess(cmd, 0, stdout="Owner/Repo\n", stderr="")

    monkeypatch.setattr(fp.subprocess, "run", fake_run)
    slug = fp._resolve_public_slug(Path("/srv/genesis"))
    assert slug == "Owner/Repo"
    assert seen["cwd"] == "/srv/genesis"  # pinned to the Genesis repo, not caller cwd


def test_sync_secret_success(tmp_path, monkeypatch):
    home = _fake_home(tmp_path, genesis_cfg={"github": {"public_repo": "PubRepo"}})
    fpfile = tmp_path / "fp.txt"
    fpfile.write_text("# hdr\n\\bzorrik\\b\n")
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append((cmd, k.get("input"), k.get("cwd")))
        if cmd[1] == "repo":
            return subprocess.CompletedProcess(cmd, 0, stdout="Owner/PubRepo\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(fp.subprocess, "run", fake_run)
    ok = fp.sync_secret(path=fpfile, repo_root=Path("/srv/genesis"), home=home)
    assert ok is True
    secret_call = next(c for c in calls if c[0][1] == "secret")
    cmd = secret_call[0]
    assert "Owner/PubRepo" in cmd  # --repo carries the resolved slug
    assert "zorrik" in secret_call[1]  # body passed via stdin (input=)
    # Contract lock (real gh has no --body-file; value must go via stdin, never
    # argv). Guards the mock-hid-the-flag bug that shipped in the first cut.
    assert "--body-file" not in cmd
    assert not any(str(a).startswith("--body") for a in cmd)
    assert "zorrik" not in " ".join(cmd)  # value never on the command line


def test_sync_secret_repo_mismatch_skips(tmp_path, monkeypatch):
    home = _fake_home(tmp_path, genesis_cfg={"github": {"public_repo": "Expected"}})
    fpfile = tmp_path / "fp.txt"
    fpfile.write_text("\\bzorrik\\b\n")
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        if cmd[1] == "repo":
            return subprocess.CompletedProcess(cmd, 0, stdout="Owner/Other\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(fp.subprocess, "run", fake_run)
    ok = fp.sync_secret(path=fpfile, repo_root=Path("/x"), home=home)
    assert ok is False
    assert not any(c[1] == "secret" for c in calls)  # never attempted the set


def test_sync_secret_empty_patterns_skips(tmp_path, monkeypatch):
    fpfile = tmp_path / "fp.txt"
    fpfile.write_text("# only comments\n\n")
    called = []
    monkeypatch.setattr(
        fp.subprocess,
        "run",
        lambda *a, **k: called.append(a) or subprocess.CompletedProcess(a, 0, "", ""),
    )
    ok = fp.sync_secret(path=fpfile, repo_root=Path("/x"), home=tmp_path)
    assert ok is False
    assert not called  # no gh invoked when there is nothing to push


def test_sync_secret_no_gh_skips(tmp_path, monkeypatch):
    fpfile = tmp_path / "fp.txt"
    fpfile.write_text("\\bzorrik\\b\n")

    def boom(*a, **k):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(fp.subprocess, "run", boom)
    ok = fp.sync_secret(path=fpfile, repo_root=Path("/x"), home=tmp_path)
    assert ok is False  # degrades cleanly when gh is absent
