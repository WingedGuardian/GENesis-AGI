"""The credentials-file resolver: order, the bash twin's parity, and no new copies."""

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

# Each state: which of the three locations hold a file, plus an optional override.
STATES = [
    ("none", set(), None),
    ("legacy_checkout_only", {"checkout"}, None),
    ("legacy_home_only", {"home_legacy"}, None),
    ("both_legacy", {"checkout", "home_legacy"}, None),
    ("new_only", {"new"}, None),
    ("new_and_legacy", {"new", "checkout", "home_legacy"}, None),
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


def _expected(present: set[str], override: str | None, home: Path, checkout: Path, paths) -> Path:
    if override == "override":
        return paths["override"]
    if override:
        return Path(override).expanduser()
    for name in ("new", "checkout", "home_legacy"):
        if name in present:
            return paths[name]
    return checkout / "secrets.env"


def _environ(override: str | None, paths) -> dict[str, str]:
    if override == "override":
        return {"SECRETS_PATH": str(paths["override"])}
    if override:
        return {"SECRETS_PATH": override}
    return {}


@pytest.mark.parametrize(("name", "present", "override"), STATES, ids=[s[0] for s in STATES])
def test_python_resolver_order(tmp_path, monkeypatch, name, present, override):
    home, checkout, paths = _layout(tmp_path, present)
    monkeypatch.setenv("HOME", str(home))  # expanduser of a ~ override
    got = secrets_location.resolve(environ=_environ(override, paths), home=home, repo_root=checkout)
    assert got == _expected(present, override, home, checkout, paths)


@pytest.mark.parametrize(("name", "present", "override"), STATES, ids=[s[0] for s in STATES])
def test_bash_twin_matches_the_python_resolver(tmp_path, monkeypatch, name, present, override):
    home, checkout, paths = _layout(tmp_path, present)
    monkeypatch.setenv("HOME", str(home))  # the Python side expands ~ too
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    env.update(_environ(override, paths))
    out = subprocess.run(
        ["bash", "-c", f'. "{BASH_TWIN}"; genesis_secrets_path "$1"', "_", str(checkout)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    py = secrets_location.resolve(environ=_environ(override, paths), home=home, repo_root=checkout)
    assert Path(out) == py == _expected(present, override, home, checkout, paths)


def test_env_secrets_path_delegates_to_the_resolver(tmp_path, monkeypatch):
    home, checkout, paths = _layout(tmp_path, {"home_legacy"})
    monkeypatch.delenv("SECRETS_PATH", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(checkout))
    # A worktree's own root holds no file; the main-tree file under HOME wins.
    assert secrets_path() == paths["home_legacy"]


def test_xdg_config_home_is_ignored(tmp_path, monkeypatch):
    home, checkout, paths = _layout(tmp_path, {"new"})
    elsewhere = tmp_path / "xdg"
    (elsewhere / "genesis").mkdir(parents=True)
    (elsewhere / "genesis" / "secrets.env").write_text("K=x\n")
    environ = {"XDG_CONFIG_HOME": str(elsewhere)}
    assert secrets_location.resolve(environ=environ, home=home, repo_root=checkout) == paths["new"]


@pytest.mark.parametrize("user", ["genesis_nonexistent_test_user", "0"])
def test_unknown_override_user_fails_without_falling_back(tmp_path, user):
    override = f"~{user}/synthetic.env"
    with pytest.raises(RuntimeError):
        secrets_location.resolve(environ={"SECRETS_PATH": override}, home=tmp_path, repo_root=tmp_path)
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


# A reader that rebuilds the location itself instead of calling a resolver.
_COPY_PATTERNS = (
    re.compile(r"\$\{SECRETS_PATH:-"),
    re.compile(r"""os\.environ(?:\.get\(|\[)["']SECRETS_PATH["']"""),
    # Not the template: secrets.env.example legitimately lives in the checkout.
    re.compile(r"\$\{?(GENESIS_ROOT|GENESIS_DIR|GENESIS_REPO|REPO_DIR)\}?/secrets\.env(?![\w.])"),
)
# Files allowed to know the location: the resolvers, and sites a later PR moves.
_ALLOWED = {
    "src/genesis/secrets_location.py",
    "scripts/lib/secrets_path.sh",
    "src/genesis/env.py",
    "src/genesis/memory/graph_census_proof.py",  # mirrors ANOTHER process's environment
    # Moved onto the resolver by the follow-up PR (units, guardian, guard):
    "scripts/hooks/secrets_target.py",
    "scripts/hooks/rm_trash_advisory.py",
    "src/genesis/guardian/cred_integrity.py",
    "scripts/ci/update_rollback_e2e.sh",
    "scripts/payments_test_vault.py",
}


def test_no_reader_rebuilds_the_secrets_location():
    files = subprocess.run(
        ["git", "ls-files", "src", "scripts"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.split()
    offenders = []
    for rel in files:
        if rel in _ALLOWED or not rel.endswith((".py", ".sh", "")) or rel.endswith(".md"):
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
