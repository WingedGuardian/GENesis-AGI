"""The branch leak scan's allowlist for owner-verified historical false positives.

scripts/ci/leak_history_allow.py drops a detect-secrets history finding only when
its commit, path, detector type and hash all match an entry read from
origin/main. These tests pin the four-field match, the fail-closed parse, and
the property that matters most: a branch cannot allowlist its own finding.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_CI = _ROOT / "scripts" / "ci"
sys.path.insert(0, str(_CI))
import leak_history_allow as lha  # noqa: E402

#: Synthetic; exists only in tmp_path scratch repos built by these tests.
_HEX_VALUE = "3c9a1f7e5d2b8a64f0e1c7d3b9a5e2f8c4d6a0b1e7f3c9d5a2b8e4f0c6d1a7b3"
_HASH = hashlib.sha1(_HEX_VALUE.encode()).hexdigest()
_C40 = "a" * 40


def _entry(**over) -> dict:
    e = {
        "commit": _C40,
        "path": "src/n.py",
        "type": "Hex High Entropy String",
        "sha1": _HASH,
        "reason": "synthetic",
    }
    e.update(over)
    return e


# ── parse: any malformed entry rejects the whole list ──────────────────


def test_a_valid_list_parses():
    assert lha.parse_allowlist(json.dumps({"entries": [_entry()]})) == [_entry()]


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        json.dumps([_entry()]),
        json.dumps({"entries": {}}),
        json.dumps({"entries": [], "extra": 1}),
        json.dumps({"entries": [{k: v for k, v in _entry().items() if k != "reason"}]}),
        json.dumps({"entries": [{**_entry(), "note": "x"}]}),
        json.dumps({"entries": [_entry(commit=_C40[:12])]}),
        json.dumps({"entries": [_entry(commit=_C40.upper())]}),
        json.dumps({"entries": [_entry(sha1="z" * 40)]}),
        json.dumps({"entries": [_entry(path="/etc/passwd")]}),
        json.dumps({"entries": [_entry(path="src/../x.py")]}),
        json.dumps({"entries": [_entry(reason=" ")]}),
        json.dumps({"entries": [_entry(type=1)]}),
        '{"entries": [], "entries": []}',
        json.dumps({"entries": [_entry()]})[:-3] + ', "sha1": "' + "f" * 40 + '"}]}',
        json.dumps({"entries": [_entry(type="Secret Keyword")]}),
        json.dumps({"entries": [_entry(reason="two\nlines")]}),
        json.dumps({"entries": [_entry(reason="::error::x")]}),
    ],
    ids=[
        "not-json",
        "not-object",
        "entries-not-list",
        "extra-key",
        "missing-field",
        "unknown-field",
        "short-commit",
        "upper-commit",
        "bad-hash",
        "absolute-path",
        "dotdot-path",
        "blank-reason",
        "non-string",
        "duplicate-top-key",
        "duplicate-entry-key",
        "non-entropy-detector",
        "multiline-reason",
        "workflow-command-reason",
    ],
)
def test_a_malformed_list_is_refused(text):
    with pytest.raises(lha.AllowlistError):
        lha.parse_allowlist(text)


# ── apply: only an exact four-field match is dropped ───────────────────


def _results(*pairs: tuple[str, dict]) -> dict:
    out: dict = {"results": {}}
    for filename, finding in pairs:
        out["results"].setdefault(filename, []).append(finding)
    return out


def _finding(**over) -> dict:
    f = {"type": "Hex High Entropy String", "hashed_secret": _HASH, "line_number": 1}
    f.update(over)
    return f


def test_an_exact_match_is_dropped_and_reported():
    results = _results((f"{_C40[:12]}/src/n.py", _finding()))
    dropped = lha.apply(results, [_entry()])
    assert results["results"] == {}
    assert [(f, r) for f, _, r in dropped] == [(f"{_C40[:12]}/src/n.py", "synthetic")]


@pytest.mark.parametrize(
    "filename,finding",
    [
        (f"{'b' * 12}/src/n.py", _finding()),  # another commit
        (f"{_C40[:12]}/src/m.py", _finding()),  # another path
        (f"{_C40[:12]}/src/n.py", _finding(type="Base64 High Entropy String")),
        (f"{_C40[:12]}/src/n.py", _finding(hashed_secret="c" * 40)),  # another value
    ],
    ids=["other-commit", "other-path", "other-detector", "other-value"],
)
def test_anything_short_of_an_exact_match_is_kept(filename, finding):
    results = _results((filename, finding))
    assert lha.apply(results, [_entry()]) == []
    assert results["results"] == {filename: [finding]}


def test_a_dropped_finding_leaves_its_neighbours():
    keep = _finding(hashed_secret="d" * 40, line_number=2)
    results = _results((f"{_C40[:12]}/src/n.py", _finding()), (f"{_C40[:12]}/src/n.py", keep))
    lha.apply(results, [_entry()])
    assert results["results"] == {f"{_C40[:12]}/src/n.py": [keep]}


# ── end to end through leak_scan.sh ─────────────────────────────────────

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "ci@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "ci@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}


class _Repo:
    """main, then a branch whose first commit adds a high-entropy value and whose
    second removes it: the tip is clean, the history is not."""

    def __init__(self, tmp_path: Path):
        self.root = tmp_path / "r"
        (self.root / "scripts" / "ci").mkdir(parents=True)
        for name in ("leak_scan.sh", "leak_scan_added_lines.py", "leak_history_allow.py"):
            shutil.copy(_CI / name, self.root / "scripts" / "ci" / name)
        self.env = {**os.environ, **_GIT_ENV, "HOME": str(self.root)}
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "main")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        self.git("checkout", "-q", "-b", "feature")
        (self.root / "src").mkdir()
        (self.root / "src" / "n.py").write_text(f'nonce = "{_HEX_VALUE}"\n', encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "add")
        self.leak_commit = self.git("rev-parse", "HEAD")
        self.git("rm", "-q", "src/n.py")
        self.git("commit", "-q", "-m", "remove")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.root, env=self.env, capture_output=True, text=True, check=True
        ).stdout.strip()

    def list_on(self, branch: str, text: str) -> None:
        """Commit the list on `branch` (main also moves origin/main), then return to feature."""
        if branch == "main":
            self.git("checkout", "-q", "main")
        (self.root / lha.ALLOW_PATH).write_text(text, encoding="utf-8")
        self.git("add", lha.ALLOW_PATH)
        self.git("commit", "-q", "-m", "allowlist")
        if branch == "main":
            self.git("update-ref", "refs/remotes/origin/main", "HEAD")
            self.git("checkout", "-q", "feature")

    def entry(self, finding_type: str) -> dict:
        return _entry(commit=self.leak_commit, type=finding_type)

    def scan(self) -> subprocess.CompletedProcess:
        env = {**self.env, "EVENT_NAME": "push", "LEAK_SCAN_RANGE": "branch"}
        # detect-secrets is a core dependency: the interpreter's bin dir has it.
        env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}"
        work = self.root.parent / "work"
        work.mkdir(exist_ok=True)
        env["LEAK_SCAN_WORKDIR"] = str(work)
        return subprocess.run(
            ["bash", str(self.root / "scripts" / "ci" / "leak_scan.sh"), "detect-secrets"],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
        )


def _finding_type(proc: subprocess.CompletedProcess) -> str:
    """The detector type the control run reported (read, not assumed)."""
    body = proc.stdout[proc.stdout.index("{") :]
    (findings,) = json.loads(body)["results"].values()
    return findings[0]["type"]


def test_control_the_history_finding_fails_the_scan(tmp_path):
    proc = _Repo(tmp_path).scan()
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "1 in the branch history" in proc.stdout
    assert _finding_type(proc) == "Hex High Entropy String"


def test_an_entry_on_origin_main_drops_exactly_that_finding(tmp_path):
    repo = _Repo(tmp_path)
    repo.list_on("main", json.dumps({"entries": [repo.entry("Hex High Entropy String")]}))
    proc = repo.scan()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "history allowlist: dropped Hex High Entropy String" in proc.stdout
    assert f"{repo.leak_commit[:12]}/src/n.py" in proc.stdout
    assert _HEX_VALUE not in proc.stdout + proc.stderr
    assert "Secret scan: CLEAN" in proc.stdout


def test_an_entry_only_on_the_branch_does_not_count(tmp_path):
    """The branch under scan cannot allowlist its own finding."""
    repo = _Repo(tmp_path)
    repo.list_on("feature", json.dumps({"entries": [repo.entry("Hex High Entropy String")]}))
    proc = repo.scan()
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "history allowlist: dropped" not in proc.stdout


def test_an_entry_for_another_value_does_not_count(tmp_path):
    repo = _Repo(tmp_path)
    other = {**repo.entry("Hex High Entropy String"), "sha1": "e" * 40}
    repo.list_on("main", json.dumps({"entries": [other]}))
    proc = repo.scan()
    assert proc.returncode == 1, proc.stdout + proc.stderr


def test_a_malformed_list_on_origin_main_fails_closed(tmp_path):
    repo = _Repo(tmp_path)
    repo.list_on("main", "{not json")
    proc = repo.scan()
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "Failing closed" in proc.stdout


def test_the_shipped_list_parses():
    """The repository's own list must stay valid: a malformed one fails every branch scan."""
    text = (_ROOT / lha.ALLOW_PATH).read_text(encoding="utf-8")
    assert lha.parse_allowlist(text)


#: gitleaks' default generic-api-key rule fires on one of these keywords near a
#: high-entropy value. The list sits outside detect-secrets' roots but inside
#: gitleaks' whole-tree scan, so none of its lines may carry one: a field named
#: `hashed_secret` once made the required leak-detector check fail.
_GITLEAKS_KEYWORDS = (
    "access",
    "api",
    "auth",
    "credential",
    "creds",
    "key",
    "passw",
    "secret",
    "token",
)


def test_the_shipped_list_carries_no_gitleaks_keyword():
    text = (_ROOT / lha.ALLOW_PATH).read_text(encoding="utf-8").lower()
    hits = [
        (n, w)
        for n, line in enumerate(text.splitlines(), 1)
        for w in _GITLEAKS_KEYWORDS
        if w in line
    ]
    assert hits == [], f"{lha.ALLOW_PATH}: gitleaks keywords on these lines {hits}"


# ── read_allowlist: every unreadable state fails closed ────────────────


def _fake_git(monkeypatch, replies: dict[str, tuple[int, str]]):
    def fake(*args):
        rc, out = replies.get(args[0], (0, ""))
        return subprocess.CompletedProcess(args, rc, out, "")

    monkeypatch.setattr(lha, "_git", fake)


def test_an_unresolvable_main_fails_closed(monkeypatch):
    _fake_git(monkeypatch, {"rev-parse": (1, "")})
    with pytest.raises(lha.AllowlistError, match="cannot be resolved"):
        lha.read_allowlist()


def test_an_unlistable_main_fails_closed(monkeypatch):
    _fake_git(monkeypatch, {"ls-tree": (128, "")})
    with pytest.raises(lha.AllowlistError, match="cannot list"):
        lha.read_allowlist()


def test_a_listed_but_unreadable_file_fails_closed(monkeypatch):
    _fake_git(monkeypatch, {"ls-tree": (0, lha.ALLOW_PATH + "\n"), "show": (128, "")})
    with pytest.raises(lha.AllowlistError, match="cannot read"):
        lha.read_allowlist()


def test_no_list_on_main_means_no_entries(monkeypatch):
    _fake_git(monkeypatch, {"ls-tree": (0, "")})
    assert lha.read_allowlist() is None


def test_the_list_is_read_from_the_remote_tracking_ref_only():
    """A tag or branch named `origin/main` must not shadow the remote ref."""
    assert lha.ALLOW_REF == "refs/remotes/origin/main"
