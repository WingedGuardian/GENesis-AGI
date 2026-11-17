"""cc-slot.sh launch mode: the builder door (spec E, E3).

A builder is started by code, with nobody at the terminal. The mode must take
the lowest free slot, create it DETACHED (never attach), never prompt, never
inject the OAuth fallback token, and start claude from a rebuilt environment
that carries none of another pane's identity or the launcher's secrets.

The real script runs against a fake `tmux` that records each argument on its
own NUL-separated record, so the pane command can be extracted exactly and
then EXECUTED under a polluted environment to see which variables reach claude.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CC_SLOT = _REPO_ROOT / "scripts" / "cc-slot.sh"
_SETTINGS = _REPO_ROOT / "config" / "builder_settings.json"
SID = "0b7e5c8a-1f2d-4c3b-9a8e-7d6c5b4a3f21"

_FAKE_TMUX = r"""#!/usr/bin/env bash
# One record per invocation: each argument NUL-terminated, then a newline record end.
{ for a in "$@"; do printf '%s\0' "$a"; done; printf '\n'; } >> "$FAKE_TMUX_ARGS"
# One file per call as well: an argument may itself contain newlines (a prompt).
_rec="$FAKE_TMUX_CALLS/$(date +%s%N)-$$"
for a in "$@"; do printf '%s\0' "$a"; done > "$_rec"
_exists() {
    if [[ -n "${FAKE_APPEAR:-}" && "$1" == "$FAKE_APPEAR" ]]; then
        n=0; [[ -f "$FAKE_APPEAR_N" ]] && n=$(cat "$FAKE_APPEAR_N")
        echo $(( n + 1 )) > "$FAKE_APPEAR_N"
        [[ "$n" -ge 1 ]] && return 0
        return 1
    fi
    [[ -f "$FAKE_SESSIONS" ]] && grep -qxF "$1" "$FAKE_SESSIONS"
}
case "$*" in
    *has-session*) n="${*##*=}"; _exists "$n" && exit 0; exit 1 ;;
esac
if [[ "$*" == *list-sessions* && "$*" == *"#{==:#{session_name},"* ]]; then
    for a in "$@"; do f="$a"; done
    name=$(printf '%s' "$f" | sed 's/^#{==:#{session_name},//; s/}$//')
    _exists "$name" && echo "$name"
    exit 0
fi
if [[ "$*" == *list-sessions* ]]; then
    [[ -f "$FAKE_SESSIONS" ]] || exit 1
    cat "$FAKE_SESSIONS"; exit 0
fi
if [[ "$*" == *new-session* ]]; then
    rc="${FAKE_NEW_RC:-0}"
    # A created session exists afterwards, unless the test says its claude died at once.
    if [[ "$rc" == 0 && -z "${FAKE_PANE_DIES:-}" ]]; then
        prev=""; for a in "$@"; do [[ "$prev" == "-s" ]] && echo "$a" >> "$FAKE_SESSIONS"; prev="$a"; done
    fi
    exit "$rc"
fi
exit 0
"""

_FAKE_VENV_PY = r"""#!/usr/bin/env bash
echo "$* SSH=[${SSH_CONNECTION-<unset>}]" >> "$FAKE_PY_LOG"
if [[ "$*" == *session_cap* ]]; then
    echo "${FAKE_CAP:-ALLOW}"; echo "cap message"; echo "reason"; exit 0
fi
if [[ "$*" == *login_gate* ]]; then echo "notice"; exit 0; fi
exit 1
"""

# Records the NAMES of its environment and its argv where the rebuilt
# environment still lets it write: HOME is on the allow-list.
_FAKE_CLAUDE = r"""#!/usr/bin/env bash
env | cut -d= -f1 | sort > "$HOME/claude_env_names.txt"
printf '%s\0' "$@" > "$HOME/claude_argv.bin"
exit 0
"""


@pytest.fixture()
def door(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("tmux", _FAKE_TMUX), ("claude", _FAKE_CLAUDE)):
        p = bin_dir / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    home = tmp_path / "home"
    home.mkdir()
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "config").mkdir()
    (checkout / ".venv" / "bin").mkdir(parents=True)
    shutil.copy2(_CC_SLOT, checkout / "scripts" / "cc-slot.sh")
    shutil.copy2(_SETTINGS, checkout / "config" / "builder_settings.json")
    py = checkout / ".venv" / "bin" / "python"
    py.write_text(_FAKE_VENV_PY)
    py.chmod(py.stat().st_mode | stat.S_IEXEC)
    prompt = tmp_path / "prompt.md"
    prompt.write_text('Build issue #7 from the snapshot. It\'s a test; quote: "x" $HOME `id`\n')
    state = {
        "args": tmp_path / "tmux_args.bin",
        "calls": tmp_path / "tmux_calls",
        "sessions": tmp_path / "sessions.txt",
        "py_log": tmp_path / "py.log",
        "appear_n": tmp_path / "appear_n.txt",
    }
    state["calls"].mkdir()

    def run(*args: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        env = {
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(home),
            "FAKE_TMUX_ARGS": str(state["args"]),
            "FAKE_TMUX_CALLS": str(state["calls"]),
            "FAKE_SESSIONS": str(state["sessions"]),
            "FAKE_PY_LOG": str(state["py_log"]),
            "FAKE_APPEAR_N": str(state["appear_n"]),
            "GENESIS_CC_LAUNCH_SETTLE_S": "0",
        }
        env.update(extra_env or {})
        return subprocess.run(
            ["bash", str(checkout / "scripts" / "cc-slot.sh"), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def launch(*more: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        return run(
            "launch",
            "--issue",
            "7",
            "--key",
            "PVTI_x@2026-11-14T20:57:39Z",
            "--session-id",
            SID,
            "--prompt-file",
            str(prompt),
            *more,
            extra_env=extra_env,
        )

    def new_session_args() -> list[str]:
        recs = []
        for rec in sorted(state["calls"].iterdir()):
            parts = rec.read_bytes().split(b"\0")[:-1]
            if b"new-session" in parts:
                recs.append([p.decode() for p in parts])
        assert len(recs) == 1, f"expected one new-session call, got {len(recs)}"
        return recs[0]

    run.launch = launch
    run.new_session_args = new_session_args
    run.state = state
    run.home = home
    run.checkout = checkout
    run.bin_dir = bin_dir
    run.prompt = prompt
    return run


def _run_pane(
    door, pane_cmd: str, pollution: dict, interp: str = "bash"
) -> tuple[set[str], list[str]]:
    """Execute the pane command the way tmux would, under a polluted environment."""
    env = {"PATH": f"{door.bin_dir}:/usr/bin:/bin", "HOME": str(door.home)}
    env.update(pollution)
    proc = subprocess.run(
        [interp, "-c", pane_cmd], env=env, capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    names = set((door.home / "claude_env_names.txt").read_text().split())
    argv = (door.home / "claude_argv.bin").read_bytes().split(b"\0")[:-1]
    return names, [a.decode() for a in argv]


class TestLaunchCreates:
    def test_lowest_free_slot_detached_never_attached(self, door):
        door.state["sessions"].write_text("cc-1\n")
        proc = door.launch()
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "launched cc-2 2"
        args = door.new_session_args()
        assert "-d" in args and "-A" not in args
        assert args[args.index("-s") + 1] == "cc-2"

    def test_never_consults_the_oauth_gate(self, door):
        proc = door.launch()
        assert proc.returncode == 0, proc.stderr
        assert "login_gate" not in door.state["py_log"].read_text()
        assert "GENESIS_CC_SLOT_OAUTH=off" in door.new_session_args()

    def test_ssh_origin_is_stripped_before_the_capacity_gate(self, door):
        """An SSH variable would make the gate offer an interactive reclaim."""
        # RFC 5737 documentation addresses: never a real network.
        proc = door.launch(extra_env={"SSH_CONNECTION": "192.0.2.1 1 198.51.100.2 22"})
        assert proc.returncode == 0, proc.stderr
        cap_calls = [
            ln for ln in door.state["py_log"].read_text().splitlines() if "session_cap" in ln
        ]
        assert cap_calls and all("SSH=[<unset>]" in ln for ln in cap_calls)


class TestPaneEnvironmentAndArgv:
    POLLUTION = {
        "CLAUDE_PID": "4242",
        "TMUX": "/tmp/tmux-1000/default,1,0",
        "TMUX_PANE": "%299",
        "GENESIS_SLOT": "4",
        "GENESIS_CC_SESSION": "1",
        "ANTHROPIC_API_KEY": "sk-test-not-real",
        "CLAUDE_CODE_OAUTH_TOKEN": "tok-test-not-real",
        "SOME_SERVICE_SECRET": "x",
        "GENESIS_SESSION_ORIGIN": "foreground",
    }

    def test_claude_sees_only_the_allow_list(self, door):
        proc = door.launch()
        assert proc.returncode == 0, proc.stderr
        names, _argv = _run_pane(door, door.new_session_args()[-1], self.POLLUTION)
        for forbidden in (
            "CLAUDE_PID",
            "GENESIS_CC_SESSION",
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "SOME_SERVICE_SECRET",
            "GENESIS_SESSION_ORIGIN",
        ):
            assert forbidden not in names, forbidden
        for required in (
            "HOME",
            "PATH",
            "LANG",
            "GENESIS_SLOT",
            "GENESIS_BUILDER_KEY",
            "GENESIS_CC_SLOT_OAUTH",
            "GENESIS_CC_PERMISSION_MODE",
        ):
            assert required in names, required

    def test_tmux_pane_identity_is_the_panes_own(self, door):
        """The pane's TMUX/TMUX_PANE pass through (tmux sets them per pane), but a
        foreign GENESIS_SLOT never does: the slot number is fixed by the door."""
        proc = door.launch()
        assert proc.returncode == 0, proc.stderr
        cmd = door.new_session_args()[-1]
        assert "GENESIS_SLOT=1" in cmd and "GENESIS_SLOT=4" not in cmd
        names, _ = _run_pane(door, cmd, {})
        assert "TMUX_PANE" not in names, "an unset pane variable is dropped, not passed empty"

    def test_fresh_launch_argv(self, door):
        proc = door.launch()
        assert proc.returncode == 0, proc.stderr
        _names, argv = _run_pane(door, door.new_session_args()[-1], {})
        assert argv[argv.index("-n") + 1] == "build-#7"
        assert argv[argv.index("--session-id") + 1] == SID
        assert argv[argv.index("--remote-control") + 1] == "build #7"
        settings = argv[argv.index("--settings") + 1]
        assert settings == str(door.checkout / "config" / "builder_settings.json")
        assert argv[-1] == door.prompt.read_text().rstrip("\n"), (
            "the prompt arrives intact, unexpanded"
        )
        assert "-r" not in argv

    def test_resume_argv(self, door):
        proc = door.launch("--resume")
        assert proc.returncode == 0, proc.stderr
        _names, argv = _run_pane(door, door.new_session_args()[-1], {})
        assert argv[argv.index("-r") + 1] == SID
        assert "--session-id" not in argv and "-n" not in argv
        assert argv[argv.index("--remote-control") + 1] == "build #7"


class TestLaunchRefuses:
    @pytest.mark.parametrize(
        "args",
        [
            ["--issue", "0"],
            ["--issue", "7x"],
            ["--key", "bad key"],
            ["--key", "a;rm"],
            ["--session-id", "NOT-A-UUID"],
            ["--session-id", SID.upper()],
            ["--bogus"],
        ],
        ids=[
            "issue-zero",
            "issue-alpha",
            "key-space",
            "key-semicolon",
            "sid-bad",
            "sid-upper",
            "unknown-flag",
        ],
    )
    def test_bad_arguments_exit_64_and_create_nothing(self, door, args):
        base = {
            "--issue": "7",
            "--key": "k",
            "--session-id": SID,
            "--prompt-file": str(door.prompt),
        }
        argv = ["launch"]
        override = dict(zip(args[::2], args[1::2], strict=False)) if len(args) > 1 else {}
        for k, v in base.items():
            argv += [k, override.get(k, v)]
        if len(args) == 1:
            argv += args
        proc = door(*argv)
        assert proc.returncode == 64, proc.stderr
        assert (
            not door.state["args"].exists() or b"new-session" not in door.state["args"].read_bytes()
        )

    def test_empty_or_oversized_prompt_exits_64(self, door):
        door.prompt.write_text("")
        assert door.launch().returncode == 64
        door.prompt.write_text("\n \n\t\n")
        assert door.launch().returncode == 64, "whitespace only is empty"
        door.prompt.write_text("x" * 16385)
        assert door.launch().returncode == 64

    def test_a_prompt_under_the_file_bound_but_over_tmuxs_limit_exits_64(self, door):
        """tmux refuses a client command past ~16.3 KB (measured); quoting and the
        env block push a 16000-byte prompt over it. Bad input, not a tmux failure."""
        door.prompt.write_text("it's " * 3200)
        proc = door.launch()
        assert proc.returncode == 64, proc.stderr
        assert "tmux refuses" in proc.stderr

    def test_a_multibyte_prompt_is_measured_in_bytes(self, door):
        """5,300 CJK characters fit the 16,384-byte file bound but assemble to
        about 17.6 KB; a character count read 7,028 and let tmux fail it (Codex)."""
        door.prompt.write_text("\u754c" * 5300)
        proc = door.launch()
        assert proc.returncode == 64, proc.stderr
        assert "tmux refuses" in proc.stderr

    def test_a_prompt_starting_with_a_dash_exits_64(self, door):
        door.prompt.write_text("--dangerously-skip-permissions\n")
        assert door.launch().returncode == 64

    def test_a_flag_with_no_value_exits_64(self, door):
        proc = door("launch", "--issue", "7", "--key")
        assert proc.returncode == 64, proc.stderr

    def test_malformed_caps_file_refuses(self, door):
        (door.checkout / "config" / "builder_settings.json").write_text("{not json")
        proc = door.launch()
        assert proc.returncode == 1
        assert "not valid JSON" in proc.stderr

    def test_missing_caps_file_refuses(self, door):
        (door.checkout / "config" / "builder_settings.json").unlink()
        proc = door.launch()
        assert proc.returncode == 1
        assert "without its caps" in proc.stderr

    @pytest.mark.parametrize("action", ["DENY", "RECLAIM"])
    def test_capacity_refusal_is_retryable_and_never_prompts(self, door, action):
        proc = door.launch(extra_env={"FAKE_CAP": action})
        assert proc.returncode == 75, proc.stderr
        assert (
            not door.state["args"].exists() or b"new-session" not in door.state["args"].read_bytes()
        )

    def test_slot_taken_after_the_search_is_retryable_never_attached(self, door):
        proc = door.launch(extra_env={"FAKE_APPEAR": "cc-1"})
        assert proc.returncode == 75, proc.stderr
        assert (
            not door.state["args"].exists() or b"new-session" not in door.state["args"].read_bytes()
        )

    def test_failed_create_by_a_racing_door_is_retryable(self, door):
        proc = door.launch(extra_env={"FAKE_NEW_RC": "1", "FAKE_APPEAR": "cc-1"})
        assert proc.returncode == 75, proc.stderr

    def test_a_pane_that_dies_at_once_is_not_reported_launched(self, door):
        proc = door.launch(extra_env={"FAKE_PANE_DIES": "1"})
        assert proc.returncode == 1, proc.stderr
        assert proc.stdout == ""
        assert "cc_exit_1.log" in proc.stderr

    def test_static_capacity_fallback_refuses_with_75(self, door):
        """Python gate unavailable: the static fallback must refuse a full box with
        75 too, never prompt."""
        door.state["sessions"].write_text("".join(f"cc-{i}\n" for i in range(1, 65)))
        (door.checkout / ".venv" / "bin" / "python").unlink()
        # The fallback's cap is clamped by nproc: one CPU, whatever host runs this.
        nproc = door.home.parent / "bin" / "nproc"
        nproc.write_text("#!/bin/sh\necho 1\n")
        nproc.chmod(0o755)
        proc = door.launch()
        assert proc.returncode == 75, proc.stderr

    def test_failed_create_is_a_real_failure(self, door):
        proc = door.launch(extra_env={"FAKE_NEW_RC": "1"})
        assert proc.returncode == 1, proc.stderr
        assert proc.stdout == ""


class TestCallerFixedSlot:
    KEY_PIN = "GENESIS_BUILDER_KEY=PVTI_x@2026-11-14T20:57:39Z"

    def test_slot_option_is_used_and_pinned_with_the_key(self, door):
        proc = door.launch("--slot", "3")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "launched cc-3 3"
        args = door.new_session_args()
        assert args[args.index("-s") + 1] == "cc-3"
        assert self.KEY_PIN in args, "the key is on the session env"
        assert "GENESIS_BUILDER_ISSUE=7" in args, "the issue is on the session env"
        lang = next(a for a in args if a.startswith("LANG="))
        assert args.index(self.KEY_PIN) < args.index(lang), "LANG stays the last pin"

    def test_a_taken_fixed_slot_is_refused_not_swapped(self, door):
        door.state["sessions"].write_text("cc-3\n")
        proc = door.launch("--slot", "3")
        assert proc.returncode == 75, proc.stderr
        raw = door.state["args"].read_bytes() if door.state["args"].exists() else b""
        assert b"new-session" not in raw

    @pytest.mark.parametrize("bad", ["0", "x", "-1", "99999"])
    def test_bad_slot_exits_64(self, door, bad):
        assert door.launch("--slot", bad).returncode == 64


class TestQuotingIsPosix:
    def test_multiline_prompt_with_quotes_reaches_claude_intact_under_sh(self, door):
        body = 'Line one: it\'s "quoted" $HOME `id` \\n\nLine two\n\nLine four'
        door.prompt.write_text(body + "\n")
        proc = door.launch()
        assert proc.returncode == 0, proc.stderr
        cmd = door.new_session_args()[-1]
        assert "$'" not in cmd, "no bash-only ANSI-C quoting in the pane command"
        _names, argv = _run_pane(door, cmd, {}, interp="sh")
        assert argv[-1] == body

    def test_model_is_the_latest_opus_alias_never_a_version(self, door):
        proc = door.launch()
        assert proc.returncode == 0, proc.stderr
        _names, argv = _run_pane(door, door.new_session_args()[-1], {})
        assert argv[argv.index("--model") + 1] == "opus"


def test_builder_settings_carry_both_caps():
    import json

    env = json.loads(_SETTINGS.read_text())["env"]
    assert int(env["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"]) < 17
    assert int(env["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"]) >= 1


def test_builder_prompt_template_names_its_fields():
    text = (_REPO_ROOT / "config" / "BUILDER_PROMPT.md").read_text()
    for field in ("{issue}", "{snapshot}", "{worktree}"):
        assert field in text
    assert "Closes #{issue}" in text


def test_manual_mode_is_unchanged_by_launch(door):
    """The interactive door still attaches-or-creates with -A."""
    proc = door("manual")
    assert proc.returncode == 0, proc.stderr
    args = door.new_session_args()
    assert "-A" in args and "-d" not in args


if os.name == "nt":  # pragma: no cover
    pytest.skip("bash door", allow_module_level=True)
