#!/usr/bin/env python3
"""Copy one stream from a FIFO to stdout with every known secret value replaced by its placeholder.

argv: <done-file> <drained-file> [repo-root]. Run by ``redact_run.sh``, one per stream (stdout, stderr). The
values come from ``known_values.py`` beside it, in memory: they are never written to a file.

One rule decides what is written: everything except the longest tail that is the start of a known value. That
tail is held until later bytes settle it (the value completes and is replaced, or the text turns out to be
something else) or the input ends. The tail is found in the text as read, before anything is replaced, so a
shorter value inside the start of a longer one is not replaced while the longer one could still complete. So a
value split across reads, across lines or across the command's exit is replaced whole, and ordinary output still
goes out as it arrives: a tail is held only while it could still become a value. When the input ends, a held tail
is written as it is (output that stops partway through a value shows that part, the plugin's exact-match rule).
Bytes are decoded incrementally, so a character split between two reads is one character when it is matched.

Replacement is one pass, as the mod's ``redactText`` is: text a replacement produced is never matched again (a
value may equal another variable's name), and placeholders this filter issued are matched as themselves.

The values are re-read whenever the secrets file changes (checked on every read). A reload that fails is tried
again, at most once per ``RETRY_S``, until one succeeds: the stamp moves only with a successful load. A value
this filter has read stays redacted after it leaves the file: a long-running Monitor outlives a key rotation.
Its placeholder gets a name of its own (``~earlier``) when a current value now has the name, the mod's scheme,
so one placeholder never stands for two texts. (A value replaced before the filter has started, in the command's
first instant, was never read and is not kept.)

When the wrapped command has exited (the runner creates ``done-file``) and nothing is waiting in the pipe, or
when the input ends, this signals ``drained-file`` so the runner can exit with the command's status on time.
After the first of those it keeps reading until every writer has closed, so a background child the command
left behind never gets SIGPIPE: it forwards while its reader is there and discards once that reader is gone.
That matches what the same command does unwrapped (measured: the child survives, late output still reaches a
background task's output file).
"""

from __future__ import annotations

import codecs
import contextlib
import importlib.util
import os
import re
import select
import sys
import time
from pathlib import Path

#: How often a filter whose last load failed tries again.
RETRY_S = 1.0


class Values:
    """The known values and their placeholders, kept current with the secrets file.

    ANY failure to read them means pass-through, never a dead filter: a filter that dies leaves the command
    writing into a FIFO with no reader, which loses its output and can kill it (measured: a resolver that raised
    did exactly that). The wrap is a privacy measure, never a reason to stop work.
    """

    def __init__(self, root_arg: str | None) -> None:
        self.pairs: list[tuple[str, str]] = []  # (text, placeholder), longest first
        self._by_text: dict[
            str, str
        ] = {}  # every text and every issued placeholder -> what it is written as
        self._matcher: re.Pattern[str] | None = None
        self._root_arg = root_arg
        self._known = None
        self._watch: list[
            str
        ] = []  # every path that can change which file the resolver picks, or what it holds
        self._stamp: tuple | None = None
        self._said = False
        self._failed = False  # the last load attempt failed
        self._tried = 0.0  # when a load was last attempted (monotonic)
        self._load()

    @staticmethod
    def _stamp_of(paths: list[str]) -> tuple:
        stamps = []
        for path in paths:
            try:
                st = os.stat(path)
                stamps.append((st.st_mtime_ns, st.st_size))
            except Exception:  # noqa: BLE001 - any unreadable path is "absent", as register.js statStamp treats it
                stamps.append((0, 0))
        return tuple(stamps)

    @staticmethod
    def _watch_of(loaded: dict) -> list[str]:
        return list(loaded.get("watch") or [loaded["path"]])

    def _load(self) -> None:
        """Read the values and commit them all at once, or commit nothing (register.js ``load``, the same shape).

        Every read and every value derived from it is built aside; only when all of it has succeeded are the values,
        the watched paths and the stamp replaced together. Any failure (a read that fails, the confirming read
        included, or a loader result of an unexpected shape) leaves everything as it was and marks the load failed,
        so it is tried again within ``RETRY_S`` and the filter never dies.
        """
        self._tried = time.monotonic()
        try:
            # Stamped BEFORE the read, so a change made while the file is read is seen at the next check.
            before = self._stamp_of(self._watch) if self._watch else None
            loaded = self._read()
            watch = self._watch_of(loaded)
            if watch != self._watch:
                # The resolver's candidates changed (first load, or a new location): stamp those, then read again
                # so the stamp is never newer than the values it stands for.
                before = self._stamp_of(watch)
                loaded = self._read()
                watch = self._watch_of(loaded)
            if not all(isinstance(path, str) for path in watch):
                raise TypeError("a watched path that is not text")
            current = [(n["text"], n["placeholder"]) for n in loaded["needles"]]
            if not all(isinstance(t, str) and isinstance(p, str) for t, p in current):
                raise TypeError("a needle that is not text")
            current = [pair for pair in current if pair[0]]  # an empty text would match everywhere
            texts = {text for text, _ in current}
            names = {placeholder for _, placeholder in current}
            gone = [pair for pair in self.pairs if pair[0] not in texts]
            # A held value keeps the name it was shown under, so a placeholder already in the output still means
            # the same text; only a name a current value now uses is renamed (register.js load, the same scheme).
            taken = names | {placeholder for _, placeholder in gone}
            kept = []
            for text, placeholder in gone:
                name = placeholder
                if placeholder in names:
                    n = 1
                    while name in taken:
                        name = re.sub(
                            r"\]$", "~earlier]" if n == 1 else f"~earlier{n}]", placeholder
                        )
                        n += 1
                    taken.add(name)
                kept.append((text, name))
            pairs = sorted(current + kept, key=lambda pair: -len(pair[0]))
            by_text = {text: placeholder for text, placeholder in pairs}
            for _, placeholder in pairs:
                by_text[placeholder] = placeholder
            alternatives = sorted(by_text, key=len, reverse=True)
            matcher = re.compile("|".join(map(re.escape, alternatives))) if alternatives else None
        except Exception as exc:  # noqa: BLE001 - see the class docstring
            self._failed = True
            self._report(
                exc
            )  # after the decision: nothing was committed, so the values in use are the old ones
            return
        self.pairs = pairs
        self._by_text = by_text
        self._matcher = matcher
        self._watch = watch
        self._stamp = before
        self._failed = False
        self._said = False  # a later failure is a new event and is reported again

    def _report(self, exc: BaseException) -> None:
        """Say once per run of failures that the values could not be read, and what that means for the output."""
        if self._said:
            return
        self._said = True
        state = "keeping the values read earlier" if self.pairs else "this output is NOT redacted"
        # A closed stderr must not stop the filter draining the command's output.
        with contextlib.suppress(OSError):
            print(
                f"secret-redact: cannot read the secret values ({type(exc).__name__}); {state}",
                file=sys.stderr,
            )

    def _read(self) -> dict:
        """One loader read. Raises on any failure; ``_load`` is the one place that handles it."""
        if self._known is None:
            spec = importlib.util.spec_from_file_location(
                "_redact_known_values", Path(__file__).with_name("known_values.py")
            )
            if spec is None or spec.loader is None:
                raise ImportError("known_values.py")
            # Kept only once its code has run: a module whose execution failed would otherwise stay cached,
            # half-built, and every later read would fail.
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self._known = module
        return self._known.load(self._known.repo_root(self._root_arg))

    def refresh(self) -> None:
        """Reload when any watched path (the secrets file, or a location the resolver would pick) changed.

        A failed load leaves the stamp unchanged, so it is tried again, at most once per ``RETRY_S``: a secrets
        file that was briefly unreadable (a rotation's chmod, a replace mid-read) must not leave a long Monitor
        raw for new values, and a restored permission changes nothing a stamp sees.
        """
        if self._failed and time.monotonic() - self._tried < RETRY_S:
            return
        if not self._watch:
            # No load has ever succeeded, so there is nothing to compare: try again.
            self._load()
            return
        if self._failed or self._stamp_of(self._watch) != self._stamp:
            self._load()

    def redact(self, text: str) -> str:
        """Every known value replaced by its placeholder, in one pass."""
        return self.process(text, final=True)[0]

    def process(self, text: str, *, final: bool = False) -> tuple[str, str]:
        """``(written, held)``: ``text`` up to its held tail with every value replaced, and the tail unreplaced.

        The tail is found in the text as read. A value that starts before the tail but runs into it is held too,
        whole: replacing it now would leave the rest of a longer value unmatched. With ``final`` nothing is held.
        """
        hold_from = len(text) if final else len(text) - self.held(text)
        out: list[str] = []
        i = 0
        if self._matcher is not None:
            for m in self._matcher.finditer(text):
                if m.start() >= hold_from:
                    break
                if m.end() > hold_from:
                    hold_from = m.start()
                    break
                out.append(text[i : m.start()])
                out.append(self._by_text.get(m.group(), m.group()))
                i = m.end()
        out.append(text[i:hold_from])
        return "".join(out), text[hold_from:]

    def held(self, text: str) -> int:
        """Length of the longest tail of ``text`` that is a proper prefix of a known value or an issued placeholder."""
        best = 0
        for needle in self._by_text:
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
        # Seen BEFORE the wait: the command writes, exits, and only then is `done` made, so whatever it wrote is
        # already readable when this wait starts. Checked after the wait instead, the command could finish its
        # last write in between and be signalled drained with that output unread.
        exited = os.path.exists(done)
        try:
            ready, _, _ = select.select([fd], [], [], 0.1)
        except InterruptedError:
            continue  # nothing was observed: look again, never treat an interrupted wait as an empty one
        if ready:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            values.refresh()
            written, pending = values.process(pending + decoder.decode(chunk))
            emit(written)
        elif exited:
            # The command has exited and its output is read. A held tail stays held: a child the command left
            # behind may still write the rest of that value.
            signal()
    emit(values.process(pending + decoder.decode(b"", final=True), final=True)[0])
    signal()
    return 0


if __name__ == "__main__":
    sys.exit(main())
