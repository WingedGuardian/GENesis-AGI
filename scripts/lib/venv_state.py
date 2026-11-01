"""Record a venv's installed packages, and compare a venv against that record.

update.sh records the package set before an update changes anything and restores
exactly that set if the update rolls back. Reinstalling the old checkout against
its version RANGES is not a restore: on 2026-10-31 it resolved fastmcp 2.14.7
with mcp 1.30.0, a pair that cannot import together, where the working install
had 2.14.6 with 1.27.0.

Run with the venv's own interpreter (``-P``, so the checkout is not on the path):
importlib.metadata must see that venv's site-packages.

    capture OUT      write the record (JSON) to OUT, atomically
    requirements IN  print one pip requirement per recorded package
    extras IN        print installed distributions the record does not name
    verify IN        exit 0 when the installed set equals the record, else
                     print what differs and exit 1

Editable installs are recorded by NAME only and are never reinstalled or removed:
the checkout itself is re-pointed by the caller, and another editable may point
at a directory that no longer exists (it could not be reinstalled anyway).
A distribution installed from a direct URL is recorded as ``name @ url``.
Exit 2 on any error, with the reason on stderr.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import tempfile
from importlib import metadata


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_REQUIREMENT = re.compile(rf"({_NAME.pattern})(==[^\s;@]+| @ \S+)")


def _installed() -> tuple[dict[str, str], list[str], dict[str, str]]:
    """{normalised name: requirement} for ordinary installs, editable names, and
    {normalised name: installed version} for the ordinary installs."""
    pinned: dict[str, str] = {}
    editable: set[str] = set()
    versions: dict[str, str] = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if not name:
            continue
        url_info = dist.read_text("direct_url.json")
        direct = json.loads(url_info) if url_info else {}
        # A package inside a repository or archive keeps its selector, as pip's
        # own direct reference does; without it the root would be built instead.
        sub = f"#subdirectory={direct['subdirectory']}" if direct.get("subdirectory") else ""
        if direct.get("dir_info", {}).get("editable"):
            editable.add(_norm(name))
            continue
        if direct.get("url") and direct.get("vcs_info"):
            vcs = direct["vcs_info"]
            pinned[_norm(name)] = f"{name} @ {vcs['vcs']}+{direct['url']}@{vcs['commit_id']}{sub}"
        elif direct.get("url"):
            # An archive, or a local directory installed without -e: from where it
            # came, never by version from an index, which may carry a different
            # package of that name.
            pinned[_norm(name)] = f"{name} @ {direct['url']}{sub}"
        else:
            pinned[_norm(name)] = f"{name}=={dist.version}"
        # Kept for every ordinary install: a URL alone cannot tell version 1.0
        # of a local directory from the 2.0 it holds after an update. (A version
        # computed at build time, as setuptools-scm does, can differ for the same
        # source; verify then reports it, which errs toward incomplete.)
        versions[_norm(name)] = dist.version
    # An editable checkout is seen twice: its dist-info in site-packages, and the
    # <name>.egg-info setuptools leaves in its source tree, which the editable
    # .pth puts on the path. The egg-info has no direct_url.json, so without this
    # it would be pinned as name==version: a checkout package that no index
    # carries, which fails the all-or-nothing requirements install (or installs a
    # stranger's package of that name) after any version bump.
    for name in editable:
        pinned.pop(name, None)
        versions.pop(name, None)
    return pinned, sorted(editable), versions


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return _check(json.load(fh))


def _check(record: dict) -> dict:
    """Raise unless the record is one the restore can use. capture runs it too,
    so a record it cannot read back is a capture failure (the update then warns
    and keeps the old re-resolve), never a discovery at rollback time."""
    if not isinstance(record, dict):
        raise ValueError("record is not an object")
    if not isinstance(record.get("pinned"), dict) or not record["pinned"]:
        raise ValueError("record has no packages")
    if not isinstance(record.get("editable"), list):
        raise ValueError("record has no editable list")
    for key, req in record["pinned"].items():
        m = _REQUIREMENT.fullmatch(req) if isinstance(req, str) else None
        if not m or _norm(m.group(1)) != key:
            raise ValueError(f"record entry {key!r} is not a pinned requirement")
    versions = record.get("versions")
    if not isinstance(versions, dict) or set(versions) != set(record["pinned"]):
        raise ValueError("record versions do not cover its packages")
    if not all(
        isinstance(v, str) and v and not any(c.isspace() for c in v) for v in versions.values()
    ):
        raise ValueError("record has an unusable version")
    both = set(record["pinned"]) & set(record["editable"])
    if both:
        raise ValueError(f"record pins editable installs: {', '.join(sorted(both))}")
    return record


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("capture", "requirements", "extras", "verify"):
        print(__doc__.split("\n\n")[2], file=sys.stderr)
        return 2
    cmd, path = argv[1], argv[2]
    try:
        if cmd == "capture":
            pinned, editable, versions = _installed()
            if not pinned:
                raise ValueError("no installed packages found")
            record = _check({"pinned": pinned, "editable": editable, "versions": versions})
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(
                        record,
                        fh,
                        indent=1,
                        sort_keys=True,
                    )
                os.replace(tmp, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
            return 0
        record = _load(path)
        if cmd == "requirements":
            for req in sorted(record["pinned"].values()):
                print(req)
            return 0
        pinned, editable, versions = _installed()
        if cmd == "extras":
            # Printed for an unquoted pip uninstall: a name that is not a plain
            # distribution name (a glob, an option) is refused, never passed on.
            known = set(record["pinned"]) | set(record["editable"])
            for name in sorted(set(pinned) - known):
                if not _NAME.fullmatch(name):
                    raise ValueError(f"installed distribution name {name!r} is not a plain name")
                print(name)
            return 0
        # verify: the ordinary installs must match exactly; editables recorded
        # must still be present (the caller re-points the checkout's own).
        diff = [
            f"missing {record['pinned'][n]}" for n in sorted(set(record["pinned"]) - set(pinned))
        ]
        diff += [f"extra {pinned[n]}" for n in sorted(set(pinned) - set(record["pinned"]))]
        diff += [
            f"changed {record['pinned'][n]} -> {pinned[n]}"
            for n in sorted(set(record["pinned"]) & set(pinned))
            if record["pinned"][n] != pinned[n]
        ]
        diff += [
            f"changed {n} {record['versions'][n]} -> {versions[n]}"
            for n in sorted(set(record["pinned"]) & set(pinned))
            if record["pinned"][n] == pinned[n] and record["versions"][n] != versions[n]
        ]
        diff += [f"editable missing {n}" for n in record["editable"] if n not in editable]
        for line in diff:
            print(line)
        return 1 if diff else 0
    except Exception as exc:  # any failure is a refusal, never a silent pass
        print(f"venv_state {cmd}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
