#!/usr/bin/env python3
"""Decide which browser-profile copies retention removes; print them, NUL-separated.

genesis.browser.profile_backup copies a profile to
``<profile>.pre-v<version>-<UTC time>[-<n>]`` before a different browser build first
opens it; ``v<version>`` (the label) is the build that had last opened it. This script
only decides. scripts/disk_hygiene.sh deletes what it prints, through its
mount-aware deleter, so a decision bug cannot reach past the names printed here.

Per profile:
- the newest copy overall is always kept;
- the newest copy of each label is kept for 90 days: going back to that build needs
  it, and going back (a pin-back) itself makes a newer copy of another label;
- older copies of a label go after 14 days;
- an unfinished ``<copy>.<random>.tmp`` goes after a day.

Only real directories whose names match exactly are considered; anything else in the
directory, including a symlink, is never printed.

usage: prune_browser_backups.py <dir holding the profiles> [--now <epoch seconds>]
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

PROFILES = ("camoufox-profile", "browser-profile")
_LABEL = r"v[0-9A-Za-z][0-9A-Za-z._+-]{0,63}"
_COPY = re.compile(
    rf"^(?P<profile>{'|'.join(PROFILES)})\.pre-(?P<label>{_LABEL})-\d{{8}}T\d{{6}}(?:-\d+)?$"
)
_UNFINISHED = re.compile(
    # TemporaryDirectory's random part: letters, digits and underscores only.
    rf"^(?:{'|'.join(PROFILES)})\.pre-{_LABEL}-\d{{8}}T\d{{6}}(?:-\d+)?\.[a-z0-9_]+\.tmp$"
)

DAY = 86400
OLDER_COPY_DAYS = 14
LABEL_NEWEST_DAYS = 90
UNFINISHED_DAYS = 1


def doomed(base: Path, now: float) -> list[Path]:
    copies: dict[str, list[tuple[float, str, Path]]] = {p: [] for p in PROFILES}
    out: list[Path] = []
    try:
        entries = list(base.iterdir())
    except OSError:
        return []
    for entry in entries:
        try:
            if entry.is_symlink() or not entry.is_dir():
                continue
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        name = entry.name
        if _UNFINISHED.fullmatch(name):
            if now - mtime > UNFINISHED_DAYS * DAY:
                out.append(entry)
            continue
        m = _COPY.fullmatch(name)
        if m:
            copies[m["profile"]].append((mtime, m["label"], entry))
    for found in copies.values():
        if not found:
            continue
        found.sort(key=lambda c: c[0], reverse=True)
        newest_overall = found[0][2]
        seen_labels: set[str] = set()
        for mtime, label, path in found:
            age = now - mtime
            newest_of_label = label not in seen_labels
            seen_labels.add(label)
            if path == newest_overall:
                continue
            limit = LABEL_NEWEST_DAYS if newest_of_label else OLDER_COPY_DAYS
            if age > limit * DAY:
                out.append(path)
    return out


def main(argv: list[str]) -> int:
    if not argv or len(argv) not in (1, 3) or (len(argv) == 3 and argv[1] != "--now"):
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    now = float(argv[2]) if len(argv) == 3 else time.time()
    for path in doomed(Path(argv[0]), now):
        sys.stdout.write(f"{path}\0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
