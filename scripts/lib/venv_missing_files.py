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

Run with the venv's own interpreter (``-P``, so the checkout is not on the path).
Exit 0 always; nothing printed means nothing is damaged.
"""

from __future__ import annotations

import csv
import sys
from importlib import metadata


def damaged(dists=None) -> list[str]:
    # RECORD is parsed here, not read through Distribution.files: from Python
    # 3.12 that property drops every entry whose file is missing, which is the
    # one condition this looks for.
    found = []
    for dist in dists if dists is not None else metadata.distributions():
        name = dist.metadata["Name"]
        record = dist.read_text("RECORD")
        if not name or not record:
            continue
        for row in csv.reader(record.splitlines()):
            if not row:
                continue
            path = row[0]
            if path.startswith("..") or "__pycache__" in path or path.endswith(".pyc"):
                continue
            if not dist.locate_file(path).exists():
                found.append(f"{name}=={dist.version}")
                break
    return sorted(set(found))


def main() -> int:
    for spec in damaged():
        print(spec)
    return 0


if __name__ == "__main__":
    sys.exit(main())
