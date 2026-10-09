#!/usr/bin/env python3
"""Owned-filesystem transport model; not a real SMB protocol server."""

import os
import shlex
import shutil
import sys
from pathlib import Path


def main():
    command = sys.argv[sys.argv.index("-c") + 1]
    with open(os.environ["SMB_LOG"], "a") as stream:
        stream.write(command + "\n")
    cwd = Path(os.environ["SMB_ROOT"])
    if "-D" in sys.argv:
        cwd = cwd / sys.argv[sys.argv.index("-D") + 1]
        if not cwd.is_dir():
            print(f"cd {cwd}: NT_STATUS_OBJECT_PATH_NOT_FOUND")
            return 1
    for clause in command.split(";"):
        args = shlex.split(clause)
        if not args:
            continue
        operation, *values = args
        if operation == "cd":
            cwd = cwd / values[0]
            if not cwd.is_dir():
                print("NT_STATUS_OBJECT_PATH_NOT_FOUND")
                return 1
        elif operation == "mkdir":
            (cwd / values[0]).mkdir(parents=True, exist_ok=True)
        elif operation == "put":
            target = cwd / values[1]
            match = os.environ.get("SMB_CORRUPT_MATCH")
            if match and match in values[1]:
                target.write_bytes(b"acknowledged-but-corrupt")
            else:
                shutil.copyfile(values[0], target)
        elif operation == "get":
            source = cwd / values[0]
            if not source.is_file():
                print("NT_STATUS_OBJECT_NAME_NOT_FOUND")
                return 1
            shutil.copyfile(source, values[1])
        elif operation == "rename":
            source, target = (cwd / name for name in values[:2])
            if target.exists() and values[2:] != ["-f"]:
                print("NT_STATUS_OBJECT_NAME_COLLISION")
                return 1
            os.replace(source, target)
        elif operation == "ls":
            entries = [cwd / values[0]] if values else sorted(cwd.iterdir())
            for entry in entries:
                if entry.exists():
                    print(f"  {entry.name} {'D' if entry.is_dir() else 'A'} 0 Synthetic")
        elif operation == "deltree":
            target = cwd / values[0]
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
        else:
            raise AssertionError(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
