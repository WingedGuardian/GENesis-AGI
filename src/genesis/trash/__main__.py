"""``<venv python> -m genesis.trash list | restore ENTRY [--to PATH]``.

Exit codes: 0 done, 1 refused (the reason is on stderr), 64 usage error.
"""

from __future__ import annotations

import argparse
import os
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


def _show(value: object) -> str:
    """Printable on any terminal: non-UTF-8 bytes as backslash escapes, control
    characters (a newline in a name) escaped, so one entry stays one line."""
    s = str(value)
    try:
        raw = os.fsencode(s)
    except UnicodeEncodeError:  # a lone surrogate from a hand-edited tombstone
        raw = s.encode("utf-8", "surrogatepass")
    text = raw.decode("utf-8", "backslashreplace")
    return "".join(c if c.isprintable() else repr(c)[1:-1] for c in text)


def _list() -> int:
    try:
        entries = list_entries()
    except TrashRefused as exc:
        print(f"genesis.trash: refused: {_show(exc)}", file=sys.stderr)
        return EXIT_REFUSED
    if not entries:
        print("trash is empty")
        return 0
    for e in entries:
        t = e.tombstone
        if t is None:
            print(f"{_show(e.path.name)}  (no readable tombstone)  {_show(e.path)}")
            continue
        size = "?" if t.size is None else f"{t.size} B"
        state = "" if e.complete else "  INCOMPLETE (no item in the entry)"
        print(
            f"{_show(t.entry_id)}  {_show(t.kind)} {size}  {_show(t.original_path)}"
            f"  [{_show(t.caller)}: {_show(t.reason)}]{state}"
        )
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
        print(f"genesis.trash: refused: {_show(exc)}", file=sys.stderr)
        return EXIT_REFUSED
    print(f"restored to {_show(dest)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
