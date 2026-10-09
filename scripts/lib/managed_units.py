"""Managed systemd user units: stamp them, and tell a hand edit from a Genesis render.

bootstrap.sh renders every ``scripts/systemd/*.{service,timer,slice}.template``
(and setup-vnc.sh the ``scripts/systemd/vnc/`` ones) into ``~/.config/systemd/user``.
Before this module it overwrote any installed unit that differed from the render,
so a local change made in the unit file itself was silently discarded by the next
update. Now every render ends with a stamp line::

    # genesis-managed v1 sha256=<sha256 of every byte above this line>

and a unit is unedited exactly when it carries a valid stamp. An edited unit is
KEPT and named, never overwritten; local changes belong in a drop-in
(``<unit>.d/*.conf``), which nothing here ever touches.

Grammar, strict on purpose: the stamp is the last non-blank line, only newlines
may follow it, no other line starts with ``# genesis-managed``, and the hash
covers the body bytes including the body's final newline. Anything else (a line
appended after the stamp, two stamps, CRLF) reads as edited.

A unit written before stamps existed carries none. It is accepted when it
matches a version of its template: the version at ``--upto``, at each ``--also``
revision, or (only with ``--accept-legacy``, which costs a scan of history) any
version in any ref or reflog. ``__TOKEN__`` placeholders match one run of
non-blank text, the same value at every occurrence.

Subcommands (run with ``python3 -I -S``, stdlib only):

* ``stamp``: read a render on stdin, write it stamped to stdout. Exit 3 if the
  render already contains the stamp prefix.
* ``classify --repo R --template REL --target PATH --upto REV [--also REV]...
  [--accept-legacy]``: print one word, exit 0: ``stamped``, ``legacy`` (unstamped,
  matches a template version), ``edited``, ``nonregular`` (a symlink or not a
  regular file, e.g. a masked unit), or ``missing``.
* ``check --repo R --unit-dir D --upto REV [--also REV]... [--accept-legacy]
  [--take NAMES] [--json | --tsv]``: classify every managed unit the templates at REV
  install. Exit 4 if any is ``edited`` and not named in ``--take`` (space-separated
  unit file names, or ``*``), else 0. Any other exit is an error: callers treat it
  as "could not check" and refuse, never as clean. Without ``--accept-legacy`` an
  unstamped unit that matches no listed revision reads ``unstamped`` and does not
  refuse (the dashboard's cheap, stamp-only view).
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import subprocess
import sys

PREFIX = "# genesis-managed"
STAMP_LEAD = "# genesis-managed v1 sha256="
_STAMP_RE = re.compile(r"# genesis-managed v1 sha256=([0-9a-f]{64})")
_TOKEN_RE = re.compile(r"__([A-Z0-9][A-Z0-9_]*)__")
_TEMPLATE_RE = re.compile(r"scripts/systemd/(?:vnc/)?([^/]+\.(?:service|timer|slice))\.template")
EXIT_REFUSE = 4
EXIT_PREFIX = 3


def _git(repo: str, *args: str, data: bytes | None = None) -> bytes | None:
    # Local reads only. GIT_* from the caller would point the call at another
    # repository, so they are dropped, as live_checkout.py does.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        p = subprocess.run(["git", "-C", repo, *args], input=data, capture_output=True, env=env)
    except OSError:
        return None
    return p.stdout if p.returncode == 0 else None


def stamp(render: bytes) -> bytes:
    if any(line.startswith(PREFIX.encode()) for line in render.split(b"\n")):
        raise ValueError("render already contains the stamp prefix")
    body = render.rstrip(b"\n") + b"\n"
    return body + f"{STAMP_LEAD}{hashlib.sha256(body).hexdigest()}\n".encode()


def verify(data: bytes) -> str:
    """``stamped`` | ``unstamped`` | ``edited``."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "edited"
    lines = text.rstrip("\n").split("\n")
    if not any(line.startswith(PREFIX) for line in lines):
        return "unstamped"
    match = _STAMP_RE.fullmatch(lines[-1])
    if not match or any(line.startswith(PREFIX) for line in lines[:-1]):
        return "edited"
    body = "\n".join(lines[:-1]) + "\n"
    return "stamped" if hashlib.sha256(body.encode("utf-8")).hexdigest() == match.group(1) else "edited"


def template_pattern(template: str) -> re.Pattern[str]:
    """The template as a regex: literal text exact, each token one non-blank run,
    bound to the same value wherever it recurs."""
    out, seen = [], set()
    for i, piece in enumerate(_TOKEN_RE.split(template.rstrip("\n"))):
        if i % 2 == 0:
            out.append(re.escape(piece))
        else:
            group = f"t_{piece}"
            out.append(f"(?P={group})" if group in seen else f"(?P<{group}>[^\\s]+)")
            seen.add(group)
    return re.compile("".join(out))


def _matches(template: bytes | None, installed: str) -> bool:
    if template is None:
        return False
    return (
        template_pattern(template.decode("utf-8", "replace")).fullmatch(installed.rstrip("\n"))
        is not None
    )


@functools.cache
def _history_index(repo: str) -> dict[str, list[bytes]]:
    """Every blob any ref or reflog ever held for each template, by the unit file
    it installs. ONE scan per process, however many units are checked (the scan
    is the slow part: tens of seconds on a long history)."""
    raw = _git(repo, "log", "--all", "--reflog", "-M", "--format=", "--raw", "--no-abbrev",
               "--", "scripts/systemd/")
    if raw is None:
        raise RuntimeError("git log over scripts/systemd failed")
    names: dict[str, set[str]] = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        meta, _, path = line.partition("\t")
        fields = meta.split()
        match = _TEMPLATE_RE.fullmatch(path.split("\t")[-1])
        if len(fields) >= 4 and match and set(fields[3]) != {"0"}:
            names.setdefault(match.group(1), set()).add(fields[3])
    shas = sorted({sha for group in names.values() for sha in group})
    blobs: dict[str, bytes] = {}
    if shas:
        batch = _git(repo, "cat-file", "--batch", data="".join(f"{sha}\n" for sha in shas).encode())
        if batch is None:
            raise RuntimeError("git cat-file failed")
        pos = 0
        while pos < len(batch):
            header_end = batch.index(b"\n", pos)
            header = batch[pos:header_end].split()
            if len(header) != 3:  # "<sha> missing": an object gc has taken
                pos = header_end + 1
                continue
            size = int(header[2])
            blobs[header[0].decode()] = batch[header_end + 1 : header_end + 1 + size]
            pos = header_end + 1 + size + 1
    return {name: [blobs[sha] for sha in group if sha in blobs] for name, group in names.items()}


def classify(repo: str, rel: str, target: str, revs: list[str], accept_legacy: bool) -> str:
    if os.path.islink(target) or (os.path.lexists(target) and not os.path.isfile(target)):
        return "nonregular"
    if not os.path.exists(target):
        return "missing"
    with open(target, "rb") as fh:
        data = fh.read()
    state = verify(data)
    if state != "unstamped":
        return state
    installed = data.decode("utf-8", "replace")
    if any(_matches(_git(repo, "show", f"{rev}:{rel}"), installed) for rev in revs):
        return "legacy"
    if not accept_legacy:
        return "unstamped"
    name = _TEMPLATE_RE.fullmatch(rel).group(1)
    return "legacy" if any(_matches(b, installed) for b in _history_index(repo).get(name, [])) else "edited"


def _templates(repo: str, rev: str) -> list[str]:
    listing = _git(repo, "ls-tree", "-r", "--name-only", rev, "--", "scripts/systemd/")
    if listing is None:
        raise RuntimeError(f"cannot list scripts/systemd at {rev}")
    return [p for p in listing.decode().splitlines() if _TEMPLATE_RE.fullmatch(p)]


def check(repo: str, unit_dir: str, revs: list[str], accept_legacy: bool, take: str) -> list[dict]:
    taken = set(take.split())
    rows = []
    for rel in _templates(repo, revs[0]):
        name = _TEMPLATE_RE.fullmatch(rel).group(1)
        state = classify(repo, rel, os.path.join(unit_dir, name), revs, accept_legacy)
        refused = state == "edited" and "*" not in taken and name not in taken
        rows.append({"unit": name, "template": rel, "state": state, "refused": refused})
    return rows


def _main(argv: list[str]) -> int:
    if argv[:1] == ["stamp"]:
        try:
            sys.stdout.buffer.write(stamp(sys.stdin.buffer.read()))
        except ValueError as exc:
            print(f"managed_units: {exc}", file=sys.stderr)
            return EXIT_PREFIX
        return 0
    if not argv or argv[0] not in ("classify", "check"):
        print("usage: managed_units.py stamp | classify ... | check ...", file=sys.stderr)
        return 2
    opts: dict[str, list[str]] = {}
    flags, rest = set(), argv[1:]
    while rest:
        key = rest.pop(0)
        if key in ("--accept-legacy", "--json", "--tsv"):
            flags.add(key)
        elif key.startswith("--") and rest:
            opts.setdefault(key, []).append(rest.pop(0))
        else:
            print(f"managed_units: bad argument {key!r}", file=sys.stderr)
            return 2
    try:
        repo = opts["--repo"][0]
        revs = [rev for rev in opts["--upto"][:1] + opts.get("--also", []) if rev]
        if any(rev.startswith("-") for rev in revs):
            raise ValueError("a revision may not start with '-'")
        legacy = "--accept-legacy" in flags
        if argv[0] == "classify":
            print(classify(repo, opts["--template"][0], opts["--target"][0], revs, legacy))
            return 0
        rows = check(repo, opts["--unit-dir"][0], revs, legacy, " ".join(opts.get("--take", [])))
    except (KeyError, IndexError, RuntimeError, OSError, ValueError) as exc:
        print(f"managed_units: could not check: {exc}", file=sys.stderr)
        return 2
    if "--json" in flags:
        print(json.dumps(rows))
    elif "--tsv" in flags:  # one "<unit>\t<state>" line each, for the shell lib
        for row in rows:
            print(f"{row['unit']}\t{row['state']}")
    else:
        for row in rows:
            if row["state"] == "edited":
                print(
                    f"  {'REFUSED' if row['refused'] else 'taken'}: {row['unit']} was edited by hand"
                )
            elif row["state"] == "nonregular":
                print(f"  kept: {row['unit']} is a symlink or not a regular file (masked?)")
    return EXIT_REFUSE if any(row["refused"] for row in rows) else 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
