"""The credentials-file resolver: its order, its twins' parity, and no new copies."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from genesis import secrets_location
from genesis.env import secrets_path

REPO = Path(__file__).resolve().parents[1]
BASH_TWIN = REPO / "scripts" / "lib" / "secrets_path.sh"

# Each state: which locations hold a file, plus an optional override.
STATES = [
    ("none", set(), None),
    ("legacy_checkout_only", {"checkout"}, None),
    ("legacy_home_only", {"home_legacy"}, None),
    ("both_legacy", {"checkout", "home_legacy"}, None),
    ("home_config_only", {"new"}, None),
    ("home_config_and_legacy", {"new", "checkout", "home_legacy"}, None),
    ("override_absent", {"new", "checkout"}, "override"),
    ("override_tilde", {"checkout"}, "~/custom/secrets.env"),
    ("override_home", {"checkout"}, "~"),
    ("override_named_user", {"checkout"}, "~root/synthetic.env"),
]


def _layout(tmp_path: Path, present: set[str]) -> tuple[Path, Path, dict[str, Path]]:
    home = tmp_path / "home"
    checkout = tmp_path / "checkout"
    paths = {
        "new": home / ".config" / "genesis" / "secrets.env",
        "home_legacy": home / "genesis" / "secrets.env",
        "checkout": checkout / "secrets.env",
        "override": tmp_path / "elsewhere" / "secrets.env",
    }
    checkout.mkdir(parents=True)
    home.mkdir(parents=True)
    for name in present:
        paths[name].parent.mkdir(parents=True, exist_ok=True)
        paths[name].write_text("K=v\n")
    return home, checkout, paths


def _expected(present: set[str], override: str | None, paths: dict[str, Path]) -> Path:
    if override == "override":
        return paths["override"]
    if override:
        return Path(override).expanduser()
    for name in ("new", "checkout", "home_legacy"):
        if name in present:
            return paths[name]
    return paths["checkout"]


def _environ(override: str | None, paths: dict[str, Path]) -> dict[str, str]:
    if override == "override":
        return {"SECRETS_PATH": str(paths["override"])}
    if override:
        return {"SECRETS_PATH": override}
    return {}


@pytest.mark.parametrize(("name", "present", "override"), STATES, ids=[s[0] for s in STATES])
def test_python_resolver_order(tmp_path, monkeypatch, name, present, override):
    home, checkout, paths = _layout(tmp_path, present)
    monkeypatch.setenv("HOME", str(home))  # expanduser of a ~ override
    environ = {"HOME": str(home), **_environ(override, paths)}
    got = secrets_location.resolve(environ=environ, repo_root=checkout)
    assert got == _expected(present, override, paths)
    # An explicit home argument gives the same answer as HOME in the environment.
    assert (
        secrets_location.resolve(environ=_environ(override, paths), repo_root=checkout, home=home)
        == got
    )


@pytest.mark.parametrize(("name", "present", "override"), STATES, ids=[s[0] for s in STATES])
def test_bash_twin_matches_the_python_resolver(tmp_path, monkeypatch, name, present, override):
    home, checkout, paths = _layout(tmp_path, present)
    monkeypatch.setenv("HOME", str(home))  # the Python side expands ~ too
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    env.update(_environ(override, paths))
    # Both paths travel as arguments, never interpolated into the script text.
    out = subprocess.run(
        ["bash", "-c", '. "$1"; genesis_secrets_path "$2"', "_", str(BASH_TWIN), str(checkout)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    py = secrets_location.resolve(environ=env, repo_root=checkout)
    assert Path(out) == py == _expected(present, override, paths)


@pytest.mark.parametrize(("name", "present", "override"), STATES, ids=[s[0] for s in STATES])
def test_guardian_twin_matches_the_python_resolver(tmp_path, monkeypatch, name, present, override):
    from genesis.guardian.cred_integrity import resolve_secrets_path

    home, checkout, paths = _layout(tmp_path, present)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(checkout))
    monkeypatch.delenv("SECRETS_PATH", raising=False)
    for key, value in _environ(override, paths).items():
        monkeypatch.setenv(key, value)
    assert resolve_secrets_path(home) == _expected(present, override, paths)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
@pytest.mark.parametrize("locked_part", [".config", ".config/genesis"])
def test_an_unsearchable_home_config_is_kept_by_all_three(tmp_path, monkeypatch, locked_part):
    # A file that may exist but cannot be checked is never skipped: Guardian
    # would restore a copy beside a live file, and a reader would silently load
    # an older legacy copy. All three copies keep it, so the reader fails loudly.
    from genesis.guardian.cred_integrity import resolve_secrets_path

    home, checkout, paths = _layout(tmp_path, {"new", "checkout"})
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(checkout))
    monkeypatch.delenv("SECRETS_PATH", raising=False)
    locked = home / locked_part
    locked.chmod(0)
    try:
        env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        out = subprocess.run(
            ["bash", "-c", '. "$1"; genesis_secrets_path "$2"', "_", str(BASH_TWIN), str(checkout)],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        py = secrets_location.resolve(environ=env, repo_root=checkout)
        assert Path(out) == py == resolve_secrets_path(home) == paths["new"]
    finally:
        locked.chmod(0o700)


@pytest.mark.parametrize("kind", ["dangling_symlink", "directory"])
def test_a_non_file_home_config_falls_through_in_all_three(tmp_path, monkeypatch, kind):
    from genesis.guardian.cred_integrity import resolve_secrets_path

    home, checkout, paths = _layout(tmp_path, {"checkout"})
    paths["new"].parent.mkdir(parents=True)
    if kind == "dangling_symlink":
        paths["new"].symlink_to(tmp_path / "nowhere")
    else:
        paths["new"].mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(checkout))
    monkeypatch.delenv("SECRETS_PATH", raising=False)
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    out = subprocess.run(
        ["bash", "-c", '. "$1"; genesis_secrets_path "$2"', "_", str(BASH_TWIN), str(checkout)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    py = secrets_location.resolve(environ=env, repo_root=checkout)
    assert Path(out) == py == resolve_secrets_path(home) == paths["checkout"]


@pytest.mark.parametrize("present", [False, True])
def test_a_relative_repo_root_resolves_without_hanging(tmp_path, monkeypatch, present):
    # A relative root (an unexpected GENESIS_DIR) must neither hang the bash
    # twin's walk-up nor disagree with the Python resolver.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    if present:
        (tmp_path / "rel").mkdir()
        (tmp_path / "rel" / "secrets.env").write_text("K=v\n")
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    out = subprocess.run(
        ["bash", "-c", '. "$1"; genesis_secrets_path "$2"', "_", str(BASH_TWIN), "rel"],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout.strip()
    py = secrets_location.resolve(environ=env, repo_root=Path("rel"))
    expected = Path("rel/secrets.env")
    assert Path(out) == py == expected


def test_env_secrets_path_delegates_to_the_resolver(tmp_path, monkeypatch):
    home, checkout, paths = _layout(tmp_path, {"home_legacy"})
    monkeypatch.delenv("SECRETS_PATH", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(checkout))
    # A worktree's own root holds no file; the main-tree file under HOME wins.
    assert secrets_path() == paths["home_legacy"]


def test_xdg_config_home_is_ignored(tmp_path, monkeypatch):
    home, checkout, paths = _layout(tmp_path, {"new"})
    monkeypatch.setenv("HOME", str(home))
    elsewhere = tmp_path / "xdg"
    (elsewhere / "genesis").mkdir(parents=True)
    (elsewhere / "genesis" / "secrets.env").write_text("K=x\n")
    environ = {"HOME": str(home), "XDG_CONFIG_HOME": str(elsewhere)}
    assert secrets_location.resolve(environ=environ, repo_root=checkout) == paths["new"]


@pytest.mark.parametrize("user", ["genesis_nonexistent_test_user", "0"])
def test_unknown_override_user_fails_without_falling_back(tmp_path, user):
    override = f"~{user}/synthetic.env"
    with pytest.raises(RuntimeError):
        secrets_location.resolve(environ={"SECRETS_PATH": override}, repo_root=tmp_path)
    result = subprocess.run(
        ["bash", "-c", '. "$1"; genesis_secrets_path "$2"', "_", str(BASH_TWIN), str(tmp_path)],
        env={"HOME": str(tmp_path), "PATH": os.environ["PATH"], "SECRETS_PATH": override},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert not result.stdout


def test_the_resolver_module_imports_nothing_from_genesis():
    # Scripts load it by path before the package is importable.
    source = (REPO / "src" / "genesis" / "secrets_location.py").read_text()
    assert not re.search(r"^\s*(from|import)\s+genesis\b", source, re.M)


# A reader or writer that rebuilds the location itself instead of calling a resolver.
_COPY_PATTERNS = (
    re.compile(r"\$\{SECRETS_PATH:-"),
    re.compile(r"""os\.environ(?:\.get\(|\[)["']SECRETS_PATH["']"""),
    # Another process's environment, read by hand (how graph_census_proof slipped by).
    re.compile(r"""\benv\.get\(["']SECRETS_PATH["']"""),
    re.compile(r"""os\.getenv\(["']SECRETS_PATH["']"""),
    # Not the template: secrets.env.example legitimately lives in the checkout.
    re.compile(r"\$\{?(GENESIS_ROOT|GENESIS_DIR|GENESIS_REPO|REPO_DIR)\}?/secrets\.env(?![\w.])"),
)
# Files allowed to know the location, each for a stated reason.
_ALLOWED = {
    "src/genesis/secrets_location.py",  # the resolver
    # Its standalone twin: the host pipes this file into the container's system
    # python, so it cannot import the resolver. The state table above keeps it
    # in step.
    "src/genesis/guardian/cred_integrity.py",
    # Read ANOTHER process's environment from /proc (a memory server's), then
    # hand it to the resolver as that process would: graph_census_proof.secrets_file.
    "src/genesis/memory/graph_census.py",
    "src/genesis/memory/graph_census_proof.py",
    "scripts/lib/secrets_path.sh",  # its bash twin
    # The checkout's file on purpose: the hook-audit stores' writers read their
    # directories from it, so disk hygiene must read the same one.
    "scripts/disk_hygiene.sh",
    # Keep their own main-tree fallback for worktree runs until the follow-up
    # PR gives the resolver a read mode that has one.
    "src/genesis/eval/longmemeval/client.py",
    "src/genesis/eval/reflection_golden_set.py",
}


def test_no_reader_rebuilds_the_secrets_location():
    files = subprocess.run(
        ["git", "ls-files", "src", "scripts"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.split()
    offenders = []
    for rel in files:
        # Extensionless files (gmodel) are scanned too; only prose is skipped.
        if rel in _ALLOWED or rel.endswith(".md"):
            continue
        path = REPO / rel
        try:
            text = path.read_text()
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if any(p.search(line) for p in _COPY_PATTERNS):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, "use genesis.env.secrets_path() / genesis_secrets_path:\n" + "\n".join(
        offenders
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
@pytest.mark.parametrize(
    ("which", "link"),
    [
        ("new", "absolute"),
        ("new", "relative"),
        ("new", "chain"),
        ("new", "missing_target"),
        ("checkout", "absolute"),
    ],
)
def test_a_candidate_symlinked_into_an_unsearchable_dir_agrees(tmp_path, monkeypatch, which, link):
    # Python's stat follows the link and cannot check the target, so it keeps
    # the candidate; the bash twin must walk from the TARGET, not the link's
    # own (searchable) parent, or it falls back to an older legacy copy.
    from genesis.guardian.cred_integrity import resolve_secrets_path

    home, checkout, paths = _layout(tmp_path, {"home_legacy"})
    locked = tmp_path / "locked"
    locked.mkdir()
    target = locked / "secrets.env"
    if link != "missing_target":
        target.write_text("K=v\n")
    candidate = paths[which]
    candidate.parent.mkdir(parents=True, exist_ok=True)
    if link == "relative":
        candidate.symlink_to(os.path.relpath(target, candidate.parent))
    elif link == "chain":
        hop = tmp_path / "hop"
        hop.symlink_to(target)
        candidate.symlink_to(hop)
    else:
        candidate.symlink_to(target)
    locked.chmod(0)
    try:
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("GENESIS_REPO_ROOT", str(checkout))
        monkeypatch.delenv("SECRETS_PATH", raising=False)
        env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
        out = subprocess.run(
            ["bash", "-c", '. "$1"; genesis_secrets_path "$2"', "_", str(BASH_TWIN), str(checkout)],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        py = secrets_location.resolve(environ=env, repo_root=checkout)
        assert Path(out) == py == resolve_secrets_path(home) == candidate
    finally:
        locked.chmod(0o700)
