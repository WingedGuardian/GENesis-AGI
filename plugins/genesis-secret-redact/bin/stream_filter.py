#!/usr/bin/env python3
"""Copy one stream from a FIFO to stdout with every known secret value replaced by its placeholder.

argv: <done-file> <drained-file> [repo-root]. Run by ``redact_run.sh``, one per stream (stdout, stderr). The
values come from ``known_values.py`` beside it, in memory: they are never written to a file.

Output goes out a line at a time, so a Monitor sees each line as it happens. A line longer than ``_FLUSH_AT``
is emitted in pieces: the text is redacted first and only the part that cannot be the start of a value still
arriving is emitted (the last ``longest needle - 1`` characters are held), so a value split across reads is
replaced whole and no prefix of one is ever written.

Once the wrapped command has exited (the runner creates ``done-file``) and nothing is waiting in the pipe,
this signals ``drained-file`` so the runner can exit with the command's status on time. It then keeps reading
until every writer has closed, so a background child the command left behind never gets SIGPIPE: it forwards
while its reader is there and discards once that reader is gone. That matches what the same command does
unwrapped (measured: the child survives, late output still reaches a background task's output file).
"""

from __future__ import annotations

import importlib.util
import os
import select
import sys
from pathlib import Path

#: A pending line longer than this is emitted in pieces (bounds memory for output that never ends a line).
_FLUSH_AT = 64 * 1024


def _needle_pairs(root_arg: str | None) -> list[tuple[str, str]]:
    """(value spelling, placeholder) pairs, longest first; empty when the values cannot be read.

    ANY failure here means pass-through, never a dead filter: a filter that dies leaves the command writing into a
    FIFO with no reader, which loses its output and can kill it (measured: a resolver that raised did exactly
    that). The wrap is a privacy measure, never a reason to stop work.
    """
    try:
        spec = importlib.util.spec_from_file_location(
            "_redact_known_values", Path(__file__).with_name("known_values.py")
        )
        if spec is None or spec.loader is None:
            raise ImportError("known_values.py")
        known = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(known)
        needles = known.load(known.repo_root(root_arg))["needles"]
    except Exception as exc:  # noqa: BLE001 - see the docstring
        print(
            f"secret-redact: cannot read the secret values ({type(exc).__name__}); "
            "this output is NOT redacted",
            file=sys.stderr,
        )
        return []
    return [(n["text"], f"[secrets.env:{n['var']}]") for n in needles]


def main() -> int:
    done, drained_flag = sys.argv[1], sys.argv[2]
    pairs = _needle_pairs(sys.argv[3] if len(sys.argv) > 3 else None)
    hold = max((len(text) for text, _ in pairs), default=1) - 1
    fd = sys.stdin.fileno()
    out = sys.stdout.buffer
    consumer = True
    drained = False
    pending = ""

    def redact(text: str) -> str:
        for needle, placeholder in pairs:
            text = text.replace(needle, placeholder)
        return text

    def emit(text: str) -> None:
        nonlocal consumer
        if not consumer or not text:
            return
        try:
            out.write(text.encode("utf-8", "surrogateescape"))
            out.flush()
        except OSError:  # the reader went away: keep draining, stop forwarding
            consumer = False

    while True:
        try:
            ready, _, _ = select.select([fd], [], [], 0.1)
        except InterruptedError:
            ready = []
        if ready:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            pending = redact(pending + chunk.decode("utf-8", "surrogateescape"))
            cut = pending.rfind("\n") + 1
            if cut == 0 and len(pending) > _FLUSH_AT:
                cut = len(pending) - hold
            emit(pending[:cut])
            pending = pending[cut:]
        elif not drained and os.path.exists(done):
            emit(redact(pending))
            pending = ""
            with open(drained_flag, "w"):
                pass
            drained = True
    emit(redact(pending))
    return 0


if __name__ == "__main__":
    sys.exit(main())
