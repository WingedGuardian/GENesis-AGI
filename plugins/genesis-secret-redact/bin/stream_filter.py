#!/usr/bin/env python3
"""Copy one stream from a FIFO to stdout with every known secret value replaced by its placeholder.

argv: <done-file> <drained-file> [repo-root]. Run by ``redact_run.sh``, one per stream (stdout, stderr). The
values come from ``known_values.py`` beside it, in memory: they are never written to a file.

One rule decides what is written: everything except the longest tail that is the start of a known value. That
tail is held until later bytes settle it (the value completes and is replaced, or the text turns out to be
something else) or the input ends. So a value split across reads, across lines or across the command's exit is
replaced whole, and ordinary output still goes out as it arrives: a tail is held only while it could still
become a value. When the input ends, a held tail is written as it is (output that stops partway through a value
shows that part, the plugin's exact-match rule). Bytes are decoded incrementally, so a character split between
two reads is one character when it is matched.

The values are re-read whenever the secrets file changes (checked on every read), and a value this filter has
read stays redacted after it leaves the file: a long-running Monitor outlives a key rotation. (A value replaced
before the filter has started, in the command's first instant, was never read and is not kept.)

When the wrapped command has exited (the runner creates ``done-file``) and nothing is waiting in the pipe, or
when the input ends, this signals ``drained-file`` so the runner can exit with the command's status on time.
After the first of those it keeps reading until every writer has closed, so a background child the command
left behind never gets SIGPIPE: it forwards while its reader is there and discards once that reader is gone.
That matches what the same command does unwrapped (measured: the child survives, late output still reaches a
background task's output file).
"""

from __future__ import annotations

import codecs
import importlib.util
import os
import select
import sys
import time
from pathlib import Path

#: How often a filter whose values never loaded tries again.
RETRY_S = 1.0


class Values:
    """The known values and their placeholders, kept current with the secrets file.

    ANY failure to read them means pass-through, never a dead filter: a filter that dies leaves the command
    writing into a FIFO with no reader, which loses its output and can kill it (measured: a resolver that raised
    did exactly that). The wrap is a privacy measure, never a reason to stop work.
    """

    def __init__(self, root_arg: str | None) -> None:
        self.pairs: list[tuple[str, str]] = []  # (text, placeholder), longest first
        self._root_arg = root_arg
        self._known = None
        self._watch: list[
            str
        ] = []  # every path that can change which file the resolver picks, or what it holds
        self._stamp: tuple | None = None
        self._said = False
        self._tried = 0.0  # when a load that never succeeded was last attempted (monotonic)
        self._load()

    @staticmethod
    def _stamp_of(paths: list[str]) -> tuple:
        stamps = []
        for path in paths:
            try:
                st = os.stat(path)
                stamps.append((st.st_mtime_ns, st.st_size))
            except OSError:
                stamps.append((0, 0))
        return tuple(stamps)

    def _load(self) -> None:
        self._tried = time.monotonic()
        # Stamped BEFORE the read, so a change made while the file is read is seen at the next check.
        before = self._stamp_of(self._watch) if self._watch else None
        loaded = self._read()
        if loaded is None:
            return
        paths = list(loaded.get("watch") or [loaded["path"]])
        if paths != self._watch:
            # The resolver's candidates changed (first load, or a new location): stamp those, then read again so
            # the values match the stamp.
            self._watch = paths
            before = self._stamp_of(paths)
            loaded = self._read() or loaded
        self._stamp = before
        current = [(n["text"], n["placeholder"]) for n in loaded["needles"]]
        texts = {text for text, _ in current}
        kept = [pair for pair in self.pairs if pair[0] not in texts]
        self.pairs = sorted(current + kept, key=lambda pair: -len(pair[0]))

    def _read(self) -> dict | None:
        try:
            if self._known is None:
                spec = importlib.util.spec_from_file_location(
                    "_redact_known_values", Path(__file__).with_name("known_values.py")
                )
                if spec is None or spec.loader is None:
                    raise ImportError("known_values.py")
                self._known = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(self._known)
            return self._known.load(self._known.repo_root(self._root_arg))
        except Exception as exc:  # noqa: BLE001 - see the class docstring
            if not self._said:
                self._said = True
                state = (
                    "keeping the values read earlier"
                    if self.pairs
                    else "this output is NOT redacted"
                )
                print(
                    f"secret-redact: cannot read the secret values ({type(exc).__name__}); {state}",
                    file=sys.stderr,
                )
            return None

    def refresh(self) -> None:
        """Reload when any watched path (the secrets file, or a location the resolver would pick) changed."""
        if not self._watch:
            # No load has ever succeeded, so there is nothing to compare: try again, at most once a second (a
            # failure at start, such as the file being replaced mid-read, must not leave a long Monitor raw).
            if time.monotonic() - self._tried >= RETRY_S:
                self._load()
            return
        stamp = self._stamp_of(self._watch)
        if stamp != self._stamp:
            self._stamp = stamp  # a failed reload is not retried until a watched path changes again
            self._load()

    def redact(self, text: str) -> str:
        for needle, placeholder in self.pairs:
            text = text.replace(needle, placeholder)
        return text

    def held(self, text: str) -> int:
        """Length of the longest tail of ``text`` that is a proper prefix of a known value."""
        best = 0
        for needle, _ in self.pairs:
            limit = min(len(needle) - 1, len(text))
            if limit <= best:
                continue
            tail = text[len(text) - limit :]
            start = tail.find(needle[0])
            while start != -1 and len(tail) - start > best:
                if needle.startswith(tail[start:]):
                    best = len(tail) - start
                    break
                start = tail.find(needle[0], start + 1)
        return best


def main() -> int:
    done, drained_flag = sys.argv[1], sys.argv[2]
    values = Values(sys.argv[3] if len(sys.argv) > 3 else None)
    decoder = codecs.getincrementaldecoder("utf-8")("surrogateescape")
    fd = sys.stdin.fileno()
    out = sys.stdout.buffer
    consumer = True
    drained = False
    pending = ""

    def emit(text: str) -> None:
        nonlocal consumer
        if not consumer or not text:
            return
        try:
            out.write(text.encode("utf-8", "surrogateescape"))
            out.flush()
        except OSError:  # the reader went away: keep draining, stop forwarding
            consumer = False

    def signal() -> None:
        nonlocal drained
        if drained:
            return
        drained = True
        try:
            with open(drained_flag, "w"):
                pass
        except OSError:  # the runner gave up waiting and removed its directory
            pass

    while True:
        try:
            ready, _, _ = select.select([fd], [], [], 0.1)
        except InterruptedError:
            ready = []
        if ready:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            values.refresh()
            pending = values.redact(pending + decoder.decode(chunk))
            cut = len(pending) - values.held(pending)
            emit(pending[:cut])
            pending = pending[cut:]
        elif os.path.exists(done):
            # The command has exited and its output is read. A held tail stays held: a child the command left
            # behind may still write the rest of that value.
            signal()
    emit(values.redact(pending + decoder.decode(b"", final=True)))
    signal()
    return 0


if __name__ == "__main__":
    sys.exit(main())
