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

Only a PLAIN remote name (``plain_name``: a letter or digit, then letters,
digits, ``.``, ``_`` or ``-``, forming a valid ref) can be the update remote. Git accepts far more
(a leading dash, Unicode separators, a name that shadows ``heads``), and every
caller passes the name to git as an argument and builds refs from it. A remote
with any other name is never chosen: an override or pin naming one is refused,
and so is the rule when such a remote names the public repository (it may be the
public remote, so falling back to ``origin`` would be a guess). Callers read ``refs/remotes/<remote>/main`` in full, never the
``<remote>/main`` shorthand, which a local branch can shadow.

Command line: ``python -m genesis.util.update_remote <checkout> [--pin]`` prints
the remote's name on the first line and how it was chosen on the second, and
exits 0; or prints why it refuses and exits 2.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

PIN_KEY = "genesis.updateRemote"
OVERRIDE_ENV = "GENESIS_UPDATE_REMOTE"


class UpdateRemoteError(RuntimeError):
    """The update remote cannot be chosen safely."""


_PLAIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def plain_name(name: str) -> bool:
    """Whether *name* may be the update remote (see the module docstring).

    Also excludes what the character class lets through but git's ref-format
    rules reject in ``refs/remotes/<name>/main``: ``..``, a trailing ``.``, and a
    ``.lock`` suffix.
    """
    return (
        bool(_PLAIN_NAME.fullmatch(name))
        and ".." not in name
        and not name.endswith((".", ".lock"))
    )


def tracking_ref(remote: str) -> str:
    """The fully qualified ref of *remote*'s main, which no local branch shadows."""
    return f"refs/remotes/{remote}/main"


def repo_name(url: str) -> str:
    """The repository name a remote URL ends in, without ``.git``, casefolded."""
    # No strip(): git accepts a local path ending in whitespace, and trimming it
    # could turn a private path into one that names the public repository.
    path = url.rstrip("/")
    head, sep, rest = path.partition(":")
    # scp-like `[user@]host:path`, by git's own rule: a colon with no slash
    # before it, in a URL with no scheme. A colon anywhere else is part of a
    # local path (`/srv/mirror:x.git`).
    if sep and "://" not in path and "/" not in head:
        path = rest
    tail = path.rsplit("/", 1)[-1].casefold()
    if tail.endswith(".git"):  # after casefold: a mirror may be named X.GIT
        tail = tail[: -len(".git")]
    return tail


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
            if not plain_name(name):
                raise UpdateRemoteError(
                    f"the {label} update remote {name!r} is not a plain remote name "
                    f"(a letter or digit, then letters, digits, '.', '_' or '-'); "
                    f"rename the remote (git remote rename) and pin the new name."
                )
            if name not in remotes:
                raise UpdateRemoteError(
                    f"the {label} update remote {name!r} is not a remote of this checkout "
                    f"(remotes: {', '.join(sorted(remotes)) or 'none'})."
                )
            return name, label
    want = public_repo.strip().casefold()
    matches = sorted(n for n, url in remotes.items() if repo_name(url) == want)
    candidates = [n for n in matches if plain_name(n)]
    if "origin" in candidates:
        return "origin", "origin"
    unusable = [n for n in matches if not plain_name(n)]
    if unusable:
        # It may be the public remote: falling back to origin (perhaps a
        # private repository) would be a guess, so refuse and say how to fix it.
        raise UpdateRemoteError(
            f"remote {', '.join(repr(n) for n in unusable)} names {public_repo} but is "
            f"not a plain remote name, so it cannot be the update source. Rename it "
            f"(git remote rename), then pin the public remote: git config {PIN_KEY} <remote>"
        )
    if not candidates:
        return "origin", "fallback"
    raise UpdateRemoteError(
        f"origin does not name {public_repo}, and {', '.join(candidates)} "
        f"{'does' if len(candidates) == 1 else 'do'}: a fork fetched for review "
        f"carries the same name, so choosing it would be a guess. If it is the "
        f"public repository, pin it once: git config {PIN_KEY} <remote>"
    )


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    # surrogateescape: a local remote path may hold bytes that are not UTF-8;
    # they round-trip into later git argv instead of raising UnicodeDecodeError.
    # Without the caller's GIT_* variables: a GIT_DIR or GIT_CONFIG_* inherited
    # from, say, a git hook would point every probe (and the pin) at another
    # repository, even with -C.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        errors="surrogateescape",
        timeout=30,
        env=env,
    )


def fetch_remotes(root: Path) -> dict[str, str]:
    """``{name: fetch url}`` for the checkout at *root*.

    Lists remote NAMES (``git remote``) and asks each for its fetch URL
    (``git remote get-url``), never splitting the human-readable ``-v`` output:
    a URL containing whitespace (a local path such as ``/srv/My Repos/x.git``)
    spans several fields there and would be silently dropped.
    """
    proc = _git(root, "remote")
    if proc.returncode != 0:
        raise UpdateRemoteError(f"cannot list the remotes of {root}: {proc.stderr.strip()}")
    remotes: dict[str, str] = {}
    # Split on git's LF only: str.splitlines() also breaks on Unicode separators
    # a remote name may contain, and strip() would change such a name.
    for name in proc.stdout.split("\n"):
        if not name:
            continue
        # `--`: a remote may be named like an option (`git remote add -- --all URL`).
        url = _git(root, "remote", "get-url", "--", name)
        if url.returncode != 0:
            raise UpdateRemoteError(
                f"cannot read the fetch URL of remote {name!r}: {url.stderr.strip()}"
            )
        # Exactly the one LF git terminates the record with; any other trailing
        # whitespace belongs to the URL.
        remotes[name] = url.stdout[:-1] if url.stdout.endswith("\n") else url.stdout
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
