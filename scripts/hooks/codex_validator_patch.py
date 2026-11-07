"""Conservative local patch targets for native Codex 0.161.0.

Collect every possible file header; native Codex still validates the chunks.
This is accident prevention, not containment of arbitrary full-access programs,
disabled/failed outer hooks, or filesystem races after this check.
"""

from __future__ import annotations

import stat
from pathlib import Path

FILE_HEADERS = ("*** Add File: ", "*** Delete File: ",
                "*** Update File: ", "*** Move to: ")
PROTECTED_COMPONENTS = {".codex", ".agents", ".git"}
MAX_TARGETS = 4096


def patch_targets(command: str) -> list[str]:
    """Over-collect headers, including header-looking context; never execute."""
    # Rust lines accepts LF/CRLF. splitlines would also treat filename bytes
    # such as NEL as line separators and could hide a native target suffix.
    lines = command.strip().split("\n")
    if (len(lines) < 3 or lines[0].strip() != "*** Begin Patch"
            or lines[-1].strip() != "*** End Patch"):
        raise ValueError("Unsupported validator patch envelope")
    targets = []
    for raw in lines[1:-1]:
        line = raw.lstrip().removesuffix("\r")
        for prefix in FILE_HEADERS:
            if line.startswith(prefix):
                target = line[len(prefix):]
                if not target or target != target.strip():
                    raise ValueError("Unsupported validator patch filename")
                targets.append(target)
                break
        else:
            if line.startswith("*** ") and line.strip() != "*** End of File":
                raise ValueError("Unsupported validator patch directive")
        if len(targets) > MAX_TARGETS:
            raise ValueError("Validator patch has too many targets")
    if not targets:
        raise ValueError("Validator patch has no file targets")
    return targets


def check_patch(command: str, workspace: Path) -> None:
    """Admit all sources/destinations only within the trusted canonical root.

    Symlinks anywhere, special files, hardlinked leaves and protected instruction
    or configuration paths are refused. Missing parents are permitted; this
    function never creates them. A later symlink/hardlink swap remains a race.
    """
    for name in patch_targets(command):
        if any(ord(ch) < 32 or ord(ch) == 127 or ch in ":\\" for ch in name):
            raise ValueError("Unsupported validator patch filename")
        target = Path(name)
        if ".." in target.parts:
            raise ValueError("Validator patch cannot traverse parent paths")
        target = target if target.is_absolute() else workspace / target
        if not target.is_relative_to(workspace) or target == workspace:
            raise ValueError("Validator patch target is outside its workspace")
        parts = target.relative_to(workspace).parts
        if any(part in PROTECTED_COMPONENTS or part == "AGENTS.md" for part in parts):
            raise ValueError("Validator patch cannot change trusted configuration")
        current = workspace
        for index, part in enumerate(parts):
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            leaf = index == len(parts) - 1
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("Validator patch cannot follow symlinks")
            if leaf:
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("Validator patch requires an unshared regular file")
            elif not stat.S_ISDIR(info.st_mode):
                raise ValueError("Validator patch ancestor is not a directory")
