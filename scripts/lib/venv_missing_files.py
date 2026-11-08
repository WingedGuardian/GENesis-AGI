"""Name installed distributions whose recorded files are missing from disk.

pip can leave a distribution installed with its files deleted: when a package
splits its modules into a second distribution (FastMCP 4 moved ``fastmcp/`` into
``fastmcp-slim``), an in-place upgrade installs the new owner's files, then
uninstalls the old distribution, whose RECORD lists the same paths, and deletes
them. Metadata still says installed, ``pip check`` passes, and the import fails.

Prints one ``name==version`` per damaged distribution, so the caller can
reinstall exactly that version with ``--no-deps``. Bytecode caches and files
recorded outside the install directory (console scripts) are not checked: a
missing one does not break an import and reinstalling for it is churn.

Two kinds of entry are never named, because reinstalling them does harm:

- pip's uninstall stash (a ``~``-prefixed dist-info, such as
  ``~itellm-1.78.7.dist-info``). pip ignores it ("Ignoring invalid
  distribution"), but ``importlib.metadata`` reports it under the real name, so
  naming it force-reinstalls an old version over the one just installed.
- A distribution that records a file another installed distribution also
  records. A fork that installs into the same package (crawl4ai's
  ``unclecode-litellm`` writes ``litellm/``) loses files whenever the other
  distribution is upgraded, and reinstalling it would overwrite that package
  with the fork's files, so it is reported on stderr instead. Namespace
  packages (``google/``, ``jaraco/``) share a directory but no file, and are
  still repaired. The refusal is symmetric: if the real package is the one with
  missing files, it is refused too, and the warning says which pair to resolve.

Run with the venv's own interpreter (``-P``, so the checkout is not on the path).
Exit 0 always; nothing printed means nothing is damaged.
"""

from __future__ import annotations

import csv
import sys
from importlib import metadata
from pathlib import Path


def _is_stash(dist) -> bool:
    # ``_path`` is private to PathDistribution, the only kind a venv yields. If a
    # future Python drops it, stashes are named again (the old downgrade), so a
    # test pins this.
    path = getattr(dist, "_path", None)
    return path is not None and Path(str(path)).name.startswith("~")


def _checked(path: str) -> bool:
    return not (
        path.startswith("..")
        or "__pycache__" in path
        or path.endswith(".pyc")
        or ".dist-info/" in path
    )


def _record_paths(dist) -> list[str]:
    record = dist.read_text("RECORD")
    if not record:
        return []
    return [row[0] for row in csv.reader(record.splitlines()) if row and _checked(row[0])]


def damaged(dists=None, warn=None) -> list[str]:
    # RECORD is parsed here, not read through Distribution.files: from Python
    # 3.12 that property drops every entry whose file is missing, which is the
    # one condition this looks for.
    warn = warn if warn is not None else (lambda line: print(line, file=sys.stderr))
    # Each distribution's name and version are read ONCE: METADATA is parsed on
    # every access (per RECORD row it was 17x slower), and a distribution pip is
    # still writing can answer differently between reads.
    candidates = []
    for dist in dists if dists is not None else metadata.distributions():
        if _is_stash(dist):
            continue
        name, version = dist.metadata["Name"], dist.version
        if name and version:
            candidates.append((dist, name, version, _record_paths(dist)))
    owners: dict[str, set[str]] = {}
    for _dist, name, _version, paths in candidates:
        for path in paths:
            owners.setdefault(path, set()).add(name)
    found = []
    for dist, name, version, paths in candidates:
        if not any(not dist.locate_file(path).exists() for path in paths):
            continue
        # The WHOLE record is compared, not only the missing files: the fork's
        # missing files are exactly the ones the upgraded package stopped
        # recording, so they would never overlap.
        others = sorted(set().union(*(owners[path] for path in paths)) - {name})
        if others:
            warn(
                f"NOT reinstalling {name}=={version}: it records files that "
                f"{', '.join(others)} also installs, so reinstalling it would "
                "overwrite them."
            )
            continue
        found.append(f"{name}=={version}")
    return sorted(set(found))


def main() -> int:
    for spec in damaged():
        print(spec)
    return 0


if __name__ == "__main__":
    sys.exit(main())
