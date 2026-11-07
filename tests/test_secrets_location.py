"""The credentials-file resolver: today's order, the bash twin's parity, and no new copies."""

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

# Each state: whether the checkout holds a file, plus an optional override.
STATES = [
    ("none", False, None),
    ("checkout_file", True, None),
    ("override_absent", True, "override"),
    ("override_tilde", True, "~/custom/secrets.env"),
    ("override_home", True, "~"),
    ("override_named_user", True, "~root/synthetic.env"),
]


def _layout(tmp_path: Path, checkout_file: bool) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    checkout = tmp_path / "checkout"
    home.mkdir()
    checkout.mkdir()
    # A file under HOME's canonical checkout and under ~/.config must NOT be
    # chosen: 1a keeps today's rule (SECRETS_PATH, else this checkout's file).
    for decoy in (home / "genesis" / "secrets.env", home / ".config" / "genesis" / "secrets.env"):
        decoy.parent.mkdir(parents=True)
        decoy.write_text("DECOY=1\n")
    if checkout_file:
        (checkout / "secrets.env").write_text("K=v\n")
    return home, checkout, tmp_path / "elsewhere" / "secrets.env"


def _environ(override: str | None, elsewhere: Path) -> dict[str, str]:
    if override == "override":
        return {"SECRETS_PATH": str(elsewhere)}
    if override:
        return {"SECRETS_PATH": override}
    return {}


def _expected(override: str | None, checkout: Path, elsewhere: Path) -> Path:
    if override == "override":
        return elsewhere
    if override:
        return Path(override).expanduser()
    return checkout / "secrets.env"


@pytest.mark.parametrize(("name", "checkout_file", "override"), STATES, ids=[s[0] for s in STATES])
def test_python_resolver_order(tmp_path, monkeypatch, name, checkout_file, override):
    home, checkout, elsewhere = _layout(tmp_path, checkout_file)
    monkeypatch.setenv("HOME", str(home))  # expanduser of a ~ override
    got = secrets_location.resolve(environ=_environ(override, elsewhere), repo_root=checkout)
    assert got == _expected(override, checkout, elsewhere)


@pytest.mark.parametrize(("name", "checkout_file", "override"), STATES, ids=[s[0] for s in STATES])
def test_bash_twin_matches_the_python_resolver(tmp_path, monkeypatch, name, checkout_file, override):
    home, checkout, elsewhere = _layout(tmp_path, checkout_file)
    monkeypatch.setenv("HOME", str(home))  # the Python side expands ~ too
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    env.update(_environ(override, elsewhere))
    # Both paths travel as arguments, never interpolated into the script text.
    out = subprocess.run(
        ["bash", "-c", '. "$1"; genesis_secrets_path "$2"', "_", str(BASH_TWIN), str(checkout)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    py = secrets_location.resolve(environ=_environ(override, elsewhere), repo_root=checkout)
    assert Path(out) == py == _expected(override, checkout, elsewhere)


def test_env_secrets_path_is_this_checkouts_file(tmp_path, monkeypatch):
    home, checkout, _ = _layout(tmp_path, False)
    monkeypatch.delenv("SECRETS_PATH", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(checkout))
    # Even with no file here and one under HOME's checkout: this checkout's path.
    assert secrets_path() == checkout / "secrets.env"


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
    # Not the template: secrets.env.example legitimately lives in the checkout.
    re.compile(r"\$\{?(GENESIS_ROOT|GENESIS_DIR|GENESIS_REPO|REPO_DIR)\}?/secrets\.env(?![\w.])"),
)
# Files allowed to know the location, each for a stated reason.
_ALLOWED = {
    "src/genesis/secrets_location.py",  # the resolver
    "scripts/lib/secrets_path.sh",  # its bash twin
    # The checkout's file on purpose: the hook-audit stores' writers read their
    # directories from it, so disk hygiene must read the same one.
    "scripts/disk_hygiene.sh",
    # Keep their own main-tree fallback for worktree runs until the follow-up
    # PR gives the resolver a read mode that has one.
    "src/genesis/eval/longmemeval/client.py",
    "src/genesis/eval/reflection_golden_set.py",
    # A writer: it moves onto the resolver's write mode in the follow-up PR.
    "scripts/bootstrap.sh",
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
