"""``<venv python> -m genesis.trash list | restore ENTRY [--to PATH]``.

Exit codes: 0 done, 1 refused (the reason is on stderr), 64 usage error.
"""

from __future__ import annotations

import argparse
import sys

from genesis.trash import TrashRefused, list_entries, restore

EXIT_REFUSED = 1
EXIT_USAGE = 64


class _Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message: str):  # argparse would exit 2; 64 is the usage code
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        sys.exit(EXIT_USAGE)


def _list() -> int:
    entries = list_entries()
    if not entries:
        print("trash is empty")
        return 0
    for e in entries:
        t = e.tombstone
        if t is None:
            print(f"{e.path.name}  (no readable tombstone)  {e.path}")
            continue
        size = "?" if t.size is None else f"{t.size} B"
        state = "" if e.complete else "  INCOMPLETE (item missing)"
        print(f"{t.entry_id}  {t.kind} {size}  {t.original_path}  [{t.caller}: {t.reason}]{state}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(prog="genesis.trash", description="List or restore trashed items.")
    sub = parser.add_subparsers(dest="cmd", required=True, parser_class=_Parser)
    sub.add_parser("list", help="every trash entry, oldest first")
    rs = sub.add_parser("restore", help="move an entry's item back")
    rs.add_argument("entry", help="the entry id shown by `list`")
    rs.add_argument("--to", help="restore here instead of the original path")
    args = parser.parse_args(argv)
    if args.cmd == "list":
        return _list()
    try:
        dest = restore(args.entry, to=args.to)
    except TrashRefused as exc:
        print(f"genesis.trash: refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    print(f"restored to {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
