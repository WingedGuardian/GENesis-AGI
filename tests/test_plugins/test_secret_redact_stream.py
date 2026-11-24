"""genesis-secret-redact's stream wrap: the PreToolUse hook, the sourced runner and the stream filter.

Monitor and background Bash output reaches records Claude Code keeps itself, so it is redacted at the source:
`bin/stream_wrap.py` rewrites the command to run through `bin/redact_run.sh`, which passes stdout and stderr
each through `bin/stream_filter.py`. Canary values only.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.conftest import private_module
from tests.test_plugins.test_secret_redact_mod import _run as _run_mod

REPO = Path(__file__).resolve().parents[2]
BIN = REPO / "plugins" / "genesis-secret-redact" / "bin"
KEY = "ck_" + "a1b2c3d4e5f60718293a4b5c6d7e8f90"
KEY2 = "ck_" + "0f9e8d7c6b5a41322314a5b6c7d8e9f0"
TOKEN = "ct_Zx9-Yw8_Vu7Ts6Rq5Po4Nm3Lk2Ji1H"
P = "[secrets.env:CANARY_API_KEY]"

wrap = private_module("_test_stream_wrap", BIN / "stream_wrap.py")
stream = private_module("_test_stream_filter", BIN / "stream_filter.py")


@pytest.fixture
def secrets_file(tmp_path, monkeypatch):
    path = tmp_path / "secrets.env"
    path.write_text(f'CANARY_API_KEY={KEY}\nexport CANARY_SERVICE_TOKEN="{TOKEN}"\n')
    monkeypatch.setenv("SECRETS_PATH", str(path))
    return path


def _source(command: str, root: Path = REPO) -> str:
    return f"source {BIN / 'redact_run.sh'} {base64.b64encode(command.encode()).decode()} {root}"


def _run(command: str, tmp_path: Path, *, preamble: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f"{preamble}\n{_source(command)}"],
        capture_output=True,
        text=True,
        env={**os.environ, "TMPDIR": str(tmp_path)},
        timeout=60,
        cwd=tmp_path,
    )


def _run_to_file(command: str, tmp_path: Path) -> tuple[int, Path, float]:
    """Run with stdout and stderr on a file, as a background task's output file is written."""
    out = tmp_path / "task.output"
    start = time.monotonic()
    with out.open("w") as fh:
        rc = subprocess.run(
            ["bash", "-c", _source(command)],
            stdout=fh,
            stderr=subprocess.STDOUT,
            env={**os.environ, "TMPDIR": str(tmp_path)},
            timeout=60,
        ).returncode
    return rc, out, time.monotonic() - start


# --- the filter's one rule ----------------------------------------------------------------------------------


def _values(tmp_path, monkeypatch, text: str):
    path = tmp_path / "secrets.env"
    path.write_text(text)
    monkeypatch.setenv("SECRETS_PATH", str(path))
    return stream.Values(str(REPO))


def test_only_a_tail_that_could_become_a_value_is_held(tmp_path, monkeypatch):
    values = _values(tmp_path, monkeypatch, f"CANARY_API_KEY={KEY}\n")
    assert values.held("progress 50%") == 0
    assert values.held("a complete line\n") == 0
    assert values.held("see " + KEY[:5]) == 5
    assert values.held("see " + KEY[:-1]) == len(KEY) - 1
    assert values.held(KEY[:5] + " and more") == 0  # it has already turned out to be something else
    # A value in full is not a PROPER prefix: it is replaced, never held.
    assert values.held(values.redact("x " + KEY)) == 0


def test_a_multiline_values_first_line_is_held(tmp_path, monkeypatch):
    values = _values(tmp_path, monkeypatch, 'PRIVATE_KEY="short-line-a\nshort-line-b"\n')
    assert values.held("short-line-a\n") == len("short-line-a\n")


def test_a_value_equal_to_another_variables_name_is_replaced_once(tmp_path, monkeypatch):
    # One pass, as the mod's redactText: a second pass would rewrite the name inside the placeholder it just
    # produced, and a placeholder already in the output is matched as itself.
    values = _values(
        tmp_path,
        monkeypatch,
        "NAME_TOKEN=OTHER_SERVICE_TOKEN\nOTHER_SERVICE_TOKEN=made-up-secret-1234567890\n",
    )
    placeholder = "[secrets.env:OTHER_SERVICE_TOKEN]"
    assert values.redact("x made-up-secret-1234567890 y") == f"x {placeholder} y"
    assert values.redact(f"seen {placeholder}") == f"seen {placeholder}"
    assert values.redact("OTHER_SERVICE_TOKEN") == "[secrets.env:NAME_TOKEN]"


def test_a_shorter_value_inside_a_longer_ones_start_waits_for_it(tmp_path, monkeypatch):
    # The first line of the key is a value of its own. Replacing it before the rest arrives would leave the rest of
    # the key unmatched, so the hold is found in the text as read.
    values = _values(tmp_path, monkeypatch, 'PRIVATE_KEY="abcdefghijklmnopqrst\nshort-tail"\n')
    assert values.process("abcdefghijklmnopqrst\n") == ("", "abcdefghijklmnopqrst\n")
    assert values.process("abcdefghijklmnopqrst\nshort-tail\n") == (
        "[secrets.env:PRIVATE_KEY]\n",
        "",
    )
    # It turned out to be only the shorter value: replaced once settled.
    assert (
        values.process("abcdefghijklmnopqrst\nother\n")[0] == "[secrets.env:PRIVATE_KEY#1]\nother\n"
    )


def test_a_failed_reload_is_tried_again_until_it_succeeds(tmp_path, monkeypatch):
    # A rotation the filter could not read (the file briefly unreadable) must not leave the new value raw until
    # some later change: a restored permission changes nothing the stamp sees.
    values = _values(tmp_path, monkeypatch, f"CANARY_API_KEY={KEY}\n")
    (tmp_path / "secrets.env").write_text(f"CANARY_API_KEY={KEY2}\n")
    real = values._known.load
    calls = []

    def failing(*a, **kw):
        calls.append(1)
        raise PermissionError("unreadable for a moment")

    monkeypatch.setattr(values._known, "load", failing)
    values.refresh()
    assert calls and values.redact(KEY2) == KEY2
    values.refresh()  # within RETRY_S: not tried again yet
    assert len(calls) == 1
    monkeypatch.setattr(values._known, "load", real)
    monkeypatch.setattr(stream, "RETRY_S", 0.0)
    values.refresh()
    assert values.redact(KEY2) == P


@pytest.mark.parametrize(
    "result",
    [
        {"path": "x"},  # no needles
        {"path": "x", "needles": [{"text": None, "placeholder": "[secrets.env:A]"}]},
        {"path": "x", "needles": "not a list"},
        ["not", "a", "dict"],
    ],
)
def test_a_loader_result_of_the_wrong_shape_is_a_failed_load(tmp_path, monkeypatch, result):
    # The filter must not die on it (the command would lose its output): the values read earlier stay, and the
    # load is tried again until one succeeds.
    values = _values(tmp_path, monkeypatch, f"CANARY_API_KEY={KEY}\n")
    (tmp_path / "secrets.env").write_text(f"CANARY_API_KEY={KEY2}\n")
    real = values._known.load
    monkeypatch.setattr(values._known, "load", lambda *a, **kw: result)
    values.refresh()
    assert values.redact(KEY) == P
    monkeypatch.setattr(values._known, "load", real)
    monkeypatch.setattr(stream, "RETRY_S", 0.0)
    values.refresh()
    assert values.redact(KEY2) == P


@pytest.mark.parametrize("bad", ["bad\0path", 7])
def test_a_watched_path_os_stat_cannot_take_does_not_stop_the_filter(tmp_path, monkeypatch, bad):
    # The confirming read's watch list is committed with the values. An entry os.stat raises something other than
    # OSError on (a NUL byte) or one that is not text must not escape refresh(): the filter would die and the
    # command lose its output. register.js statStamp treats any error as an absent file.
    values = _values(tmp_path, monkeypatch, f"CANARY_API_KEY={KEY}\n")
    real = values._known.load
    calls = []

    def load(*a, **kw):
        # Only the CONFIRMING read names the bad path, so it is committed without ever being stamped in _load.
        calls.append(1)
        got = dict(real(*a, **kw))
        if len(calls) >= 2:
            got["watch"] = [*(got.get("watch") or [got["path"]]), bad]
        return got

    monkeypatch.setattr(values._known, "load", load)
    values._watch, values._stamp = [], None  # as on a first load, so the watch list is replaced
    values._load()
    monkeypatch.setattr(stream, "RETRY_S", 0.0)
    values.refresh()  # must not raise
    values.refresh()
    if isinstance(bad, str):
        assert values.redact(KEY) == P  # committed; the unstatable path just reads as absent
    else:
        assert (
            values._failed
        )  # a non-text entry fails the load, which is retried, and the filter lives


def test_a_failed_confirming_read_does_not_stand_for_the_newer_stamp(tmp_path, monkeypatch):
    # A load on a new location reads, stamps, then reads again. The key rotates between the first read and the
    # stamp, and the second read fails: the first read's values must not be committed under the newer stamp, or the
    # new key would stay raw until some unrelated change.
    values = _values(tmp_path, monkeypatch, f"CANARY_API_KEY={KEY}\n")
    real = values._known.load
    calls = []

    def load(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            got = real(*a, **kw)
            (tmp_path / "secrets.env").write_text(
                f"CANARY_API_KEY={KEY2}\n"
            )  # rotated before the stamp
            return got
        raise PermissionError("unreadable for a moment")

    monkeypatch.setattr(values._known, "load", load)
    values._watch, values._stamp = [], None  # as on a first load, or a new location
    values._load()
    assert len(calls) == 2
    assert values.redact(KEY) == P  # the first read's values are still used
    monkeypatch.setattr(values._known, "load", real)
    monkeypatch.setattr(stream, "RETRY_S", 0.0)
    values.refresh()
    assert values.redact(KEY2) == P


def test_a_values_module_that_failed_to_run_is_not_kept(tmp_path, monkeypatch):
    # The module is cached only once its code has run; a failed run is retried at the next read.
    values = _values(tmp_path, monkeypatch, f"CANARY_API_KEY={KEY}\n")
    values._known = None
    real_spec = stream.importlib.util.spec_from_file_location
    runs = []

    class Loader:
        def __init__(self, inner):
            self.inner = inner

        def create_module(self, spec):
            return self.inner.create_module(spec)

        def exec_module(self, module):
            runs.append(1)
            if len(runs) == 1:
                raise OSError("read failed for a moment")
            self.inner.exec_module(module)

    def spec_from_file_location(name, location, *a, **kw):
        spec = real_spec(name, location, *a, **kw)
        if name == "_redact_known_values":  # the values module only, not what it loads in turn
            spec.loader = Loader(spec.loader)
        return spec

    monkeypatch.setattr(stream.importlib.util, "spec_from_file_location", spec_from_file_location)
    with pytest.raises(OSError):
        values._read()
    assert values._known is None
    assert values._read()["needles"]
    assert len(runs) == 2


def test_output_written_just_before_the_command_exits_is_drained_first(tmp_path, monkeypatch):
    # The command writes its last line and exits while the filter is waiting for input. The filter must not then
    # signal drained with that line unread: the runner would return before the task file holds it.
    done, drained = tmp_path / "done", tmp_path / "drained"
    monkeypatch.setenv("SECRETS_PATH", str(tmp_path / "absent.env"))
    r, w = os.pipe()
    real_select = stream.select.select
    first = []

    def select(rlist, wlist, xlist, timeout=None):
        if not first:
            first.append(1)
            os.write(w, b"last line\n")  # written, then the command exits and done is made...
            os.close(w)
            done.touch()
            return [], [], []  # ...all after the wait had already come back empty
        return real_select(rlist, wlist, xlist, timeout)

    class Out:
        buffer = __import__("io").BytesIO()

    class In:
        @staticmethod
        def fileno():
            return r

    seen_at_signal = []
    real_open = open

    def opener(path, *a, **kw):
        if str(path) == str(drained):
            seen_at_signal.append(Out.buffer.getvalue())
        return real_open(path, *a, **kw)

    monkeypatch.setattr(stream.select, "select", select)
    monkeypatch.setattr(stream, "open", opener, raising=False)
    monkeypatch.setattr(sys, "stdin", In)
    monkeypatch.setattr(sys, "stdout", Out)
    monkeypatch.setattr(sys, "argv", ["stream_filter.py", str(done), str(drained)])
    assert stream.main() == 0
    os.close(r)
    assert seen_at_signal and seen_at_signal[0] == b"last line\n"


def test_a_failed_fifo_setup_under_errexit_still_runs_the_command(secrets_file, tmp_path):
    # mkfifo fails after the temp folder exists; the cleanup of FIFOs that were never made must not end the shell
    # under the caller's `set -e` before the warning and the unwrapped command.
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "mkfifo").write_text("#!/bin/sh\nexit 1\n")
    (shim / "mkfifo").chmod(0o755)
    run = subprocess.run(
        ["bash", "-c", f"set -e\n{_source('echo ran; exit 6')}"],
        capture_output=True,
        text=True,
        env={**os.environ, "TMPDIR": str(tmp_path), "PATH": f"{shim}:{os.environ['PATH']}"},
        timeout=60,
    )
    assert run.returncode == 6 and run.stdout == "ran\n"
    assert "NOT redacted" in run.stderr
    assert not list(tmp_path.glob("secret-redact.*"))


def test_the_filter_and_the_mod_redact_alike(tmp_path, monkeypatch):
    # The filter carries its own copy of the mod's rules (one pass, placeholders as themselves, ~earlier names on
    # rotation), because the mod is JavaScript in Claude Code and the filter runs beside the command. This drives
    # both through one corpus so a drift in either shows here.
    key3 = "ck_" + "aa11bb22cc33dd44ee55ff6677889900"
    first = (
        "TOKEN_ENV_NAME=CANARY_SERVICE_TOKEN\n"
        f"CANARY_SERVICE_TOKEN={KEY}\n"
        'PRIVATE_KEY="abcdefghijklmnopqrst\nshort-tail"\n'
    )
    steps = [
        {"in": f"val {KEY}"},
        {"in": "CANARY_SERVICE_TOKEN"},
        {"in": "seen [secrets.env:CANARY_SERVICE_TOKEN] and [secrets.env:TOKEN_ENV_NAME]"},
        {"in": f"{KEY}{KEY}"},
        {"in": "[secrets.env:" + KEY + "]"},
        {"in": "abcdefghijklmnopqrst\nshort-tail and abcdefghijklmnopqrst alone"},
        {"write": f"TOKEN_ENV_NAME=CANARY_SERVICE_TOKEN\nCANARY_SERVICE_TOKEN={KEY2}\n"},
        {"in": f"new {KEY2} old {KEY}"},
        {"in": "[secrets.env:CANARY_SERVICE_TOKEN~earlier]"},
        {"write": f"CANARY_SERVICE_TOKEN={key3}\n"},
        {"in": f"{KEY} {KEY2} {key3} CANARY_SERVICE_TOKEN"},
    ]
    mod_dir = tmp_path / "mod"
    mod_dir.mkdir()
    res = _run_mod(
        mod_dir,
        """
        const out = [];
        for (const step of t.v.steps) {
          if (step.write !== undefined) { t.write(step.write); continue; }
          out.push((await t.emit("tool.call", { tool: "Bash" }, async () => ({ result: step.in }))).result);
        }
        return out;
        """,
        secrets=first,
        steps=steps,
    )
    filter_dir = tmp_path / "filter"
    filter_dir.mkdir()
    path = filter_dir / "secrets.env"
    path.write_text(first)
    monkeypatch.setenv("SECRETS_PATH", str(path))
    values = stream.Values(str(REPO))
    got, tick = [], time.time()
    for step in steps:
        if "write" in step:
            path.write_text(step["write"])
            tick += 5  # a later mtime for each write, as the mod's driver does
            os.utime(path, (tick, tick))
            continue
        values.refresh()
        got.append(values.redact(step["in"]))
    assert got == res["out"]
    # Not equal by both doing nothing: every raw value is gone from both.
    assert not any(raw in text for text in got for raw in (KEY, KEY2, key3, "short-tail"))


def test_the_filter_and_the_mod_handle_a_failed_confirming_read_alike(tmp_path, monkeypatch):
    # The first load reads, stamps, and reads again to confirm. The key rotates right after the first read and the
    # confirming read fails. register.js load then commits nothing and retries later; the filter must do the same
    # (no values from the unconfirmed read, no stamp), and both must agree once the retry succeeds.
    text = f"one {KEY} two {KEY2}"
    mod_dir = tmp_path / "mod"
    mod_dir.mkdir()
    res = _run_mod(
        mod_dir,
        """
        t.afterNextLoader(() => { t.write("CANARY_API_KEY=" + t.v.key2 + "\\n"); t.failNext(1); });
        const call = async () => (await t.emit("tool.call", { tool: "Bash" }, async () => ({ result: t.v.text }))).result;
        const first = await call();
        t.advance(31000);  // past the mod's retry back-off
        return [first, await call()];
        """,
        text=text,
    )
    filter_dir = tmp_path / "filter"
    filter_dir.mkdir()
    path = filter_dir / "secrets.env"
    path.write_text(f"CANARY_API_KEY={KEY}\n")
    monkeypatch.setenv("SECRETS_PATH", str(path))
    real_read = stream.Values._read
    reads = []

    def read(self):
        reads.append(1)
        if len(reads) == 1:
            got = real_read(self)
            path.write_text(f"CANARY_API_KEY={KEY2}\n")
            later = time.time() + 5
            os.utime(path, (later, later))
            return got
        if len(reads) == 2:
            # The loader itself fails, inside the filter's own read, as a failed loader run does in the mod.
            real_load = self._known.load

            def failing(*a, **kw):
                raise RuntimeError("known_values exited 1")

            self._known.load = failing
            try:
                return real_read(self)
            finally:
                self._known.load = real_load
        return real_read(self)

    monkeypatch.setattr(stream.Values, "_read", read)
    values = stream.Values(str(REPO))
    first = values.redact(text)
    monkeypatch.setattr(stream, "RETRY_S", 0.0)
    values.refresh()
    got = [first, values.redact(text)]
    assert got == res["out"]
    assert got[0] == text  # nothing from the unconfirmed read was committed
    assert got[1] == f"one {KEY} two {P}"  # the retry read the rotated file


def test_a_rotated_out_value_gets_a_placeholder_of_its_own(tmp_path, monkeypatch):
    # One placeholder must stand for one text, or restoring it gives the wrong value (the mod's ~earlier scheme).
    values = _values(tmp_path, monkeypatch, f"CANARY_API_KEY={KEY}\n")
    (tmp_path / "secrets.env").write_text(f"CANARY_API_KEY={KEY2}\n")
    values.refresh()
    assert values.redact(KEY2) == P
    assert values.redact(KEY) == "[secrets.env:CANARY_API_KEY~earlier]"
    (tmp_path / "secrets.env").write_text(
        "CANARY_API_KEY=ck_" + "99887766554433221100aabbccddeeff\n"
    )
    values.refresh()
    assert values.redact(KEY2) == "[secrets.env:CANARY_API_KEY~earlier2]"
    assert values.redact(KEY) == "[secrets.env:CANARY_API_KEY~earlier]"
    placeholders = [placeholder for _, placeholder in values.pairs]
    assert len(placeholders) == len(set(placeholders))


# --- runner -------------------------------------------------------------------------------------------------


def test_runner_redacts_both_streams_and_keeps_the_exit_status(secrets_file, tmp_path):
    run = _run(f"echo out {KEY}; echo err {TOKEN} >&2; exit 7", tmp_path)
    assert run.returncode == 7
    assert run.stdout == f"out {P}\n"
    assert run.stderr == "err [secrets.env:CANARY_SERVICE_TOKEN]\n"


def test_an_ordinary_command_ends_as_soon_as_it_exits(secrets_file, tmp_path):
    # The filters signal on end of input. Without that the runner waited out its whole 5 second bound on
    # every command that left no writer behind (measured: 5.5 s for `echo done`).
    start = time.monotonic()
    run = _run("echo done", tmp_path)
    assert run.stdout == "done\n"
    assert time.monotonic() - start < 3


def test_runner_keeps_the_callers_shell_functions(secrets_file, tmp_path):
    run = _run("probe_fn", tmp_path, preamble="probe_fn() { echo fn-ran; }")
    assert run.stdout == "fn-ran\n"


def test_runner_carries_heredocs_and_quoting(secrets_file, tmp_path):
    command = f"cat <<'EOF'\n'single' \"double\" $HOME {KEY}\nEOF\necho \"$((1 + 2))\""
    run = _run(command, tmp_path)
    assert run.stdout == f"'single' \"double\" $HOME {P}\n3\n"


def test_value_split_across_writes_is_redacted_whole(secrets_file, tmp_path):
    half = len(KEY) // 2
    run = _run(f"printf '%s' '{KEY[:half]}'; sleep 0.3; printf '%s\\n' '{KEY[half:]}'", tmp_path)
    assert run.stdout == f"{P}\n"


def test_a_held_tail_that_is_not_a_value_is_written_once_it_is_settled(secrets_file, tmp_path):
    run = _run(f"printf '%s' 'see {KEY[:8]}'; sleep 0.3; echo ' and nothing more'", tmp_path)
    assert run.stdout == f"see {KEY[:8]} and nothing more\n"


def test_a_character_split_between_two_reads_is_one_character(tmp_path, monkeypatch):
    value = "clé-secrète-0123456789"
    path = tmp_path / "secrets.env"
    path.write_text(f"ACCENT_API_KEY={value}\n", encoding="utf-8")
    monkeypatch.setenv("SECRETS_PATH", str(path))
    raw = value.encode()
    cut = raw.index("é".encode()) + 1  # between the two bytes of the first accented letter
    (tmp_path / "a.bin").write_bytes(raw[:cut])
    (tmp_path / "b.bin").write_bytes(raw[cut:] + b"\n")
    run = _run("cat a.bin; sleep 0.3; cat b.bin", tmp_path)
    assert run.stdout == "[secrets.env:ACCENT_API_KEY]\n"


def test_a_multiline_value_of_short_lines_written_a_line_at_a_time(tmp_path, monkeypatch):
    # Neither line is long enough to be a needle alone, so the whole value has to be matched across the newline.
    path = tmp_path / "secrets.env"
    path.write_text('PRIVATE_KEY="short-line-a\nshort-line-b"\n')
    monkeypatch.setenv("SECRETS_PATH", str(path))
    run = _run("echo short-line-a; sleep 0.3; echo short-line-b; echo after", tmp_path)
    assert run.stdout == "[secrets.env:PRIVATE_KEY]\nafter\n"


def test_a_lingering_child_does_not_hold_the_command(secrets_file, tmp_path):
    # Claude Code judges a command finished when its process exits (measured), not when every holder of the
    # output closes it; the filter keeps the output open for the child on purpose.
    rc, out, took = _run_to_file("sleep 30 & echo started", tmp_path)
    assert rc == 0 and took < 15
    assert out.read_text() == "started\n"


def _await(path: Path, seconds: float = 10) -> None:
    deadline = time.monotonic() + seconds
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.1)


def test_a_late_writing_child_survives_and_its_output_is_redacted(secrets_file, tmp_path):
    marker = tmp_path / "child.marker"
    rc, out, _ = _run_to_file(
        f"(sleep 1; echo late {KEY}; echo alive > {marker}) & exit 4", tmp_path
    )
    assert rc == 4
    _await(marker)
    assert marker.read_text() == "alive\n"
    time.sleep(0.3)
    assert out.read_text() == f"late {P}\n"


def test_a_value_begun_by_the_command_and_finished_by_its_child_is_redacted(secrets_file, tmp_path):
    # The command writes half a value and exits; a child it left behind writes the rest. The half must stay
    # held across the exit, not be written when the command's own output has been read.
    half = len(KEY) // 2
    marker = tmp_path / "child.marker"
    command = (
        f"printf '%s' '{KEY[:half]}'; (sleep 1.5; echo '{KEY[half:]}'; echo ok > {marker}) & exit 0"
    )
    rc, out, took = _run_to_file(command, tmp_path)
    assert rc == 0 and took < 1.4  # the command did not wait for its child
    assert out.read_text() == ""  # nothing of the value yet
    _await(marker)
    time.sleep(0.3)
    assert out.read_text() == f"{P}\n"


def test_a_key_rotated_while_the_command_runs_is_redacted_and_the_old_one_still_is(
    secrets_file, tmp_path
):
    # The first sleep lets the filters read the values before the rotation: a value is kept only if the filter
    # has read it, and a command that rotates a key in its first instant can beat the filters' start-up.
    command = (
        f"echo before; sleep 1; printf 'CANARY_API_KEY=%s\\n' '{KEY2}' > '{secrets_file}'; sleep 0.2; "
        f"echo new {KEY2}; echo old {KEY}"
    )
    run = _run(command, tmp_path)
    assert run.stdout == f"before\nnew {P}\nold [secrets.env:CANARY_API_KEY~earlier]\n"


def test_a_multiline_value_whose_first_line_is_a_value_written_in_two_reads(tmp_path, monkeypatch):
    path = tmp_path / "secrets.env"
    path.write_text('PRIVATE_KEY="abcdefghijklmnopqrst\nshort-tail"\n')
    monkeypatch.setenv("SECRETS_PATH", str(path))
    run = _run("printf 'abcdefghijklmnopqrst\\n'; sleep 0.3; printf 'short-tail\\n'", tmp_path)
    assert run.stdout == "[secrets.env:PRIVATE_KEY]\n"


def test_a_secrets_file_the_command_creates_is_picked_up(tmp_path, monkeypatch):
    path = tmp_path / "secrets.env"
    monkeypatch.setenv("SECRETS_PATH", str(path))
    command = (
        f"echo before; printf 'CANARY_API_KEY=%s\\n' '{KEY}' > '{path}'; sleep 0.2; echo {KEY}"
    )
    run = _run(command, tmp_path)
    assert run.stdout == f"before\n{P}\n"


def test_a_secrets_file_appearing_where_the_resolver_looks_first_is_picked_up(
    tmp_path, monkeypatch
):
    # No override: the resolver prefers the home config over the checkout's file, so a file created there while
    # the command runs changes which file holds the values. Watching only the file first resolved misses it.
    # The sleep lets the filters load first, as in the rotation test above.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv("SECRETS_PATH", raising=False)
    monkeypatch.setenv("HOME", str(home))
    config = home / ".config" / "genesis" / "secrets.env"
    command = (
        f"echo before; sleep 1; mkdir -p '{config.parent}'; printf 'CANARY_API_KEY=%s\\n' '{KEY}' > '{config}'; "
        f"sleep 0.2; echo {KEY}"
    )
    run = _run(command, tmp_path)
    assert run.stdout == f"before\n{P}\n"


def test_runner_leaves_no_job_notice_and_no_temp_dir(secrets_file, tmp_path):
    run = _run("true & wait; echo done; exit 3", tmp_path, preamble="set -m")
    assert run.returncode == 3
    assert "]+" not in run.stdout + run.stderr and "_sr_" not in run.stdout + run.stderr
    time.sleep(0.3)
    assert not list(tmp_path.glob("secret-redact.*"))


def test_runner_finishes_under_a_callers_errexit(secrets_file, tmp_path):
    # Claude Code's command shell does not set errexit (measured), but the runner must not depend on that: a
    # non-zero status is the normal case, and stopping before the filters are told would leave them waiting.
    start = time.monotonic()
    run = _run(f"echo {KEY}; exit 3", tmp_path, preamble="set -e")
    assert run.returncode == 3 and run.stdout == f"{P}\n"
    assert time.monotonic() - start < 3
    time.sleep(0.3)
    assert not list(tmp_path.glob("secret-redact.*"))


def test_wrapped_command_does_not_see_the_wrappers_arguments(secrets_file, tmp_path):
    run = _run('echo "args=$#"', tmp_path)
    assert run.stdout == "args=0\n"


def test_runner_without_a_temp_dir_runs_the_command_unwrapped(secrets_file, tmp_path):
    run = subprocess.run(
        ["bash", "-c", _source("echo ran; exit 6")],
        capture_output=True,
        text=True,
        env={**os.environ, "TMPDIR": str(tmp_path / "does-not-exist")},
        timeout=60,
    )
    assert run.returncode == 6 and run.stdout == "ran\n"
    assert "NOT redacted" in run.stderr


def test_a_failing_loader_passes_output_through_and_says_so(tmp_path):
    # A resolver that raises something other than OSError must not kill the filter (the command would then lose
    # its output, or die writing into a FIFO with no reader).
    root = tmp_path / "fake-repo"
    (root / "src" / "genesis").mkdir(parents=True)
    (root / "src" / "genesis" / "secrets_location.py").write_text(
        "def resolve(**kw):\n    raise RuntimeError('boom')\n"
    )
    run = subprocess.run(
        ["bash", "-c", _source("echo before; echo after; exit 3", root)],
        capture_output=True,
        text=True,
        env={**os.environ, "TMPDIR": str(tmp_path)},
        timeout=60,
    )
    assert run.returncode == 3 and run.stdout == "before\nafter\n"
    assert "NOT redacted" in run.stderr


def test_a_filter_whose_first_load_failed_tries_again(tmp_path, monkeypatch):
    # The values could not be read when the filter started (a resolver error at start). Once they can, the next
    # output is redacted: a filter must not stay raw for a long command's whole life.
    secrets = tmp_path / "secrets.env"
    secrets.write_text(f"CANARY_API_KEY={KEY}\n")
    flag = tmp_path / "ok"
    monkeypatch.setenv("SECRETS_PATH", str(secrets))
    root = tmp_path / "fake-repo"
    (root / "src" / "genesis").mkdir(parents=True)
    (root / "src" / "genesis" / "secrets_location.py").write_text(
        "from pathlib import Path\n"
        "def resolve(*, environ, repo_root, home=None):\n"
        f"    if not Path({str(flag)!r}).exists():\n"
        "        raise RuntimeError('not yet')\n"
        "    return Path(environ['SECRETS_PATH'])\n"
    )
    command = f"echo before; sleep 1.5; touch '{flag}'; sleep 1.2; echo {KEY}"
    run = subprocess.run(
        ["bash", "-c", _source(command, root)],
        capture_output=True,
        text=True,
        env={**os.environ, "TMPDIR": str(tmp_path)},
        timeout=60,
    )
    assert run.returncode == 0 and run.stdout == f"before\n{P}\n"


def test_a_filter_that_cannot_start_runs_the_command_unwrapped_and_says_so(secrets_file, tmp_path):
    # A filter that fails to start would leave the command writing into a FIFO with no reader (measured: the
    # output was lost, silently). The runner checks it can start one first.
    root = tmp_path / "root"
    (root / ".venv" / "bin").mkdir(parents=True)
    broken = root / ".venv" / "bin" / "python"
    broken.write_text("#!/bin/sh\nexit 1\n")
    broken.chmod(0o755)
    command = _source("echo line1; echo line2 >&2; exit 4", root)
    run = subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        text=True,
        env={**os.environ, "TMPDIR": str(tmp_path)},
        timeout=60,
        cwd=tmp_path,
    )
    assert run.returncode == 4 and run.stdout == "line1\n"
    assert "line2" in run.stderr and "NOT redacted" in run.stderr
    assert not list(tmp_path.glob("secret-redact.*"))


def test_a_shell_that_is_not_bash_runs_the_command_unwrapped_and_says_so(secrets_file, tmp_path):
    # zsh is the other shell Claude Code runs commands in; none is installed here, so this takes the same branch
    # with bash's version variable unset (zsh's `source` passes arguments as bash's does; dash's `.` does not).
    run = _run("echo line1; exit 3", tmp_path, preamble="unset BASH_VERSION")
    assert run.returncode == 3 and run.stdout == "line1\n" and "NOT redacted" in run.stderr


def test_runner_without_values_passes_output_through(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_PATH", str(tmp_path / "absent.env"))
    run = _run(f"echo {KEY}", tmp_path)
    assert run.stdout == f"{KEY}\n"


# --- the hook -----------------------------------------------------------------------------------------------


def _payload(tool: str, mode: str | None = "bypassPermissions", **tool_input) -> dict:
    return {"tool_name": tool, "tool_input": tool_input, "permission_mode": mode}


@pytest.mark.parametrize("mode", ["default", "acceptEdits", "plan", "auto", "dontAsk", None])
def test_wrap_applies_only_in_a_bypass_session(mode):
    # Outside bypass mode Claude Code's permission check judges the rewritten command and refuses it (measured).
    assert wrap.rewrite(_payload("Monitor", mode=mode, command="tail -f log"), REPO) is None
    assert (
        wrap.rewrite(_payload("Bash", mode=mode, command="make", run_in_background=True), REPO)
        is None
    )


@pytest.mark.parametrize(
    ("payload", "wrapped"),
    [
        (_payload("Monitor", command="tail -f log"), True),
        (_payload("Bash", command="make", run_in_background=True), True),
        (_payload("Bash", command="ls"), False),
        (_payload("Bash", command="ls", run_in_background=False), False),
        (_payload("Read", file_path="/x"), False),
        (_payload("Monitor", command="   "), False),
    ],
)
def test_wrap_applies_only_to_monitor_and_background_bash(payload, wrapped):
    assert (wrap.rewrite(payload, REPO) is not None) is wrapped


def test_wrap_never_decides_permission_and_round_trips_the_command():
    command = "printf '%s' \"$X\" 'it'\"'\"'s' `date` && echo ${HOME} # comment"
    out = wrap.rewrite(_payload("Monitor", command=command, description="d"), REPO)
    hso = out["hookSpecificOutput"]
    assert set(hso) == {"hookEventName", "updatedInput"}
    assert hso["updatedInput"]["description"] == "d"
    encoded = hso["updatedInput"]["command"].split()[2]
    assert base64.b64decode(encoded).decode() == command


def test_a_command_that_mentions_the_runner_is_still_wrapped():
    assert wrap.rewrite(_payload("Monitor", command="cat app.conf # redact_run.sh x"), REPO)


def test_a_command_that_only_starts_like_a_wrapped_one_is_still_wrapped():
    once = wrap.rewrite(_payload("Monitor", command="echo hi"), REPO)
    lookalike = once["hookSpecificOutput"]["updatedInput"]["command"] + "; cat app.conf"
    assert wrap.rewrite(_payload("Monitor", command=lookalike), REPO) is not None


def test_wrap_is_idempotent():
    once = wrap.rewrite(_payload("Monitor", command="echo hi"), REPO)
    assert (
        wrap.rewrite(_payload("Monitor", **once["hookSpecificOutput"]["updatedInput"]), REPO)
        is None
    )


def test_the_hook_never_reads_the_secret_values(tmp_path):
    # Whether there is anything to redact is the filter's question, asked while the command runs. So the hook
    # wraps with no secrets file at all, and with one it cannot read.
    for path in (tmp_path / "absent.env", tmp_path):
        run = subprocess.run(
            [sys.executable, "-I", str(BIN / "stream_wrap.py")],
            input=json.dumps(_payload("Monitor", command="echo hi")),
            capture_output=True,
            text=True,
            env={**os.environ, "SECRETS_PATH": str(path)},
            timeout=30,
        )
        assert run.returncode == 0 and run.stderr == ""
        assert (
            "redact_run.sh"
            in json.loads(run.stdout)["hookSpecificOutput"]["updatedInput"]["command"]
        )


def test_wrap_hook_exits_0_silently_on_bad_input():
    run = subprocess.run(
        [sys.executable, "-I", str(BIN / "stream_wrap.py")],
        input="not json",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0 and run.stdout == ""


def test_wrapped_command_runs_end_to_end(secrets_file, tmp_path):
    out = wrap.rewrite(
        _payload("Bash", command=f"echo {KEY}; exit 5", run_in_background=True), REPO
    )
    run = subprocess.run(
        ["bash", "-c", out["hookSpecificOutput"]["updatedInput"]["command"]],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "TMPDIR": str(tmp_path)},
    )
    assert run.returncode == 5 and run.stdout == f"{P}\n"
