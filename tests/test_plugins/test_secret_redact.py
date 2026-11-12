"""genesis-secret-redact: the value loader, the stream runner and the PreToolUse stream wrap.

The plugin's in-process mod (hooks/register.ts) needs a real Claude Code and is exercised by
``scripts/e2e_secret_redact.py``; everything it delegates to is tested here. Canary values only.
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
TOKEN = "ct_Zx9-Yw8_Vu7Ts6Rq5Po4Nm3Lk2Ji1H"

known = private_module("_test_known_values", BIN / "known_values.py")
wrap = private_module("_test_stream_wrap", BIN / "stream_wrap.py")


@pytest.fixture
def secrets_file(tmp_path, monkeypatch):
    path = tmp_path / "secrets.env"
    path.write_text(
        f'CANARY_API_KEY={KEY}\nexport CANARY_SERVICE_TOKEN="{TOKEN}"\nCANARY_REGION=us-east-1-long-name\n'
    )
    monkeypatch.setenv("SECRETS_PATH", str(path))
    return path


# --- loader -------------------------------------------------------------------------------------------------


DOTENV_CASES = [
    "PLAIN=value",
    "export EXPORTED=value2",
    "SINGLE='single quoted value'",
    'DOUBLE="double quoted value"',
    'ESCAPED="line\\nnext \\"q\\""',
    "INLINE=value # trailing comment",
    "HASH_IN_VALUE=abc#def",
    "EQUALS=a=b=c",
    "EMPTY=",
    "   SPACED   =   spaced value   ",
    "# a comment",
    "",
]


def test_dotenv_parse_matches_python_dotenv():
    dotenv = pytest.importorskip("dotenv")
    text = "\n".join(DOTENV_CASES) + "\n"
    ours = known.parse_dotenv(text)
    from io import StringIO

    theirs = {k: (v or "") for k, v in dotenv.dotenv_values(stream=StringIO(text)).items()}
    assert ours == theirs


def test_selection_is_by_name_and_length():
    pairs = {
        "CANARY_API_KEY": KEY,
        "SHORT_TOKEN": "tooshort",
        "USER_TIMEZONE": "America/New_York_Region_Name",
        "SERVICE_PASSWORD": "p" * 16,
    }
    vars_ = {n["var"] for n in known.needles(pairs)}
    assert vars_ == {"CANARY_API_KEY", "SERVICE_PASSWORD"}


def test_webhooks_dsns_and_credential_urls_are_secrets():
    pairs = {
        "DISCORD_WEBHOOK_GENERAL": "https://discord.example/api/webhooks/123/abcdefghij",
        "SENTRY_DSN": "https://abcdef0123456789@sentry.example/1",
        "DATABASE_URL": "postgres://app:s3cretpassw0rd@db.example:5432/app",
        "HA_URL": "http://homeassistant.example:8123",
    }
    assert {n["var"] for n in known.needles(pairs)} == {
        "DISCORD_WEBHOOK_GENERAL",
        "SENTRY_DSN",
        "DATABASE_URL",
    }


FALLBACK_CASES = [
    'QUOTED_COMMENT="quoted value" # note',
    "SINGLE_COMMENT='single value' # note",
    'MULTI="first line of it\nsecond line of it"',
    "export EXPORTED=plain-value-here",
    'ESCAPED="a\\"b\\\\c\\nd"',
    "UNQUOTED=value with spaces # comment",
]


def test_fallback_parser_matches_python_dotenv_on_secret_file_shapes():
    dotenv = pytest.importorskip("dotenv")
    from io import StringIO

    text = "\n".join(FALLBACK_CASES) + "\n"
    theirs = {k: (v or "") for k, v in dotenv.dotenv_values(stream=StringIO(text)).items()}
    assert known._fallback_parse(text) == theirs


def test_parse_prefers_python_dotenv_when_importable():
    pytest.importorskip("dotenv")
    assert known.parse_dotenv("A=x\nB=${A}-y\n")["B"] == "x-y"


def test_each_long_line_of_a_multiline_value_is_its_own_needle():
    value = "-----BEGIN KEY-----\nMIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX\n-----END KEY-----"
    texts = {n["text"] for n in known.needles({"PRIVATE_KEY_PEM": value})}
    assert "MIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX" in texts and "-----BEGIN KEY-----" in texts


def test_raw_flag_marks_only_the_value_as_written():
    raws = [n for n in known.needles({"CANARY_API_KEY": KEY}) if n["raw"]]
    assert raws == [{"var": "CANARY_API_KEY", "text": KEY, "raw": True}]


@pytest.mark.parametrize("prefix_len", range(6))
def test_a_base64_spelling_is_found_at_every_alignment(prefix_len):
    texts = [n["text"] for n in known.needles({"CANARY_API_KEY": KEY})]
    blob = base64.b64encode(b"u" * prefix_len + KEY.encode() + b":trailer").decode()
    assert any(t in blob for t in texts if t != KEY)


def test_url_and_json_spellings():
    value = 'abc/def+ghi=jkl"mno\\pqr'
    texts = {n["text"] for n in known.needles({"API_KEY": value})}
    assert {value, "abc%2Fdef%2Bghi%3Djkl%22mno%5Cpqr", 'abc/def+ghi=jkl\\"mno\\\\pqr'} <= texts


def test_needles_are_longest_first():
    lengths = [
        len(n["text"])
        for n in known.needles({"CANARY_API_KEY": KEY, "CANARY_SERVICE_TOKEN": TOKEN})
    ]
    assert lengths == sorted(lengths, reverse=True)


def test_load_uses_the_runtime_resolver(secrets_file):
    result = known.load(known.repo_root(str(REPO)))
    assert result["path"] == str(secrets_file)
    assert {n["var"] for n in result["needles"]} == {"CANARY_API_KEY", "CANARY_SERVICE_TOKEN"}


def test_missing_file_gives_no_needles(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_PATH", str(tmp_path / "absent.env"))
    assert known.load(known.repo_root(str(REPO)))["needles"] == []


def test_unreadable_file_exits_2_and_prints_no_value(secrets_file, capsys):
    secrets_file.chmod(0)
    try:
        if os.access(secrets_file, os.R_OK):
            pytest.skip("running as a user that can read a mode-000 file")
        assert known.main(["--json", "--repo-root", str(REPO)]) == 2
    finally:
        secrets_file.chmod(0o600)
    out = capsys.readouterr()
    assert KEY not in out.out + out.err


def test_summary_mode_prints_no_value(secrets_file, capsys):
    assert known.main(["--repo-root", str(REPO)]) == 0
    assert KEY not in capsys.readouterr().out


# --- runner -------------------------------------------------------------------------------------------------


def _run(
    command: str, tmp_path: Path, *, preamble: str = "", timeout: float = 60
) -> subprocess.CompletedProcess:
    encoded = base64.b64encode(command.encode()).decode()
    script = f"{preamble}\nsource {BIN / 'redact_run.sh'} {encoded} {REPO}"
    env = {**os.environ, "TMPDIR": str(tmp_path)}
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        cwd=tmp_path,
    )


def test_runner_redacts_both_streams_and_keeps_the_exit_status(secrets_file, tmp_path):
    run = _run(f"echo out {KEY}; echo err {TOKEN} >&2; exit 7", tmp_path)
    assert run.returncode == 7
    assert run.stdout == "out [secrets.env:CANARY_API_KEY]\n"
    assert run.stderr == "err [secrets.env:CANARY_SERVICE_TOKEN]\n"


def test_runner_keeps_the_callers_shell_functions(secrets_file, tmp_path):
    run = _run("probe_fn", tmp_path, preamble="probe_fn() { echo fn-ran; }")
    assert run.stdout == "fn-ran\n"


def test_runner_carries_heredocs_and_quoting(secrets_file, tmp_path):
    command = f"cat <<'EOF'\n'single' \"double\" $HOME {KEY}\nEOF\necho \"$((1 + 2))\""
    run = _run(command, tmp_path)
    assert run.stdout == "'single' \"double\" $HOME [secrets.env:CANARY_API_KEY]\n3\n"


def test_value_split_across_writes_is_redacted_whole(secrets_file, tmp_path):
    half = len(KEY) // 2
    run = _run(f"printf '%s' '{KEY[:half]}'; sleep 0.3; printf '%s\\n' '{KEY[half:]}'", tmp_path)
    assert run.stdout == "[secrets.env:CANARY_API_KEY]\n"


def test_long_line_without_newline_is_redacted_at_any_cut(secrets_file, tmp_path):
    # A line past the flush size is emitted in pieces. Make a piece end MID-value: the filler plus the value's
    # first half arrive, the filter must emit (the line is over the flush size), then the second half arrives.
    filler = "x" * (70 * 1024)
    half = len(KEY) // 2
    run = _run(
        f"printf '%s' '{filler}{KEY[:half]}'; sleep 0.5; printf '%s' '{KEY[half:]}tail'", tmp_path
    )
    assert KEY[:half] not in run.stdout
    assert run.stdout == filler + "[secrets.env:CANARY_API_KEY]tail"


def test_a_lingering_child_does_not_hold_the_command(secrets_file, tmp_path):
    # Claude Code judges a command finished when its process exits (measured: no delay with a lingering child),
    # not when every holder of the output closes it; the filter keeps the output open for the child on purpose.
    # So time the process, writing to a file as a background task's output file is written.
    out = tmp_path / "task.output"
    encoded = base64.b64encode(b"sleep 30 & echo started").decode()
    start = time.monotonic()
    with out.open("w") as fh:
        rc = subprocess.run(
            ["bash", "-c", f"source {BIN / 'redact_run.sh'} {encoded} {REPO}"],
            stdout=fh,
            stderr=subprocess.STDOUT,
            env={**os.environ, "TMPDIR": str(tmp_path)},
            timeout=60,
        ).returncode
    assert rc == 0
    assert time.monotonic() - start < 15
    assert out.read_text() == "started\n"


def test_a_late_writing_child_survives_and_its_output_is_redacted(secrets_file, tmp_path):
    out = tmp_path / "task.output"
    marker = tmp_path / "child.marker"
    command = f"(sleep 1; echo late {KEY}; echo alive > {marker}) & exit 4"
    encoded = base64.b64encode(command.encode()).decode()
    env = {**os.environ, "TMPDIR": str(tmp_path)}
    with out.open("w") as fh:
        rc = subprocess.run(
            ["bash", "-c", f"source {BIN / 'redact_run.sh'} {encoded} {REPO}"],
            stdout=fh,
            stderr=subprocess.STDOUT,
            env=env,
            timeout=60,
        ).returncode
    assert rc == 4
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert marker.read_text() == "alive\n"
    time.sleep(0.3)
    assert out.read_text() == "late [secrets.env:CANARY_API_KEY]\n"


def test_runner_leaves_no_job_notice_and_no_temp_dir(secrets_file, tmp_path):
    run = _run("true & wait; echo done; exit 3", tmp_path, preamble="set -m")
    assert run.returncode == 3
    assert "]+" not in run.stdout + run.stderr and "_sr_" not in run.stdout + run.stderr
    time.sleep(0.3)
    assert not list(tmp_path.glob("secret-redact.*"))


def test_multiline_value_split_across_writes_is_redacted(tmp_path, monkeypatch):
    path = tmp_path / "secrets.env"
    path.write_text('PRIVATE_KEY="line-one-of-the-secret-x\nline-two-of-the-secret-y"\n')
    monkeypatch.setenv("SECRETS_PATH", str(path))
    run = _run("echo line-one-of-the-secret-x; sleep 0.3; echo line-two-of-the-secret-y", tmp_path)
    assert "line-one" not in run.stdout and "line-two" not in run.stdout


def test_wrapped_command_does_not_see_the_wrappers_arguments(secrets_file, tmp_path):
    run = _run('echo "args=$#"', tmp_path)
    assert run.stdout == "args=0\n"


def test_runner_without_a_temp_dir_runs_the_command_unwrapped(secrets_file, tmp_path):
    encoded = base64.b64encode(b"echo ran; exit 6").decode()
    env = {**os.environ, "TMPDIR": str(tmp_path / "does-not-exist")}
    run = subprocess.run(
        ["bash", "-c", f"source {BIN / 'redact_run.sh'} {encoded} {REPO}"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert run.returncode == 6 and run.stdout == "ran\n"
    assert "NOT redacted" in run.stderr


def test_a_failing_loader_passes_output_through_and_says_so(tmp_path, monkeypatch):
    # A resolver that raises something other than OSError must not kill the filter (the command would then lose
    # its output, or die writing into a FIFO with no reader).
    root = tmp_path / "fake-repo"
    (root / "src" / "genesis").mkdir(parents=True)
    (root / "src" / "genesis" / "secrets_location.py").write_text(
        "def resolve(**kw):\n    raise RuntimeError('boom')\n"
    )
    encoded = base64.b64encode(b"echo before; echo after; exit 3").decode()
    run = subprocess.run(
        ["bash", "-c", f"source {BIN / 'redact_run.sh'} {encoded} {root}"],
        capture_output=True,
        text=True,
        env={**os.environ, "TMPDIR": str(tmp_path)},
        timeout=60,
    )
    assert run.returncode == 3 and run.stdout == "before\nafter\n"
    assert "NOT redacted" in run.stderr


def test_runner_without_values_passes_output_through(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_PATH", str(tmp_path / "absent.env"))
    run = _run(f"echo {KEY}", tmp_path)
    assert run.stdout == f"{KEY}\n"


# --- stream wrap --------------------------------------------------------------------------------------------


def _payload(tool: str, **tool_input) -> dict:
    return {"tool_name": tool, "tool_input": tool_input}


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
def test_wrap_applies_only_to_monitor_and_background_bash(secrets_file, payload, wrapped):
    out = wrap.rewrite(payload, REPO)
    assert (out is not None) is wrapped


def test_wrap_never_decides_permission_and_round_trips_the_command(secrets_file):
    command = "printf '%s' \"$X\" 'it'\"'\"'s' `date` && echo ${HOME} # comment"
    out = wrap.rewrite(_payload("Monitor", command=command, description="d"), REPO)
    hso = out["hookSpecificOutput"]
    assert set(hso) == {"hookEventName", "updatedInput"}
    assert hso["updatedInput"]["description"] == "d"
    encoded = hso["updatedInput"]["command"].split()[2]
    assert base64.b64decode(encoded).decode() == command


def test_a_command_that_mentions_the_runner_is_still_wrapped(secrets_file):
    assert (
        wrap.rewrite(_payload("Monitor", command="cat app.conf # redact_run.sh x"), REPO)
        is not None
    )


def test_wrap_is_idempotent(secrets_file):
    once = wrap.rewrite(_payload("Monitor", command="echo hi"), REPO)["hookSpecificOutput"][
        "updatedInput"
    ]
    assert wrap.rewrite(_payload("Monitor", **once), REPO) is None


def test_no_wrap_when_there_is_nothing_to_redact(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_PATH", str(tmp_path / "absent.env"))
    assert wrap.rewrite(_payload("Monitor", command="echo hi"), REPO) is None


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
    command = out["hookSpecificOutput"]["updatedInput"]["command"]
    run = subprocess.run(
        ["bash", "-c", command],
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "TMPDIR": str(tmp_path)},
    )
    assert run.returncode == 5 and run.stdout == "[secrets.env:CANARY_API_KEY]\n"


def test_plugin_manifest_wires_the_mod_and_the_wrap():
    hooks = json.loads((BIN.parent / "hooks" / "hooks.json").read_text())
    assert hooks["modules"] == ["./register.ts"]
    (entry,) = hooks["hooks"]["PreToolUse"]
    assert entry["matcher"] == "^(Monitor|Bash)$"
    assert "bin/stream_wrap.py" in entry["hooks"][0]["command"]
