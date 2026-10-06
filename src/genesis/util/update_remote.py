"""Which git remote an install takes its updates from.

``scripts/update.sh`` fetches and merges ``<remote>/main`` and then installs and
runs what it got, and the version collector
(``genesis.learning.signals.genesis_version``) reports how far the install is
behind the same ref. So the choice of remote decides what code the install runs,
and it must not move when somebody adds a remote: fetching a contributor's fork
to review their pull request is routine, and a fork carries the same repository
name.

The rule, first match wins:

1. ``GENESIS_UPDATE_REMOTE``, for one run;
2. the remote pinned in the checkout's OWN git config (``genesis.updateRemote``,
   read with ``--local``, so a global or system entry is never taken as a pin);
3. ``origin``, when its URL names the public repository (``github_public_repo``):
   it is what the install was cloned from;
4. ``origin`` when no remote names the public repository, as before;
5. otherwise a remote other than ``origin`` names it: refuse, and say how to
   choose. That includes a LONE such remote. In the setup where ``origin`` is the
   private repository and another remote is the public one, a fork fetched to
   review a pull request carries the public repository's name too, so a lone
   match is not evidence of anything; the operator pins the public remote once
   (``git config genesis.updateRemote <remote>``).

``update.sh`` pins what rules 3-4 chose (``--pin``): both are ``origin``, which the
install was cloned from, so a remote added later cannot change the source,
whatever its name. A refusal pins nothing. A repository name is compared as a
whole last path component, case-insensitively, never as a substring.

Command line: ``python -m genesis.util.update_remote <checkout> [--pin]`` prints
the remote's name on the first line and how it was chosen on the second, and
exits 0; or prints why it refuses and exits 2.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PIN_KEY = "genesis.updateRemote"
OVERRIDE_ENV = "GENESIS_UPDATE_REMOTE"


class UpdateRemoteError(RuntimeError):
    """The update remote cannot be chosen safely."""


def repo_name(url: str) -> str:
    """The repository name a remote URL ends in, without ``.git``, casefolded."""
    tail = url.strip().rstrip("/").rsplit("/", 1)[-1]
    if ":" in tail:  # scp-like host:repo with no owner path
        tail = tail.rsplit(":", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[: -len(".git")]
    return tail.casefold()


def select(
    remotes: dict[str, str],
    public_repo: str,
    *,
    override: str | None = None,
    pinned: str | None = None,
) -> tuple[str, str]:
    """Return ``(remote, how)``; ``how`` is one of override, pinned, origin,
    fallback. Raises UpdateRemoteError when the choice would be a guess."""
    for label, name in (("override", override), ("pinned", pinned)):
        if name:
            if name not in remotes:
                raise UpdateRemoteError(
                    f"the {label} update remote {name!r} is not a remote of this checkout "
                    f"(remotes: {', '.join(sorted(remotes)) or 'none'})."
                )
            return name, label
    want = public_repo.strip().casefold()
    candidates = sorted(n for n, url in remotes.items() if repo_name(url) == want)
    if "origin" in candidates:
        return "origin", "origin"
    if not candidates:
        return "origin", "fallback"
    raise UpdateRemoteError(
        f"origin does not name {public_repo}, and {', '.join(candidates)} "
        f"{'does' if len(candidates) == 1 else 'do'}: a fork fetched for review "
        f"carries the same name, so choosing it would be a guess. If it is the "
        f"public repository, pin it once: git config {PIN_KEY} <remote>"
    )


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=30
    )


def fetch_remotes(root: Path) -> dict[str, str]:
    """``{name: fetch url}`` for the checkout at *root*."""
    proc = _git(root, "remote", "-v")
    if proc.returncode != 0:
        raise UpdateRemoteError(f"cannot list the remotes of {root}: {proc.stderr.strip()}")
    remotes: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == "(fetch)":
            remotes[parts[0]] = parts[1]
    return remotes


def pinned_remote(root: Path) -> str | None:
    """The pin in this checkout's own config; global and system scope are ignored."""
    proc = _git(root, "config", "--local", "--get", PIN_KEY)
    value = proc.stdout.strip() if proc.returncode == 0 else ""
    return value or None


def update_remote(root: Path, public_repo: str | None = None, *, pin: bool = False) -> str:
    """The remote to update from."""
    return resolve(root, public_repo, pin=pin)[0]


def resolve(
    root: Path, public_repo: str | None = None, *, pin: bool = False
) -> tuple[str, str]:
    """``(remote, how)``. With *pin*, record a choice the rule made from origin
    (never one from the override or an existing pin)."""
    if public_repo is None:
        from genesis.env import github_public_repo

        public_repo = github_public_repo()
    remotes = fetch_remotes(root)
    name, how = select(
        remotes,
        public_repo,
        override=os.environ.get(OVERRIDE_ENV, "").strip() or None,
        pinned=pinned_remote(root),
    )
    if pin and how in ("origin", "fallback") and name in remotes:
        proc = _git(root, "config", "--local", PIN_KEY, name)
        if proc.returncode != 0:
            raise UpdateRemoteError(f"cannot pin the update remote: {proc.stderr.strip()}")
    return name, how


def main(argv: list[str]) -> int:
    args = [a for a in argv if a != "--pin"]
    if len(args) != 1:
        print("usage: python -m genesis.util.update_remote <checkout> [--pin]")
        return 2
    try:
        name, how = resolve(Path(args[0]), pin="--pin" in argv)
        print(name)
        print(how)
    except UpdateRemoteError as exc:
        print(exc)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
