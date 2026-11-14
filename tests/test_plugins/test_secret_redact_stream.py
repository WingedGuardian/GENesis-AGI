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
    assert run.stdout == f"before\nnew {P}\nold {P}\n"


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
