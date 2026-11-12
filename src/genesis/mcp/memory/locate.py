"""locate — deterministic self-retrieval over Genesis's known roots (Track 5.1).

A Genesis-owned MCP tool that surfaces recently-changed files (by mtime) across
known roots — plans, output, the repo, git worktrees — filtered by time window,
file type, scope, name glob, and optional content match, ranked by recency.

It exists because CC's Grep/Glob are flaky under the open-agents A/B bug; this is
a deterministic alternative for "what changed recently / where is X?".

STRICTLY READ-ONLY: only `os.scandir`/`stat`/`open`-for-read and `rg` (itself
read-only) are used. Nothing here writes, creates, or deletes.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from genesis import env
from genesis.security.credential_names import is_credential_component
from genesis.security.output_scanner import LOCATION_FINDINGS, scan_outbound_normalised
from genesis.session_awareness.zero_drop_git import scrubbed_git_env

from ..memory import mcp

logger = logging.getLogger(__name__)

# Suffixes treated as code/config for the file_type filter (yaml/json/toml count
# as code/config; documented in the tool docstring).
_CODE_EXTS = frozenset({
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".sh", ".bash", ".zsh",
    ".sql", ".c", ".h", ".cpp", ".hpp", ".cc", ".java", ".rb", ".php", ".lua",
    ".yaml", ".yml", ".toml", ".cfg", ".ini",
})

# Directory names whose entire subtree is skipped (build/dep/churn/binary noise).
# "worktrees" prunes standard worktree subdirectories; linked worktrees anywhere are
# also dynamically pruned via git worktree list --porcelain in `_walk`.
_PRUNE_DIRS = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", ".cache", "dist", "build", ".next", ".turbo",
    "target", "worktrees", "worktree-trash", "background-sessions", "cc-tmp",
    "embedding_cache", "qdrant_storage", "browser-profile", "camoufox-profile",
    "paste-cache", "file-history", "backups", "downloads",
})

# Directory names whose files are never content-searched (still found by name):
# tool config and auth state (gh's hosts.yml carries a token; ssh, cloud and
# registry credentials live in the others).
_CREDENTIAL_DIRS = frozenset({".config", ".ssh", ".aws", ".docker", ".gnupg", ".kube"})

# Content-search additions to the shared credential-name predicate. The
# dashboard's vocabulary only gates which paths it serves; here a missed name
# means its contents are returned, so names ending in these words or carrying
# these extensions are not content-searched either, unless the file's bytes
# equal its blob on the public repo's main (it went through CI's leak scan; see
# _tracked_files), and on a live install these words
# otherwise skipped 12 ordinary source files (runtime/init/secrets.py and the
# like) from code search.
_LOCATE_CREDENTIAL_STEMS = frozenset({"secret", "secrets", "key", "keys", "apikey"})
_LOCATE_CREDENTIAL_SUFFIXES = (".secret", ".tfvars", ".s3cfg", ".boto", ".dockercfg")
_LOCATE_CREDENTIAL_NAMES = frozenset({"kubeconfig", "auth.json", "cookies.txt"})

# Values from the secrets file that mark a file as holding a credential. A
# value under a secret-named key counts from this length: measured on a live
# install, the secret-named values of 6+ characters occurred in no tracked or
# working-doc file, while a 4-character one occurred in 323 (an ordinary
# substring). A value under any other key counts only when it looks random,
# from 12 characters.
_MIN_NAMED_SECRET_LEN = 6
_MIN_SECRET_VALUE_LEN = 12
_SECRETS_FILE_MAX_BYTES = 1_048_576

# Per-file glob skips (binary/churn artifacts not worth surfacing).
_SKIP_FILE_GLOBS = ("*.db", "*.db-wal", "*.db-shm", "*.sqlite", "*.sqlite3", "*.lock")


_MAX_CONTENT_BYTES = 1_048_576       # 1 MiB — matches `rg --max-filesize 1M`
_BINARY_SNIFF_BYTES = 8192
_MAX_FILES_SCANNED = 50_000          # pathological-walk backstop
_CONTENT_POOL_CAP = 5_000            # most-recent N considered for content match
_HARD_LIMIT = 200                    # absolute cap on returned results
_SNIPPET_CHARS = 200
_MAX_MATCHES_PER_FILE = 10
_RG_CHUNK = 1_000                    # paths per rg invocation (ARG_MAX safety)

_VALID_SCOPES = ("docs", "plans", "output", "repo", "worktrees", "all")
_VALID_TYPES = ("any", "code", "noncode")


# ── small helpers ─────────────────────────────────────────────────────────────


def _have_rg() -> bool:
    """True if ripgrep is on PATH. Monkeypatchable in tests."""
    return shutil.which("rg") is not None


def _parse_window(within: str) -> float | None:
    """Parse a time-window token into seconds, or None for 'no limit'.

    Accepts ``Nh`` (hours), ``Nd`` (days), bare ``N`` (days), and
    ``0`` / ``""`` / ``all`` / ``any`` (no limit). Raises ValueError on garbage.
    """
    token = (within or "").strip().lower()
    if token in ("", "0", "all", "any"):
        return None
    if token.endswith("h"):
        seconds = float(token[:-1]) * 3600
    elif token.endswith("d"):
        seconds = float(token[:-1]) * 86400
    else:
        seconds = float(token) * 86400  # bare number = days; ValueError if non-numeric
    if seconds < 0:
        raise ValueError(f"negative time window: {within!r}")
    return seconds


def _humanize_age(mtime_epoch: float, now_epoch: float) -> str:
    diff = max(0.0, now_epoch - mtime_epoch)
    if diff < 60:
        return "just now"
    if diff < 3600:
        return f"{int(diff // 60)}m ago"
    if diff < 86400:
        return f"{int(diff // 3600)}h ago"
    return f"{int(diff // 86400)}d ago"


def _display(p: Path) -> str:
    s = str(p)
    home = str(Path.home())
    return "~" + s[len(home):] if s.startswith(home) else s


def _list_worktrees(repo_root: Path) -> list[Path]:
    """Worktree paths (excluding the main worktree) via git porcelain.

    Catches worktrees at every location (siblings, nested under the repo, /tmp).
    Returns [] on any error (not a git repo, git absent, timeout).
    """
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True, cwd=str(repo_root), timeout=10,
            env=scrubbed_git_env(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    if result.returncode != 0:
        return []
    paths: list[Path] = []
    is_first = True
    for line_bytes in result.stdout.splitlines():
        if line_bytes.startswith(b"worktree "):
            path_bytes = line_bytes[len(b"worktree "):]
            if is_first:
                is_first = False  # first entry is the main worktree — skip
            else:
                paths.append(Path(os.fsdecode(path_bytes)))
    return paths


def _resolve_roots(scope: str) -> list[tuple[str, Path]]:
    """Map a scope to (label, path) roots. Raises ValueError on unknown scope."""
    plans = ("plans", env.plans_dir())
    output = ("output", env.output_dir())
    repo = ("repo", env.repo_root())
    if scope == "plans":
        return [plans]
    if scope == "output":
        return [output]
    if scope == "docs":
        return [plans, output]
    if scope == "repo":
        return [repo]
    if scope == "worktrees":
        return [(f"worktree:{p.name}", p) for p in _list_worktrees(env.repo_root())]
    if scope == "all":
        wts = [(f"worktree:{p.name}", p) for p in _list_worktrees(env.repo_root())]
        return [plans, output, repo, *wts]
    raise ValueError(f"unknown scope: {scope!r}")


def _matches_skip_glob(name: str) -> bool:
    return any(fnmatch.fnmatch(name, g) for g in _SKIP_FILE_GLOBS)


def _secret_file_ids() -> set[tuple[int, int]]:
    """(device, inode) of the configured secrets file, whatever it is named.

    ``SECRETS_PATH`` can point anywhere, and a hard link or a backup copy under
    a neutral name is not caught by a name rule; the inode is.
    """
    try:
        st = env.secrets_path().stat()
    except OSError:
        return set()
    return {(st.st_dev, st.st_ino)}


# A value is treated as a secret when its key names one, or when it looks
# random. secrets.env also holds ordinary settings (URLs, paths, model names, a
# timezone); measured on a live install, 14 of 35 values of 12+ characters were
# such settings, and treating them as secrets would hide ordinary files. Key
# words are anchored to whole name parts, so GIT_AUTHOR_NAME or BYPASS is not
# a secret name.
_SECRET_KEY_WORDS = re.compile(
    r"(?:^|_)(?:KEY|KEYS|TOKEN|TOKENS|SECRET|SECRETS|PASS|PASSWORD|PASSWD|PASSPHRASE|"
    r"CREDENTIAL|CREDENTIALS|AUTH|COOKIE|PRIVATE|SIGNING|WEBHOOK|APIKEY|"
    r"PAT|DSN|JWT|HMAC|SALT)(?:$|_)",
    re.IGNORECASE,
)
_NOT_RANDOM = re.compile(r"^[a-z][a-z0-9+.-]*://|^[/~.]|^[^@\s]+@[^@\s]+$", re.IGNORECASE)
# The password in a URL's userinfo: the part after the colon, before the '@'.
_URL_PASSWORD = re.compile(r"^[a-z][a-z0-9+.-]*://[^/@:\s]*:([^@/\s]+)@", re.IGNORECASE)
# A generated token carries a long unbroken alphanumeric run mixing letters and
# digits; a model name or slug (``large-v3-turbo``) has only short segments.
_RANDOM_RUN = re.compile(r"[A-Za-z0-9]{16,}")


def _looks_random(value: str) -> bool:
    if any(ch.isspace() for ch in value) or _NOT_RANDOM.search(value):
        return False
    return any(
        any(ch.isdigit() for ch in run) and any(ch.isalpha() for ch in run)
        for run in _RANDOM_RUN.findall(value)
    )


def _value_parts(raw: str) -> list[str]:
    """A value and, when it spans lines (a PEM key, real or literal newlines), each line.

    A copy may hold only part of a multiline value, so each line is matched on
    its own; armor lines (``-----BEGIN ...-----``) are markers, not key material.
    """
    value = raw.strip().strip("'\"")
    lines = value.replace("\\n", "\n").splitlines()
    parts = [value] if len(lines) <= 1 else [line.strip() for line in lines]
    return [part for part in parts if part and not part.startswith("-----")]


_secret_cache: dict[tuple, frozenset[str]] = {}
# The content search runs in worker threads (asyncio.to_thread), so concurrent
# searches share this cache; one load at a time keeps the read consistent.
_secret_lock = threading.Lock()
# Why the value screen is off for the last load, or None when it is on.
_SCREEN_OFF_MISSING = "no secrets file"
_SCREEN_OFF_OVERSIZE = f"secrets file over {_SECRETS_FILE_MAX_BYTES} bytes"
_SCREEN_OFF_UNREADABLE = "secrets file unreadable"
_warned_secrets: set[str] = set()


def _warn_secrets_once(why: str) -> None:
    if why not in _warned_secrets:
        _warned_secrets.add(why)
        logger.warning("locate: %s; copies of credentials are not detected by value", why)


def _secret_values() -> tuple[frozenset[str], str | None]:
    """``_load_secret_values`` under the cache lock."""
    with _secret_lock:
        return _load_secret_values()


def _load_secret_values() -> tuple[frozenset[str], str | None]:
    """The secret values in the configured secrets file (never returned), and
    why the value screen is off (None when the file loaded).

    The file is read the way the runtime loads it (python-dotenv, which strips
    inline comments, ``export`` and quotes) and the way the Telegram bridge
    reads it, and both readings are kept. Cached by the file's identity, size
    and mtime. A missing, oversized or unreadable file yields no values and a
    reason the response reports (also logged once): the name, directory and
    inode rules still apply.
    """
    from dotenv import dotenv_values

    from genesis.channels.bridge_config import parse_secrets_env_text

    path = env.secrets_path()
    try:
        st = path.stat()
    except OSError:
        _warn_secrets_once(f"no secrets file at {path}")
        return frozenset(), _SCREEN_OFF_MISSING
    if st.st_size > _SECRETS_FILE_MAX_BYTES:
        _warn_secrets_once(_SCREEN_OFF_OVERSIZE)
        return frozenset(), _SCREEN_OFF_OVERSIZE
    cache_key = (str(path), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
    cached = _secret_cache.get(cache_key)
    if cached is not None:
        return cached, None
    try:
        text = path.read_text(errors="replace")
        runtime = dotenv_values(path)
    except (OSError, ValueError):
        _warn_secrets_once(f"secrets file at {path} is unreadable")
        return frozenset(), _SCREEN_OFF_UNREADABLE
    pairs = [(k, v or "") for k, v in runtime.items()]
    pairs += list(parse_secrets_env_text(text).items())
    values = set()
    for key, raw in pairs:
        named = bool(_SECRET_KEY_WORDS.search(key))
        for part in _value_parts(raw):
            if (named and len(part) >= _MIN_NAMED_SECRET_LEN) or (
                len(part) >= _MIN_SECRET_VALUE_LEN and _looks_random(part)
            ):
                values.add(part)
            password = _URL_PASSWORD.match(part)
            if password and len(password.group(1)) >= _MIN_NAMED_SECRET_LEN:
                values.add(password.group(1))
    result = frozenset(values)
    _secret_cache.clear()
    _secret_cache[cache_key] = result
    return result, None


def _has_credential_shape(text: str) -> bool:
    """Whether ``text`` has a credential shape (an API key, a secret assignment,
    a card number) by the shared output scanner, location findings aside.

    Any hit in the whole text counts. The scanner's normalisation deletes
    control characters, and its assignment patterns span line breaks, so a
    shape can exist only across a line boundary; confirming hits line by line
    would miss exactly those. A cross-line false positive only skips a file.
    """
    return bool(scan_outbound_normalised(text) - LOCATION_FINDINGS)


# Shape verdicts by file identity. A verdict depends only on the file's bytes,
# never on the pattern, so a file unchanged since its last screen is not read
# or scanned again. Bounded by clearing, not by eviction order.
_shape_cache: dict[tuple, bool] = {}
_SHAPE_CACHE_MAX = 100_000
# Total time one content search may spend screening uncached files for
# credential shapes. The failure mode it bounds: a root holding thousands of
# large untracked files (the scan costs about 0.4 s per MiB in Python,
# measured on a synthetic 1 MiB file) would hold the call for many minutes.
# Files not reached are skipped and counted, never searched unscreened, and
# the next call starts from the cached verdicts. 60 s is the same bound the
# content search already gives each rg call.
_SHAPE_SCREEN_BUDGET_S = 60.0


def _read_text_raw(path: str | Path) -> str:
    """A file's text with its line endings as written. Universal-newline
    reading turns a bare ``\\r`` into ``\\n``, which the scanner keeps, so a
    shape split by ``\\r`` would be seen in rg's bytes and missed here."""
    with open(path, encoding="utf-8", errors="replace", newline="") as fh:
        return fh.read()


def _shape_key(path: str) -> tuple | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    # ctime too: os.utime can restore a rewritten file's mtime, never its ctime.
    return (
        os.path.realpath(path), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns,
    )


def _remember_shape(key: tuple | None, verdict: bool) -> None:
    if key is None:
        return
    if len(_shape_cache) >= _SHAPE_CACHE_MAX:
        _shape_cache.clear()
    _shape_cache[key] = verdict


def _file_has_shape(path: str) -> bool:
    """A file's shape verdict, cached by identity; an unreadable file counts as one."""
    key = _shape_key(path)
    cached = _shape_cache.get(key) if key is not None else None
    if cached is not None:
        return cached
    try:
        text = _read_text_raw(path)
    except OSError:
        return True
    verdict = _has_credential_shape(text)
    _remember_shape(key, verdict)
    return verdict


def _files_holding_secrets(
    records: list[dict], values: frozenset[str], exempt: set[str],
) -> tuple[set[str], set[str]]:
    """(files carrying a secret value or a credential shape, files not screened in budget).

    Such a file is never content-searched at all, not even partly: hiding only
    the matching lines would tell a caller whether a pattern matched a line
    with a secret on it, which recovers the secret one character at a time.
    The patterns go to rg on stdin, never into a file or the argv. Paths come
    back NUL-terminated and as bytes: a file name may hold a line break or
    bytes that are not UTF-8, and a path that does not round-trip is a file
    that is never excluded.

    The whole text of each file is also checked for a credential shape
    (``_has_credential_shape``), so a key that is not in the secrets file skips
    the file too, whatever the pattern. ``exempt`` are files whose bytes equal
    their blob on the public repo's main (``_tracked_files``): their content is
    already public and passed CI's leak scan, so a shape there is a fixture or
    an example, and they are not shape-checked. Each other file is read at most once, in
    Python, for both checks; an unchanged file's shape verdict comes from the
    cache. Files the screen cannot reach within ``_SHAPE_SCREEN_BUDGET_S`` come
    back in the second set, and the caller skips them too.
    """
    if not records:
        return set(), set()
    paths = [str(r["path"]) for r in records]
    by_rg = False
    found: set[str] = set()
    if values and _have_rg():
        found: set[str] = set()
        patterns = ("\n".join(sorted(values)) + "\n").encode()
        for i in range(0, len(paths), _RG_CHUNK):
            chunk = paths[i:i + _RG_CHUNK]
            try:
                proc = subprocess.run(
                    ["rg", "-F", "-l", "--null", "--no-messages", "-f", "-", "--", *chunk],
                    input=patterns, capture_output=True, timeout=60,
                )
            except (subprocess.TimeoutExpired, OSError):
                found = set()
                break
            if proc.returncode not in (0, 1):
                found = set()
                break
            found.update(os.fsdecode(name) for name in proc.stdout.split(b"\0") if name)
        else:
            by_rg = True
    check_values = bool(values) and not by_rg
    deadline = time.monotonic() + _SHAPE_SCREEN_BUDGET_S
    unscreened: set[str] = set()
    for path in paths:
        check_shape = path not in exempt and path not in found
        if not (check_values or check_shape):
            continue
        key = _shape_key(path) if check_shape else None
        cached = _shape_cache.get(key) if key is not None else None
        if cached is not None and (cached or not check_values):
            if cached:
                found.add(path)
            continue
        if time.monotonic() >= deadline:
            unscreened.add(path)
            continue
        try:
            text = _read_text_raw(path)
        except OSError:
            continue
        if check_values and any(value in text for value in values):
            found.add(path)
            continue
        if check_shape:
            verdict = cached if cached is not None else _has_credential_shape(text)
            _remember_shape(key, verdict)
            if verdict:
                found.add(path)
    return found, unscreened


def _normalized_name(part: str) -> str:
    """A path component with editor-backup wrapping removed (``x~``, ``.x.swp``, ``#x#``)."""
    name = part.lower().rstrip("~").strip("#")
    for suffix in (".swp", ".swo"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name


def _is_locate_credential_part(part: str) -> bool:
    name = _normalized_name(part)
    if part in _CREDENTIAL_DIRS or is_credential_component(name):
        return True
    if name in _LOCATE_CREDENTIAL_NAMES or name.endswith(_LOCATE_CREDENTIAL_SUFFIXES):
        return True
    return any(
        segment and re.split(r"[_\-]+", segment)[-1] in _LOCATE_CREDENTIAL_STEMS
        for segment in name.lstrip(".").split(".")[:-1] or [name.lstrip(".")]
    )


# A GitHub remote URL (https, ssh, or scp-like) and the owner/name it names.
_GITHUB_REMOTE = re.compile(
    r"^(?:https?://(?:[^@/]+@)?github\.com/|ssh://(?:[^@/]+@)?github\.com/|(?:[^@/:]+@)?github\.com:)"
    r"([^/]+)/([^/]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)


def _git_out(root: Path, *args: str) -> bytes | None:
    """stdout of a read-only git command in ``root``, or None on any failure."""
    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", *args],
            capture_output=True, cwd=str(root), timeout=30, env=scrubbed_git_env(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    return result.stdout if result.returncode == 0 else None


def _public_main_ref(root: Path) -> str | None:
    """The local remote-tracking main of the public repo in ``root``, or None.

    The public repo is the configured tracker (``github.user`` /
    ``github.public_repo``); a remote counts only when its URL names it, so a
    private fork's origin does not. ``origin`` is preferred when it qualifies.
    Nothing is fetched: the ref is used as it is locally, and a stale ref only
    narrows what it exempts.
    """
    listing = _git_out(root, "config", "--get-regexp", r"^remote\..*\.url$")
    if not listing:
        return None
    from genesis.board.config import tracker_repo

    public = tracker_repo()
    if public is None:
        return None
    want = (public[0].lower(), public[1].lower())
    remotes = []
    for line in os.fsdecode(listing).splitlines():
        key, _, url = line.partition(" ")
        match = _GITHUB_REMOTE.match(url.strip())
        if match and (match.group(1).lower(), match.group(2).lower()) == want:
            remotes.append(key[len("remote."):-len(".url")])
    for remote in sorted(remotes, key=lambda name: (name != "origin", name)):
        ref = f"refs/remotes/{remote}/main"
        if _git_out(root, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}") is not None:
            return ref
    return None


def _blob_oids(listing: bytes, root: Path) -> dict[str, bytes]:
    """``git ls-tree -z`` output as {normalized absolute path: blob id}."""
    oids: dict[str, bytes] = {}
    for entry in listing.split(b"\0"):
        meta, tab, name = entry.partition(b"\t")
        fields = meta.split()
        if tab and len(fields) == 3 and fields[1] == b"blob":
            oids[os.path.normpath(os.path.join(str(root), os.fsdecode(name)))] = fields[2]
    return oids


def _tracked_files(records: list[dict]) -> tuple[set[str], set[str]]:
    """(files holding exactly their HEAD blob's bytes, those also on the public main).

    Only the second set exempts anything (the caller discards the first): a
    file is spared the credential-name rules and the credential-shape screen
    only when it has been through CI's leak scan, i.e. its HEAD blob equals its
    blob on the public repo's remote-tracking main (``_public_main_ref``). A
    locally committed or unpushed file is in the first set only. Both are
    empty on any error.

    Tracking alone says nothing about the working tree: a tracked file edited
    since the last commit holds content that is neither public nor
    leak-scanned, so it is not exempt. The comparison is the HEAD blob id
    (``git ls-tree``) against the file's own bytes (``git hash-object
    --no-filters``), never ``git diff``: a clean filter, ``--assume-unchanged``
    or ``--skip-worktree`` make an edited file look unchanged to diff, and
    diff runs a repo's configured filter commands. No HEAD (a repo with no
    commit yet), no public main ref, any mismatch and any error exempt nothing.

    One helper for both exemptions that depend on it: the credential names,
    and the credential-shape screen. Asked per root, in chunks; a root
    that is not a checkout answers with an error and exempts nothing.
    """
    by_root: dict[Path, list[str]] = {}
    for record in records:
        by_root.setdefault(record["root"], []).append(str(record["path"]))
    tracked: set[str] = set()
    public: set[str] = set()
    for root, paths in by_root.items():
        ref = _public_main_ref(root)
        for i in range(0, len(paths), _RG_CHUNK):
            chunk = paths[i:i + _RG_CHUNK]
            try:
                listing = subprocess.run(
                    ["git", "-c", "core.fsmonitor=false", "--literal-pathspecs",
                     "ls-tree", "-z", "HEAD", "--", *chunk],
                    capture_output=True, cwd=str(root), timeout=30, env=scrubbed_git_env(),
                )
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                break
            if listing.returncode != 0:
                break
            head_oids = _blob_oids(listing.stdout, root)
            candidates = [p for p in chunk if os.path.normpath(p) in head_oids]
            if not candidates:
                continue
            try:
                hashed = subprocess.run(
                    ["git", "-c", "core.fsmonitor=false",
                     "hash-object", "--no-filters", "--", *candidates],
                    capture_output=True, cwd=str(root), timeout=30, env=scrubbed_git_env(),
                )
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                break
            oids = hashed.stdout.split()
            if hashed.returncode != 0 or len(oids) != len(candidates):
                break
            unchanged = [
                p for p, oid in zip(candidates, oids, strict=True)
                if head_oids[os.path.normpath(p)] == oid
            ]
            tracked.update(unchanged)
            if ref is None or not unchanged:
                continue
            ref_listing = _git_out(root, "--literal-pathspecs", "ls-tree", "-z", ref, "--", *unchanged)
            if ref_listing is None:
                ref = None
                continue
            ref_oids = _blob_oids(ref_listing, root)
            public.update(
                p for p in unchanged
                if ref_oids.get(os.path.normpath(p)) == head_oids[os.path.normpath(p)]
            )
    return tracked, public


def _parts(record: dict) -> tuple[str, ...]:
    try:
        return record["path"].relative_to(record["root"]).parts
    except ValueError:
        return (record["name"],)


def _holds_credentials(
    record: dict, secret_ids: set[tuple[int, int]], public: set[str] | None = None,
) -> bool:
    """Whether a file's content must never be searched, by name, place or inode (#4129).

    A content match returns the matching lines, which in a credential file are
    the secrets themselves. Such files stay findable by name; stored
    credentials are read through ``reference_lookup``, which audits each read.
    Uses the predicate the dashboard's file routes refuse paths by, plus the
    content-search additions above, which spare a file only when it is in
    ``public`` (its bytes are on the public repo's main).
    """
    if record["file_id"] in secret_ids:
        return True
    parts = _parts(record)
    if any(part in _CREDENTIAL_DIRS or is_credential_component(part) for part in parts):
        return True
    if public is not None and str(record["path"]) in public:
        return False
    return any(_is_locate_credential_part(part) for part in parts)


# ── walk ──────────────────────────────────────────────────────────────────────


def _walk(
    label: str,
    root: Path,
    cutoff: float | None,
    file_type: str,
    name_glob: str,
    scan_budget: list[int],
    prune_worktrees: set[Path] | None = None,
) -> list[dict]:
    """Return file records under ``root`` passing recency/type/glob filters.

    Iterative, prune-aware, symlink-safe (never recurses into or lists symlinks).
    ``scan_budget`` is a single-element list mutated as a shared scanned-file
    counter / ceiling across roots.
    """
    records: list[dict] = []
    name_pat = name_glob.lower() if name_glob else ""
    stack: list[Path] = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except (PermissionError, OSError):
            continue
        for e in entries:
            if scan_budget[0] >= _MAX_FILES_SCANNED:
                return records
            try:
                if e.is_dir(follow_symlinks=False):
                    e_path = Path(e.path)
                    if prune_worktrees and e_path.resolve() in prune_worktrees:
                        continue
                    if e.name not in _PRUNE_DIRS:
                        stack.append(e_path)
                    continue
                if not e.is_file(follow_symlinks=False):
                    continue  # symlinks, sockets, fifos — skip
            except OSError:
                continue
            nm = e.name
            if _matches_skip_glob(nm):
                continue
            scan_budget[0] += 1
            if name_pat and not fnmatch.fnmatch(nm.lower(), name_pat):
                continue
            suffix = os.path.splitext(nm)[1].lower()
            if file_type == "code" and suffix not in _CODE_EXTS:
                continue
            if file_type == "noncode" and suffix in _CODE_EXTS:
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if cutoff is not None and st.st_mtime < cutoff:
                continue
            records.append({
                "path": Path(e.path),
                "name": nm,
                "scope": label,
                "mtime": st.st_mtime,
                "size": st.st_size,
                "root": root,
                "file_id": (st.st_dev, st.st_ino),
            })
    return records


# ── content match ─────────────────────────────────────────────────────────────


def _is_text_and_small(path: Path) -> bool:
    """True if the file is <= 1 MiB and not binary (no NUL in the first 8 KiB).

    Applied to BOTH backends before matching: rg bypasses its own binary/size
    filtering when files are passed as explicit path args (it only self-filters
    during recursive traversal), so we must gate candidates ourselves.
    """
    try:
        if path.stat().st_size > _MAX_CONTENT_BYTES:
            return False
        with path.open("rb") as fh:
            head = fh.read(_BINARY_SNIFF_BYTES)
    except OSError:
        return False
    return b"\x00" not in head


def _content_match(
    records: list[dict], contains: str, regex: bool,
) -> tuple[list[dict], str | None, int, str, int]:
    """Filter records to those whose content matches, attaching ``matches``.

    Returns (records_with_matches, error, credential_files_skipped,
    value_screen, unscreened). ``error`` set only on invalid regex.
    ``value_screen`` is "on", or "off: <reason>" when the secrets file could
    not be loaded or holds no qualifying value. ``unscreened`` counts the files
    the shape screen could not reach in its budget (skipped, and included in
    the skip count). A file that holds credentials, by its name, its place,
    its inode, a secret value or a credential shape in its content, is never
    searched; the count does not depend on the pattern, so it reveals nothing
    about one.
    Not covered: encoded, re-wrapped or partial copies of a secret, and secrets
    that are not in the secrets file.
    Binary/oversize files are excluded up front (see ``_is_text_and_small``).
    Tries rg first (fast); falls back to pure Python when rg is absent or
    errors — identical result shape either way.
    """
    if regex:
        try:
            re.compile(contains)
        except re.error as exc:
            return [], f"invalid regex: {exc}", 0, "on", 0
    secret_ids = _secret_file_ids()
    _, public = _tracked_files(records)
    searchable = [r for r in records if not _holds_credentials(r, secret_ids, public)]
    eligible = [r for r in searchable if _is_text_and_small(r["path"])]
    values, screen_off = _secret_values()
    if screen_off is None and not values:
        screen_off = "no qualifying values"
    screen = f"off: {screen_off}" if screen_off else "on"
    holding, unscreened = _files_holding_secrets(eligible, values, public)
    eligible = [r for r in eligible if str(r["path"]) not in holding | unscreened]
    skipped = len(records) - len(searchable) + len(holding) + len(unscreened)
    if _have_rg():
        out = _content_match_rg(eligible, contains, regex, values, public)
        if out is not None:
            return out[0], None, skipped + out[1], screen, len(unscreened)
    matched, dropped = _content_match_python(eligible, contains, regex, values, public)
    return matched, None, skipped + dropped, screen, len(unscreened)


def _content_match_rg(
    records: list[dict], contains: str, regex: bool, values: frozenset[str], exempt: set[str],
) -> tuple[list[dict], int] | None:
    """rg-backed content match: (matches, files dropped), or None to fall back to Python.

    rg reads each file again after the value screen, so a file written in
    between is judged again: any matched line carrying a secret value, or a
    credential shape outside ``exempt``, drops the whole file, and so does a
    shape anywhere in a matched file outside ``exempt`` (``_file_has_shape``,
    which an unchanged file answers from the cache).
    """
    by_path = {str(r["path"]): r for r in records}
    if not by_path:
        return [], 0
    matches_by_path: dict[str, list[dict]] = {}
    dropped: set[str] = set()
    paths = list(by_path.keys())
    base = ["rg", "--json", "--max-filesize", "1M"]
    if not regex:
        base.append("-F")
    base += ["-e", contains, "--"]
    for i in range(0, len(paths), _RG_CHUNK):
        chunk = paths[i:i + _RG_CHUNK]
        try:
            proc = subprocess.run(base + chunk, capture_output=True, text=True, timeout=60)
        except (subprocess.TimeoutExpired, OSError):
            return None  # fall back entirely for a consistent result
        if proc.returncode not in (0, 1):  # 0=matches, 1=no matches; >=2 = error
            return None
        for line in proc.stdout.splitlines():
            if not line:
                continue
            try:
                evt = json.loads(line)
            except ValueError:
                continue
            if evt.get("type") != "match":
                continue
            data = evt.get("data", {})
            p = data.get("path", {}).get("text")
            if p is None:
                continue
            line_text = data.get("lines", {}).get("text") or ""
            if any(value in line_text for value in values) or (
                p not in exempt and _has_credential_shape(line_text)
            ):
                dropped.add(p)
            lst = matches_by_path.setdefault(p, [])
            if len(lst) >= _MAX_MATCHES_PER_FILE:
                continue
            lst.append({"line": data.get("line_number"), "text": line_text.strip()[:_SNIPPET_CHARS]})
    dropped.update(
        p for p in matches_by_path
        if p not in dropped and p not in exempt and _file_has_shape(p)
    )
    return [
        {**rec, "matches": matches_by_path[p]}
        for p, rec in by_path.items()
        if p in matches_by_path and p not in dropped
    ], len(dropped & by_path.keys())


def _content_match_python(
    records: list[dict], contains: str, regex: bool, values: frozenset[str], exempt: set[str],
) -> tuple[list[dict], int]:
    """Pure-Python content match fallback: (matches, files dropped).

    Binary/oversize-guarded. The text matched is the text screened: a file
    whose content carries a secret value, or a credential shape outside
    ``exempt``, at this read is dropped whole.
    """
    pattern = re.compile(contains) if regex else None
    out: list[dict] = []
    dropped = 0
    for rec in records:
        path: Path = rec["path"]
        key = _shape_key(str(path))
        try:
            if path.stat().st_size > _MAX_CONTENT_BYTES:
                continue
            with path.open("rb") as fh:
                head = fh.read(_BINARY_SNIFF_BYTES)
            if b"\x00" in head:
                continue
            text = _read_text_raw(path)
        except OSError:
            continue
        shaped = False
        if str(path) not in exempt:
            shaped = _shape_cache.get(key) if key is not None else None
            if shaped is None:
                shaped = _has_credential_shape(text)
                _remember_shape(key, shaped)
        if shaped or any(value in text for value in values):
            dropped += 1
            continue
        matches: list[dict] = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            hit = pattern.search(line) if pattern else (contains in line)
            if hit:
                matches.append({"line": lineno, "text": line.strip()[:_SNIPPET_CHARS]})
                if len(matches) >= _MAX_MATCHES_PER_FILE:
                    break
        if matches:
            out.append({**rec, "matches": matches})
    return out, dropped


# ── response assembly ─────────────────────────────────────────────────────────


def _error_response(query: dict, error: str, *, roots=None, skipped=None, scanned=0) -> dict:
    return {
        "summary": f"locate error: {error}",
        "query": query,
        "count": 0,
        "total_matched": 0,
        "truncated": False,
        "content_pool_capped": False,
        "content_skipped_credential_files": 0,
        "content_value_screen": None,
        "scan_truncated": False,
        "scanned": scanned,
        "roots": roots or [],
        "skipped_roots": skipped or [],
        "results": [],
        "error": error,
    }


def _summarize(
    scope, within, file_type, contains, total, shown, truncated, skipped, capped, scan_truncated,
    credential_skipped=0, value_screen=None, unscreened=0,
) -> str:
    extra = []
    if skipped:
        extra.append(f"{len(skipped)} root(s) absent")
    if unscreened:
        extra.append(
            f"{unscreened} file(s) not screened for credentials within "
            f"{_SHAPE_SCREEN_BUDGET_S:g}s, so not content-searched (repeat the search; "
            "screened files are remembered)"
        )
    if value_screen and value_screen != "on":
        extra.append(
            f"secret-value screen off ({value_screen.removeprefix('off: ')}): copies of "
            "credentials in other files are not detected"
        )
    if credential_skipped:
        extra.append(
            f"{credential_skipped} credential file(s) not content-searched "
            "(find them by name; read stored credentials with reference_lookup)"
        )
    if capped:
        extra.append(f"content pool capped at {_CONTENT_POOL_CAP}")
    if scan_truncated:
        extra.append(
            f"scan hit the {_MAX_FILES_SCANNED}-file ceiling — results may be "
            "incomplete (narrow scope or within)"
        )
    suffix = (" · " + " · ".join(extra)) if extra else ""
    if total == 0:
        return f"no files match in scope '{scope}' (within {within}, {file_type})" + suffix
    head = f"{shown} of {total}" if truncated else f"{total}"
    quals = [f"within {within}", file_type]
    if contains:
        quals.append(f"containing {contains!r}")
    return f"{head} file(s) in '{scope}' ({', '.join(quals)})" + suffix


async def _impl_locate(
    scope: str = "docs",
    within: str = "24h",
    file_type: str = "any",
    name: str = "",
    contains: str = "",
    regex: bool = False,
    limit: int = 50,
) -> dict:
    now = time.time()
    scope = (scope or "docs").strip().lower()
    file_type = (file_type or "any").strip().lower()
    query = {
        "scope": scope, "within": within, "file_type": file_type,
        "name": name, "contains": contains, "regex": regex, "limit": limit,
    }

    if scope not in _VALID_SCOPES:
        return _error_response(query, f"unknown scope {scope!r}; valid: {', '.join(_VALID_SCOPES)}")
    if file_type not in _VALID_TYPES:
        return _error_response(query, f"unknown file_type {file_type!r}; valid: {', '.join(_VALID_TYPES)}")

    try:
        window_s = _parse_window(within)
    except ValueError:
        return _error_response(query, f"invalid 'within' value: {within!r}")
    cutoff = (now - window_s) if window_s is not None else None

    try:
        roots = _resolve_roots(scope)
    except ValueError as exc:
        return _error_response(query, str(exc))

    walked_roots: list[str] = []
    skipped_roots: list[str] = []
    scan_budget = [0]
    records: list[dict] = []

    # Dynamically obtain linked worktrees to prune them when scanning repo scope.
    # When git is unavailable or fails, _list_worktrees returns [] (fail-open):
    # repo scope scanning falls back to standard directory pruning without excluding linked worktrees,
    # ensuring the locate MCP tool remains functional even without git.
    if scope == "all":
        prune_wts = {path.resolve() for label, path in roots if label.startswith("worktree:")}
    elif scope == "repo":
        prune_wts = {p.resolve() for p in _list_worktrees(env.repo_root())}
    else:
        prune_wts = set()

    for label, path in roots:
        if not path.is_dir():
            skipped_roots.append(_display(path))
            continue
        walked_roots.append(_display(path))
        prune_arg = prune_wts if label == "repo" else None
        records.extend(_walk(label, path, cutoff, file_type, name, scan_budget, prune_worktrees=prune_arg))

    scan_truncated = scan_budget[0] >= _MAX_FILES_SCANNED

    capped = False
    credential_skipped = 0
    value_screen = None
    unscreened = 0
    if contains:
        if len(records) > _CONTENT_POOL_CAP:
            records.sort(key=lambda r: (-r["mtime"], str(r["path"])))
            records = records[:_CONTENT_POOL_CAP]
            capped = True
        # Off the event loop: the credential screens read and scan files.
        records, error, credential_skipped, value_screen, unscreened = await asyncio.to_thread(
            _content_match, records, contains, regex,
        )
        if error:
            return _error_response(
                query, error, roots=walked_roots, skipped=skipped_roots, scanned=scan_budget[0],
            )

    records.sort(key=lambda r: (-r["mtime"], str(r["path"])))
    total = len(records)
    li = int(limit)
    eff_limit = _HARD_LIMIT if li <= 0 else min(li, _HARD_LIMIT)  # 0/neg = all up to cap
    top = records[:eff_limit]

    results: list[dict] = []
    for r in top:
        item = {
            "path": str(r["path"]),
            "name": r["name"],
            "scope": r["scope"],
            "mtime": datetime.fromtimestamp(r["mtime"], tz=UTC).isoformat(timespec="seconds"),
            "age": _humanize_age(r["mtime"], now),
            "size": r["size"],
        }
        if "matches" in r:
            item["matches"] = r["matches"]
        results.append(item)

    truncated = total > len(results)
    return {
        "summary": _summarize(
            scope, within, file_type, contains, total, len(results),
            truncated, skipped_roots, capped, scan_truncated, credential_skipped, value_screen,
            unscreened,
        ),
        "query": query,
        "count": len(results),
        "total_matched": total,
        "truncated": truncated,
        "content_pool_capped": capped,
        "content_skipped_credential_files": credential_skipped,
        "content_value_screen": value_screen,
        "scan_truncated": scan_truncated,
        "scanned": scan_budget[0],
        "roots": walked_roots,
        "skipped_roots": skipped_roots,
        "results": results,
        "error": None,
    }


@mcp.tool()
async def locate(
    scope: str = "docs",
    within: str = "24h",
    file_type: str = "any",
    name: str = "",
    contains: str = "",
    regex: bool = False,
    limit: int = 50,
) -> dict:
    """Find recently-changed files across Genesis's known roots, ranked by recency.

    Deterministic, read-only self-retrieval — use this (not Grep/Glob) to answer
    "what working docs changed recently?" or "where is the file about X?".

    Args:
        scope: Which roots to search.
            "docs" (default) = ~/.claude/plans + ~/.genesis/output (fast, the
            working-docs case); "plans"; "output"; "repo" = the Genesis repo
            (pruned of .git/.venv/node_modules/worktrees/…); "worktrees" = all
            git worktrees; "all" = docs + repo + worktrees.
        within: Time window: "12h", "24h" (default), "7d", "30d", a bare number
            (days), or "0"/"all" for no time limit. Filters by file mtime.
        file_type: "any" (default), "code" (.py/.js/.ts/.go/.yaml/.json-ish/…),
            or "noncode" (.md/.txt/.pdf/… — the working-docs class).
        name: Case-insensitive glob on the filename, e.g. "*roadmap*.md". Empty = all.
        contains: Optional content match (only files passing the other filters are
            opened). Empty = no content filtering. Credential files (the
            secrets file and its backups, .env, private keys, token and
            credential files, any file carrying a value from the secrets file)
            are never content-searched: findable by name, counted in
            content_skipped_credential_files; read stored credentials with
            reference_lookup.
        regex: Treat ``contains`` as a regex (default: literal substring).
        limit: Max results (default 50, hard cap 200; 0 or negative = all up to
            the cap). Truncation is reported explicitly via count / total_matched
            / truncated.

    Returns a dict with: summary, query (echoed), count, total_matched, truncated,
    content_pool_capped, content_skipped_credential_files, content_value_screen
    ("on", "off: <reason>", null without contains), scan_truncated (true if
    the walk hit its file ceiling: results may be incomplete), scanned, roots,
    skipped_roots, and results[] (path, name, scope, mtime, age, size, and
    matches[] when ``contains`` is used), newest first.
    """
    return await _impl_locate(
        scope=scope, within=within, file_type=file_type,
        name=name, contains=contains, regex=regex, limit=limit,
    )
