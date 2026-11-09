"""Shared public-repository identity and native destination spellings."""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

_SCP_PUSH_URL_RE = re.compile(r"^[^/@:]+@[^/:]+:")
_URL_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):")
_ALLOWED_PUSH_URL_SCHEMES = frozenset({"https", "http", "ssh", "git", "file"})


def _normalize_repo(value: str) -> str | None:
    """A plain github.com OWNER/REPO from a gh --repo value, or None if it cannot
    be resolved to one.

    Accepts ``owner/repo``, ``github.com/owner/repo``, and a github.com URL.
    Returns None for a shell variable (``$X`` / backtick), an enterprise
    ``HOST/OWNER/REPO`` (would misnormalize to github.com and gate the wrong
    repo), or any other shape — the caller turns None-when-present into a
    fail-closed block. Genesis is github.com-only, so refusing the exotic forms
    is safe (a user can re-issue as OWNER/REPO)."""
    if not value or "$" in value or "`" in value:
        return None
    v = value.strip().split("://", 1)[-1]  # strip scheme if a URL
    parts = [p for p in v.split("/") if p]
    # Drop a leading github.com HOST COMPONENT (exact match of the first path
    # segment — NOT a substring test, which would mishandle `github.com.evil/…`
    # or `evilgithub.com/…`; CodeQL py/incomplete-url-substring-sanitization).
    if parts and parts[0] == "github.com":
        parts = parts[1:]
    if len(parts) != 2:  # not a plain OWNER/REPO (host-prefixed / malformed)
        return None
    return f"{parts[0]}/{parts[1]}"


def _repo_identity_from_url(url: str) -> tuple[str, str] | None:
    """``(host, owner/repo)`` for a git remote URL, or None if it has neither.

    Normalises the spellings of one remote so they compare equal: `https://`,
    bare `git@host:owner/repo`, `ssh://git@host/owner/repo`, and a trailing
    `.git` or `/` all reduce to the same pair. Any userinfo before the host is
    dropped, since `git@` is not part of the destination's identity.
    """
    raw = url.strip().lower().rstrip("/")
    raw = raw.removesuffix(".git")
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    elif ":" in raw and "/" not in raw.split(":", 1)[0]:
        raw = raw.replace(":", "/", 1)            # git@host:owner/repo
    if "@" in raw.split("/", 1)[0]:
        raw = raw.split("@", 1)[1]                # strip userinfo
    parts = [x for x in raw.split("/") if x]
    if len(parts) < 3:
        return None
    host = parts[0].split(":", 1)[0]              # drop any :port
    return host, "/".join(parts[-2:])


def _push_url_transport(url: str) -> str | None:
    """Classify only built-in push transports and local paths."""
    if not url or url != url.strip() or "\n" in url or "\r" in url:
        return None
    if url.startswith(("/", "./", "../", "~")) or re.match(r"^[A-Za-z]:[\\/]", url):
        return "local"
    if _SCP_PUSH_URL_RE.match(url):
        return "ssh"
    scheme = _URL_SCHEME_RE.match(url)
    if scheme is not None:
        name = scheme.group(1).lower()
        if name not in _ALLOWED_PUSH_URL_SCHEMES:
            return None
        return name if url.lower().startswith(f"{name}://") else None
    if ":" in url:
        return None
    return "local"


def public_repo_config(*, limit: int | None = None) -> tuple[str, str | None, str]:
    """Return declared/undeclared/unknown, identity and an opaque config digest."""
    path = Path(os.path.expanduser("~/.genesis/config/genesis.yaml"))
    try:
        with path.open("rb") as stream:
            raw = stream.read(-1 if limit is None else limit + 1)
    except FileNotFoundError:
        return "undeclared", None, "missing"
    except OSError:
        return "unknown", None, "unreadable"
    digest = hashlib.sha256(raw).hexdigest()
    if limit is not None and len(raw) > limit:
        return "unknown", None, "oversize-" + digest
    try:
        import yaml

        cfg = yaml.safe_load(raw)
        cfg = {} if cfg is None else cfg
        gh = cfg.get("github")
        gh = {} if gh is None else gh
        user, repo = gh.get("user"), gh.get("public_repo")
        user = "" if user is None else user.strip()
        repo = "" if repo is None else repo.strip()
        if not user and not repo:
            return "undeclared", None, digest
        canonical = _normalize_repo(f"{user}/{repo}") if user and repo else None
        return ("declared" if canonical else "unknown"), canonical, digest
    except Exception:
        return "unknown", None, digest
