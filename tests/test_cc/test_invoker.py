"""Tests for CCInvoker."""

import asyncio
import json
import logging
import signal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.cc.exceptions import (
    CCDeployInProgressError,
    CCProcessError,
    CCStreamTruncatedError,
    CCTimeoutError,
)
from genesis.cc.invoker import CCInvoker
from genesis.cc.types import (
    CCInvocation,
    CCModel,
    ChannelType,
    EffortLevel,
    StreamEvent,
    clamp_effort,
    model_supports_effort,
)


@pytest.fixture
def invoker():
    return CCInvoker(claude_path="/usr/bin/claude")


def test_build_args_defaults(invoker):
    inv = CCInvocation(prompt="hello")
    args = invoker._build_args(inv)
    assert args[0] == "/usr/bin/claude"
    assert "-p" in args
    assert "--model" in args
    assert "sonnet" in args
    assert "--output-format" in args
    assert "json" in args
    # Prompt is passed via stdin, not as a CLI argument
    assert "hello" not in args


def test_build_args_with_resume(invoker):
    inv = CCInvocation(prompt="continue", resume_session_id="sess-123")
    args = invoker._build_args(inv)
    assert "--resume" in args
    assert "sess-123" in args


def test_build_args_with_system_prompt(invoker):
    inv = CCInvocation(prompt="hello", system_prompt="You are Genesis.")
    args = invoker._build_args(inv)
    assert "--system-prompt" in args


def test_build_args_append_system_prompt(invoker):
    inv = CCInvocation(prompt="hello", system_prompt="You are Genesis.", append_system_prompt=True)
    args = invoker._build_args(inv)
    assert "--append-system-prompt" in args
    assert "--system-prompt" not in args


def test_build_args_with_mcp_config(invoker):
    inv = CCInvocation(prompt="hello", mcp_config="/path/to/mcp.json")
    args = invoker._build_args(inv)
    assert "--mcp-config" in args
    assert "/path/to/mcp.json" in args


def test_build_args_strict_mcp_config(invoker):
    inv = CCInvocation(
        prompt="hello",
        mcp_config="/path/to/mcp.json",
        strict_mcp_config=True,
    )
    args = invoker._build_args(inv)
    assert "--strict-mcp-config" in args


def test_build_args_strict_is_default(invoker):
    """Secure-by-default: a plain invocation emits --strict-mcp-config so
    --mcp-config is authoritative and user-scoped ~/.claude.json servers can't
    leak in. (Flipped from opt-in on 2026-08-09; see CCInvocation.strict_mcp_config.)"""
    args = invoker._build_args(CCInvocation(prompt="hello"))
    assert "--strict-mcp-config" in args


def test_build_args_strict_opt_out(invoker):
    """Foreground/interactive sites opt out to keep the full user-scoped toolset."""
    args = invoker._build_args(CCInvocation(prompt="hello", strict_mcp_config=False))
    assert "--strict-mcp-config" not in args


def test_build_args_strict_suppressed_under_bare(invoker):
    """--bare already disables all MCP; --bare + --strict-mcp-config makes CC exit
    non-zero (probe-verified), so the invoker must NOT emit strict under bare even
    when strict_mcp_config is True (which is the default)."""
    args = invoker._build_args(CCInvocation(prompt="hello", bare=True, strict_mcp_config=True))
    assert "--bare" in args
    assert "--strict-mcp-config" not in args


def test_build_args_safe_mode(invoker):
    args = invoker._build_args(CCInvocation(prompt="hello", safe_mode=True))
    assert "--safe-mode" in args
    default_args = invoker._build_args(CCInvocation(prompt="hello"))
    assert "--safe-mode" not in default_args


def test_build_args_includes_span_settings(invoker, monkeypatch):
    """Dispatched sessions get --settings pointing at the span-hook file."""
    import genesis.cc.invoker as inv_mod

    monkeypatch.setattr(
        inv_mod,
        "cc_span_settings_path",
        lambda *_a, **_k: "/tmp/cc-span-settings.json",
    )
    args = invoker._build_args(CCInvocation(prompt="hi"))
    assert "--settings" in args
    assert args[args.index("--settings") + 1] == "/tmp/cc-span-settings.json"


def test_build_args_pins_inline_when_the_settings_file_is_unavailable(invoker, monkeypatch):
    """No settings FILE (launcher absent, unwritable ~/.genesis): the env pins
    still reach the session, inline, so no dispatch launches unpinned."""
    import genesis.cc.invoker as inv_mod

    monkeypatch.setattr(inv_mod, "cc_span_settings_path", lambda *_a, **_k: None)
    args = invoker._build_args(CCInvocation(prompt="hi"))
    inline = json.loads(args[args.index("--settings") + 1])
    assert inline == {"env": {var: "" for var in sorted(inv_mod._GH_CREDENTIAL_ENV)}}


def _fake_genesis_hook_repo(tmp_path):
    """Create a fake repo root containing a genesis-hook launcher."""
    hook = tmp_path / "repo" / ".claude" / "hooks" / "genesis-hook"
    hook.parent.mkdir(parents=True)
    hook.write_text("#!/bin/bash\n")
    return tmp_path / "repo", hook


def test_cc_span_settings_path_generates_file(monkeypatch, tmp_path):
    """Generates a minimal settings file with the span hook at an ABSOLUTE path."""
    import genesis.cc.invoker as inv_mod

    fake_repo, hook = _fake_genesis_hook_repo(tmp_path)
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(fake_repo))
    out = tmp_path / "settings.json"
    monkeypatch.setattr(inv_mod, "_CC_SPAN_SETTINGS_PATH", out)

    result = inv_mod.cc_span_settings_path()
    assert result == str(out)
    data = json.loads(out.read_text())
    entry = data["hooks"]["PostToolUse"][0]
    assert entry["matcher"] == ".*"
    cmd = entry["hooks"][0]["command"]
    # Absolute launcher path, no ${CLAUDE_PROJECT_DIR} (unset in dispatched cwd).
    assert cmd == f"{hook} hooks/cc_span_hook.py"
    assert cmd.startswith("/")
    assert "${CLAUDE_PROJECT_DIR}" not in cmd


def test_cc_span_settings_path_none_when_hook_missing(monkeypatch, tmp_path):
    """Returns None (→ no --settings) when the launcher is absent."""
    import genesis.cc.invoker as inv_mod

    fake_repo = tmp_path / "repo"  # no .claude/hooks/genesis-hook
    fake_repo.mkdir()
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(fake_repo))
    monkeypatch.setattr(inv_mod, "_CC_SPAN_SETTINGS_PATH", tmp_path / "x.json")
    assert inv_mod.cc_span_settings_path() is None


def test_cc_span_settings_path_idempotent(monkeypatch, tmp_path):
    """Second call with unchanged content does not rewrite the file."""
    import genesis.cc.invoker as inv_mod

    fake_repo, _ = _fake_genesis_hook_repo(tmp_path)
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(fake_repo))
    out = tmp_path / "settings.json"
    monkeypatch.setattr(inv_mod, "_CC_SPAN_SETTINGS_PATH", out)

    inv_mod.cc_span_settings_path()
    mtime1 = out.stat().st_mtime_ns
    inv_mod.cc_span_settings_path()
    assert out.stat().st_mtime_ns == mtime1


def test_cc_span_settings_path_rewrites_when_stale(monkeypatch, tmp_path):
    """A stale/corrupt file is rewritten to the correct content."""
    import genesis.cc.invoker as inv_mod

    fake_repo, _ = _fake_genesis_hook_repo(tmp_path)
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(fake_repo))
    out = tmp_path / "settings.json"
    monkeypatch.setattr(inv_mod, "_CC_SPAN_SETTINGS_PATH", out)

    inv_mod.cc_span_settings_path()
    out.write_text("STALE")  # corrupt it
    inv_mod.cc_span_settings_path()  # should rewrite
    data = json.loads(out.read_text())
    assert data["hooks"]["PostToolUse"][0]["matcher"] == ".*"


# --- Bash-allowlist enforcement for dispatched sessions ---------------------


def test_cc_span_settings_registers_the_bash_allowlist_guard(monkeypatch, tmp_path):
    """The injected file carries the PreToolUse hook that enforces the allowlist.

    ``_build_env`` exports GENESIS_BASH_ALLOWLIST for a scoped profile, but that
    is only a declaration — this hook is the reader. It is registered
    unconditionally because it no-ops without the env var, which is what keeps
    the file a single fixed path with no per-invocation content.
    """
    import genesis.cc.invoker as inv_mod

    fake_repo, hook = _fake_genesis_hook_repo(tmp_path)
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(fake_repo))
    out = tmp_path / "settings.json"
    monkeypatch.setattr(inv_mod, "_CC_SPAN_SETTINGS_PATH", out)

    inv_mod.cc_span_settings_path()
    data = json.loads(out.read_text())

    entry = data["hooks"]["PreToolUse"][0]
    # Anchored on purpose. MEASURED on CC 2.1.246: a matcher is a REGEX (a hook
    # registered as "^Bash$" fires on a Bash call), so a bare "Bash" also
    # matches "BashOutput", which carries no .tool_input.command and would be
    # refused by the guard's fail-closed leg with a misleading reason.
    assert entry["matcher"] == "^Bash$"
    cmd = entry["hooks"][0]["command"]
    assert cmd == f"{hook} hooks/bash_allowlist_guard.sh"
    # Absolute launcher path — CC leaves ${CLAUDE_PROJECT_DIR} unset in a dispatch.
    assert cmd.startswith("/")
    assert "${CLAUDE_PROJECT_DIR}" not in cmd
    # The span hook must survive alongside it, not be displaced by it.
    assert data["hooks"]["PostToolUse"][0]["matcher"] == ".*"


def test_registered_guard_command_survives_a_space_in_the_install_root(monkeypatch, tmp_path):
    """An install root containing a space must still produce a runnable command.

    Unquoted, the shell CC runs the hook with would split the path and fail to
    resolve it — exit 127, which is a NON-BLOCKING error, so the tool call
    proceeds. That is the same fail-open shape this whole change exists to
    remove, reached by a filesystem layout rather than a missing file.

    Asserted by round-tripping through shlex, which is how the pre-launch
    binding check parses it back.
    """
    import shlex

    import genesis.cc.invoker as inv_mod

    spaced = tmp_path / "install root with spaces"
    hook = spaced / "repo" / ".claude" / "hooks" / "genesis-hook"
    hook.parent.mkdir(parents=True)
    hook.write_text("#!/bin/bash\n")
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(spaced / "repo"))
    out = tmp_path / "settings.json"
    monkeypatch.setattr(inv_mod, "_CC_SPAN_SETTINGS_PATH", out)

    inv_mod.cc_span_settings_path()
    command = json.loads(out.read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    argv = shlex.split(command)
    assert argv == [str(hook), inv_mod._ALLOWLIST_GUARD_SCRIPT], (
        f"the launcher path did not survive quoting: {argv}"
    )


def test_bash_allowlist_guard_timeout_is_generous(monkeypatch, tmp_path):
    """A tight timeout on a containment hook converts it into a silent permit.

    A PreToolUse hook that exceeds its declared timeout is killed and the call
    PROCEEDS, so the bound has to sit far above the guard's real cost rather
    than close to it. Bounded rather than omitted so a hang cannot stall the
    session forever.
    """
    import genesis.cc.invoker as inv_mod

    fake_repo, _ = _fake_genesis_hook_repo(tmp_path)
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(fake_repo))
    out = tmp_path / "settings.json"
    monkeypatch.setattr(inv_mod, "_CC_SPAN_SETTINGS_PATH", out)

    inv_mod.cc_span_settings_path()
    data = json.loads(out.read_text())
    timeout = data["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"]
    assert timeout >= 10, "too tight — a slow guard would be killed, i.e. permit"


def test_build_args_refuses_an_allowlist_it_cannot_enforce(invoker, monkeypatch):
    """No settings file → the enforcing hook is not registered → do not launch.

    Launching anyway gives the profile unrestricted Bash while every
    declaration in the codebase says it is confined to its allowlist, which is
    worse than having declared no allowlist at all.
    """
    import genesis.cc.invoker as inv_mod

    monkeypatch.setattr(inv_mod, "cc_span_settings_path", lambda *_a, **_k: None)
    with pytest.raises(RuntimeError, match="Refusing to launch"):
        invoker._build_args(CCInvocation(prompt="hi", bash_allowlist=("gh",)))


_REPO = Path(__file__).resolve().parents[2]
_REAL_GUARD = _REPO / "scripts" / "hooks" / "bash_allowlist_guard.sh"


def _settings_registering(tmp_path, command: str) -> str:
    """Write a settings file whose PreToolUse Bash hook runs ``command``.

    The pre-launch checks read and EXECUTE whatever the settings file
    registers, so a test supplies the command directly rather than going
    through the launcher — the launcher's own resolution is covered by
    ``tests/test_scripts/test_bash_allowlist_guard.py``.
    """
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": command, "timeout": 30}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    return str(path)


@pytest.mark.parametrize(
    ("flag", "expected"),
    [("bare", "--bare skips hooks"), ("safe_mode", "--safe-mode disables all hooks")],
)
def test_build_args_refuses_an_allowlist_under_a_hook_disabling_flag(
    invoker, monkeypatch, tmp_path, flag, expected
):
    """--bare skips hooks; --safe-mode disables all customizations.

    Either one silently un-enforces the allowlist even though the settings file
    was written correctly, so the settings check alone would not catch it.
    Nothing sets these alongside an allowlist today — this keeps it that way.

    The settings file here is REAL and enforcing, and the assertion names the
    specific reason: otherwise these would pass on any later check's refusal
    too, which is how a test starts passing for the wrong reason.
    """
    import genesis.cc.invoker as inv_mod

    settings = _settings_registering(tmp_path, f"bash {_REAL_GUARD}")
    monkeypatch.setattr(inv_mod, "cc_span_settings_path", lambda *_a, **_k: settings)
    with pytest.raises(RuntimeError, match=expected):
        invoker._build_args(CCInvocation(prompt="hi", bash_allowlist=("gh",), **{flag: True}))


def test_build_args_refuses_when_env_overrides_would_blank_the_allowlist(
    invoker, monkeypatch, tmp_path
):
    """env_overrides is applied last in _build_env and wins over everything."""
    import genesis.cc.invoker as inv_mod

    settings = _settings_registering(tmp_path, f"bash {_REAL_GUARD}")
    monkeypatch.setattr(inv_mod, "cc_span_settings_path", lambda *_a, **_k: settings)
    with pytest.raises(RuntimeError, match="env_overrides sets"):
        invoker._build_args(
            CCInvocation(
                prompt="hi",
                bash_allowlist=("gh",),
                env_overrides={"GENESIS_BASH_ALLOWLIST": ""},
            )
        )


async def test_build_args_refuses_when_the_guard_was_rewritten_out_of_the_settings(
    invoker, monkeypatch, tmp_path
):
    """The shared settings path is one file, and older code rewrites it.

    A concurrently-running Genesis process on pre-merge code recomputes the
    span-only payload, sees this content as stale, and replaces it — after we
    wrote it. Re-reading at launch is what catches that.
    """
    import genesis.cc.invoker as inv_mod

    settings = _settings_registering(tmp_path, "some-other-hook.py")
    monkeypatch.setattr(inv_mod, "cc_span_settings_path", lambda *_a, **_k: settings)
    with pytest.raises(RuntimeError, match="does not register"):
        await _verify(invoker, CCInvocation(prompt="hi", bash_allowlist=("gh",)))


def _arm(monkeypatch, tmp_path, argv):
    """Register ``argv`` as the guard AND make the invoker expect exactly it.

    The binding check refuses to execute anything other than the argv it
    computes locally, so a test that wants the PROBE arms exercised has to move
    both ends together. Patching only the settings file exercises the
    tamper check instead — which is a different test, below.
    """
    import shlex

    import genesis.cc.invoker as inv_mod

    settings = _settings_registering(tmp_path, shlex.join(argv))
    monkeypatch.setattr(inv_mod, "cc_span_settings_path", lambda *_a, **_k: settings)
    monkeypatch.setattr(inv_mod, "_allowlist_guard_argv", lambda: list(argv))
    return settings


async def _verify(invoker, inv):
    """Drive the checks a real spawn path drives, in the same order.

    `_build_args` keeps only the cheap checks; the settings read, the seal and
    the two subprocess probes moved behind `verify_allowlist_enforceable` so
    they do not run on the event loop. Both spawn paths call them back to back,
    so the tests do too — asserting on `_build_args` alone would now assert on
    half the gate.
    """
    invoker._build_args(inv)
    await invoker.verify_allowlist_enforceable(inv)


async def test_build_args_refuses_a_hook_registered_by_someone_else(invoker, monkeypatch, tmp_path):
    """The settings file is outside the repo and any same-uid process can write
    it — including a confined session that can drop a file somewhere.

    The probe runs in the PARENT, which carries the server's whole environment,
    so executing a command read out of that file would hand whoever won one
    write a credential-bearing shell. The registered command is therefore
    COMPARED against the locally-computed argv and a mismatch is a refusal.
    """
    import genesis.cc.invoker as inv_mod

    planted = tmp_path / "planted.sh"
    planted.write_text("#!/usr/bin/env bash\nexit 2\n", encoding="utf-8")
    settings = _settings_registering(tmp_path, f"bash {planted} {inv_mod._ALLOWLIST_GUARD_SCRIPT}")
    monkeypatch.setattr(inv_mod, "cc_span_settings_path", lambda *_a, **_k: settings)
    with pytest.raises(RuntimeError, match="does not register"):
        await _verify(invoker, CCInvocation(prompt="hi", bash_allowlist=("gh",)))


async def test_build_args_refuses_a_registered_hook_that_does_not_refuse(
    invoker, monkeypatch, tmp_path
):
    """Registered is not the same as BINDING, and that gap is a real defect.

    The launcher resolves hooks against the MAIN worktree while the registrar
    checks the invoking tree, so a guard the registrar can see may be absent to
    the launcher — which then exits non-blocking and every command runs. The
    only thing that distinguishes those cases is running the hook.

    Here the guard is present and permits everything.
    """
    permit_all = tmp_path / "permit_all.sh"
    permit_all.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    _arm(monkeypatch, tmp_path, ["bash", str(permit_all)])
    with pytest.raises(RuntimeError, match="instead of refusing it"):
        await _verify(invoker, CCInvocation(prompt="hi", bash_allowlist=("gh",)))


async def test_build_args_refuses_a_registered_hook_that_refuses_everything(
    invoker, monkeypatch, tmp_path
):
    """The other direction, and it is not redundant.

    A hook that refuses EVERYTHING also refuses the probe, so a one-directional
    check would call it enforcing. That state is exactly what the launcher
    produces when the guard is missing: contained, but unable to run even its
    own allowlisted binary.
    """
    refuse_all = tmp_path / "refuse_all.sh"
    refuse_all.write_text("#!/usr/bin/env bash\nexit 2\n", encoding="utf-8")
    _arm(monkeypatch, tmp_path, ["bash", str(refuse_all)])
    with pytest.raises(RuntimeError, match="could do nothing at all"):
        await _verify(invoker, CCInvocation(prompt="hi", bash_allowlist=("gh",)))


async def test_build_args_probes_in_the_childs_environment_not_the_parents(
    invoker, monkeypatch, tmp_path
):
    """env_overrides wins in _build_env, so a probe under os.environ would test
    a different PATH than the session gets — and PATH is where the guard
    resolves jq and awk from. Defending one variable by name was the narrow
    version of this; building the same env is the structural one.

    The stand-in guard reports what it sees, so the assertion is about the
    environment reaching it rather than about a verdict.
    """
    reporter = tmp_path / "reporter.sh"
    reporter.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s" "$GENESIS_PROBE_MARKER" > {tmp_path}/seen.txt\n'
        '[ "$1" = "__genesis_allowlist_verification_probe__" ] && exit 2\n'
        "cat >/dev/null; exit 2\n",
        encoding="utf-8",
    )
    _arm(monkeypatch, tmp_path, ["bash", str(reporter)])
    with pytest.raises(RuntimeError):
        await _verify(
            invoker,
            CCInvocation(
                prompt="hi",
                bash_allowlist=("gh",),
                env_overrides={"GENESIS_PROBE_MARKER": "from-child-env"},
            ),
        )
    assert (tmp_path / "seen.txt").read_text() == "from-child-env", (
        "the probe ran in the parent environment, so it cannot see what the "
        "session's own PATH or dev-local override would do to the guard"
    )


async def test_build_args_launches_an_allowlisted_profile_when_the_guard_binds(
    invoker, monkeypatch, tmp_path
):
    """The permitting path, against the REAL guard. A check that only refuses
    proves nothing — and the earliest version of this test pointed at a settings
    path that did not exist, so it asserted a launch that was never enforceable.
    """
    settings = _arm(monkeypatch, tmp_path, ["bash", str(_REAL_GUARD)])
    inv = CCInvocation(prompt="hi", bash_allowlist=("gh",))
    args = invoker._build_args(inv)
    await invoker.verify_allowlist_enforceable(inv)
    assert args[args.index("--settings") + 1] == settings


@pytest.mark.parametrize("flag", ["bare", "safe_mode"])
def test_build_args_allows_hook_disabling_flags_without_an_allowlist(invoker, monkeypatch, flag):
    """No collateral refusal.

    --bare and --safe-mode are legitimate for the eval-bench arms, which
    declare no allowlist. The refusal is about the COMBINATION, so a profile
    using either alone must still launch — and must not pay the probes.
    """
    import genesis.cc.invoker as inv_mod

    monkeypatch.setattr(inv_mod, "cc_span_settings_path", lambda *_a, **_k: "/tmp/s.json")
    invoker._build_args(CCInvocation(prompt="hi", **{flag: True}))


def test_build_args_allows_a_missing_settings_file_without_an_allowlist(invoker, monkeypatch):
    """An unwritable settings file is not itself fatal — only unenforceability is."""
    import genesis.cc.invoker as inv_mod

    monkeypatch.setattr(inv_mod, "cc_span_settings_path", lambda *_a, **_k: None)
    args = invoker._build_args(CCInvocation(prompt="hi"))
    assert "env" in json.loads(args[args.index("--settings") + 1])


def test_build_env_strips_claudecode(invoker):
    with patch.dict(
        "os.environ",
        {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "HOME": "/home/test"},
    ):
        env = invoker._build_env()
        assert "CLAUDECODE" not in env
        assert "CLAUDE_CODE_ENTRYPOINT" not in env
        assert env["HOME"] == "/home/test"


def test_build_env_sets_anthropic_base_url(invoker):
    inv = CCInvocation(prompt="hello", anthropic_base_url="http://localhost:8100")
    env = invoker._build_env(inv)
    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:8100"


def test_build_env_omits_anthropic_base_url_when_none(invoker):
    inv = CCInvocation(prompt="hello")
    with patch.dict("os.environ", {}, clear=False):
        import os

        os.environ.pop("ANTHROPIC_BASE_URL", None)
        env = invoker._build_env(inv)
        assert "ANTHROPIC_BASE_URL" not in env


def test_build_env_strips_parent_anthropic_base_url(invoker):
    """Parent env ANTHROPIC_BASE_URL must not leak when field is None."""
    inv = CCInvocation(prompt="hello")
    with patch.dict("os.environ", {"ANTHROPIC_BASE_URL": "http://leaked:8100"}):
        env = invoker._build_env(inv)
        assert "ANTHROPIC_BASE_URL" not in env


def test_scope_args_empty_when_probe_fails(monkeypatch):
    """An env-scrubbed spawner (some agent CLIs' shell tooling, CI runners) has
    the systemd-run binary but no reachable user manager — the probe must fail
    closed to 'no scope wrap' instead of letting systemd-run kill the CC
    subprocess at 0.0s with 'Failed to connect to bus'."""
    import subprocess as real_subprocess

    import genesis.cc.invoker as inv_mod

    monkeypatch.setattr(inv_mod.shutil, "which", lambda _: "/usr/bin/systemd-run")

    def _probe_fails(*args, **kwargs):
        return real_subprocess.CompletedProcess(args[0], 1, b"", b"Failed to connect to bus")

    monkeypatch.setattr(inv_mod.subprocess, "run", _probe_fails)
    assert inv_mod._build_scope_args() == []


def test_scope_args_empty_when_probe_raises(monkeypatch):
    """Probe timeout / spawn failure also degrades to no wrap, never raises."""
    import subprocess as real_subprocess

    import genesis.cc.invoker as inv_mod

    monkeypatch.setattr(inv_mod.shutil, "which", lambda _: "/usr/bin/systemd-run")

    def _probe_times_out(*args, **kwargs):
        raise real_subprocess.TimeoutExpired(cmd="systemd-run", timeout=15)

    monkeypatch.setattr(inv_mod.subprocess, "run", _probe_times_out)
    assert inv_mod._build_scope_args() == []


def test_probe_raising_announces_the_lost_isolation(monkeypatch, caplog):
    """A raising probe must degrade LOUDLY, like the non-zero-exit branch.

    Silence here is indistinguishable from a scoped box: MemoryHigh/MemoryMax
    are gone and nothing in the log says so. `announce=False` (a backoff
    re-probe) still demotes to debug so a permanently-unscoped box does not
    warn on every retry forever.
    """
    import subprocess as real_subprocess

    import genesis.cc.invoker as inv_mod

    monkeypatch.setattr(inv_mod.shutil, "which", lambda _: "/usr/bin/systemd-run")

    def _probe_times_out(*args, **kwargs):
        raise real_subprocess.TimeoutExpired(cmd="systemd-run", timeout=15)

    monkeypatch.setattr(inv_mod.subprocess, "run", _probe_times_out)

    with caplog.at_level(logging.WARNING, logger=inv_mod.logger.name):
        assert inv_mod._build_scope_args(announce=True) == []
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "probe raised and nothing warned — the degradation is silent"
    assert "TimeoutExpired" in warnings[0].getMessage()

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=inv_mod.logger.name):
        assert inv_mod._build_scope_args(announce=False) == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], (
        "re-probe warned again — announce=False must demote to debug"
    )


def test_probe_sets_the_same_properties_as_the_real_invocation(monkeypatch):
    """The probe must carry the scope's properties, not just ask for a scope.

    `systemd-run` exits non-zero on a property it cannot accept (measured on
    systemd 255: "Unknown assignment: ..." and "Failed to parse MemoryMax=..."
    both exit 1), and older systemd predates the ``N%`` syntax. A property-free
    probe would SUCCEED on such a box, cache that verdict for the process
    lifetime, and leave every real dispatch dying inside systemd-run before
    Claude starts.
    """
    import subprocess as real_subprocess

    import genesis.cc.invoker as inv_mod

    monkeypatch.setattr(inv_mod.shutil, "which", lambda _: "/usr/bin/systemd-run")
    seen = []

    def _probe_ok(*args, **kwargs):
        seen.append(list(args[0]))
        return real_subprocess.CompletedProcess(args[0], 0, b"", b"")

    monkeypatch.setattr(inv_mod.subprocess, "run", _probe_ok)
    out = inv_mod._build_scope_args()

    assert len(seen) == 1
    probe_argv = seen[0]
    assert probe_argv[-1] == "/bin/true"
    # Everything the real prefix passes, the probe passed too — compared as the
    # whole argv so a future property added to one side and not the other fails
    # here instead of at dispatch time.
    assert probe_argv[:-1] == out
    for prop in inv_mod._SCOPE_PROPERTIES:
        assert ["-p", prop] == probe_argv[probe_argv.index(prop) - 1 : probe_argv.index(prop) + 1]
        assert prop in out


def test_scope_args_built_when_probe_succeeds(monkeypatch):
    import subprocess as real_subprocess

    import genesis.cc.invoker as inv_mod

    monkeypatch.setattr(inv_mod.shutil, "which", lambda _: "/usr/bin/systemd-run")

    def _probe_ok(*args, **kwargs):
        return real_subprocess.CompletedProcess(args[0], 0, b"", b"")

    monkeypatch.setattr(inv_mod.subprocess, "run", _probe_ok)
    out = inv_mod._build_scope_args()
    assert out[:3] == ["systemd-run", "--user", "--scope"]
    assert "MemoryMax=75%" in out


# --- _get_scope_args caching: success is permanent, FAILURE is not ------------
# genesis-server is long-lived. Caching one transient probe failure for the
# process lifetime silently drops MemoryHigh/MemoryMax from every later CC
# subprocess, for days, on a swapless box — the exact thing the scope exists to
# prevent. These pin the asymmetry in both directions.


def _stub_probe(monkeypatch, results):
    """Patch the probe to yield `results` in order; return the call counter."""
    import subprocess as real_subprocess

    import genesis.cc.invoker as inv_mod

    calls = []
    seq = list(results)

    def _probe(*args, **kwargs):
        calls.append(args[0])
        # NOT `next(iter(...))`. An exhausted iterator raises StopIteration,
        # which is pathological across an `await` — the over-probing case this
        # stub exists to catch HUNG the test run instead of failing it, so the
        # mutation read as "no result" rather than RED. Fail loudly instead.
        if len(calls) > len(seq):
            raise AssertionError(
                f"probe called {len(calls)}x but only {len(seq)} result(s) were "
                "stubbed — the caller is probing more often than expected"
            )
        return real_subprocess.CompletedProcess(args[0], seq[len(calls) - 1], b"", b"bus error")

    monkeypatch.setattr(inv_mod.shutil, "which", lambda _: "/usr/bin/systemd-run")
    monkeypatch.setattr(inv_mod.subprocess, "run", _probe)
    # Reset the module cache through monkeypatch so it is restored for siblings.
    monkeypatch.setattr(inv_mod, "_SCOPE_ARGS", None)
    monkeypatch.setattr(inv_mod, "_SCOPE_PROBE_FAILED_AT", None)
    monkeypatch.setattr(inv_mod, "_SCOPE_PROBE_FAILURES", 0)
    monkeypatch.setattr(inv_mod, "_SCOPE_PROBE_LOCK", None)
    return calls


@pytest.mark.asyncio
async def test_scope_probe_failure_is_retried_after_the_cooldown(monkeypatch):
    import genesis.cc.invoker as inv_mod

    calls = _stub_probe(monkeypatch, [1, 0])  # fail, then succeed
    now = [1000.0]
    # Patch the module's own clock seam, NOT time.monotonic — `inv_mod.time` is
    # the stdlib module object, so patching its attribute would replace the
    # clock process-wide for the duration of this test.
    monkeypatch.setattr(inv_mod, "_now", lambda: now[0])

    assert await inv_mod._get_scope_args() == []
    assert len(calls) == 1

    # Inside the first cooldown step: no re-probe, still degraded.
    now[0] += inv_mod._SCOPE_RETRY_SCHEDULE_S[0] - 1
    assert await inv_mod._get_scope_args() == []
    assert len(calls) == 1, "re-probed inside the cooldown — probes every dispatch"

    # Past it: re-probe, and the recovered scope is used again.
    now[0] += 2
    out = await inv_mod._get_scope_args()
    assert len(calls) == 2, "never re-probed — one transient failure is permanent"
    assert "MemoryMax=75%" in out


@pytest.mark.asyncio
async def test_scope_probe_backoff_escalates_on_repeated_failure(monkeypatch):
    """A permanently-unscoped box must decay to hourly, not probe every 5min.

    The no-reachable-bus case is a property of the machine, so a fixed retry
    would spawn a subprocess 288x/day forever and log a warning each time.
    """
    import genesis.cc.invoker as inv_mod

    calls = _stub_probe(monkeypatch, [1, 1, 1, 1, 1])
    now = [1000.0]
    monkeypatch.setattr(inv_mod, "_now", lambda: now[0])

    schedule = inv_mod._SCOPE_RETRY_SCHEDULE_S
    assert await inv_mod._get_scope_args() == []
    for step, wait in enumerate(schedule, start=1):
        # Just before this step elapses, still cooling down.
        now[0] += wait - 1
        assert await inv_mod._get_scope_args() == []
        assert len(calls) == step, f"re-probed early at step {step}"
        now[0] += 2
        assert await inv_mod._get_scope_args() == []
        assert len(calls) == step + 1, f"failed to re-probe at step {step}"

    # The last interval is the cap — it must not keep growing past the table.
    assert schedule[-1] == max(schedule)

    # BEYOND the table: failures now outnumber the schedule, so the index has
    # to CLAMP rather than walk off the end. Asserting the constant above only
    # says the table is sorted; this exercises the clamp itself — an unclamped
    # index raises IndexError inside the cooldown check, and a clamp to the
    # WRONG end (first entry) would re-probe after 300s instead of the 3600s
    # cap, restoring the 288-warnings-a-day behaviour the schedule prevents.
    now[0] += schedule[0] + 1
    assert await inv_mod._get_scope_args() == []
    assert len(calls) == len(schedule) + 1, (
        "re-probed one short-interval after the cap — the backoff index clamped "
        "to the wrong end of the schedule"
    )
    now[0] += schedule[-1] - schedule[0] + 1
    assert await inv_mod._get_scope_args() == []
    assert len(calls) == len(schedule) + 2, "never re-probed past the capped interval"


@pytest.mark.asyncio
async def test_only_the_first_probe_failure_warns(monkeypatch):
    """Announce once, then demote — otherwise the retry turns a one-line
    degradation notice into 288 warnings a day."""
    import genesis.cc.invoker as inv_mod

    _stub_probe(monkeypatch, [1, 1])
    now = [1000.0]
    monkeypatch.setattr(inv_mod, "_now", lambda: now[0])
    announced: list[bool] = []
    real_build = inv_mod._build_scope_args
    monkeypatch.setattr(
        inv_mod,
        "_build_scope_args",
        lambda announce=True: (announced.append(announce), real_build(announce))[1],
    )

    await inv_mod._get_scope_args()
    now[0] += inv_mod._SCOPE_RETRY_SCHEDULE_S[0] + 1
    await inv_mod._get_scope_args()
    assert announced == [True, False], announced


@pytest.mark.asyncio
async def test_concurrent_dispatches_share_one_probe(monkeypatch):
    """Single-flight: N dispatches during startup must not spawn N probes."""
    import genesis.cc.invoker as inv_mod

    calls = _stub_probe(monkeypatch, [0])
    monkeypatch.setattr(inv_mod, "_now", lambda: 1000.0)

    results = await asyncio.gather(*(inv_mod._get_scope_args() for _ in range(5)))
    assert len(calls) == 1, f"{len(calls)} probes for 5 concurrent dispatches"
    assert all("MemoryMax=75%" in r for r in results)


@pytest.mark.asyncio
async def test_scope_probe_success_is_cached_for_the_process_lifetime(monkeypatch):
    """The other direction: a working user manager must not be re-probed."""
    import genesis.cc.invoker as inv_mod

    calls = _stub_probe(monkeypatch, [0])
    monkeypatch.setattr(inv_mod, "_now", lambda: 1e9)

    first = await inv_mod._get_scope_args()
    assert "MemoryMax=75%" in first
    for _ in range(3):
        assert await inv_mod._get_scope_args() == first
    assert len(calls) == 1, "re-probed despite a cached success"


def test_build_env_applies_env_overrides_last(invoker):
    """env_overrides wins over keys the invoker itself computes.

    GENESIS_CC_SESSION and CLAUDE_CODE_TMPDIR are both set unconditionally by
    _build_env, so overriding them proves the applied-LAST contract (not just
    dict-merge over the inherited environ).
    """
    inv = CCInvocation(
        prompt="hello",
        env_overrides={
            "GENESIS_CC_SESSION": "bench-override",
            "CLAUDE_CONFIG_DIR": "/isolated/config",
        },
    )
    env = invoker._build_env(inv)
    assert env["GENESIS_CC_SESSION"] == "bench-override"
    assert env["CLAUDE_CONFIG_DIR"] == "/isolated/config"


def test_build_env_no_overrides_is_noop(invoker):
    """Default env_overrides=None changes nothing (regression guard)."""
    inv = CCInvocation(prompt="hello")
    env = invoker._build_env(inv)
    assert env["GENESIS_CC_SESSION"] == "1"


def test_parse_result_dict_ignores_auxiliary_model_for_downgrade(invoker):
    """modelUsage lists CC's auxiliary haiku calls (titles/topics) alongside
    the main model, in arbitrary dict order. The main model = highest tier
    present; an aux haiku listed FIRST must not read as a downgrade
    (false-positived the bench fairness check, 2026-07-09)."""
    result_data = {
        "result": "ok",
        "session_id": "s",
        "usage": {},
        "modelUsage": {"claude-haiku-4-5-20251001": {}, "claude-sonnet-5": {}},
    }
    out = invoker._parse_result_dict(
        result_data,
        CCInvocation(prompt="x", model=CCModel.SONNET),
        100,
    )
    assert out.downgraded is False
    assert "sonnet" in out.model_used


def test_parse_result_dict_detects_genuine_downgrade(invoker):
    result_data = {
        "result": "ok",
        "session_id": "s",
        "usage": {},
        "modelUsage": {"claude-haiku-4-5-20251001": {}},
    }
    out = invoker._parse_result_dict(
        result_data,
        CCInvocation(prompt="x", model=CCModel.SONNET),
        100,
    )
    assert out.downgraded is True


def test_build_env_sets_bash_allowlist(invoker):
    """Steward-style invocations export GENESIS_BASH_ALLOWLIST for the hook."""
    inv = CCInvocation(prompt="hello", bash_allowlist=("gh",))
    env = invoker._build_env(inv)
    assert env["GENESIS_BASH_ALLOWLIST"] == "gh"


def test_build_env_omits_bash_allowlist_when_empty(invoker):
    """Default (no allowlist) must NOT set the env var, and must not leak parent."""
    inv = CCInvocation(prompt="hello")
    with patch.dict("os.environ", {"GENESIS_BASH_ALLOWLIST": "leaked"}):
        env = invoker._build_env(inv)
        assert "GENESIS_BASH_ALLOWLIST" not in env


def test_build_env_sets_session_origin(invoker):
    """WS-3: an origin-tagged invocation exports GENESIS_SESSION_ORIGIN so the
    session's memory MCP writes classify accordingly."""
    inv = CCInvocation(prompt="hello", origin="external_untrusted")
    env = invoker._build_env(inv)
    assert env["GENESIS_SESSION_ORIGIN"] == "external_untrusted"


def test_build_env_pops_session_origin_when_unset(invoker):
    """No origin → the var is POPPED (a stale parent value must never leak
    into a first-party session)."""
    inv = CCInvocation(prompt="hello")
    with patch.dict("os.environ", {"GENESIS_SESSION_ORIGIN": "external_untrusted"}):
        env = invoker._build_env(inv)
        assert "GENESIS_SESSION_ORIGIN" not in env


def test_build_env_env_overrides_win_over_session_origin(invoker):
    """env_overrides are applied LAST by contract — they beat the origin stamp."""
    inv = CCInvocation(
        prompt="hello",
        origin="external_untrusted",
        env_overrides={"GENESIS_SESSION_ORIGIN": "first_party"},
    )
    env = invoker._build_env(inv)
    assert env["GENESIS_SESSION_ORIGIN"] == "first_party"


def test_build_env_sets_supervised_marker(invoker):
    """WS-3 B4: a supervised (owner-attended conversation) invocation exports
    GENESIS_SESSION_SUPERVISED so the gate-4 enforce drop spares the surface."""
    inv = CCInvocation(prompt="hello", supervised=True)
    env = invoker._build_env(inv)
    assert env["GENESIS_SESSION_SUPERVISED"] == "1"


def test_build_env_pops_supervised_marker_when_unset(invoker):
    """Default (headless dispatch) → the marker is POPPED: a stale parent value
    must never make a background session read as owner-attended."""
    inv = CCInvocation(prompt="hello")
    with patch.dict("os.environ", {"GENESIS_SESSION_SUPERVISED": "1"}):
        env = invoker._build_env(inv)
        assert "GENESIS_SESSION_SUPERVISED" not in env


async def test_conversation_invocations_are_supervised():
    """Every ConversationManager CCInvocation construction site must carry
    supervised=True — GENESIS_SESSION_ID alone is attribution, and foreground
    conversations set one too (Codex P2 on #1048). AST-pinned so a new
    invocation site in conversation.py can't silently ship unsupervised."""
    import ast
    from pathlib import Path

    import genesis.cc.conversation as conv_mod

    tree = ast.parse(Path(conv_mod.__file__).read_text())
    sites = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", "")) == "CCInvocation"
    ]
    assert len(sites) >= 3, "expected the three conversation invocation sites"
    for node in sites:
        kwargs = {k.arg for k in node.keywords if k.arg}
        assert "supervised" in kwargs, (
            f"CCInvocation at conversation.py:{node.lineno} missing supervised=True"
        )


def test_invocation_rejects_invalid_origin():
    """Producer-side loud validation: a typo'd origin fails at construction,
    never silently classifies a session first_party."""
    with pytest.raises(ValueError, match="origin"):
        CCInvocation(prompt="hello", origin="external-untrusted")  # hyphen typo


@pytest.mark.asyncio
async def test_run_success(invoker):
    # Match real CLI JSON shape (verified 2026-03-08)
    result_line = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Hello world",
            "session_id": "sess-out-1",
            "total_cost_usd": 0.186,
            "duration_ms": 1500,
            "usage": {
                "input_tokens": 50,
                "output_tokens": 20,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
            "modelUsage": {
                "claude-sonnet-4-6": {
                    "inputTokens": 50,
                    "outputTokens": 20,
                    "costUSD": 0.186,
                },
            },
        }
    )
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(result_line.encode(), b""))
    mock_proc.returncode = 0

    with (
        patch("genesis.cc.invoker.wait_for_deploy_clear", return_value=True),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
    ):
        output = await invoker.run(CCInvocation(prompt="hello"))
    assert output.text == "Hello world"
    assert output.session_id == "sess-out-1"
    assert output.cost_usd == 0.186
    assert output.input_tokens == 50
    assert output.output_tokens == 20
    assert output.model_used == "claude-sonnet-4-6"
    assert output.exit_code == 0
    assert not output.is_error
    assert not output.via_proxy


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["run", "run_streaming"])
async def test_deploy_hold_refuses_spawn(invoker, method):
    with (
        patch("genesis.cc.invoker.wait_for_deploy_clear", return_value=False) as wait,
        patch("asyncio.create_subprocess_exec") as spawn,
        pytest.raises(CCDeployInProgressError),
    ):
        if method == "run":
            await invoker.run(CCInvocation(prompt="hello"))
        else:
            await invoker.run_streaming(
                CCInvocation(prompt="hello"),
                on_event=AsyncMock(),
            )

    wait.assert_awaited_once_with()
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_via_proxy_sets_flag(invoker):
    """When anthropic_base_url is set, output.via_proxy should be True."""
    result_line = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "proxied response",
            "session_id": "sess-proxy-1",
            "total_cost_usd": 0.05,
            "duration_ms": 1000,
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "modelUsage": {"claude-sonnet-4-6": {}},
        }
    )
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(result_line.encode(), b""))
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        output = await invoker.run(
            CCInvocation(
                prompt="hello",
                anthropic_base_url="http://localhost:8100",
            )
        )
    assert output.via_proxy is True
    assert output.text == "proxied response"


@pytest.mark.asyncio
async def test_run_timeout(invoker, monkeypatch):
    # Never let the migrated kill path issue a REAL killpg(99999) — pgid 99999
    # can exist on a long-lived box and would SIGKILL an innocent group.
    killpg_calls = []
    monkeypatch.setattr(
        "genesis.util.proc_kill.os.killpg",
        lambda pgid, sig: killpg_calls.append((pgid, sig)),
    )
    mock_proc = AsyncMock()
    mock_proc.pid = (
        99999  # Must set — AsyncMock().pid int() == 1 → killpg(1) == kill(-1) == kill ALL
    )
    mock_proc.communicate = AsyncMock(side_effect=TimeoutError)
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock()
    mock_proc.returncode = -9

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        pytest.raises(CCTimeoutError, match="Timeout"),
    ):
        await invoker.run(CCInvocation(prompt="hello", timeout_s=1))


@pytest.mark.asyncio
async def test_run_nonzero_exit(invoker):
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b"Error: something failed"))
    mock_proc.returncode = 1

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        pytest.raises(CCProcessError),
    ):
        await invoker.run(CCInvocation(prompt="hello"))


def test_build_args_with_effort(invoker):
    inv = CCInvocation(prompt="hello", effort=EffortLevel.HIGH)
    args = invoker._build_args(inv)
    assert "--effort" in args
    assert "high" in args


def test_build_args_default_effort(invoker):
    inv = CCInvocation(prompt="hello")
    args = invoker._build_args(inv)
    assert "--effort" in args
    assert "medium" in args


def test_parse_output_fallback(invoker):
    """When no JSON result line found, treat as plain text."""
    output = invoker._parse_output(
        "Just plain text response",
        CCInvocation(prompt="test"),
        100,
    )
    assert output.text == "Just plain text response"
    assert output.exit_code == 0
    assert not output.is_error


def test_build_args_prompt_not_in_args(invoker):
    """Prompt is passed via stdin, not as a CLI argument."""
    inv = CCInvocation(
        prompt="evaluate this",
        allowed_tools=["WebFetch", "Read"],
        skip_permissions=True,
    )
    args = invoker._build_args(inv)
    assert "evaluate this" not in args
    assert "--" not in args
    assert "--allowedTools" in args


def test_build_args_no_separator(invoker):
    """No '--' separator needed — prompt goes via stdin."""
    inv = CCInvocation(prompt="hello")
    args = invoker._build_args(inv)
    assert "--" not in args
    assert "hello" not in args


def test_build_args_with_disallowed_tools(invoker):
    inv = CCInvocation(prompt="reflect", disallowed_tools=["Bash", "Edit"])
    args = invoker._build_args(inv)
    assert "--disallowedTools" in args
    dt_idx = args.index("--disallowedTools")
    assert args[dt_idx + 1] == "Bash,Edit"
    # Prompt is passed via stdin, not in args
    assert "reflect" not in args


# --- Streaming tests ---


def _make_stream_lines(*events: dict) -> bytes:
    """Build newline-delimited JSON bytes from event dicts."""
    return b"\n".join(json.dumps(e).encode() for e in events) + b"\n"


def _make_mock_stdin():
    """Create a mock stdin with async drain and sync write/close."""
    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()
    stdin.close = MagicMock()
    return stdin


def _make_mock_stderr(data: bytes = b""):
    """Create a mock async stderr reader."""

    class _AsyncReader:
        async def read(self):
            return data

    return _AsyncReader()


def _make_async_stdout(data: bytes, *, raise_on: tuple[int, ...] = ()):
    """A faithful-enough stand-in for asyncio.StreamReader over `data`.

    Faithfulness matters here in one specific way. The reader consumes stdout
    with readline(), where an empty return means EOF and ONLY EOF — a blank
    line in the stream comes back as b"\n". A fake built on data.split(b"\n")
    yields a bare b"" for a blank line, which the reader would take as EOF and
    silently truncate the stream mid-run. So lines keep their terminator and
    b"" is emitted exactly once, at the end.

    `raise_on` names 0-based line indices where readline() raises ValueError,
    reproducing StreamReader's over-limit behaviour: it discards the offending
    span BEFORE raising, so the next call returns the FOLLOWING line — which is
    what makes skip-and-continue safe rather than an infinite loop.
    """

    class _AsyncStdout:
        def __init__(self, payload: bytes):
            self._lines = payload.splitlines(keepends=True)
            self._i = 0
            # Which indices the reader actually asked for. A fault injected at
            # an index that is never requested makes a test VACUOUS, and the
            # loop breaks on the `result` event — so anything after it is never
            # read. Tests assert against this rather than assuming.
            self.reads: list[int] = []

        async def readline(self) -> bytes:
            if self._i >= len(self._lines):
                return b""
            idx = self._i
            self._i += 1  # consumed BEFORE raising
            self.reads.append(idx)
            if idx in raise_on:
                raise ValueError("Separator is not found, and chunk exceed the limit")
            return self._lines[idx]

        def __aiter__(self):
            return self

        async def __anext__(self):
            line = await self.readline()
            if not line:
                raise StopAsyncIteration
            return line

    return _AsyncStdout(data)


@pytest.mark.asyncio
async def test_run_streaming_success(invoker):
    events = [
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "hello"}]},
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "hello",
            "session_id": "s1",
            "total_cost_usd": 0.05,
            "duration_ms": 500,
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "modelUsage": {"claude-sonnet-4-6": {}},
        },
    ]
    data = _make_stream_lines(*events)

    mock_proc = AsyncMock()
    mock_proc.stdout = _make_async_stdout(data)
    mock_proc.stdin = _make_mock_stdin()
    mock_proc.stderr = _make_mock_stderr()
    mock_proc.wait = AsyncMock()
    mock_proc.terminate = MagicMock()
    mock_proc.returncode = 0

    collected: list[StreamEvent] = []

    async def on_event(ev: StreamEvent):
        collected.append(ev)

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        output = await invoker.run_streaming(
            CCInvocation(prompt="hello"),
            on_event=on_event,
        )

    assert output.text == "hello"
    assert output.session_id == "s1"
    assert output.cost_usd == 0.05
    assert not output.is_error
    mock_proc.terminate.assert_called_once()  # Verify subprocess terminated after result

    event_types = [e.event_type for e in collected]
    assert "init" in event_types
    assert "text" in event_types
    assert "result" in event_types


@pytest.mark.asyncio
async def test_run_streaming_timeout_returns_partial(invoker, monkeypatch):
    # Spy killpg — never issue the real syscall against a mock pid (see
    # test_run_timeout).
    monkeypatch.setattr("genesis.util.proc_kill.os.killpg", lambda *a: None)
    """On timeout, collected text is returned as partial output."""
    events = [
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "partial "}]},
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "response"}]},
        },
    ]
    data = _make_stream_lines(*events)

    mock_proc = AsyncMock()

    # Simulate: stdout yields lines then hangs → timeout fires. Must expose
    # readline() (the reader no longer uses the async-iterator protocol), and
    # must hang at the AWAIT rather than end the stream — an EOF would exit
    # the loop cleanly and never reach the timeout this test is about.
    class _SlowStdout:
        def __init__(self, payload: bytes):
            self._lines = payload.splitlines(keepends=True)
            self._i = 0

        async def readline(self) -> bytes:
            if self._i < len(self._lines):
                line = self._lines[self._i]
                self._i += 1
                return line
            await asyncio.sleep(3600)  # hang, do not EOF
            return b""

    mock_proc.stdout = _SlowStdout(data)
    mock_proc.stdin = _make_mock_stdin()
    mock_proc.stderr = _make_mock_stderr()
    mock_proc.pid = 99999  # Must set — see test_run_timeout comment
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock()
    mock_proc.returncode = -9

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        pytest.raises(CCTimeoutError, match="Timeout"),
    ):
        await invoker.run_streaming(
            CCInvocation(prompt="hello", timeout_s=0),
        )


@pytest.mark.asyncio
async def test_run_streaming_no_callback(invoker):
    """run_streaming works fine with on_event=None."""
    events = [
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "ok",
            "session_id": "s2",
            "total_cost_usd": 0.01,
            "duration_ms": 100,
            "usage": {"input_tokens": 5, "output_tokens": 2},
            "modelUsage": {},
        },
    ]
    data = _make_stream_lines(*events)

    mock_proc = AsyncMock()
    mock_proc.stdout = _make_async_stdout(data)
    mock_proc.stdin = _make_mock_stdin()
    mock_proc.stderr = _make_mock_stderr()
    mock_proc.wait = AsyncMock()
    mock_proc.terminate = MagicMock()
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        output = await invoker.run_streaming(CCInvocation(prompt="test"))

    assert output.text == "ok"
    assert not output.is_error


@pytest.mark.asyncio
async def test_run_streaming_tool_use_events(invoker):
    """Tool use events are properly parsed and forwarded."""
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": "Read", "input": {"path": "foo.py"}}]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [{"tool_use_id": "t1", "type": "tool_result", "content": "data"}]
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "found it",
            "session_id": "s3",
            "total_cost_usd": 0.02,
            "duration_ms": 200,
            "usage": {"input_tokens": 8, "output_tokens": 3},
            "modelUsage": {},
        },
    ]
    data = _make_stream_lines(*events)

    mock_proc = AsyncMock()
    mock_proc.stdout = _make_async_stdout(data)
    mock_proc.stdin = _make_mock_stdin()
    mock_proc.stderr = _make_mock_stderr()
    mock_proc.wait = AsyncMock()
    mock_proc.terminate = MagicMock()
    mock_proc.returncode = 0

    collected: list[StreamEvent] = []

    async def on_event(ev: StreamEvent):
        collected.append(ev)

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        output = await invoker.run_streaming(
            CCInvocation(prompt="find foo"),
            on_event=on_event,
        )

    assert output.text == "found it"
    tool_events = [e for e in collected if e.event_type == "tool_use"]
    assert len(tool_events) == 1
    assert tool_events[0].tool_name == "Read"
    # ...and the runtime's own record of it survives onto the output. Without
    # this the collector is unwired: deleting its append left 172 tests green.
    assert output.tools_used == ("Read",)


@pytest.mark.asyncio
async def test_run_streaming_records_tools_in_first_seen_order_without_repeats(invoker):
    """`tools_used` exists so a consumer never has to scrape tool names out of
    the response text, which cannot tell a tool that RAN from one the reply
    merely discussed. So it must match the shape that fallback produces:
    first-seen order, no duplicates — and it must stay EMPTY when nothing ran,
    because a consumer reads emptiness as "fall back", not as "no tools"."""

    def _tool(name):
        return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name}]}}

    def _result(text):
        return {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": text,
            "session_id": "s9",
            "total_cost_usd": 0.0,
            "duration_ms": 1,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "modelUsage": {},
        }

    async def _run(events):
        mock_proc = AsyncMock()
        mock_proc.stdout = _make_async_stdout(_make_stream_lines(*events))
        mock_proc.stdin = _make_mock_stdin()
        mock_proc.stderr = _make_mock_stderr()
        mock_proc.wait = AsyncMock()
        mock_proc.terminate = MagicMock()
        mock_proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            return await invoker.run_streaming(CCInvocation(prompt="go"))

    out = await _run([_tool("Bash"), _tool("Read"), _tool("Bash"), _result("done")])
    assert out.tools_used == ("Bash", "Read")

    # A response that only TALKS about tools reports none — this is the whole
    # point of the field, and the text here is exactly what defeats the regex.
    # `()` not None: the runtime DID watch this one and saw nothing.
    out2 = await _run([_result("I would run Tool: Bash, but I did not.")])
    assert out2.tools_used == ()

    # A tool_use block that is not FIRST in the message. StreamEvent.from_raw
    # stops at the first recognised block, so parsing the event would drop this
    # name — and a partial list marked runtime-sourced renders as authoritative
    # while silently incomplete. MEASURED at 13 of 8655 real assistant messages.
    buried = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "thinking", "thinking": "considering"},
                {"type": "text", "text": "let me look"},
                {"type": "tool_use", "name": "Grep"},
            ]
        },
    }
    out3 = await _run([buried, _result("done")])
    assert out3.tools_used == ("Grep",)


@pytest.mark.asyncio
async def test_run_streaming_attaches_tools_when_the_stream_has_no_result(invoker):
    """The no-result exit is a SECOND attachment site, and mutating the shared
    collector cannot distinguish the two — deleting this site alone left 173
    tests green. Reachable whenever CC's stream ends without a result event."""
    events = [
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Grep"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "partial"}]}},
    ]
    mock_proc = AsyncMock()
    mock_proc.stdout = _make_async_stdout(_make_stream_lines(*events))
    mock_proc.stdin = _make_mock_stdin()
    mock_proc.stderr = _make_mock_stderr()
    mock_proc.wait = AsyncMock()
    mock_proc.terminate = MagicMock()
    mock_proc.returncode = 0
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        out = await invoker.run_streaming(CCInvocation(prompt="go"))
    assert out.text == "partial"
    assert out.tools_used == ("Grep",)


# --- Streaming rate-limit event tests ---


@pytest.mark.asyncio
async def test_run_streaming_rate_limit_with_valid_response():
    """rate_limit_event with valid text returns the response, sets RATE_LIMITED."""
    statuses: list[str] = []

    async def on_status(s: str):
        statuses.append(s)

    inv = CCInvoker(claude_path="claude", on_cc_status_change=on_status)

    events = [
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Got it"}]}},
        {"type": "rate_limit_event", "info": {}},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Got it",
            "session_id": "s1",
            "total_cost_usd": 0.01,
            "duration_ms": 100,
            "usage": {"input_tokens": 5, "output_tokens": 3},
            "modelUsage": {"claude-sonnet-4-6": {}},
        },
    ]
    data = _make_stream_lines(*events)

    mock_proc = AsyncMock()
    mock_proc.stdout = _make_async_stdout(data)
    mock_proc.stdin = _make_mock_stdin()
    mock_proc.stderr = _make_mock_stderr()
    mock_proc.wait = AsyncMock()
    mock_proc.terminate = MagicMock()
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        output = await inv.run_streaming(CCInvocation(prompt="test"))

    # Response delivered despite rate limit signal
    assert output.text == "Got it"
    assert not output.is_error
    # Status callback fired for rate limit
    assert statuses == ["RATE_LIMITED"]


@pytest.mark.asyncio
async def test_run_streaming_rate_limit_with_empty_response_raises():
    """rate_limit_event with empty text raises CCRateLimitError."""
    from genesis.cc.exceptions import CCRateLimitError

    inv = CCInvoker(claude_path="claude")

    events = [
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "rate_limit_event", "info": {}},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "",
            "session_id": "s1",
            "total_cost_usd": 0.0,
            "duration_ms": 50,
            "usage": {"input_tokens": 5, "output_tokens": 0},
            "modelUsage": {"claude-sonnet-4-6": {}},
        },
    ]
    data = _make_stream_lines(*events)

    mock_proc = AsyncMock()
    mock_proc.stdout = _make_async_stdout(data)
    mock_proc.stdin = _make_mock_stdin()
    mock_proc.stderr = _make_mock_stderr()
    mock_proc.wait = AsyncMock()
    mock_proc.terminate = MagicMock()
    mock_proc.returncode = 0

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        pytest.raises(CCRateLimitError),
    ):
        await inv.run_streaming(CCInvocation(prompt="test"))


# --- Error classification tests ---


def test_classify_error_session_expired(invoker):
    from genesis.cc.exceptions import CCSessionError

    err = invoker._classify_error("Session 'abc' not found or expired")
    assert isinstance(err, CCSessionError)


def test_classify_error_rate_limit(invoker):
    from genesis.cc.exceptions import CCRateLimitError

    err = invoker._classify_error("Rate limit exceeded, status 429")
    assert isinstance(err, CCRateLimitError)


def test_classify_error_mcp(invoker):
    from genesis.cc.exceptions import CCMCPError

    err = invoker._classify_error("MCP server 'memory' returned error")
    assert isinstance(err, CCMCPError)
    assert err.server_name == "memory"


def test_classify_error_generic(invoker):
    err = invoker._classify_error("Something unknown went wrong")
    assert isinstance(err, CCProcessError)


def test_classify_error_thinking_block(invoker):
    """Thinking-block corruption on resume classified as session error."""
    from genesis.cc.exceptions import CCSessionError

    err = invoker._classify_error("thinking blocks cannot be modified after initial creation")
    assert isinstance(err, CCSessionError)


def test_classify_error_thinking_block_from_stdout(invoker):
    """Thinking-block signal can appear in stdout (streaming mode)."""
    from genesis.cc.exceptions import CCSessionError

    err = invoker._classify_error(
        "",
        stdout_text="Error: thinking blocks cannot be modified after initial creation",
    )
    assert isinstance(err, CCSessionError)


# --- interrupt() tests ---


def _live_proc():
    p = MagicMock()
    p.returncode = None
    return p


@pytest.mark.asyncio
async def test_interrupt_sends_sigint():
    inv = CCInvoker()
    mock_proc = _live_proc()
    inv._register_proc("k", mock_proc)
    await inv.interrupt()  # no key → most-recent live
    mock_proc.send_signal.assert_called_once_with(signal.SIGINT)


@pytest.mark.asyncio
async def test_interrupt_noop_when_idle():
    inv = CCInvoker()
    await inv.interrupt()  # empty registry — should not raise


@pytest.mark.asyncio
async def test_interrupt_noop_when_finished():
    inv = CCInvoker()
    mock_proc = MagicMock()
    mock_proc.returncode = 0  # Already exited
    inv._active_procs["k"] = mock_proc  # bypass prune to assert no-signal
    await inv.interrupt()
    mock_proc.send_signal.assert_not_called()


@pytest.mark.asyncio
async def test_interrupt_targets_keyed_proc_not_others():
    """cc-loop-01: /stop with a session key hits THAT proc, not a concurrent one."""
    inv = CCInvoker()
    proc_a, proc_b = _live_proc(), _live_proc()
    inv._register_proc("session-a", proc_a)
    inv._register_proc("session-b", proc_b)
    await inv.interrupt("session-a")
    proc_a.send_signal.assert_called_once_with(signal.SIGINT)
    proc_b.send_signal.assert_not_called()


@pytest.mark.asyncio
async def test_interrupt_no_key_targets_most_recent_live():
    inv = CCInvoker()
    proc_a, proc_b = _live_proc(), _live_proc()
    inv._register_proc("background", proc_a)
    inv._register_proc("foreground", proc_b)  # registered last
    await inv.interrupt()
    proc_b.send_signal.assert_called_once_with(signal.SIGINT)
    proc_a.send_signal.assert_not_called()


@pytest.mark.asyncio
async def test_interrupt_unknown_key_is_noop():
    inv = CCInvoker()
    inv._register_proc("session-a", _live_proc())
    await inv.interrupt("does-not-exist")  # no matching proc — no raise, no signal
    assert inv._active_procs["session-a"].send_signal.call_count == 0


def test_register_prunes_dead_entries():
    """The registry only ever holds live procs (safety net for un-popped keys)."""
    inv = CCInvoker()
    dead = MagicMock()
    dead.returncode = 1
    inv._active_procs["stale"] = dead
    inv._register_proc("fresh", _live_proc())
    assert "stale" not in inv._active_procs
    assert "fresh" in inv._active_procs


@pytest.mark.asyncio
async def test_run_registers_under_session_key_and_clears(invoker, monkeypatch):
    """End-to-end: run() registers the proc under invocation.session_key while
    executing, and unregisters it in finally (cc-loop-01)."""

    def _killpg_gone(*a):
        raise ProcessLookupError  # vacant group — never live-fire a real probe

    monkeypatch.setattr("genesis.util.proc_kill.os.killpg", _killpg_gone)
    result_line = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "ok",
            "session_id": "s",
            "total_cost_usd": 0.0,
            "duration_ms": 1,
            "usage": {
                "input_tokens": 1,
                "output_tokens": 1,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        }
    )
    captured: dict = {}
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.pid = 4242

    async def _capture(*_a, **_k):
        captured["keys"] = list(invoker._active_procs.keys())
        return (result_line.encode(), b"")

    mock_proc.communicate = _capture
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await invoker.run(CCInvocation(prompt="hi", session_key="tg:7:9"))

    assert captured["keys"] == ["tg:7:9"]  # registered under the session key mid-run
    assert invoker._active_procs == {}  # unregistered in finally


@pytest.mark.asyncio
async def test_run_streaming_registers_under_session_key_and_clears(invoker, monkeypatch):
    """run_streaming registers under session_key during streaming and clears in finally."""

    def _killpg_gone(*a):
        raise ProcessLookupError  # vacant group — never live-fire a real probe

    monkeypatch.setattr("genesis.util.proc_kill.os.killpg", _killpg_gone)
    events = [
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "ok",
            "session_id": "s1",
            "total_cost_usd": 0.0,
            "duration_ms": 1,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "modelUsage": {"claude-sonnet-4-6": {}},
        },
    ]
    mock_proc = AsyncMock()
    mock_proc.stdout = _make_async_stdout(_make_stream_lines(*events))
    mock_proc.stdin = _make_mock_stdin()
    mock_proc.stderr = _make_mock_stderr()
    mock_proc.wait = AsyncMock()
    mock_proc.terminate = MagicMock()
    mock_proc.returncode = 0
    mock_proc.pid = 5555

    captured: dict = {}

    async def on_event(_ev):
        captured.setdefault("keys", list(invoker._active_procs.keys()))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await invoker.run_streaming(
            CCInvocation(prompt="hi", session_key="tg:3:4"),
            on_event=on_event,
        )

    assert captured["keys"] == ["tg:3:4"]  # registered during streaming
    assert invoker._active_procs == {}  # cleared in finally


@pytest.mark.asyncio
async def test_interrupt_real_procs_targets_correct_one():
    """E2E with REAL subprocesses + REAL signals: interrupt(keyA) kills A, B survives.

    Proves the per-session registry delivers SIGINT to the user's proc, not a
    concurrent one (cc-loop-01). (systemd-run scope propagation is unchanged by
    this fix — same signal path, different target — and verified at deploy.)
    """
    import os as _os

    inv = CCInvoker()
    procs: dict[str, object] = {}
    try:
        for key in ("session-a", "session-b"):
            p = await asyncio.create_subprocess_exec(
                "sleep",
                "30",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                preexec_fn=_os.setpgrp,
            )
            inv._register_proc(key, p)
            procs[key] = p

        await inv.interrupt("session-a")

        try:
            await asyncio.wait_for(procs["session-a"].wait(), timeout=5)
        except TimeoutError:
            pytest.fail("proc A did not exit after interrupt('session-a')")
        assert procs["session-a"].returncode is not None  # A got SIGINT
        assert procs["session-b"].returncode is None  # B untouched
    finally:
        for p in procs.values():
            if p.returncode is None:
                p.kill()
                await p.wait()


# --- AgentProvider protocol conformance ---


def test_invoker_satisfies_agent_provider():
    from genesis.cc.protocol import AgentProvider

    assert isinstance(CCInvoker(), AgentProvider)


# --- Quota detection and status callback tests ---


def test_classify_error_quota_exhausted(invoker):
    """Hard quota exhaustion (usage limit) is distinct from transient 429."""
    from genesis.cc.exceptions import CCQuotaExhaustedError

    for msg in [
        "Usage limit exceeded for this billing period",
        "Quota exceeded — try again in 4 hours",
        "Your usage limit has been reached",
        "Usage cap exceeded for your plan",
    ]:
        err = invoker._classify_error(msg)
        assert isinstance(err, CCQuotaExhaustedError), f"Failed for: {msg}"


def test_classify_error_rate_limit_not_quota(invoker):
    """Transient rate limit (429) should NOT be classified as quota."""
    from genesis.cc.exceptions import CCQuotaExhaustedError, CCRateLimitError

    err = invoker._classify_error("Rate limit exceeded, status 429")
    assert isinstance(err, CCRateLimitError)
    assert not isinstance(err, CCQuotaExhaustedError)


def test_classify_error_rate_limit_from_stdout(invoker):
    """Rate-limit signal can appear in stdout (streaming-JSON mode) while
    stderr is empty. Classifier must check both. Observed in practice: CC
    exit=1, empty stderr, rate-limit text only on stdout — previously
    misclassified as CCProcessError and skipped retry path.
    """
    from genesis.cc.exceptions import CCRateLimitError

    err = invoker._classify_error(
        "",
        stdout_text='{"type": "error", "error": "You\'ve hit your limit · resets 8pm"}',
    )
    assert isinstance(err, CCRateLimitError)


def test_classify_error_falls_back_to_stderr_when_stdout_empty(invoker):
    """Backward compatibility: single-arg classifier (stderr only) still works."""
    from genesis.cc.exceptions import CCRateLimitError

    err = invoker._classify_error("hit your limit")
    assert isinstance(err, CCRateLimitError)


def test_classify_error_quota_from_stdout(invoker):
    """Quota exhaustion in stdout should also be classified correctly."""
    from genesis.cc.exceptions import CCQuotaExhaustedError

    err = invoker._classify_error(
        "",
        stdout_text="usage limit exceeded for this billing period",
    )
    assert isinstance(err, CCQuotaExhaustedError)


def test_classify_error_session_limit(invoker):
    """The Max-plan session-limit wording must classify as a typed limit error,
    not generic CCProcessError. Regression for the exact live-captured message
    (reflex signal CCProcessError×cc): before the fix it matched NO pattern
    ("hit your limit" is not a substring of "hit your session limit"), fell
    through to CCProcessError, and the rate-limit park/resume layer never
    engaged — background sessions died instead of parking.
    """
    from genesis.cc.exceptions import CCProcessError, CCQuotaExhaustedError

    # Exact live message (tz preserved; not private).
    err = invoker._classify_error(
        "", stdout_text="You've hit your session limit · resets 4:10am (America/Los_Angeles)"
    )
    assert isinstance(err, CCQuotaExhaustedError)
    assert not isinstance(err, CCProcessError)
    # raw_text must be carried so the park layer can parse the reset.
    assert err.raw_text is not None and "session limit" in err.raw_text.lower()


def test_classify_error_weekly_limit(invoker):
    """Weekly-limit wording also classifies as a typed limit error (quota-side),
    covering the message family, not just the session instance."""
    from genesis.cc.exceptions import CCProcessError, CCQuotaExhaustedError

    for msg in [
        "You've hit your weekly limit · resets Monday 9am",
        "Weekly limit reached for your plan",
    ]:
        err = invoker._classify_error(msg)
        assert isinstance(err, CCQuotaExhaustedError), f"Failed for: {msg}"
        assert not isinstance(err, CCProcessError)


@pytest.mark.asyncio
async def test_status_callback_on_quota_exhaustion():
    """Quota exhaustion triggers UNAVAILABLE status callback."""
    statuses: list[str] = []

    async def on_status(s: str):
        statuses.append(s)

    inv = CCInvoker(claude_path="claude", on_cc_status_change=on_status)

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b"Usage limit exceeded"))
    mock_proc.returncode = 1

    from genesis.cc.exceptions import CCQuotaExhaustedError

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        pytest.raises(CCQuotaExhaustedError),
    ):
        await inv.run(CCInvocation(prompt="hello"))

    assert statuses == ["UNAVAILABLE"]


@pytest.mark.asyncio
async def test_status_callback_on_rate_limit():
    """Transient rate limit triggers RATE_LIMITED status callback."""
    statuses: list[str] = []

    async def on_status(s: str):
        statuses.append(s)

    inv = CCInvoker(claude_path="claude", on_cc_status_change=on_status)

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b"Rate limit exceeded, 429"))
    mock_proc.returncode = 1

    from genesis.cc.exceptions import CCRateLimitError

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        pytest.raises(CCRateLimitError),
    ):
        await inv.run(CCInvocation(prompt="hello"))

    assert statuses == ["RATE_LIMITED"]


@pytest.mark.asyncio
async def test_status_callback_recovery_after_failure():
    """Success after a failure triggers NORMAL callback."""
    statuses: list[str] = []

    async def on_status(s: str):
        statuses.append(s)

    inv = CCInvoker(claude_path="claude", on_cc_status_change=on_status)

    # First call: rate limit error
    mock_proc_fail = AsyncMock()
    mock_proc_fail.communicate = AsyncMock(return_value=(b"", b"Rate limit exceeded, 429"))
    mock_proc_fail.returncode = 1

    from genesis.cc.exceptions import CCRateLimitError

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc_fail),
        pytest.raises(CCRateLimitError),
    ):
        await inv.run(CCInvocation(prompt="hello"))

    assert statuses == ["RATE_LIMITED"]

    # Second call: success
    result_json = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "ok",
            "session_id": "s1",
            "total_cost_usd": 0.01,
            "duration_ms": 100,
            "usage": {"input_tokens": 5, "output_tokens": 2},
            "modelUsage": {},
        }
    )
    mock_proc_ok = AsyncMock()
    mock_proc_ok.communicate = AsyncMock(return_value=(result_json.encode(), b""))
    mock_proc_ok.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc_ok):
        output = await inv.run(CCInvocation(prompt="hello"))

    assert output.text == "ok"
    assert statuses == ["RATE_LIMITED", "NORMAL"]


@pytest.mark.asyncio
async def test_no_callback_on_generic_error():
    """Generic process errors should NOT trigger status callback."""
    statuses: list[str] = []

    async def on_status(s: str):
        statuses.append(s)

    inv = CCInvoker(claude_path="claude", on_cc_status_change=on_status)

    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b"Something unknown went wrong"))
    mock_proc.returncode = 1

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        pytest.raises(CCProcessError),
    ):
        await inv.run(CCInvocation(prompt="hello"))

    assert statuses == []  # No callback for generic errors


@pytest.mark.asyncio
async def test_run_streaming_uses_invocation_working_dir():
    """Streaming: invocation working_dir overrides invoker default."""
    inv = CCInvoker(claude_path="claude", working_dir="/default-dir")

    events = [
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "ok",
            "session_id": "s1",
            "total_cost_usd": 0.01,
            "duration_ms": 100,
            "usage": {"input_tokens": 5, "output_tokens": 2},
            "modelUsage": {},
        },
    ]
    data = _make_stream_lines(*events)

    mock_proc = AsyncMock()
    mock_proc.stdout = _make_async_stdout(data)
    mock_proc.stdin = _make_mock_stdin()
    mock_proc.stderr = _make_mock_stderr()
    mock_proc.wait = AsyncMock()
    mock_proc.terminate = MagicMock()
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        await inv.run_streaming(
            CCInvocation(prompt="hello", working_dir="/override-dir"),
        )

    _, kwargs = mock_exec.call_args
    assert kwargs["cwd"] == "/override-dir"


@pytest.mark.asyncio
async def test_run_uses_invocation_working_dir():
    """Invocation working_dir overrides invoker default."""
    inv = CCInvoker(claude_path="claude", working_dir="/default-dir")

    result_json = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "ok",
            "session_id": "s1",
            "total_cost_usd": 0.01,
            "duration_ms": 100,
            "usage": {"input_tokens": 5, "output_tokens": 2},
            "modelUsage": {},
        }
    )
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(result_json.encode(), b""))
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        await inv.run(CCInvocation(prompt="hello", working_dir="/override-dir"))

    _, kwargs = mock_exec.call_args
    assert kwargs["cwd"] == "/override-dir"


@pytest.mark.asyncio
async def test_run_falls_back_to_invoker_working_dir():
    """When invocation has no working_dir, invoker default is used."""
    inv = CCInvoker(claude_path="claude", working_dir="/invoker-dir")

    result_json = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "ok",
            "session_id": "s1",
            "total_cost_usd": 0.01,
            "duration_ms": 100,
            "usage": {"input_tokens": 5, "output_tokens": 2},
            "modelUsage": {},
        }
    )
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(result_json.encode(), b""))
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        await inv.run(CCInvocation(prompt="hello"))

    _, kwargs = mock_exec.call_args
    assert kwargs["cwd"] == "/invoker-dir"


@pytest.mark.asyncio
async def test_no_callback_on_repeated_success():
    """Repeated success should NOT trigger callback (only recovery does)."""
    statuses: list[str] = []

    async def on_status(s: str):
        statuses.append(s)

    inv = CCInvoker(claude_path="claude", on_cc_status_change=on_status)

    result_json = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "ok",
            "session_id": "s1",
            "total_cost_usd": 0.01,
            "duration_ms": 100,
            "usage": {"input_tokens": 5, "output_tokens": 2},
            "modelUsage": {},
        }
    )
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(result_json.encode(), b""))
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await inv.run(CCInvocation(prompt="hello"))
        await inv.run(CCInvocation(prompt="hello"))

    assert statuses == []  # No callback — was never in error state


# --- Tests for --bare flag and CLAUDE_STREAM_IDLE_TIMEOUT_MS (CC 2.1.85) ---


def test_build_args_bare_true(invoker):
    inv = CCInvocation(prompt="hello", bare=True)
    args = invoker._build_args(inv)
    assert "--bare" in args


def test_build_args_bare_default(invoker):
    inv = CCInvocation(prompt="hello")
    args = invoker._build_args(inv)
    assert "--bare" not in args


def test_build_env_stream_idle_timeout(invoker):
    inv = CCInvocation(prompt="test", stream_idle_timeout_ms=180000)
    with patch.dict("os.environ", {"HOME": "/home/test"}, clear=True):
        env = invoker._build_env(inv)
    assert env["CLAUDE_STREAM_IDLE_TIMEOUT_MS"] == "180000"


def test_build_env_no_stream_idle_timeout(invoker):
    inv = CCInvocation(prompt="test")
    with patch.dict("os.environ", {"HOME": "/home/test"}, clear=True):
        env = invoker._build_env(inv)
    assert "CLAUDE_STREAM_IDLE_TIMEOUT_MS" not in env


def test_build_env_no_invocation(invoker):
    """_build_env still works when called with no invocation (backward compat)."""
    with patch.dict("os.environ", {"HOME": "/home/test"}, clear=True):
        env = invoker._build_env()
    assert env["GENESIS_CC_SESSION"] == "1"
    assert "CLAUDE_STREAM_IDLE_TIMEOUT_MS" not in env


def test_build_args_bare_with_other_flags(invoker):
    """--bare coexists with other flags like --dangerously-skip-permissions."""
    inv = CCInvocation(
        prompt="hello",
        bare=True,
        skip_permissions=True,
        mcp_config="/path/to/no_mcp.json",
    )
    args = invoker._build_args(inv)
    assert "--bare" in args
    assert "--dangerously-skip-permissions" in args
    assert "--mcp-config" in args


# --- on_spawn callback tests ---


def test_invocation_on_spawn_construction():
    """CCInvocation accepts on_spawn as a callable field."""

    async def my_callback(pid: int) -> None:
        pass

    inv = CCInvocation(prompt="hello", on_spawn=my_callback)
    assert inv.on_spawn is my_callback


def test_invocation_on_spawn_excluded_from_eq():
    """on_spawn is excluded from __eq__ (compare=False)."""

    async def cb1(pid: int) -> None:
        pass

    async def cb2(pid: int) -> None:
        pass

    inv1 = CCInvocation(prompt="hello", on_spawn=cb1)
    inv2 = CCInvocation(prompt="hello", on_spawn=cb2)
    assert inv1 == inv2  # compare=False means callbacks don't affect equality


def test_invocation_on_spawn_excluded_from_repr():
    """on_spawn is excluded from repr (repr=False)."""

    async def cb(pid: int) -> None:
        pass

    inv = CCInvocation(prompt="hello", on_spawn=cb)
    assert "on_spawn" not in repr(inv)


@pytest.mark.asyncio
async def test_run_fires_on_spawn_with_pid(invoker):
    """on_spawn callback is called with the subprocess PID."""
    spawned_pids: list[int] = []

    async def on_spawn(pid: int) -> None:
        spawned_pids.append(pid)

    result_json = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "ok",
            "session_id": "s1",
            "total_cost_usd": 0.01,
            "duration_ms": 100,
            "usage": {"input_tokens": 5, "output_tokens": 2},
            "modelUsage": {},
        }
    )
    mock_proc = AsyncMock()
    mock_proc.pid = 42000
    mock_proc.communicate = AsyncMock(return_value=(result_json.encode(), b""))
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        await invoker.run(CCInvocation(prompt="hello", on_spawn=on_spawn))

    assert spawned_pids == [42000]


@pytest.mark.asyncio
async def test_run_on_spawn_exception_does_not_abort(invoker):
    """on_spawn failure must not kill the subprocess or abort the run."""

    async def bad_callback(pid: int) -> None:
        raise RuntimeError("DB write failed")

    result_json = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "ok",
            "session_id": "s1",
            "total_cost_usd": 0.01,
            "duration_ms": 100,
            "usage": {"input_tokens": 5, "output_tokens": 2},
            "modelUsage": {},
        }
    )
    mock_proc = AsyncMock()
    mock_proc.pid = 42001
    mock_proc.communicate = AsyncMock(return_value=(result_json.encode(), b""))
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        output = await invoker.run(CCInvocation(prompt="hello", on_spawn=bad_callback))

    assert output.text == "ok"  # Run completed despite callback failure


@pytest.mark.asyncio
async def test_run_no_on_spawn_callback(invoker):
    """Without on_spawn, run() works exactly as before (backward compat)."""
    result_json = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "ok",
            "session_id": "s1",
            "total_cost_usd": 0.01,
            "duration_ms": 100,
            "usage": {"input_tokens": 5, "output_tokens": 2},
            "modelUsage": {},
        }
    )
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(result_json.encode(), b""))
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        output = await invoker.run(CCInvocation(prompt="hello"))

    assert output.text == "ok"


# ---------------------------------------------------------------------------
# Model-aware effort guard
# ---------------------------------------------------------------------------


class TestEffortClamping:
    """clamp_effort() / model_supports_effort() and _build_args() effort gating.

    Verified live against the claude CLI on 2026-07-02: `sonnet` → claude-sonnet-5,
    `fable` → claude-fable-5, `opus` → claude-opus-4-8 all accept the full
    low..max range (incl. xhigh/max); haiku (claude-haiku-4-5) uses no effort
    setting, so --effort is omitted for it entirely.
    """

    def test_clamp_effort_opus_passes_xhigh(self):
        assert clamp_effort(CCModel.OPUS, EffortLevel.XHIGH) == EffortLevel.XHIGH

    def test_clamp_effort_opus_passes_max(self):
        assert clamp_effort(CCModel.OPUS, EffortLevel.MAX) == EffortLevel.MAX

    def test_clamp_effort_sonnet_passes_xhigh(self):
        assert clamp_effort(CCModel.SONNET, EffortLevel.XHIGH) == EffortLevel.XHIGH

    def test_clamp_effort_sonnet_passes_max(self):
        assert clamp_effort(CCModel.SONNET, EffortLevel.MAX) == EffortLevel.MAX

    def test_clamp_effort_fable_passes_max(self):
        assert clamp_effort(CCModel.FABLE, EffortLevel.MAX) == EffortLevel.MAX

    def test_clamp_effort_sonnet_low_unchanged(self):
        assert clamp_effort(CCModel.SONNET, EffortLevel.LOW) == EffortLevel.LOW

    def test_haiku_uses_no_effort(self):
        assert model_supports_effort(CCModel.HAIKU) is False
        for model in (CCModel.OPUS, CCModel.SONNET, CCModel.FABLE):
            assert model_supports_effort(model) is True

    def test_build_args_sonnet_xhigh_passthrough(self, invoker):
        inv = CCInvocation(prompt="hi", model=CCModel.SONNET, effort=EffortLevel.XHIGH)
        args = invoker._build_args(inv)
        assert args[args.index("--effort") + 1] == "xhigh"

    def test_build_args_sonnet_max_passthrough(self, invoker):
        inv = CCInvocation(prompt="hi", model=CCModel.SONNET, effort=EffortLevel.MAX)
        args = invoker._build_args(inv)
        assert args[args.index("--effort") + 1] == "max"

    def test_build_args_haiku_omits_effort(self):
        haiku_invoker = CCInvoker(claude_path="/usr/bin/claude")
        inv = CCInvocation(prompt="hi", model=CCModel.HAIKU, effort=EffortLevel.MAX)
        args = haiku_invoker._build_args(inv)
        assert "--effort" not in args

    def test_build_args_opus_xhigh_unchanged(self, invoker):
        inv = CCInvocation(prompt="hi", model=CCModel.OPUS, effort=EffortLevel.XHIGH)
        args = invoker._build_args(inv)
        assert args[args.index("--effort") + 1] == "xhigh"

    def test_build_args_opus_max_unchanged(self, invoker):
        inv = CCInvocation(prompt="hi", model=CCModel.OPUS, effort=EffortLevel.MAX)
        args = invoker._build_args(inv)
        assert args[args.index("--effort") + 1] == "max"

    def test_build_args_fable_max(self, invoker):
        inv = CCInvocation(prompt="hi", model=CCModel.FABLE, effort=EffortLevel.MAX)
        args = invoker._build_args(inv)
        assert args[args.index("--model") + 1] == "fable"
        assert args[args.index("--effort") + 1] == "max"

    def test_build_args_no_clamp_warning_for_sonnet_xhigh(self, invoker, caplog):
        import logging

        inv = CCInvocation(prompt="hi", model=CCModel.SONNET, effort=EffortLevel.XHIGH)
        with caplog.at_level(logging.WARNING, logger="genesis.cc.invoker"):
            invoker._build_args(inv)
        assert not any("clamping" in r.message.lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_run_streaming_cancelled_kills_subprocess(invoker, monkeypatch):
    # Spy killpg — never issue the real syscall against a mock pid.
    monkeypatch.setattr("genesis.util.proc_kill.os.killpg", lambda *a: None)
    """Cancellation mid-stream must terminate the CC child.

    The streaming loop's CancelledError path previously only unregistered
    the proc — the child kept running (spending tokens, editing files)
    after the session row was finalized. Mirrors the guarded-killpg
    pattern the TimeoutError path already uses.
    """
    mock_proc = AsyncMock()
    mock_proc.pid = 99999  # real int — killpg(1) would signal EVERYTHING
    mock_proc.returncode = None
    mock_proc.kill = MagicMock()
    mock_proc.terminate = MagicMock()
    stdin = MagicMock()
    stdin.drain = AsyncMock()
    mock_proc.stdin = stdin

    class _CancelledStream:
        def __aiter__(self):
            return self

        async def readline(self):
            # Cancellation is delivered at the stdout await point, which is
            # now readline() rather than __anext__.
            raise asyncio.CancelledError()

        async def __anext__(self):
            # Simulate task.cancel() delivered at the stdout await point
            raise asyncio.CancelledError

    mock_proc.stdout = _CancelledStream()

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        pytest.raises(asyncio.CancelledError),
    ):
        await invoker.run_streaming(CCInvocation(prompt="hello"))

    # kill_process_group signals pid-as-pgid; killpg(99999) raises
    # ProcessLookupError (no such group) = already gone — treated as success,
    # so the direct-kill fallback must NOT fire. The kill attempt itself is
    # asserted by the group-kill tests; here we assert the run still raises
    # CancelledError (above) without leaking an unhandled error.
    assert not mock_proc.kill.called, "group-kill path must not fall back on a vanished group"


# --- Background-wait ceiling ownership + truncation detection (D1) ---


def test_stderr_bg_truncated_detects_marker():
    from genesis.cc.invoker import _stderr_bg_truncated

    assert _stderr_bg_truncated("Background tasks still running after 600s; terminating.")
    assert not _stderr_bg_truncated("some unrelated stderr noise")
    assert not _stderr_bg_truncated("")
    assert not _stderr_bg_truncated(None)


def test_build_env_sets_bg_wait_ceiling(invoker):
    """A field value well below the hard timeout is exported verbatim (ms)."""
    inv = CCInvocation(prompt="hi", timeout_s=7200, bg_wait_ceiling_ms=300_000)
    with patch.dict("os.environ", {}, clear=False):
        import os

        os.environ.pop("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", None)
        env = invoker._build_env(inv)
    assert env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] == "300000"


def test_build_env_clamps_bg_ceiling_below_hard_timeout(invoker):
    """A ceiling >= timeout_s is clamped to timeout_s*1000 - margin so the CLI's
    graceful truncation always precedes the asyncio SIGKILL."""
    from genesis.cc.invoker import _BG_WAIT_HARD_MARGIN_MS

    inv = CCInvocation(prompt="hi", timeout_s=600, bg_wait_ceiling_ms=600 * 1000)
    with patch.dict("os.environ", {}, clear=False):
        import os

        os.environ.pop("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", None)
        env = invoker._build_env(inv)
    assert env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] == str(600 * 1000 - _BG_WAIT_HARD_MARGIN_MS)


def test_build_env_bg_ceiling_operator_override_wins(invoker):
    """An operator's inherited env value beats the field (setdefault)."""
    inv = CCInvocation(prompt="hi", timeout_s=7200, bg_wait_ceiling_ms=300_000)
    with patch.dict("os.environ", {"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "42"}):
        env = invoker._build_env(inv)
    assert env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] == "42"


def test_build_env_omits_bg_ceiling_when_field_none(invoker):
    """No field + no inherited value -> the var is absent (CLI default stands)."""
    inv = CCInvocation(prompt="hi")
    with patch.dict("os.environ", {}, clear=False):
        import os

        os.environ.pop("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", None)
        env = invoker._build_env(inv)
    assert "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS" not in env


def _bg_result_events():
    return [
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "partial"}]}},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "partial",
            "session_id": "s1",
            "total_cost_usd": 0.0,
            "duration_ms": 10,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "modelUsage": {"claude-sonnet-4-6": {}},
        },
    ]


@pytest.mark.asyncio
async def test_run_streaming_sets_bg_truncated_on_ceiling_marker(monkeypatch):
    """The 'Background tasks still running...' stderr marker sets bg_truncated,
    and the partial result is still delivered (not dropped)."""

    def _killpg_gone(*a):
        raise ProcessLookupError  # vacant group — never live-fire a real probe

    monkeypatch.setattr("genesis.util.proc_kill.os.killpg", _killpg_gone)
    inv = CCInvoker(claude_path="claude")
    data = _make_stream_lines(*_bg_result_events())
    mock_proc = AsyncMock()
    mock_proc.stdout = _make_async_stdout(data)
    mock_proc.stdin = _make_mock_stdin()
    mock_proc.stderr = _make_mock_stderr(
        b"Background tasks still running after 600s; terminating.\n"
    )
    mock_proc.wait = AsyncMock()
    mock_proc.terminate = MagicMock()
    mock_proc.pid = 4242
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        output = await inv.run_streaming(CCInvocation(prompt="test"))

    assert output.bg_truncated is True
    assert output.text == "partial"


@pytest.mark.asyncio
async def test_run_streaming_no_bg_truncated_without_marker(monkeypatch):
    def _killpg_gone(*a):
        raise ProcessLookupError  # vacant group — never live-fire a real probe

    monkeypatch.setattr("genesis.util.proc_kill.os.killpg", _killpg_gone)
    inv = CCInvoker(claude_path="claude")
    data = _make_stream_lines(*_bg_result_events())
    mock_proc = AsyncMock()
    mock_proc.stdout = _make_async_stdout(data)
    mock_proc.stdin = _make_mock_stdin()
    mock_proc.stderr = _make_mock_stderr(b"")
    mock_proc.wait = AsyncMock()
    mock_proc.terminate = MagicMock()
    mock_proc.pid = 4242
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        output = await inv.run_streaming(CCInvocation(prompt="test"))

    assert output.bg_truncated is False


# --- D1 review fixes: no-result truncation + short-timeout clamp ---


@pytest.mark.asyncio
async def test_run_streaming_bg_truncated_on_no_result_branch(monkeypatch):
    """Whole-tree kill before a result line flushes: the no-result branch must
    still mark bg_truncated (review Finding 2)."""

    def _killpg_gone(*a):
        raise ProcessLookupError  # vacant group — never live-fire a real probe

    monkeypatch.setattr("genesis.util.proc_kill.os.killpg", _killpg_gone)
    inv = CCInvoker(claude_path="claude")
    events = [
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "partial"}]}},
    ]
    data = _make_stream_lines(*events)
    mock_proc = AsyncMock()
    mock_proc.stdout = _make_async_stdout(data)
    mock_proc.stdin = _make_mock_stdin()
    mock_proc.stderr = _make_mock_stderr(
        b"Background tasks still running after 600s; terminating.\n"
    )
    mock_proc.wait = AsyncMock()
    mock_proc.terminate = MagicMock()
    mock_proc.pid = 4243
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        output = await inv.run_streaming(CCInvocation(prompt="test"))

    assert output.bg_truncated is True
    assert output.text == "partial"


def test_build_env_skips_bg_ceiling_when_timeout_too_short(invoker):
    """timeout_s at/under the margin must NOT emit the ceiling env (0 = the CLI's
    'wait indefinitely', the opposite of intent) — leave the CLI default (Finding 3)."""
    inv = CCInvocation(prompt="hi", timeout_s=60, bg_wait_ceiling_ms=60_000)
    with patch.dict("os.environ", {}, clear=False):
        import os

        os.environ.pop("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", None)
        env = invoker._build_env(inv)
    assert "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS" not in env


def test_build_env_bg_ceiling_just_above_margin(invoker):
    """timeout_s just above the margin still emits a clamped ceiling."""
    from genesis.cc.invoker import _BG_WAIT_HARD_MARGIN_MS

    inv = CCInvocation(prompt="hi", timeout_s=120, bg_wait_ceiling_ms=120 * 1000)
    with patch.dict("os.environ", {}, clear=False):
        import os

        os.environ.pop("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", None)
        env = invoker._build_env(inv)
    assert env["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"] == str(120 * 1000 - _BG_WAIT_HARD_MARGIN_MS)


# --- Spawn-hardening migration: start_new_session + shared guarded group-kill ---
# (PR #1415 pattern applied to the core CC spawner; follow-up 741c6c9c.)


@pytest.mark.asyncio
async def test_run_spawned_in_new_session(invoker):
    """Both spawns must use start_new_session=True (setsid in the C helper —
    never preexec_fn: arbitrary post-fork Python can deadlock in the threaded
    server) so the kill paths can killpg the whole claude tree."""
    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured.update(kwargs)
        raise FileNotFoundError  # short-circuit after capture

    with (
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        pytest.raises(CCProcessError),
    ):
        await invoker.run(CCInvocation(prompt="hello"))
    assert captured.get("start_new_session") is True
    assert "preexec_fn" not in captured


@pytest.mark.asyncio
async def test_streaming_spawned_in_new_session(invoker):
    captured: dict = {}

    async def fake_exec(*args, **kwargs):
        captured.update(kwargs)
        raise FileNotFoundError

    with (
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        pytest.raises(CCProcessError),
    ):
        await invoker.run_streaming(CCInvocation(prompt="hello"))
    assert captured.get("start_new_session") is True
    assert "preexec_fn" not in captured


@pytest.mark.asyncio
async def test_run_timeout_group_kills_by_pid_when_leader_reaped(invoker, monkeypatch):
    """The timeout kill must signal proc.pid AS the pgid, never via
    os.getpgid — once asyncio reaps the leader (a descendant can keep
    communicate() pending), getpgid raises and a bare proc.kill() no-ops,
    leaking the very tree the kill exists to reap (the #1409 round-3 class)."""
    killpg_calls = []
    monkeypatch.setattr(
        "genesis.util.proc_kill.os.killpg",
        lambda pgid, sig: killpg_calls.append((pgid, sig)),
    )

    mock_proc = AsyncMock()
    mock_proc.pid = 99998  # explicit — never a mock default (killpg(1) trap)
    mock_proc.communicate = AsyncMock(side_effect=TimeoutError)
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock()
    mock_proc.returncode = -9
    mock_proc.stderr = None

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        pytest.raises(CCTimeoutError, match="Timeout"),
    ):
        await invoker.run(CCInvocation(prompt="hello", timeout_s=1))
    assert killpg_calls and killpg_calls[0][0] == 99998
    mock_proc.kill.assert_not_called()


@pytest.mark.asyncio
async def test_run_cancel_group_kills_tree(invoker, monkeypatch):
    """Cancellation mid-communicate: the abnormal-exit cleanup must GROUP-kill
    the detached claude tree — a bare proc.kill() orphans its MCP children."""
    killpg_calls = []
    monkeypatch.setattr(
        "genesis.util.proc_kill.os.killpg",
        lambda pgid, sig: killpg_calls.append((pgid, sig)),
    )
    mock_proc = AsyncMock()
    mock_proc.pid = 99997
    mock_proc.communicate = AsyncMock(side_effect=asyncio.CancelledError)
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock()
    mock_proc.returncode = None  # still running at cleanup time

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        pytest.raises(asyncio.CancelledError),
    ):
        await invoker.run(CCInvocation(prompt="hello"))
    assert killpg_calls and killpg_calls[0][0] == 99997


@pytest.mark.asyncio
async def test_run_timeout_reap_is_bounded(invoker, monkeypatch):
    """The post-kill reap must be BOUNDED — a paused pipe transport can stall
    an unbounded proc.wait() forever, turning timeout recovery into a hang."""
    monkeypatch.setattr("genesis.util.proc_kill.os.killpg", lambda *a: None)
    monkeypatch.setattr("genesis.util.proc_kill.DEFAULT_REAP_TIMEOUT_S", 0.2)

    async def _hang(*a, **k):
        await asyncio.sleep(600)

    mock_proc = AsyncMock()
    mock_proc.pid = 99996
    mock_proc.communicate = AsyncMock(side_effect=TimeoutError)
    mock_proc.kill = MagicMock()
    mock_proc.wait = _hang  # unbounded reap would hang here forever
    mock_proc.returncode = -9
    mock_proc.stderr = None

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        pytest.raises(CCTimeoutError, match="Timeout"),
    ):
        await asyncio.wait_for(
            invoker.run(CCInvocation(prompt="hello", timeout_s=1)),
            timeout=10,  # the test bound: recovery must not hang
        )


@pytest.mark.asyncio
async def test_streaming_stdin_feed_failure_group_kills(invoker, monkeypatch):
    """A failure between spawn and the stream loop (broken pipe on the stdin
    feed) must GROUP-kill the tree, not just the direct child."""
    killpg_calls = []
    monkeypatch.setattr(
        "genesis.util.proc_kill.os.killpg",
        lambda pgid, sig: killpg_calls.append((pgid, sig)),
    )
    mock_proc = AsyncMock()
    mock_proc.pid = 99995
    mock_proc.kill = MagicMock()
    mock_proc.stdin = MagicMock()
    mock_proc.stdin.write = MagicMock(side_effect=RuntimeError("broken pipe"))

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        pytest.raises(RuntimeError, match="broken pipe"),
    ):
        await invoker.run_streaming(CCInvocation(prompt="hello"))
    assert killpg_calls and killpg_calls[0][0] == 99995


@pytest.mark.asyncio
async def test_streaming_on_event_failure_group_kills(invoker, monkeypatch):
    """Any non-timeout, non-cancel exception escaping the stream loop (an
    on_event callback raising, an over-limit stream line) must group-kill the
    live, already-unregistered tree — otherwise it leaks detached and even
    /stop can't reach it (architect finding 1)."""
    killpg_calls = []
    monkeypatch.setattr(
        "genesis.util.proc_kill.os.killpg",
        lambda pgid, sig: killpg_calls.append((pgid, sig)),
    )
    events = [{"type": "system", "subtype": "init", "session_id": "s1"}]
    mock_proc = AsyncMock()
    mock_proc.pid = 99994
    mock_proc.stdout = _make_async_stdout(_make_stream_lines(*events))
    mock_proc.stdin = _make_mock_stdin()
    mock_proc.stderr = _make_mock_stderr()
    mock_proc.wait = AsyncMock()
    mock_proc.kill = MagicMock()
    mock_proc.returncode = None

    async def bad_on_event(ev):
        raise RuntimeError("callback exploded")

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        pytest.raises(RuntimeError, match="callback exploded"),
    ):
        await invoker.run_streaming(CCInvocation(prompt="hello"), on_event=bad_on_event)
    assert killpg_calls and killpg_calls[0][0] == 99994


@pytest.mark.asyncio
async def test_streaming_terminate_ignored_escalates_to_group_kill(invoker, monkeypatch):
    """The terminate-after-result reap must be BOUNDED: a CC that ignores the
    graceful SIGTERM (wedged node/MCP teardown) previously hung the dispatch
    forever AFTER the result was already obtained. Bounded reap → escalate to
    the group kill → bounded reap again (architect finding 2)."""
    killpg_calls = []
    monkeypatch.setattr(
        "genesis.util.proc_kill.os.killpg",
        lambda pgid, sig: killpg_calls.append((pgid, sig)),
    )
    monkeypatch.setattr("genesis.util.proc_kill.DEFAULT_REAP_TIMEOUT_S", 0.2)

    events = [
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "done",
            "session_id": "s1",
            "total_cost_usd": 0.01,
            "duration_ms": 100,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "modelUsage": {"claude-sonnet-4-6": {}},
        },
    ]
    mock_proc = AsyncMock()
    mock_proc.pid = 99993
    mock_proc.stdout = _make_async_stdout(_make_stream_lines(*events))
    mock_proc.stdin = _make_mock_stdin()
    mock_proc.stderr = _make_mock_stderr()
    mock_proc.terminate = MagicMock()  # graceful stop is IGNORED (no exit)
    mock_proc.kill = MagicMock()

    hang_then_exit = {"calls": 0}

    async def _wait():
        hang_then_exit["calls"] += 1
        if hang_then_exit["calls"] == 1:
            await asyncio.sleep(600)  # SIGTERM ignored — first reap must bound out
        mock_proc.returncode = -9
        return -9

    mock_proc.wait = _wait
    mock_proc.returncode = None

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        output = await asyncio.wait_for(
            invoker.run_streaming(CCInvocation(prompt="hello")),
            timeout=10,  # the test bound: the reap must not hang the dispatch
        )
    assert output.text == "done"
    assert killpg_calls and killpg_calls[0][0] == 99993  # escalation fired


@pytest.mark.asyncio
async def test_streaming_escalates_when_leader_exits_but_group_survives(
    invoker,
    monkeypatch,
):
    """Codex P2 (PR #1417): after terminate(), the LEADER can exit (returncode
    set, e.g. -15) while an MCP/helper child survives in the group. Escalation
    gated on returncode-None alone would skip the group kill and leak the
    descendant while Genesis reports completion — the gate must probe GROUP
    liveness."""
    calls = []

    def _killpg(pgid, sig):
        calls.append((pgid, sig))
        # sig 0 probe: group still ALIVE (a descendant survives) → no raise

    monkeypatch.setattr("genesis.util.proc_kill.os.killpg", _killpg)
    monkeypatch.setattr("genesis.cc.invoker._ESCALATION_GRACE_S", 0.01)

    events = [
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "done",
            "session_id": "s1",
            "total_cost_usd": 0.01,
            "duration_ms": 100,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "modelUsage": {"claude-sonnet-4-6": {}},
        },
    ]
    mock_proc = AsyncMock()
    mock_proc.pid = 99992
    mock_proc.stdout = _make_async_stdout(_make_stream_lines(*events))
    mock_proc.stdin = _make_mock_stdin()
    mock_proc.stderr = _make_mock_stderr()
    mock_proc.terminate = MagicMock()
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock(return_value=-15)
    mock_proc.returncode = -15  # leader ALREADY exited — the P2's trap

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        output = await invoker.run_streaming(CCInvocation(prompt="hello"))
    assert output.text == "done"
    # the SIGKILL escalation must have fired despite returncode being set
    import signal as _signal

    assert (99992, _signal.SIGKILL) in calls


# --- Over-limit stream-json lines must cost the LINE, not the SESSION -------
# MEASURED 2026-09-02: a browser session emitted one stream-json line above the
# 1 MiB reader limit. StreamReader.readline() raised ValueError, it propagated
# out of `async for raw_line in proc.stdout`, and the whole session died after
# 104.4s of completed work. One occurrence since 2026-08-01 — rare, and total
# loss when it fires.


def _result_event(text: str = "done") -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": text,
        "session_id": "s1",
        "total_cost_usd": 0.01,
        "duration_ms": 10,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _streaming_proc(data: bytes, *, raise_on: tuple[int, ...] = ()):
    proc = AsyncMock()
    proc.stdout = _make_async_stdout(data, raise_on=raise_on)
    proc.stdin = _make_mock_stdin()
    proc.stderr = _make_mock_stderr()
    proc.wait = AsyncMock()
    proc.terminate = MagicMock()
    proc.returncode = 0
    return proc


def _no_host_syscalls(monkeypatch):
    """Keep BOTH host-touching calls off the real process table.

    `killpg` is the known one (procedure `process_kill_safety`). The second bites
    one syscall earlier and the same fake pid feeds it: `run_streaming` calls
    `set_oom_score_adj(proc.pid, 500)` (`invoker.py:1202`), which writes
    `/proc/<pid>/oom_score_adj` (`invoker.py:57-58`). A MagicMock pid makes that
    path nonsense and it fails harmlessly, but an int pid that happens to be a
    live same-uid process gets its OOM score raised to +500 — the kernel is then
    told to prefer killing an unrelated process. Stub it rather than gamble on
    the pid being vacant.
    """

    def _gone(*a):
        raise ProcessLookupError  # vacant group — never live-fire a real probe

    monkeypatch.setattr("genesis.util.proc_kill.os.killpg", _gone)
    monkeypatch.setattr("genesis.cc.invoker.set_oom_score_adj", lambda *a, **k: None)


@pytest.mark.asyncio
async def test_over_limit_line_is_dropped_and_the_session_survives(invoker):
    """The exact incident shape: an oversized line mid-stream, with the real
    result arriving after it."""
    data = _make_stream_lines(
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
        _result_event("survived"),
    )
    # index 1 = the assistant line; it raises instead of being returned.
    proc = _streaming_proc(data, raise_on=(1,))

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        output = await invoker.run_streaming(CCInvocation(prompt="x"))

    assert output.text == "survived"
    assert output.session_id == "s1"
    assert not output.is_error
    assert 1 in proc.stdout.reads, proc.stdout.reads  # the fault was reached


@pytest.mark.asyncio
async def test_multiple_over_limit_lines_all_dropped(invoker):
    """Several oversized lines in one stream must not compound into a failure,
    and must not spin: readline() consumes the span before raising."""
    data = _make_stream_lines(
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "a"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "b"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "c"}]}},
        _result_event("still here"),
    )
    proc = _streaming_proc(data, raise_on=(1, 2, 3))

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        output = await invoker.run_streaming(CCInvocation(prompt="x"))

    assert output.text == "still here"
    assert {1, 2, 3} <= set(proc.stdout.reads), proc.stdout.reads


@pytest.mark.asyncio
async def test_dropping_the_result_line_raises_instead_of_faking_success(invoker):
    """THE dangerous case, and the reason dropping cannot be silent.

    If the dropped line was the `result` event there is no result at all. The
    no-result path builds CCOutput(is_error=False, session_id="", cost_usd=0.0),
    and downstream `success = not output.is_error` would record a phantom
    completion — which on the home model calls note_home_recovery() and clears
    an account-wide rate-limit fallback, and whose empty-text shape forges the
    silent subscription-cap signature that becomes a CRITICAL alert. Before the
    drop-and-continue loop this raised; it must keep raising.

    Also the drop-then-EOF shape: nothing parseable follows, so a loop that
    failed to advance would spin instead of reaching EOF.

    The TYPE is pinned, not just the raising. This asserted `CCProcessError`,
    which `CCStreamTruncatedError` subclasses — so reverting to the generic
    error left this test green while restoring the replay hazard it exists to
    prevent (Codex P1, PR #1625 round 1). The subclass relationship is asserted
    separately, since existing `except CCProcessError` handlers depend on it.
    """
    data = _make_stream_lines(
        {"type": "system", "subtype": "init", "session_id": "s1"},
        _result_event("never seen"),
    )
    proc = _streaming_proc(data, raise_on=(1,))  # the RESULT line is dropped

    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        pytest.raises(CCStreamTruncatedError, match="NO result event"),
    ):
        await asyncio.wait_for(invoker.run_streaming(CCInvocation(prompt="x")), timeout=10)

    assert issubclass(CCStreamTruncatedError, CCProcessError), (
        "handlers catching CCProcessError must keep catching this"
    )

    # Guard the guard: prove the injected fault was actually REACHED. The reader
    # breaks on a result event, so a fault placed after one is never read and
    # the test would pass with the whole except-branch deleted.
    assert 1 in proc.stdout.reads, proc.stdout.reads


@pytest.mark.asyncio
async def test_dropped_line_with_empty_result_does_not_feed_the_cap_detector(invoker):
    """A drop is a KNOWN cause of thin output, so it must not be reported as the
    unexplained-empty signature the silent-cap detector aggregates into a
    CRITICAL alert.

    The reachable shape is a result that DID arrive but is empty, alongside a
    drop. (Drop + NO result raises before reaching any detector, so a guard on
    that branch would be dead code — a mutation sweep caught exactly that.)

    Not firing the detector was only HALF the answer, and the half this test
    originally asserted — returning the empty output as a success — was the
    other half done wrong (Codex P1, PR #1625 round 1). An empty result after a
    drop is a LOST ANSWER: downstream `success = not output.is_error` records a
    phantom completion, and on the home model that clears an account-wide
    rate-limit fallback. So the run raises, and the assertion that matters here
    is that it raises WITHOUT forging the cap signature on its way out.
    """
    data = _make_stream_lines(
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}},
        _result_event(""),
    )
    proc = _streaming_proc(data, raise_on=(1,))  # drop the assistant line
    fired = []

    async def _spy(*a, **k):
        fired.append(a)

    invoker._fire_empty_output_callback = _spy

    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        pytest.raises(CCStreamTruncatedError),
    ):
        await invoker.run_streaming(CCInvocation(prompt="x", expect_output=True))

    assert 1 in proc.stdout.reads, proc.stdout.reads  # the fault was reached
    assert not fired, "a dropped-line run forged the silent-cap signature"


@pytest.mark.asyncio
async def test_bg_truncation_explains_a_missing_result_better_than_a_drop_does(
    invoker, monkeypatch
):
    """A drop plus NO result normally raises — but not when something else
    already accounts for the missing result.

    A background run SIGKILLed at the CLI's wait ceiling legitimately emits no
    result event, and the no-result fallback returns what it collected with
    ``bg_truncated=True`` and its own truncation notice. Raising instead throws
    away a usable partial deliverable and blames a cause that is not the cause
    (Codex P2, PR #1625 round 1): the trace line was oversized, the ANSWER was
    not — it is right there in the collected text.

    Ordering is the whole finding. The raise sat ahead of the fallback, so the
    two conditions could never be weighed against each other.
    """
    _no_host_syscalls(monkeypatch)
    data = _make_stream_lines(
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "oversized"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "keep me"}]}},
    )
    # index 1 = the oversized tool-result trace; no result event ever arrives.
    proc = _streaming_proc(data, raise_on=(1,))
    proc.stderr = _make_mock_stderr(b"Background tasks still running after 600s; terminating.\n")
    proc.pid = 424201  # explicit + distinct; never a mock default

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        output = await invoker.run_streaming(CCInvocation(prompt="x", expect_output=True))

    assert 1 in proc.stdout.reads, proc.stdout.reads  # the drop really happened
    assert output.text == "keep me", "the partial deliverable was discarded"
    assert output.bg_truncated is True, "the truncation notice was lost"
    # The OTHER return path that hands back a drop-affected output. Its CCOutput
    # is hand-built rather than derived from a result event, so it needs its own
    # assertion — the sibling stamp on the result path cannot cover it.
    assert output.stream_lines_dropped == 1, (
        "the no-result path returned a partial run as a clean one"
    )


@pytest.mark.asyncio
async def test_an_empty_result_without_a_drop_is_still_the_cap_signature(invoker, monkeypatch):
    """CLAUSE COVER for `oversized_dropped` in the result guard.

    Empty output with NO drop is the unexplained-empty shape the silent-cap
    detector exists to aggregate. Only a DROP explains it away. Without this,
    deleting that clause — so any empty result raises — passes the suite, and
    the cap detector goes permanently silent behind an exception.
    """
    _no_host_syscalls(monkeypatch)
    data = _make_stream_lines(
        {"type": "system", "subtype": "init", "session_id": "s1"},
        _result_event(""),
    )
    proc = _streaming_proc(data)  # nothing dropped
    proc.pid = 424202  # explicit + distinct; never a mock default
    fired = []

    async def _spy(*a, **k):
        fired.append(a)

    invoker._fire_empty_output_callback = _spy

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        output = await invoker.run_streaming(CCInvocation(prompt="x", expect_output=True))

    assert output.text == ""
    assert fired, "an unexplained empty result stopped reaching the cap detector"


@pytest.mark.asyncio
async def test_no_result_and_no_drop_returns_the_collected_text(invoker, monkeypatch):
    """CLAUSE COVER for `oversized_dropped` in the no-result guard.

    A stream that ends without a result event is an ordinary supported shape —
    the collected text IS the response. Deleting that clause turns every one of
    those into a raise, which this pins.
    """
    _no_host_syscalls(monkeypatch)
    data = _make_stream_lines(
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "answer"}]}},
    )
    proc = _streaming_proc(data)  # nothing dropped, no result event
    proc.pid = 424203  # explicit + distinct; never a mock default

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        output = await invoker.run_streaming(CCInvocation(prompt="x"))

    assert output.text == "answer"


@pytest.mark.asyncio
async def test_a_drop_with_surviving_text_still_raises_without_bg_truncation(invoker, monkeypatch):
    """CLAUSE COVER for the `bg_truncated` conjunct of the exemption.

    Surviving partial text is NOT on its own a reason to forgive a missing
    result — the exemption exists for a background run killed at the wait
    ceiling, which is what makes the absence explainable. Drop the
    ``bg_truncated`` conjunct and any run with leftover text goes quiet.
    """
    _no_host_syscalls(monkeypatch)
    data = _make_stream_lines(
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "oversized"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "keep me"}]}},
    )
    proc = _streaming_proc(data, raise_on=(1,))
    proc.pid = 424204  # explicit + distinct; never a mock default

    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        pytest.raises(CCStreamTruncatedError, match="NO result event"),
    ):
        await invoker.run_streaming(CCInvocation(prompt="x"))


@pytest.mark.asyncio
async def test_bg_truncation_with_nothing_collected_still_raises(invoker, monkeypatch):
    """CLAUSE COVER for the `partial_text` conjunct of the exemption.

    Background truncation forgives a missing result only when there is a
    deliverable to return instead. With the answer itself dropped there is
    nothing to hand back, so this is the lost-answer case again and must raise.
    Drop that conjunct and it returns an empty success — the phantom completion
    the whole guard exists to prevent.
    """
    _no_host_syscalls(monkeypatch)
    data = _make_stream_lines(
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "the answer"}]}},
    )
    proc = _streaming_proc(data, raise_on=(1,))  # the only text line is dropped
    proc.stderr = _make_mock_stderr(b"Background tasks still running after 600s; terminating.\n")
    proc.pid = 424205  # explicit + distinct; never a mock default

    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        pytest.raises(CCStreamTruncatedError, match="NO result event"),
    ):
        await invoker.run_streaming(CCInvocation(prompt="x"))


@pytest.mark.asyncio
async def test_a_dropped_result_never_retries_the_same_failover_peer():
    """The SECOND retry site, which the first fix missed entirely.

    `_run_failover_peer` re-runs the same prompt on the same peer when a sticky
    resume fails. Its only side-effect guard is "answer text already streamed" —
    and an oversized line eats the answer, so that guard reads empty exactly
    when a re-run is least safe. Before the failure was typed it arrived as a
    bare ValueError and missed this handler; typing it ARMED this path, so the
    type has to be re-raised here too or the fix relocates the hazard.
    """
    from genesis.cc.conversation import ConversationLoop

    loop = ConversationLoop.__new__(ConversationLoop)
    calls = []

    async def _invoke(inv, on_event):
        calls.append(inv)
        raise CCStreamTruncatedError("result line dropped")

    loop._invoke_peer = _invoke

    with pytest.raises(CCStreamTruncatedError):
        await loop._run_failover_peer(
            "peer-a",
            CCInvocation(prompt="x"),
            sticky={"roster_model": "peer-a", "cc_session_id": "sess-1"},
            on_event=None,
            streamed={"text": ""},  # the answer was lost, so nothing streamed
        )

    assert len(calls) == 1, f"the prompt was replayed on the same peer ({len(calls)}x)"


@pytest.mark.asyncio
async def test_a_dropped_result_never_triggers_stale_resume_recovery():
    """The error type is load-bearing, so pin it at the HANDLER, not the raise.

    A resumed turn that raises a bare ``CCError`` lands in
    ``_recover_stale_resume``, which fails the session and re-runs the prompt
    from scratch — after the first attempt already executed its tool calls. An
    MCP write or an outreach send would happen twice, with nothing downstream
    to dedupe (Codex P1, PR #1625 round 1).

    Asserting the raise alone would not catch this: the old code raised too,
    just with a type the retry path swallowed. What must hold is that the
    exception REACHES the caller on a resume.
    """
    from genesis.cc.conversation import ConversationLoop

    loop = ConversationLoop.__new__(ConversationLoop)
    loop._invoker = SimpleNamespace(
        run_streaming=AsyncMock(side_effect=CCStreamTruncatedError("result line dropped"))
    )

    async def _must_not_run(*a, **k):  # pragma: no cover - the point is it never runs
        raise AssertionError("stale-resume recovery replayed a size failure")

    loop._recover_stale_resume = _must_not_run

    with pytest.raises(CCStreamTruncatedError):
        await loop._try_invoke_streaming(
            CCInvocation(prompt="x"),
            session={"session_id": "s1"},
            was_resume=True,  # the dangerous case: a live session mid-conversation
            prompt_text="x",
            model=CCModel.SONNET,
            effort=EffortLevel.MEDIUM,
            user_id="u1",
            channel=ChannelType.TELEGRAM,
            thread_id=None,
            on_event=None,
        )


def _error_result_event(text: str) -> dict:
    ev = _result_event(text)
    ev["subtype"] = "error_during_execution"
    ev["is_error"] = True
    return ev


@pytest.mark.asyncio
async def test_a_drop_before_an_error_result_is_not_a_retryable_error(invoker, monkeypatch):
    """The RETRYABLE branches run before the drop guard, so they had to learn it.

    The first round's fix only covered the shapes that fall THROUGH to the guard.
    An oversized line followed by an `is_error` result never reaches it: the
    error is classified and raised first, and a classified CCError is exactly
    what `_recover_stale_resume` reruns and what roster failover replaces with a
    second full-tools peer run — after the tool calls behind the dropped line
    already happened (Codex P1, PR #1625 round 2). Before drop-and-continue this
    shape aborted the read with a bare ValueError, so it never reached a retry
    path at all; surviving the line is what exposed it.

    Note the result here carries TEXT. The hazard is not "the answer is missing"
    — it is "this run must not be replayed" — so a guard keyed on empty text
    would sail straight past this case.
    """
    from genesis.cc.exceptions import CCQuotaExhaustedError

    _no_host_syscalls(monkeypatch)
    data = _make_stream_lines(
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "oversized tool result"},
                ]
            },
        },
        _error_result_event("You've hit your usage limit"),
    )
    proc = _streaming_proc(data, raise_on=(1,))
    proc.pid = 424206  # explicit + distinct; never a mock default
    statuses: list = []

    async def _spy(err):
        statuses.append(err)

    invoker._notify_status_change = _spy

    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        pytest.raises(CCStreamTruncatedError) as caught,
    ):
        await invoker.run_streaming(CCInvocation(prompt="x"))

    assert 1 in proc.stdout.reads, proc.stdout.reads  # the drop really happened
    assert isinstance(caught.value.__cause__, CCQuotaExhaustedError), (
        "the provider's own classification must survive as the cause"
    )
    # The status signal is real evidence about the account and still worth
    # having — suppressing the retry must not also suppress the back-off.
    assert [type(e) for e in statuses] == [CCQuotaExhaustedError]


@pytest.mark.asyncio
async def test_an_error_result_without_a_drop_still_raises_the_classified_error(
    invoker, monkeypatch
):
    """CLAUSE COVER for `oversized_dropped` at the is_error branch.

    A run that errors with the stream fully read is an ordinary, retryable
    failure — stale-resume recovery and roster failover exist for it. Drop the
    clause and every CC error becomes un-retryable, which silently disables both.
    """
    from genesis.cc.exceptions import CCQuotaExhaustedError

    _no_host_syscalls(monkeypatch)
    data = _make_stream_lines(
        {"type": "system", "subtype": "init", "session_id": "s1"},
        _error_result_event("You've hit your usage limit"),
    )
    proc = _streaming_proc(data)  # nothing dropped
    proc.pid = 424207  # explicit + distinct; never a mock default

    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        pytest.raises(CCQuotaExhaustedError),
    ):
        await invoker.run_streaming(CCInvocation(prompt="x"))


@pytest.mark.asyncio
async def test_a_drop_before_an_empty_rate_limited_result_is_not_retryable(invoker, monkeypatch):
    """The second retryable branch, and the more expensive one.

    A rate-limit error is what sends the turn to roster failover, so replaying
    it costs a whole second peer running the same prompt with full tools. Same
    ordering defect as the is_error branch: it raises before the drop guard.
    """
    from genesis.cc.exceptions import CCRateLimitError

    _no_host_syscalls(monkeypatch)
    data = _make_stream_lines(
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "oversized tool result"},
                ]
            },
        },
        {"type": "rate_limit_event", "info": {}},
        _result_event(""),
    )
    proc = _streaming_proc(data, raise_on=(1,))
    proc.pid = 424208  # explicit + distinct; never a mock default

    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        pytest.raises(CCStreamTruncatedError) as caught,
    ):
        await invoker.run_streaming(CCInvocation(prompt="x"))

    assert 1 in proc.stdout.reads, proc.stdout.reads  # the drop really happened
    assert isinstance(caught.value.__cause__, CCRateLimitError)


@pytest.mark.asyncio
async def test_an_empty_rate_limited_result_without_a_drop_still_rate_limits(invoker, monkeypatch):
    """CLAUSE COVER for `oversized_dropped` at the rate-limit branch.

    Without a drop, an empty rate-limited result is exactly what failover is
    for. Drop the clause and the turn stops reaching a peer at all.
    """
    from genesis.cc.exceptions import CCRateLimitError

    _no_host_syscalls(monkeypatch)
    data = _make_stream_lines(
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "rate_limit_event", "info": {}},
        _result_event(""),
    )
    proc = _streaming_proc(data)  # nothing dropped
    proc.pid = 424209  # explicit + distinct; never a mock default

    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        pytest.raises(CCRateLimitError),
    ):
        await invoker.run_streaming(CCInvocation(prompt="x"))


@pytest.mark.asyncio
async def test_a_drop_does_not_discard_a_rate_limited_answer_that_survived(invoker, monkeypatch):
    """The BOUND on the two fixes above, and the reason they are placed where
    they are rather than hoisted above the whole result block.

    A rate-limit event alongside a real answer RETURNS that answer — it raises
    nothing, so it permits no replay and needs no guard. Hoisting the drop check
    over this branch would throw away a delivered answer to prevent a retry that
    was never going to happen.
    """
    _no_host_syscalls(monkeypatch)
    data = _make_stream_lines(
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "oversized tool result"},
                ]
            },
        },
        {"type": "rate_limit_event", "info": {}},
        _result_event("the answer survived"),
    )
    proc = _streaming_proc(data, raise_on=(1,))
    proc.pid = 424210  # explicit + distinct; never a mock default

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        output = await invoker.run_streaming(CCInvocation(prompt="x"))

    assert 1 in proc.stdout.reads, proc.stdout.reads  # the drop really happened
    assert output.text == "the answer survived"


@pytest.mark.asyncio
async def test_a_surviving_answer_still_reports_the_lines_it_lost(invoker, monkeypatch):
    """The drop has to be visible OUTSIDE `run_streaming`.

    This is the return path that matters most, because it is the one that looks
    completely healthy: an oversized tool trace is dropped, the real answer
    arrives, and the output is handed back as an ordinary success. Any consumer
    that derives an inventory from the events it saw — `direct_session`'s tool
    telemetry, and through it the protected-path auditor's pre-filter — then
    treats a floor as a complete list.

    Asserted against the REAL reader on purpose. The direct-session test for the
    same mechanism fakes `run_streaming` and constructs its own `CCOutput`, so
    it cannot see whether the invoker stamps anything — a mutation sweep caught
    exactly that, with the stamp deleted and that test still green.
    """
    _no_host_syscalls(monkeypatch)
    data = _make_stream_lines(
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "oversized tool result"},
                ]
            },
        },
        _result_event("the answer survived"),
    )
    proc = _streaming_proc(data, raise_on=(1,))
    proc.pid = 424301  # explicit + distinct; never a mock default

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        output = await invoker.run_streaming(CCInvocation(prompt="x"))

    assert 1 in proc.stdout.reads, proc.stdout.reads  # the drop really happened
    assert output.text == "the answer survived"
    assert output.stream_lines_dropped == 1, "a run that lost a line reported itself as a clean one"


@pytest.mark.asyncio
async def test_a_clean_run_reports_no_dropped_lines(invoker, monkeypatch):
    """The other direction of the stamp: an intact stream reports zero, or every
    ego dispatch would skip the cheap audit pre-filter forever.

    Stated precisely, because the obvious mutation for it does nothing. A clean
    run SKIPS the guarded stamp entirely, and `replace(..., 0)` is a no-op
    anyway, so no edit inside that branch can be felt here. What this actually
    pins is the field DEFAULT meaning "nothing was dropped" — changing
    `CCOutput.stream_lines_dropped`'s default is what turns it RED, and that is
    the mutation it was verified against.
    """
    _no_host_syscalls(monkeypatch)
    data = _make_stream_lines(
        {"type": "system", "subtype": "init", "session_id": "s1"},
        _result_event("clean"),
    )
    proc = _streaming_proc(data)  # nothing dropped
    proc.pid = 424302  # explicit + distinct; never a mock default

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        output = await invoker.run_streaming(CCInvocation(prompt="x"))

    assert output.stream_lines_dropped == 0


class _TailAfterOverrunStdout:
    """The half of ``readline()``'s overrun behaviour the shared fake omits.

    MEASURED against CPython 3.12 ``StreamReader.readline`` (``asyncio/streams.py``,
    the ``LimitOverrunError`` arm): it deletes through the separator only when the
    separator is ALREADY BUFFERED. When the newline has not arrived yet it merely
    clears the buffer, so the REMAINDER of that physical line is returned by the
    NEXT call. A 64-byte-limit probe returned the tail verbatim as the following
    "line". ``_make_async_stdout`` models only the favourable case — which is
    exactly why the raw-tail path went unnoticed.
    """

    def __init__(self, tail: bytes, rest: bytes):
        self._steps: list = [
            ValueError("Separator is not found"),
            tail,
            *rest.splitlines(keepends=True),
        ]
        self._i = 0
        self.reads: list[int] = []

    async def readline(self) -> bytes:
        if self._i >= len(self._steps):
            return b""
        step = self._steps[self._i]
        self.reads.append(self._i)
        self._i += 1
        if isinstance(step, BaseException):
            raise step
        return step


@pytest.mark.asyncio
async def test_the_tail_of_a_dropped_line_never_reaches_the_log_verbatim(
    invoker, monkeypatch, caplog
):
    """A dropped over-limit line does not vanish — its TAIL comes back.

    `readline()` clears the buffer without consuming the rest of the physical
    line, so the next call hands back raw tool-result bytes that fail JSON
    parsing and used to be logged 200 characters at a time. Tool output can
    carry a credential or personal data, and this repo's logs feed health
    snapshots and LLM prompts elsewhere, so the bytes must not go to the log.
    The size still does: it is what shows the stream resynchronising.
    """
    _no_host_syscalls(monkeypatch)
    canary = "tok-live-canary-value"
    proc = _streaming_proc(b"")
    proc.stdout = _TailAfterOverrunStdout(
        tail=f'nput":"{canary}"}}}}\n'.encode(),
        rest=_make_stream_lines(_result_event("the real answer")),
    )
    proc.pid = 424211  # explicit + distinct; never a mock default

    with (
        caplog.at_level("WARNING", logger="genesis.cc.invoker"),
        patch("asyncio.create_subprocess_exec", return_value=proc),
    ):
        output = await invoker.run_streaming(CCInvocation(prompt="x"))

    assert output.text == "the real answer"  # the run still completes
    messages = [r.getMessage() for r in caplog.records]
    assert not any(canary in m for m in messages), (
        f"raw tool bytes reached the log: {[m for m in messages if canary in m]}"
    )
    assert any("content withheld" in m for m in messages), (
        "the resync was silent — nothing says the stream dropped and recovered"
    )


@pytest.mark.asyncio
async def test_a_clean_stream_still_logs_a_non_json_line_verbatim(invoker, monkeypatch, caplog):
    """CLAUSE COVER for `oversized_dropped` at the non-JSON log.

    With nothing dropped, a non-JSON line is a CLI protocol fault and its text
    is the whole diagnostic. Withholding it unconditionally would blind that.
    """
    _no_host_syscalls(monkeypatch)
    data = b"this-is-not-json-at-all\n" + _make_stream_lines(_result_event("ok"))
    proc = _streaming_proc(data)  # nothing dropped
    proc.pid = 424212  # explicit + distinct; never a mock default

    with (
        caplog.at_level("WARNING", logger="genesis.cc.invoker"),
        patch("asyncio.create_subprocess_exec", return_value=proc),
    ):
        output = await invoker.run_streaming(CCInvocation(prompt="x"))

    assert output.text == "ok"
    assert any("this-is-not-json-at-all" in r.getMessage() for r in caplog.records), (
        "a protocol fault on a clean stream lost its only diagnostic"
    )


@pytest.mark.asyncio
async def test_blank_line_mid_stream_is_not_treated_as_eof(invoker):
    """readline() returns b"" ONLY at EOF; a blank line comes back as b"\\n".

    A reader that treats any falsy line as EOF truncates the stream at the
    first blank line and silently loses the result — which is worse than the
    crash being fixed, because it looks like a clean empty run.
    """
    data = (
        _make_stream_lines({"type": "system", "subtype": "init", "session_id": "s1"})
        + b"\n"
        + _make_stream_lines(_result_event("after blank"))
    )
    proc = _streaming_proc(data)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        output = await invoker.run_streaming(CCInvocation(prompt="x"))

    assert output.text == "after blank"


@pytest.mark.asyncio
async def test_a_multi_block_assistant_line_warns_exactly_once(invoker, caplog):
    """Pin the assumption the stream loop relies on, instead of coding around a
    condition that does not occur.

    MEASURED 2026-09-04 against the real surface (`claude -p --output-format
    stream-json --verbose`, two probes): 8/8 `assistant` lines carried exactly
    ONE content block, 0 multi-block — including a thinking→text→tool_use turn
    and three PARALLEL tool calls, which the API packs into a single message and
    the CLI splits across three lines. `StreamEvent.from_raw` keeps only the
    first recognized block, which is therefore lossless here.

    That is an external CLI's wire format, not a contract. If a future CC starts
    batching, this fails LOUDLY rather than silently dropping tool calls and
    answer text. Once per invocation, not once per line — the same flood shape
    fixed on the peer-availability read path.
    """
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "hmm"},
                    {"type": "tool_use", "name": "Read", "input": {}},
                ]
            },
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "hi"},
                    {"type": "tool_use", "name": "Bash", "input": {}},
                ]
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "done",
            "session_id": "s9",
            "total_cost_usd": 0.01,
            "duration_ms": 100,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "modelUsage": {},
        },
    ]
    mock_proc = AsyncMock()
    mock_proc.stdout = _make_async_stdout(_make_stream_lines(*events))
    mock_proc.stdin = _make_mock_stdin()
    mock_proc.stderr = _make_mock_stderr()
    mock_proc.wait = AsyncMock()
    mock_proc.terminate = MagicMock()
    mock_proc.returncode = 0

    with (
        caplog.at_level("WARNING", logger="genesis.cc.invoker"),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
    ):
        await invoker.run_streaming(CCInvocation(prompt="x"))

    hits = [r for r in caplog.records if "content blocks" in r.getMessage()]
    assert len(hits) == 1, f"two multi-block lines, {len(hits)} warnings (want 1)"


@pytest.mark.asyncio
async def test_the_canary_does_not_fire_on_an_unrecognized_block(invoker, caplog):
    """A canary that cries wolf trains its reader to ignore it.

    `from_raw` returns on the first RECOGNIZED block, so a line pairing an
    unrecognized block (`redacted_thinking`, or any future type) with one
    recognized block loses nothing. Counting raw list length would fire here and
    devalue every real firing.
    """
    events = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "redacted_thinking", "data": "x"},
                    {"type": "text", "text": "hi"},
                ]
            },
        },
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "done",
            "session_id": "s10",
            "total_cost_usd": 0.01,
            "duration_ms": 100,
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "modelUsage": {},
        },
    ]
    mock_proc = AsyncMock()
    mock_proc.stdout = _make_async_stdout(_make_stream_lines(*events))
    mock_proc.stdin = _make_mock_stdin()
    mock_proc.stderr = _make_mock_stderr()
    mock_proc.wait = AsyncMock()
    mock_proc.terminate = MagicMock()
    mock_proc.returncode = 0

    with (
        caplog.at_level("WARNING", logger="genesis.cc.invoker"),
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
    ):
        output = await invoker.run_streaming(CCInvocation(prompt="x"))

    assert output.text == "done"
    hits = [r for r in caplog.records if "content blocks" in r.getMessage()]
    assert not hits, "canary fired on a line from_raw parses losslessly"


@pytest.mark.asyncio
async def test_a_drop_then_a_timeout_is_still_unreplayable(invoker, monkeypatch):
    """The THIRD retryable exit, and the one a wrong comment hid.

    A failover peer can run its tools, drop an oversized line, and only then hit
    `timeout_s`. `CCTimeoutError` looks safe because `_try_invoke` and
    `_try_invoke_streaming` both re-raise it — but `_run_failover_peer`
    (`conversation.py:1114-1119`) does NOT carry it, so it lands on
    `_try_roster_failover`'s generic `except CCError` and the loop advances to
    the next peer, replaying the prompt with full tools (Codex P1, PR #1625
    round 5). Before drop-and-continue the over-limit ValueError escaped every
    retry path, so this combination is newly reachable.

    A drop outranks the timeout: the only question the TYPE answers is "may I
    re-run this?", and after a drop the answer is no regardless of what else
    went wrong.
    """
    _no_host_syscalls(monkeypatch)
    data = _make_stream_lines(
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "oversized tool result"},
                ]
            },
        },
    )

    class _DropThenHang:
        """Drop line 1, then never return — the reader hits its timeout."""

        def __init__(self, payload: bytes):
            self._lines = payload.splitlines(keepends=True)
            self._i = 0
            self.reads: list[int] = []

        async def readline(self) -> bytes:
            idx = self._i
            self.reads.append(idx)
            self._i += 1
            if idx == 1:
                raise ValueError("Separator is not found, and chunk exceed the limit")
            if idx < len(self._lines):
                return self._lines[idx]
            await asyncio.Event().wait()  # hang until the timeout fires
            return b""

    proc = _streaming_proc(b"")
    proc.stdout = _DropThenHang(data)
    proc.pid = 424303  # explicit + distinct; never a mock default

    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        pytest.raises(CCStreamTruncatedError, match="timed out"),
    ):
        await invoker.run_streaming(CCInvocation(prompt="x", timeout_s=1))

    assert 1 in proc.stdout.reads, proc.stdout.reads  # the drop really happened


@pytest.mark.asyncio
async def test_a_timeout_without_a_drop_is_still_a_timeout(invoker, monkeypatch):
    """CLAUSE COVER: an ordinary timeout must keep its own type, or the
    timeout-specific handling in every caller stops matching."""
    _no_host_syscalls(monkeypatch)

    class _Hang:
        def __init__(self):
            self.reads: list[int] = []

        async def readline(self) -> bytes:
            self.reads.append(0)
            await asyncio.Event().wait()
            return b""

    proc = _streaming_proc(b"")
    proc.stdout = _Hang()
    proc.pid = 424304  # explicit + distinct; never a mock default

    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        pytest.raises(CCTimeoutError),
    ):
        await invoker.run_streaming(CCInvocation(prompt="x", timeout_s=1))


def _proc_with_one_huge_line(data: bytes, *, overruns: int, at: int):
    """A stdout whose line `at` is a SINGLE physical line far over the limit.

    The existing `_make_async_stdout` models one raise per line, which is the
    shape of a line slightly over the limit. A line MUCH larger behaves
    differently and that difference is the defect under test: CPython's
    `StreamReader.readline()` raises once per buffer fill, so one
    20,000,000-byte line against a 1 MiB limit raised 18 times before its
    newline arrived (MEASURED, Python 3.12). Only the last of those reads
    returns anything — the unusable tail.
    """

    class _HugeLineStdout:
        def __init__(self, payload: bytes):
            self._lines = payload.splitlines(keepends=True)
            self._i = 0
            self._left = overruns
            self.reads: list[int] = []
            self.raises = 0

        async def readline(self) -> bytes:
            if self._i >= len(self._lines):
                return b""
            if self._i == at and self._left > 0:
                # Same physical line, another buffer fill. The index is NOT
                # advanced: nothing has been consumed to a newline yet.
                self._left -= 1
                self.raises += 1
                raise ValueError("Separator is not found, and chunk exceed the limit")
            idx = self._i
            self._i += 1
            self.reads.append(idx)
            return self._lines[idx]

    proc = AsyncMock()
    proc.stdout = _HugeLineStdout(data)
    proc.stdin = _make_mock_stdin()
    proc.stderr = _make_mock_stderr()
    proc.wait = AsyncMock()
    proc.terminate = MagicMock()
    proc.returncode = 0
    return proc


@pytest.mark.asyncio
async def test_one_huge_line_counts_as_one_dropped_line_not_many(invoker):
    """`stream_lines_dropped` must mean LINES, because that is what every
    consumer reads it as.

    It reaches the caller as an event count, drives the MCP projections, and
    is printed in operator diagnostics — so reporting 18 for a single missing
    event is a false fact in all three places, and it inflates precisely when
    the line is largest and the loss is most confusing (Codex P2, PR #1625).
    """
    data = _make_stream_lines(
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}},
        _result_event("survived"),
    )
    proc = _proc_with_one_huge_line(data, overruns=18, at=1)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        output = await invoker.run_streaming(CCInvocation(prompt="x"))

    assert proc.stdout.raises == 18, (
        f"the fake did not reproduce repeated overruns ({proc.stdout.raises})"
    )
    assert output.text == "survived", "the stream did not recover"
    assert output.stream_lines_dropped == 1, (
        "one oversized physical line was reported as "
        f"{output.stream_lines_dropped} dropped lines — the counter is "
        "measuring buffer overruns, not lost events"
    )


@pytest.mark.asyncio
async def test_two_separate_huge_lines_still_count_as_two(invoker):
    """CONTROL. Collapsing every overrun into a single count would satisfy the
    test above while under-reporting genuinely distinct losses — the opposite
    error, and the one that hides missing events instead of inventing them."""
    data = _make_stream_lines(
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "a"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "b"}]}},
        _result_event("survived"),
    )

    class _TwoHuge:
        def __init__(self, payload: bytes):
            self._lines = payload.splitlines(keepends=True)
            self._i = 0
            self._left = {1: 5, 2: 7}
            self.reads: list[int] = []

        async def readline(self) -> bytes:
            if self._i >= len(self._lines):
                return b""
            if self._left.get(self._i, 0) > 0:
                self._left[self._i] -= 1
                raise ValueError("Separator is not found, and chunk exceed the limit")
            idx = self._i
            self._i += 1
            self.reads.append(idx)
            return self._lines[idx]

    proc = AsyncMock()
    proc.stdout = _TwoHuge(data)
    proc.stdin = _make_mock_stdin()
    proc.stderr = _make_mock_stderr()
    proc.wait = AsyncMock()
    proc.terminate = MagicMock()
    proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        output = await invoker.run_streaming(CCInvocation(prompt="x"))

    assert output.stream_lines_dropped == 2, (
        "two distinct oversized lines (5 and 7 overruns) were reported as "
        f"{output.stream_lines_dropped} — distinct losses are being merged"
    )


@pytest.mark.asyncio
async def test_a_dropped_event_withholds_the_tool_inventory_from_triage(invoker):
    """A partial inventory must not be presented to learning as a complete one.

    `tools_used` has three meanings downstream: None is "no runtime report",
    () is "the runtime watched and saw zero tools", and a populated tuple is
    an authoritative list. Triage keys `tool_calls_from_runtime` on
    non-None-ness (`learning/triage/summarizer.py:203`), so a tuple built from
    a stream that DROPPED an event asserts something the runtime does not
    know — and when the dropped event was the only tool request, graders are
    told no tools ran. That false fact reaches prefiltering and permanent
    learning (Codex P2, PR #1625).

    None is the honest value and already carries this meaning, so triage falls
    back to extracting from the text exactly as it does for a non-streaming
    turn.
    """
    data = _make_stream_lines(
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}},
        _result_event("survived"),
    )
    proc = _streaming_proc(data, raise_on=(1,))

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        output = await invoker.run_streaming(CCInvocation(prompt="x"))

    assert output.stream_lines_dropped == 1, "the drop path was not exercised"
    assert output.tools_used is None, (
        "a stream with a dropped event still reported an authoritative tool "
        f"inventory ({output.tools_used!r}) — triage will read it as a "
        "complete runtime report"
    )


@pytest.mark.asyncio
async def test_a_clean_stream_still_reports_an_empty_inventory(invoker):
    """CONTROL, and the distinction this file already protects: on a clean
    tool-free turn `tools_used` must stay `()`, not `None`. Collapsing them
    would satisfy the test above while making "Tools used: none" unsayable on
    every streaming turn — the exact downgrade the surrounding comment warns
    against."""
    data = _make_stream_lines(
        {"type": "system", "subtype": "init", "session_id": "s1"},
        _result_event("clean"),
    )
    proc = _streaming_proc(data)

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        output = await invoker.run_streaming(CCInvocation(prompt="x"))

    assert output.stream_lines_dropped == 0
    assert output.tools_used == (), (
        f"a clean tool-free stream lost its runtime report ({output.tools_used!r})"
    )


# --- Per-binary hardening: gh cannot be told to spawn a shell ---------------


def _seal(monkeypatch, tmp_path, *, hosts: str | None = "github.com:\n  oauth_token: x\n"):
    """Point the sealer at a synthetic source and target, and run it.

    ``hosts`` writes a credential into the SOURCE directory, and since 2026-09-25
    the sealer does not read it — which is exactly why the parameter stays: the
    invariant worth testing is that a readable source credential does NOT reach
    the seal, and a fixture with no credential to leak could not test it.
    ``hosts=None`` is the never-authenticated arm. The source ``config.yml``
    carries a shell alias for the same reason: config.yml IS synthesised, so the
    alias must not appear either.
    """
    import genesis.cc.invoker as inv_mod

    source = tmp_path / "src-gh"
    source.mkdir()
    if hosts is not None:
        (source / "hosts.yml").write_text(hosts, encoding="utf-8")
    (source / "config.yml").write_text("aliases:\n  x: !whoami\n", encoding="utf-8")
    monkeypatch.setenv("GH_CONFIG_DIR", str(source))
    monkeypatch.setattr(inv_mod, "_SEALED_GH_CONFIG_DIR", tmp_path / "sealed")
    return inv_mod._sealed_gh_config_dir()


def test_sealed_gh_config_is_unwritable_by_the_session(monkeypatch, tmp_path):
    """gh writes its aliases, pager and editor into config.yml.

    Each of those runs a command of the caller's choosing, reached with `gh` as
    every token — so a first-token allowlist permits both the command that
    installs the escape and the command that triggers it. Making the file
    unwritable is what closes it; the modes ARE the mechanism, so assert them.
    """
    sealed = Path(_seal(monkeypatch, tmp_path))
    assert sealed.stat().st_mode & 0o777 == 0o500, "session could create new files here"
    # config.yml is the whole seal now — the credential copy was removed, so the
    # loop this used to run over ("config.yml", "hosts.yml") would raise
    # FileNotFoundError rather than fail an assertion.
    # Assert the expected SET first: iterating whatever happens to be present
    # would pass VACUOUSLY on an empty seal, which is the state this seal exists
    # to prevent.
    assert sorted(f.name for f in sealed.iterdir()) == ["config.yml"]
    for name in ("config.yml",):
        mode = (sealed / name).stat().st_mode & 0o777
        assert mode == 0o400, f"{name} is writable ({oct(mode)}) — gh could rewrite it"


def test_sealed_gh_config_carries_no_aliases_and_a_shell_free_pager(monkeypatch, tmp_path):
    """config.yml is SYNTHESISED, not copied.

    Copying would carry the operator's own aliases into the confined session,
    which is the escape arriving by inheritance rather than by being installed.
    The fixture's source config deliberately contains a shell alias.
    """
    sealed = Path(_seal(monkeypatch, tmp_path))
    body = (sealed / "config.yml").read_text()
    assert "aliases: {}" in body
    assert "pager: cat" in body
    assert "whoami" not in body, "the operator's own aliases leaked into the seal"


def test_the_seal_never_carries_the_credential_even_with_a_readable_source(
    monkeypatch, tmp_path
):
    """THE INVARIANT THIS PR EXISTS FOR — asserted with a READABLE source.

    Until 2026-09-25 the seal copied `hosts.yml`, handing every gh-allowlisted
    dispatch the operator OAuth token (MEASURED scopes: delete_repo, gist,
    read:org, repo, workflow). An absent-source test cannot catch a regression
    here, because the copy is conditional on the source existing — so the
    fixture deliberately WRITES a source credential and this asserts it does not
    arrive.

    The seal is a closed set, not a denylist of one name: assert the exact
    contents, so a future key added to `desired` has to be justified here.
    """
    sealed = Path(_seal(monkeypatch, tmp_path))
    assert not (sealed / "hosts.yml").exists(), (
        "the operator credential was copied into the seal — a dispatched "
        "session can authenticate as the operator"
    )
    assert sorted(f.name for f in sealed.iterdir()) == ["config.yml"]
    body = (sealed / "config.yml").read_text(encoding="utf-8")
    assert "oauth_token" not in body, "the credential arrived inside config.yml"


def test_an_existing_seal_holding_a_credential_is_healed_on_the_next_launch(
    monkeypatch, tmp_path
):
    """The arm operators actually depend on: installs that ALREADY copied it.

    This arm covers the SWEEP: `hosts.yml` is no longer in `desired`, so
    `_seal_matches` reports a mismatch and the stale sweep unlinks it. VERIFIED
    on the live install: the real seal went from ['config.yml', 'hosts.yml'] to
    ['config.yml'] on the first call.

    It is NOT the migration, and calling it one was the error this docstring
    used to carry. The sweep runs only when an allowlisted dispatch reaches
    `_sealed_gh_config_dir`, which has never happened here — so the path that
    actually heals an existing install is `reconcile_gh_seal()` at startup,
    covered by `test_reconcile_gh_seal_strips_a_credential_from_an_existing_seal`.

    `_seal_matches` has a fast path, which is exactly what could skip the
    repair — so the pre-existing seal here is built at the CORRECT modes, the
    shape that fast path accepts.
    """
    import genesis.cc.invoker as inv_mod

    target = tmp_path / "sealed"
    target.mkdir()
    for name, body in (
        ("config.yml", inv_mod._SEALED_GH_CONFIG_YML),
        ("hosts.yml", "github.com:\n  oauth_token: LEAKED\n"),
    ):
        (target / name).write_text(body, encoding="utf-8")
        (target / name).chmod(0o400)
    target.chmod(0o500)

    source = tmp_path / "src-gh"
    source.mkdir()
    (source / "hosts.yml").write_text("github.com:\n  oauth_token: x\n", encoding="utf-8")
    monkeypatch.setenv("GH_CONFIG_DIR", str(source))
    monkeypatch.setattr(inv_mod, "_SEALED_GH_CONFIG_DIR", target)

    assert inv_mod._sealed_gh_config_dir() == str(target)
    assert not (target / "hosts.yml").exists(), (
        "an existing seal kept its copied credential — the stale sweep did not "
        "reach it, so every install that ran the old code stays exposed"
    )
    assert target.stat().st_mode & 0o777 == 0o500, "the repair left the seal writable"


def test_the_seal_is_byte_identical_whatever_the_operators_own_config_holds(
    monkeypatch, tmp_path
):
    """Source-INDEPENDENCE, which is the property left once the copy is gone.

    This used to read "no credential is not a reason to leave the alias route
    open" and drive `hosts=None`. That assertion went inert with the copy: the
    sealer no longer reads the source at all, so `hosts=None` and a readable
    credential take the same code path, and the test duplicated
    `test_the_seal_never_carries_the_credential_even_with_a_readable_source`
    while appearing to cover a second case. Two precedence tests were deleted
    this round for exactly that — a test kept alive against a rewritten subject
    asserts whatever the rewrite happens to do — so this one is repointed rather
    than left standing.

    What is worth asserting now is the pair, in both directions: the seal is
    SYNTHESISED, so the bytes must not vary with what the operator's own gh
    config contains — including whether it exists. That is the claim
    "synthesised end to end" actually makes.
    """
    # Separate roots, created first: `_seal` mkdirs its source non-recursively.
    with_creds = tmp_path / "with-creds"
    without_creds = tmp_path / "without-creds"
    with_creds.mkdir()
    without_creds.mkdir()

    authenticated = Path(_seal(monkeypatch, with_creds))
    never = Path(_seal(monkeypatch, without_creds, hosts=None))

    assert (authenticated / "config.yml").read_bytes() == (
        never / "config.yml"
    ).read_bytes(), "the seal's contents vary with the operator's own config"
    for sealed in (authenticated, never):
        assert sorted(p.name for p in sealed.iterdir()) == ["config.yml"]
        assert (sealed / "config.yml").stat().st_mode & 0o777 == 0o400
        assert sealed.stat().st_mode & 0o777 == 0o500


def test_sealed_gh_config_is_idempotent_and_self_healing(monkeypatch, tmp_path):
    """Re-runs must not churn, and a tampered seal must be repaired."""
    import genesis.cc.invoker as inv_mod

    sealed = Path(_seal(monkeypatch, tmp_path))
    assert inv_mod._sealed_gh_config_dir() == str(sealed)

    sealed.chmod(0o700)
    (sealed / "config.yml").chmod(0o600)
    (sealed / "config.yml").write_text("aliases:\n  evil: !whoami\n", encoding="utf-8")
    repaired = Path(inv_mod._sealed_gh_config_dir())
    assert "evil" not in (repaired / "config.yml").read_text()
    assert (repaired / "config.yml").stat().st_mode & 0o777 == 0o400


def test_build_env_hardens_gh_only_for_an_allowlisted_session(invoker, monkeypatch, tmp_path):
    """The hardening rides the allowlist, not the binary being present.

    An ordinary dispatch must keep the operator's own gh config — repointing
    GH_CONFIG_DIR for every session would change unrelated behaviour.
    """
    _seal(monkeypatch, tmp_path)

    scoped = invoker._build_env(CCInvocation(prompt="hi", bash_allowlist=("gh",)))
    assert scoped["GH_CONFIG_DIR"].endswith("sealed")
    assert scoped["GH_PAGER"] == "cat"

    unscoped = invoker._build_env(CCInvocation(prompt="hi"))
    assert unscoped.get("GH_CONFIG_DIR") != scoped["GH_CONFIG_DIR"]
    assert "GH_PAGER" not in unscoped or unscoped["GH_PAGER"] != "cat"


def test_build_env_does_not_harden_gh_for_an_unrelated_allowlist(invoker, monkeypatch, tmp_path):
    """A profile allowlisting something else gets no gh hardening.

    Uses `jq`, not `git`. This test named `git` until 2026-09-26, when the
    hardening lookup went three-state: `git` is now REFUSED as unreviewed, because
    `git -c core.pager=…`, `-c alias.x='!sh'` and `core.sshCommand` each make it
    run a program of its own accord, exactly like gh. The property under test here
    is "an unrelated binary gets no GH pins", which needs a binary that is
    classified as needing none — `jq`. That `git` refuses is asserted separately,
    in the three-state test, because it is a different claim.
    """
    _seal(monkeypatch, tmp_path)
    env = invoker._build_env(CCInvocation(prompt="hi", bash_allowlist=("jq",)))
    assert env["GENESIS_BASH_ALLOWLIST"] == "jq"
    assert not env["GH_CONFIG_DIR"].endswith("sealed")


# --- The verification must stay wired to every spawn path -------------------


def test_every_spawn_path_awaits_the_allowlist_verification():
    """Structural lock on the async split.

    `_build_args` keeps only the cheap checks now; the settings read, the seal
    and the two subprocess probes moved behind `verify_allowlist_enforceable`
    so they do not stall the event loop. That split is only safe while EVERY
    path that calls `_build_args` also awaits the verification — a path that
    skipped it would launch a profile whose confinement was never demonstrated,
    which is the defect this whole change exists to remove.

    Asserted by AST over the module rather than by reading, so a spawn path
    added later fails here instead of launching unverified. Allowlist polarity:
    the test enumerates the callers and requires the await in each, so a NEW
    caller is a failure by construction rather than an omission nobody sees.
    """
    import ast
    import inspect

    from genesis.cc import invoker as inv_mod

    tree = ast.parse(inspect.getsource(inv_mod))

    def _awaits_verification(fn: ast.AST) -> bool:
        """An ast.Await node, not merely a MENTION of the name.

        The first version of this checked for the attribute in the dump, which
        a bare `self.verify_allowlist_enforceable(inv)` satisfies — the
        coroutine is then created and never run, so the verification silently
        does not happen and the launch proceeds unverified. MEASURED: deleting
        only the `await` keyword left that version GREEN. The sweep missed it
        too, because it deleted the whole statement rather than the keyword.
        """
        return any(
            isinstance(node, ast.Await)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "verify_allowlist_enforceable"
            for node in ast.walk(fn)
        )

    callers: dict[str, bool] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        if "attr='_build_args'" not in ast.dump(node):
            continue
        callers[node.name] = _awaits_verification(node)

    assert callers, "no caller of _build_args found — the AST probe is inert"
    missing = sorted(name for name, ok in callers.items() if not ok)
    assert not missing, (
        f"these call _build_args without awaiting verify_allowlist_enforceable: "
        f"{missing}. An allowlisted profile launched from there would never "
        f"have its confinement demonstrated."
    )


# --- Fail closed when a binary's own hardening is not in force --------------


async def test_refuses_to_launch_when_the_gh_seal_cannot_be_prepared(
    invoker, monkeypatch, tmp_path
):
    """A seal that could not be built is a REFUSAL, not a degraded mode.

    Without it the session falls back to the operator's own writable config —
    exactly where `gh alias set --shell` installs an escape, and where any
    alias the operator already has is already waiting. Pinning the pager alone
    would close one route of five and read as hardening.
    """
    import genesis.cc.invoker as inv_mod

    _arm(monkeypatch, tmp_path, ["bash", str(_REAL_GUARD)])
    monkeypatch.setattr(inv_mod, "_sealed_gh_config_dir", lambda **_k: None)
    with pytest.raises(RuntimeError, match="could not be prepared"):
        await _verify(invoker, CCInvocation(prompt="hi", bash_allowlist=("gh",)))


# DELETED 2026-09-25 with the credential copy: two tests pinned gh's SOURCE
# precedence (GH_CONFIG_DIR > $XDG_CONFIG_HOME/gh > ~/.config/gh). That chain
# existed only to locate `hosts.yml` and went with it — the seal is now
# synthesised from a constant and reads nothing from the operator's config, so
# there is no source to resolve and nothing left for those tests to assert. They
# are deleted rather than retargeted: a test kept alive against a rewritten
# subject asserts whatever the rewrite happens to do.
#
# The invariant that replaced them is
# `test_the_seal_never_carries_the_credential_even_with_a_readable_source`, which
# runs WITH a readable source credential present.


# --- The seal is a FLAT SET OF FILES, and a directory is never legitimate ---


def test_a_directory_planted_in_the_seal_is_neither_reported_clean_nor_kept(tmp_path, monkeypatch):
    """The extension closure rests on `<seal>/gh/extensions` not existing.

    `XDG_DATA_HOME` points at the seal, so a planted `gh/extensions/gh-x` is a
    live extension tree — the exact route the pin exists to close. Enumerating
    only `is_file()` was blind to it twice over: `_seal_matches` reported the
    contaminated seal CLEAN, and the stale sweep (also file-only) left it in
    place and then locked it in at 0500, permanently, across every reseal.

    MEASURED before the fix: planted extension survived a reseal and
    `_seal_matches` returned True. Both halves are asserted here because
    fixing either one alone still leaves the route open — a clean verdict with
    the tree present, or a correct verdict that never removes it.
    """
    import genesis.cc.invoker as inv_mod

    seal = tmp_path / "gh-sealed"
    monkeypatch.setattr(inv_mod, "_SEALED_GH_CONFIG_DIR", seal)
    assert inv_mod._sealed_gh_config_dir() == str(seal)

    seal.chmod(0o700)
    planted = seal / "gh" / "extensions" / "gh-evil"
    planted.mkdir(parents=True)
    (planted / "gh-evil").write_text("#!/bin/bash\necho pwned\n", encoding="utf-8")
    seal.chmod(0o500)

    # `desired` MUST match the seal's own file set exactly, or this test passes
    # for the wrong reason. It used to add `hosts.yml` whenever the DEVELOPER had
    # one at ~/.config/gh — and since the seal no longer carries that file, the
    # resulting key mismatch made `_seal_matches` return False all by itself. The
    # planted directory was then never the thing under test, and directory
    # detection could have broken silently. Caught in review of the change that
    # removed the copy; the file-set is now read FROM the seal.
    desired = {
        f.name: f.read_text(encoding="utf-8") for f in seal.iterdir() if f.is_file()
    }
    assert desired == {"config.yml": inv_mod._SEALED_GH_CONFIG_YML}, (
        f"the seal's file set is not what this test assumes ({sorted(desired)}); "
        f"the control below would not isolate the planted directory"
    )

    # POSITIVE CONTROL: with that exact file set and no directory, the seal
    # MATCHES. Without this arm, a `_seal_matches` that returned False
    # unconditionally would satisfy the assertion that follows.
    seal.chmod(0o700)
    import shutil

    shutil.rmtree(seal / "gh")
    seal.chmod(0o500)
    assert inv_mod._seal_matches(seal, desired) is True, (
        "the seal does not match its own contents — the arm below cannot then "
        "attribute a False verdict to the planted directory"
    )

    # Re-plant, and now a False verdict can only be about the directory.
    seal.chmod(0o700)
    planted = seal / "gh" / "extensions" / "gh-evil"
    planted.mkdir(parents=True)
    (planted / "gh-evil").write_text("#!/bin/bash\necho pwned\n", encoding="utf-8")
    seal.chmod(0o500)

    assert inv_mod._seal_matches(seal, desired) is False, (
        "a seal containing a directory was reported CLEAN — XDG_DATA_HOME "
        "points here, so that directory is a live gh extension tree."
    )
    assert inv_mod._sealed_gh_config_dir() == str(seal)
    assert not planted.exists(), "the stale sweep left the planted extension tree"
    assert not (seal / "gh").exists()
    assert seal.stat().st_mode & 0o777 == 0o500


# --- The env that was CHECKED must be the env that LAUNCHES ----------------


def test_the_launch_gate_refuses_an_env_that_lost_its_hardening(invoker, monkeypatch):
    """`_build_env` is not the last word, which is what this catches.

    Both spawn paths merge `_apply_login_fallback` on top of the built env and
    launch the merged result, so a check that ended at the builder inspected a
    dict that was then added to. This is the same class as the `env_overrides`
    hole, one call later, and it made the PR's own structural claim false.
    """
    import genesis.cc.invoker as inv_mod

    monkeypatch.setattr(inv_mod, "_sealed_gh_config_dir", lambda **_k: "/seal")
    inv = CCInvocation(prompt="hi", bash_allowlist=("gh",))
    env = invoker._build_env(inv)

    # survives untouched
    assert invoker._launch_env(dict(env), inv)["GH_CONFIG_DIR"] == "/seal"

    # a later merge strips the confinement — exactly what the fallback could do
    tampered = {**env, "GH_CONFIG_DIR": "/tmp/attacker-writable"}
    with pytest.raises(RuntimeError, match="not in the environment"):
        invoker._launch_env(tampered, inv)


def test_every_spawn_path_gates_the_env_it_actually_launches():
    """Structural lock: nothing may build an env, spawn, and skip the gate.

    KEYED ON THE SPAWN, not on a helper. An earlier version enumerated callers
    of `_apply_login_fallback` and required each to gate — which binds today only
    because both launch paths happen to use that helper. A method that called
    `_build_env` and `create_subprocess_exec` without it was invisible to the
    probe and passed, and that is not hypothetical in spirit: two launchers
    elsewhere in this codebase spawn a session on a hand-copied `os.environ`
    while carrying comments saying they mirror `_build_env`.

    This matters more than an ordinary structural test, because
    `_assert_no_gh_credentials`'s "runs on every dispatch" claim rests entirely
    on `_launch_env` being on every path that launches — and this is the only
    check that defends it structurally rather than by someone reading the file.

    Both arms are asserted non-empty, because an AST probe whose predicate
    matches nothing is the failure mode it is most likely to have: it reports
    green forever and nobody notices the rename that silenced it.
    """
    import ast
    import inspect

    from genesis.cc import invoker as inv_mod

    # Spelled broadly on purpose — a future path reaching for `Popen` or a
    # blocking `run` to launch a session must be caught too, not just asyncio.
    SPAWN = ("attr='create_subprocess_exec'", "attr='Popen'", "attr='run'")

    tree = ast.parse(inspect.getsource(inv_mod))
    launchers: dict[str, bool] = {}
    fallback_callers: dict[str, bool] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        dumped = ast.dump(node)

        # ARM 1 — builds the dispatch env AND spawns ⇒ must gate. A function
        # that builds the env WITHOUT spawning is a verifier, not a launcher
        # (`_verify_allowlist_enforceable_blocking` is one), and a probe that
        # spawns without building the dispatch env is not launching a session.
        if "attr='_build_env'" in dumped and any(s in dumped for s in SPAWN):
            launchers[node.name] = "attr='_launch_env'" in dumped

        # ARM 2 — the original regression, kept: `_apply_login_fallback` was
        # called AFTER the builder's assertion in both paths and returned a
        # merged dict, so the env that was checked was not the env launched.
        if "attr='_apply_login_fallback'" in dumped and node.name != "_apply_login_fallback":
            fallback_callers[node.name] = "attr='_launch_env'" in dumped

    assert launchers, (
        "no function both builds the dispatch env and spawns — the predicate "
        "matches nothing, so this test proves nothing. A rename in invoker.py "
        "is the likely cause."
    )
    assert fallback_callers, "no caller of _apply_login_fallback found — arm 2 is inert"

    ungated = sorted(name for name, ok in launchers.items() if not ok)
    assert not ungated, (
        f"these build an env and spawn without calling _launch_env: {ungated}. "
        f"The credential check would not run for sessions they launch, which is "
        f"exactly the 'unconditional' claim this PR makes."
    )
    unregated = sorted(name for name, ok in fallback_callers.items() if not ok)
    assert not unregated, (
        f"these mutate the env after _build_env without re-gating it: "
        f"{unregated}. The environment that was checked would not be the one "
        f"launched."
    )


# --- The seal answers, rather than raising, when it moves under us ----------


def test_a_seal_that_moves_under_the_check_reports_no_rather_than_raising(tmp_path):
    """A concurrent rewrite must not turn into a refused launch.

    The first `_seal_matches` call runs OUTSIDE the rewrite lock, so a writer
    unlinking a stale file between the listing and the read is expected rather
    than exceptional. If that escaped, it would reach the caller's fallback and
    refuse a launch that should have succeeded — the whole point of queueing
    behind the lock is to handle it, and that path is only reached by answering
    False.

    Fail-closed is preserved and asserted: the answer is never True. This is
    "cannot confirm", which is not the same as "does not match", and both are
    correctly handled by going on to take the lock.

    RETARGETED at `os.listdir(dir_fd)`. This used to stub `Path.iterdir`, which
    `_seal_matches` no longer calls — it enumerates through the pinned
    directory descriptor now, so the stub became inert and the test failed by
    reporting a clean seal rather than by finding a defect. The PROPERTY is
    unchanged and still worth pinning: a name that vanishes between the listing
    and the open raises `FileNotFoundError`, which is an `OSError`, so the
    function answers False instead of letting it escape.
    """
    import genesis.cc.invoker as inv_mod

    target = tmp_path / "seal"
    target.mkdir()
    (target / "config.yml").write_text("x", encoding="utf-8")
    (target / "config.yml").chmod(0o400)
    target.chmod(0o500)

    import os

    real_listdir = os.listdir

    def vanishing(fd):
        names = real_listdir(fd)
        # The concurrent writer, between the listing and the open.
        for name in names:
            os.chmod(name, 0o600, dir_fd=fd)
            os.unlink(name, dir_fd=fd)
        return names

    with patch.object(os, "listdir", vanishing):
        assert inv_mod._seal_matches(target, {"config.yml": "x"}) is False


# --- The confinement is checked where the env is BUILT ----------------------


def test_build_env_itself_refuses_when_a_binary_cannot_be_confined(invoker, monkeypatch):
    """The refusal lives in the builder, not only in the pre-launch verifier.

    This is the binding that matters, and the reason it is not merely a second
    copy of the verifier's test: BOTH spawn paths call `_build_env` AGAIN after
    `verify_allowlist_enforceable` has passed, and launch what that second call
    returns. A check that lives only in the verifier therefore inspects an
    environment that is then thrown away. Refusing inside the builder makes the
    environment that was checked the environment that runs, by construction
    rather than by the two staying in step.

    Drives `_build_env` DIRECTLY for that reason — going through the verifier
    would pass even with the builder failing open, which is the state this
    replaces.
    """
    import genesis.cc.invoker as inv_mod

    monkeypatch.setattr(inv_mod, "_sealed_gh_config_dir", lambda **_k: None)
    with pytest.raises(RuntimeError, match="could not be prepared"):
        invoker._build_env(CCInvocation(prompt="hi", bash_allowlist=("gh",)))


def test_build_env_refuses_when_an_override_strips_the_confinement(invoker):
    """`env_overrides` is applied last and wins — including over hardening.

    Asserted at the builder for the same reason as above: the override is
    applied on every build, so the check has to be on every build too.
    """
    with pytest.raises(RuntimeError, match="not in the environment"):
        invoker._build_env(
            CCInvocation(
                prompt="hi",
                bash_allowlist=("gh",),
                env_overrides={"XDG_DATA_HOME": "/tmp/attacker-writable"},
            )
        )


def test_an_unallowlisted_invocation_is_untouched_by_any_of_this(invoker):
    """No allowlist, no hardening, no refusal — the collateral-damage arm.

    Without it a refusal that fired on every dispatch would look exactly like
    a working confinement.
    """
    env = invoker._build_env(CCInvocation(prompt="hi"))
    assert "GENESIS_BASH_ALLOWLIST" not in env
    assert "XDG_DATA_HOME" not in env


# --- Every route gh documents for running a program of its own accord -------


def test_the_gh_confinement_pins_the_extension_data_dir(monkeypatch):
    """Extensions are NOT under the config dir, which is the whole trap.

    MEASURED: `gh` resolves extensions from `$XDG_DATA_HOME/gh/extensions`, so
    a sealed `GH_CONFIG_DIR` leaves `gh extension install` followed by
    `gh extension exec` as arbitrary execution with `gh` as both first tokens.
    An extension planted under the config dir was not found; one under the data
    dir ran. Pinning the data dir at the same read-only seal closes both halves
    — the install cannot create the directory, and the exec finds nothing.
    """
    import genesis.cc.invoker as inv_mod

    monkeypatch.setattr(inv_mod, "_sealed_gh_config_dir", lambda **_k: "/seal")
    hardened = inv_mod._gh_hardening()
    assert hardened is not None
    assert hardened["XDG_DATA_HOME"] == "/seal", (
        "the extension route is open: extensions resolve from the DATA dir, "
        "not from GH_CONFIG_DIR, so sealing the config dir alone does nothing "
        "about `gh extension`."
    )


def test_the_gh_confinement_pins_every_documented_program_route(monkeypatch):
    """Enumerated from `gh help environment`, not from review findings.

    Each of these names a program gh will run, or a directory gh will run a
    program out of. They are asserted as a SET so that dropping one fails here
    rather than in the next review round — the routes arrived one review at a
    time, which is the signature of a denylist, and the fix for that is to bind
    the whole documented set at once.

    `GH_PATH` is deliberately absent, and that absence is asserted below rather
    than left ambiguous: it was MEASURED inert — with a planted value an
    ordinary read still ran the real gh, and with extensions already unreachable
    it redirects nothing.
    """
    import genesis.cc.invoker as inv_mod

    monkeypatch.setattr(inv_mod, "_sealed_gh_config_dir", lambda **_k: "/seal")
    hardened = inv_mod._gh_hardening()
    assert hardened is not None
    assert hardened == {
        "GH_CONFIG_DIR": "/seal",
        "XDG_DATA_HOME": "/seal",
        "GH_PAGER": "cat",
        "PAGER": "cat",
        "GH_EDITOR": "true",
        "GIT_EDITOR": "true",
        "VISUAL": "true",
        "EDITOR": "true",
        "GH_BROWSER": "true",
        "BROWSER": "true",
    }
    assert "GH_PATH" not in hardened
    # CREDENTIALS ARE DELIBERATELY ABSENT from this dict, and their absence is
    # asserted rather than left implicit. They were briefly splatted in here, which
    # tied "no dispatched session holds a credential" — a claim about EVERY session
    # — to a check that only runs for an allowlisted one. They now live in
    # `_assert_no_gh_credentials`, called unconditionally.
    #
    # Asserting absence also closes a live hazard: the splat sat LAST in this
    # literal, so a name added to `_GH_CREDENTIAL_ENV` that collided with a key
    # above — `GH_CONFIG_DIR` being the obvious one — would have silently
    # overwritten the seal pin with `""` and passed every check, because
    # `_assert_hardening_present` recomputes the same wrong dict.
    assert not (set(inv_mod._GH_CREDENTIAL_ENV) & set(hardened)), (
        "a credential variable is back in the binary hardening — that scopes an "
        "every-session claim to allowlisted sessions only"
    )
    # And the constant must not have drifted BELOW what is asserted above — a
    # name removed from it would silently stop being pinned in `_build_env`,
    # which this dict cannot see.
    assert set(inv_mod._GH_CREDENTIAL_ENV) == {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
    }, (
        "the credential-variable enumeration changed. It comes from "
        "`gh help environment`; if gh added or removed one, update this list and "
        "cite the release — the first version of this pinned GH_TOKEN alone and "
        "was bypassed by GITHUB_TOKEN, documented on the same line."
    )


def test_the_confinement_reaches_the_env_a_dispatch_would_receive(invoker, monkeypatch):
    """End of the chain: the pins are in the dict the spawn paths launch.

    The unit above proves `_gh_hardening` returns them. This proves they
    survive everything `_build_env` does afterwards, which is where a later
    edit would quietly drop them.
    """
    import genesis.cc.invoker as inv_mod

    monkeypatch.setattr(inv_mod, "_sealed_gh_config_dir", lambda **_k: "/seal")
    env = invoker._build_env(CCInvocation(prompt="hi", bash_allowlist=("gh",)))
    assert env["XDG_DATA_HOME"] == "/seal"
    assert env["GH_CONFIG_DIR"] == "/seal"
    assert env["BROWSER"] == "true"
    assert env["EDITOR"] == "true"


# --- No dispatched session inherits a GitHub credential --------------------
#
# Two separate mechanisms, and they close different holes. The GH_TOKEN pin
# closes the INHERITED-ENV route for every dispatch. The GH_CONFIG_DIR pin
# closes the ON-DISK route, and only for external-untrusted origins. MEASURED
# 2026-09-25, which is why both exist: `GH_TOKEN="" gh auth status` with no
# GH_CONFIG_DIR pin is STILL FULLY AUTHENTICATED, because gh reads an empty
# GH_TOKEN as unset and falls back to hosts.yml. Anyone who deletes one of
# these two believing the other covers it is repeating that error.


def test_every_documented_credential_variable_is_pinned(invoker, monkeypatch):
    """THE BYPASS THAT SHIPPED, and the discipline that would have caught it.

    The first version pinned `GH_TOKEN` alone. `gh help environment` (gh 2.100.0)
    documents FOUR credential variables — "`GH_TOKEN`, `GITHUB_TOKEN` (in order of
    precedence)" and "`GH_ENTERPRISE_TOKEN`, `GITHUB_ENTERPRISE_TOKEN` (in order
    of precedence)" — and MEASURED, `GH_TOKEN="" GITHUB_TOKEN=<value> gh auth
    token` returns the fallback. So the pin was bypassed by the variable on the
    same documentation line.

    Each variable is its own arm, and each is INHERITED non-empty first, because
    the failure mode was a whole name being absent rather than a value being
    wrong. A loop over `_GH_CREDENTIAL_ENV` would pass for any contents of that
    tuple, including the broken one.
    """
    for var in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
    ):
        monkeypatch.setenv(var, f"ghp_INHERITED_{var}")
        for inv in (
            CCInvocation(prompt="hi"),
            CCInvocation(prompt="hi", bash_allowlist=("gh",)),
        ):
            env = invoker._build_env(inv)
            assert env[var] == "", (
                f"{var} survived into a dispatch with "
                f"bash_allowlist={inv.bash_allowlist!r}. gh resolves it as a "
                f"credential, so this outranks the credential-free seal."
            )


@pytest.mark.parametrize(
    "var",
    ["GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"],
)
def test_env_overrides_cannot_restore_a_credential_on_ANY_session(invoker, var):
    """THE HOLE THIS ROUND EXISTS FOR, and the arm the first version lacked.

    The enforcement used to live in `_gh_hardening`'s returned dict, checked by
    `_assert_hardening_present` — which `_launch_env` calls only
    `if inv.bash_allowlist:`. Exactly one shipped profile declares an allowlist on
    this branch (`steward`, for `gh`) and it has never been dispatched (MEASURED:
    0 rows), with a companion change removing it — so the pin was SET by the
    builder and ENFORCED only for a shape that has never occurred, and an
    `env_overrides` credential won silently on every session that has ever run.

    So this drives a session with NO allowlist, which is the case that was broken.
    The previous version of this test passed `bash_allowlist=("gh",)` and therefore
    could not have caught it.

    Parametrised per variable because the failure mode was a whole NAME being
    absent from the enumeration, not a value being wrong.
    """
    inv = CCInvocation(prompt="hi", env_overrides={var: "ghp_RESTORED"})
    env = invoker._build_env(inv)
    with pytest.raises(RuntimeError, match=var):
        invoker._launch_env(env, inv)


def test_the_credential_check_runs_on_a_session_with_no_allowlist(invoker):
    """The same property stated positively: the check is UNCONDITIONAL.

    Distinct from the parametrised test above, which proves a refusal. This proves
    the ordinary case still LAUNCHES — without it, a check that raised for every
    session would satisfy the refusals and break every background dispatch on the
    box.
    """
    import genesis.cc.invoker as inv_mod

    inv = CCInvocation(prompt="hi")
    env = invoker._build_env(inv)
    assert invoker._launch_env(env, inv) is env
    for var in inv_mod._GH_CREDENTIAL_ENV:
        assert env[var] == ""


def test_an_allowlist_entry_is_classified_into_one_of_THREE_states(invoker):
    """HARDENED / deliberately-needs-none / UNREVIEWED-and-refused.

    A bare `dict.get` + skip was a DENYLIST wearing allowlist grammar: it silently
    permitted every binary nobody had considered. `git` is the concrete case and
    the next entry anyone adds for PR work — `git -c core.pager=…`,
    `-c alias.x='!sh'`, `core.sshCommand`, a writable ~/.gitconfig — each makes it
    run a program of its own accord, exactly like gh. Skipping it handed out an
    unsealed escape with no warning.

    `basename` earns its place and `strip` does not, and both are asserted for
    what they actually do. MEASURED against the shipped guard predicate, varying
    the ALLOWLIST ENTRY: entry `/usr/bin/gh` with command `/usr/bin/gh …` is
    PERMITTED (rc=0) while a raw lookup misses — a real unsealed launch. Entry
    `"gh "` or `" gh"` is REFUSED for every command (rc=2), because the guard takes
    the first token with `awk '{print $1}'`, which can never yield a token
    containing whitespace — so `strip` closes no hole and is normalisation only.
    An earlier version of this test claimed both were permitted; only the path
    form is.
    """
    import genesis.cc.invoker as inv_mod

    # HARDENED — and `/usr/bin/gh` is the spelling that was launching unsealed.
    for token in ("gh", "/usr/bin/gh", "/opt/homebrew/bin/gh"):
        required = inv_mod._required_hardening(token)
        assert required and required.get("GH_CONFIG_DIR"), f"{token!r} lost its seal"
        # Resolution alone proves nothing; an UNSEALED env for that spelling must
        # also be refused, which is the property that matters.
        with pytest.raises(RuntimeError):
            inv_mod._assert_hardening_present({}, (token,))

    # DELIBERATELY NEEDS NONE — returns None, and still launches.
    assert inv_mod._required_hardening("jq") is None
    assert inv_mod._required_hardening("/usr/bin/jq") is None
    inv = CCInvocation(prompt="hi", bash_allowlist=("jq",))
    env = invoker._build_env(inv)
    assert invoker._launch_env(env, inv) is env

    # UNREVIEWED — refused, with the binary named so the reader knows what to
    # classify. `git` is called out in the message because it is the likely one.
    for token in ("git", "npm", "python3", "curl", "GH"):
        with pytest.raises(RuntimeError, match="neither _BINARY_HARDENING"):
            inv_mod._required_hardening(token)

    # And an unreviewed entry stops a LAUNCH, not just the classifier.
    inv = CCInvocation(prompt="hi", bash_allowlist=("git",))
    with pytest.raises(RuntimeError, match="neither _BINARY_HARDENING"):
        invoker._build_env(inv)


def test_reconcile_gh_seal_strips_a_credential_from_an_existing_seal(monkeypatch, tmp_path):
    """THE MIGRATION, and why it cannot live on a dispatch path.

    The seal's stale sweep runs only inside `_sealed_gh_config_dir`, whose single
    caller is `_gh_hardening` — reachable only for an invocation that declares a
    Bash allowlist. Once no shipped profile declares one, nothing calls it again,
    so an install that already ran the copying version keeps the operator's token
    in the seal indefinitely while the changelog claims installs "heal themselves
    with no migration step". This is what makes that claim true.

    Driven with a pre-heal seal built at the CORRECT modes — the shape
    `_seal_matches`'s fast path accepts — because that is the state an install is
    actually in, and a wrong-modes seal would be repaired for the wrong reason.
    """
    import genesis.cc.invoker as inv_mod

    target = tmp_path / "gh-sealed"
    target.mkdir()
    for name, body in (
        ("config.yml", inv_mod._SEALED_GH_CONFIG_YML),
        ("hosts.yml", "github.com:\n  oauth_token: LEAKED\n"),
    ):
        (target / name).write_text(body, encoding="utf-8")
        (target / name).chmod(0o400)
    target.chmod(0o500)
    monkeypatch.setattr(inv_mod, "_SEALED_GH_CONFIG_DIR", target)

    # CONTROL: the planted credential is really there and really readable before
    # the sweep, or a passing assertion below would prove nothing.
    assert (target / "hosts.yml").read_text(encoding="utf-8").endswith("LEAKED\n")

    inv_mod.reconcile_gh_seal()

    assert not (target / "hosts.yml").exists(), (
        "the sweep left the copied credential in place — an install that ran the "
        "old code keeps the operator's token forever"
    )
    assert (target / "config.yml").exists(), "the sweep removed the seal itself"
    assert target.stat().st_mode & 0o777 == 0o500, "the seal was left writable"


def test_reconcile_gh_seal_does_not_CREATE_a_seal(monkeypatch, tmp_path):
    """A no-op where no seal exists — asserted, because the opposite is worse.

    This runs at startup on EVERY install. Building a seal unprompted would put a
    directory on every box to solve a problem only some have, and would make the
    machinery look used when it is not.
    """
    import genesis.cc.invoker as inv_mod

    absent = tmp_path / "never-built"
    monkeypatch.setattr(inv_mod, "_SEALED_GH_CONFIG_DIR", absent)
    inv_mod.reconcile_gh_seal()
    assert not absent.exists(), "the startup sweep created a seal that was not there"


def test_the_credential_gate_refuses_an_ABSENT_variable_not_only_a_set_one():
    """An absent key is as unverified as a restored one.

    `env.get(v) != ""` already refused an absent key, which is the right
    polarity — but it reported it as "arrived non-empty", sending a reader after
    a value that is not there. This locks BOTH the polarity and the two
    distinguishable messages, because the remedies differ: a non-empty value is
    something that set it, an absent one is something that deleted it.

    Unreachable from the invoker today (`_build_env` sets all four, and nothing
    between it and the gate removes a key), so this is a lock on a direct caller
    and on a future refactor — which is exactly when it would otherwise be
    quietly relaxed to `.get(v, "")`.
    """
    import genesis.cc.invoker as inv_mod

    complete = {v: "" for v in inv_mod._GH_CREDENTIAL_ENV}
    # CONTROL: the complete, all-empty env is PERMITTED, or the assertions below
    # would pass for a gate that simply refuses everything.
    inv_mod._assert_no_gh_credentials(complete)

    for victim in inv_mod._GH_CREDENTIAL_ENV:
        short = {k: v for k, v in complete.items() if k != victim}
        with pytest.raises(RuntimeError, match="ABSENT") as absent:
            inv_mod._assert_no_gh_credentials(short)
        assert victim in str(absent.value)

        restored = dict(complete)
        restored[victim] = "ghp_value"
        with pytest.raises(RuntimeError, match="NON-EMPTY") as nonempty:
            inv_mod._assert_no_gh_credentials(restored)
        assert victim in str(nonempty.value)

    # Both classes at once, each named for what it is.
    both = {k: v for k, v in complete.items() if k != inv_mod._GH_CREDENTIAL_ENV[-1]}
    both[inv_mod._GH_CREDENTIAL_ENV[0]] = "ghp_value"
    with pytest.raises(RuntimeError) as exc:
        inv_mod._assert_no_gh_credentials(both)
    assert "NON-EMPTY" in str(exc.value) and "ABSENT" in str(exc.value)


@pytest.mark.timeout(30)
@pytest.mark.parametrize("planted", ["fifo", "socket"])
@pytest.mark.parametrize("entry_name", ["hosts.yml", "config.yml"])
def test_only_a_REGULAR_FILE_counts_as_a_seal_entry(
    monkeypatch, tmp_path, planted, entry_name
):
    """The fifth file type, and why the check is now an ALLOWLIST.

    `_seal_matches` filtered entries with `is_file()` after rejecting symlinks and
    directories — a DENYLIST of types, and every review round found the next one
    nobody had listed. A FIFO is not a symlink, not a directory, and `is_file()`
    is ALSO False for it, so it was silently dropped from `present`,
    `set(present)` still equalled `set(desired)`, and this function reported the
    seal CLEAN. MEASURED: a seal holding a correct `config.yml` plus
    `mkfifo hosts.yml` survived both the boot reconciliation and the dispatch
    rewrite with nothing logged, and gh then read the planted `hosts.yml` THROUGH
    the FIFO and adopted the account in it — the credential this change exists to
    remove, restored by a named pipe.

    So the polarity is inverted: an entry must be `S_ISREG`, and a FIFO, socket,
    device, directory or symlink is a non-match by construction. Parametrised over
    two types deliberately — a fix that special-cased FIFOs would be the sixth
    round of the same mistake, and the socket arm is the one that proves it did
    not.

    PARAMETRISED OVER THE NAME as well as the type, because the two names fail
    for DIFFERENT reasons and a sweep caught the first version testing only the
    weaker one. Under a name NOT in `desired` the set comparison rejects the seal
    whatever the type is — so that arm passes with the type check deleted, which is
    exactly what a mutation arm reported. Under `config.yml` — a name that IS in
    `desired` — the set matches and only the type check can say no.

    WHICH GATE REJECTS WHICH TYPE, measured rather than assumed, because an earlier
    version of this docstring claimed the socket arm was the one proving the type
    check and that was false — the MODE check was rejecting it first, making three
    of these four arms vacuous. Under the descriptor form
    (`open(O_RDONLY|O_NOFOLLOW|O_NONBLOCK)` then `fstat` then `S_ISREG`):

      * fifo      -> open SUCCEEDS, rejected by the S_ISREG gate
      * directory -> open SUCCEEDS, rejected by the S_ISREG gate
      * socket    -> rejected by the OPEN, errno 6 (ENXIO)
      * symlink   -> rejected by the OPEN, errno 40 (ELOOP)

    So `S_ISREG` is load-bearing for the FIFO and the directory; the socket and the
    symlink never reach it. All four are refusals and all four are worth pinning,
    but only the first two exercise the type test, and saying otherwise is how the
    vacuity got missed.

    `O_NONBLOCK` is what makes the FIFO arm safe to run at all. MEASURED:
    `Path.read_text()` on a FIFO with no writer BLOCKS INDEFINITELY, so a version
    that read before testing the type would wedge `_seal_matches` on every dispatch
    and, since the reconciliation calls it, every boot. The 30s timeout marker is
    here so such a regression fails this test rather than hanging the suite.

    Each arm asserts BOTH halves: the fast path says no, and the rewrite actually
    removes it. Reporting the mismatch without repairing it would leave the seal
    rewritten on every dispatch forever.
    """
    import os
    import socket as socket_mod

    import genesis.cc.invoker as inv_mod

    seal = tmp_path / "gh-sealed"
    seal.mkdir()
    if entry_name != "config.yml":
        # Under a name not in `desired` the seal also needs a VALID config.yml,
        # or it would be rejected for the boring reason.
        (seal / "config.yml").write_text(inv_mod._SEALED_GH_CONFIG_YML)
        (seal / "config.yml").chmod(0o400)
    if planted == "fifo":
        os.mkfifo(seal / entry_name, 0o400)
    else:
        sock = socket_mod.socket(socket_mod.AF_UNIX)
        sock.bind(str(seal / entry_name))
    seal.chmod(0o500)
    monkeypatch.setattr(inv_mod, "_SEALED_GH_CONFIG_DIR", seal)

    # CONTROL: the planted entry really is present and really is not a regular
    # file, or neither assertion below means anything.
    assert (seal / entry_name).exists()
    assert not (seal / entry_name).is_file()

    assert not inv_mod._seal_matches(seal, {"config.yml": inv_mod._SEALED_GH_CONFIG_YML}), (
        f"a {planted} named {entry_name} reported the seal CLEAN — under a name gh "
        f"reads, gh opens the planted entry and adopts what comes out of it"
    )

    inv_mod.reconcile_gh_seal()
    assert sorted(p.name for p in seal.iterdir()) == ["config.yml"], (
        f"the {planted} named {entry_name} survived the reconciliation"
    )
    assert (seal / "config.yml").is_file(), "the replacement is not a regular file"


@pytest.mark.timeout(30)
def test_a_fifo_that_already_holds_the_bytes_is_refused(
    monkeypatch, tmp_path
):
    """The arm that was retired on a false premise, restored with the right fixture.

WHAT THIS PINS, stated carefully because the claim around it was wrong four
    times running and this test was named after the fourth version of it.

    It pins the OUTCOME: a pipe holding exactly the expected bytes is refused. It
    does NOT pin which gate does the refusing, and it must not be read as doing so
    — in the shipped order the type test fires first, but deleting that line alone
    changes no outcome here, because the size gate refuses the same fixture (a pipe
    reports st_size 0, MEASURED). The two shadow each other, so the sweep arm for
    this fixture is a COMBINED mutation removing both; a single-gate arm is
    unpinnable by construction and reads as coverage if left in place.

    The history is worth keeping because it is the failure mode, not the bug. Round
    A: the type test is redundant, "a FIFO reads as b'' under O_NONBLOCK so the
    content compare rejects it anyway" — true of an EMPTY pipe, generalised without
    testing a full one. Round B: therefore it is the SOLE gate — wrong, see above.
    Round C: the read ceiling is belt-and-braces — backwards, it is the
    load-bearing half. Round D: this test's own control drained the pipe it was
    establishing.

    A pipe can hold data with no writer attached. Open a reader, write the seal
    constant, close the WRITE end: the bytes stay buffered in the pipe, and a
    descriptor read returns them. MEASURED: those exact 36 bytes come back, so
    `st_nlink` is 1, the mode is 0400 and the contents are EQUAL — every gate
    passes except the type test, which is therefore the only thing standing between
    a planted pipe and a seal reported CLEAN. And the commit already measured what
    happens then: gh reads the planted name through the pipe and adopts the account
    in it.

    The reader is held open for the lifetime of the assertion, because closing it
    would drain the pipe and quietly restore the empty case this test exists to
    stop being confused with.
    """
    import fcntl
    import os
    import stat
    import struct
    import termios

    import genesis.cc.invoker as inv_mod

    seal = tmp_path / "gh-sealed"
    seal.mkdir()
    fifo = seal / "config.yml"
    os.mkfifo(fifo, 0o600)

    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        writer = os.open(fifo, os.O_WRONLY)
        try:
            os.write(writer, inv_mod._SEALED_GH_CONFIG_YML.encode("utf-8"))
        finally:
            os.close(writer)
        os.chmod(fifo, 0o400)
        seal.chmod(0o500)
        monkeypatch.setattr(inv_mod, "_SEALED_GH_CONFIG_DIR", seal)

        # CONTROL: this really is a pipe, and it really does still hold the bytes.
        #
        # COUNT them, do not READ them. The first version of this control opened
        # the pipe and read it to prove the bytes were there — which CONSUMED
        # them, so by the time the assertion below ran the pipe was empty and the
        # function refused it for the empty-pipe reason. That is precisely the case
        # the original false claim was about, so the test passed while proving
        # nothing, and a mutation arm deleting BOTH gates still could not turn it
        # red. FIONREAD reports how many bytes are available without removing any.
        st = os.lstat(fifo)
        assert stat.S_ISFIFO(st.st_mode) and not stat.S_ISREG(st.st_mode)
        assert st.st_nlink == 1 and st.st_mode & 0o777 == 0o400
        available = struct.unpack("i", fcntl.ioctl(reader, termios.FIONREAD, b"\0" * 4))[0]
        assert available == len(inv_mod._SEALED_GH_CONFIG_YML.encode("utf-8")), (
            f"the pipe holds {available} bytes, not the expected "
            f"{len(inv_mod._SEALED_GH_CONFIG_YML.encode('utf-8'))} — if it is empty "
            f"this test is back to the case that made the original claim look true"
        )

        assert not inv_mod._seal_matches(
            seal, {"config.yml": inv_mod._SEALED_GH_CONFIG_YML}
        ), (
            "a pipe holding the right bytes reported the seal CLEAN. gh would read "
            "the planted name through that pipe and adopt the account in it "
            "(MEASURED). Both the type test and the size gate refuse this fixture, "
            "so BOTH must have been removed for this to fire"
        )
    finally:
        os.close(reader)


def test_an_oversized_entry_is_refused_WITHOUT_being_read(monkeypatch, tmp_path):
    """A size gate, and it is a denial-of-service fix rather than a tidy-up.

    The content read had no bound, so a planted `config.yml` of any size was read
    into memory in full before the comparison rejected it. MEASURED on the
    unbounded version: 64 MiB sparse cost 0.33s and +192 MiB RSS — roughly three
    times the file, resident, and linear, so a multi-GiB plant is free to create
    with `truncate` and would exhaust this box. It matters more than an ordinary
    slow path because `reconcile_gh_seal` calls this at bootstrap step ONE,
    synchronously, ahead of secrets and database init, and the read happens BEFORE
    the purge — so the seal cannot heal its way out of it either.

    Comparing `st_size` first is also strictly more correct than reading: a size
    that differs from the expected bytes is a content mismatch by definition.

    ASSERTED BY COUNTING READS, not by timing or memory, and the first version of
    this test got that wrong in a way worth recording. It asserted `elapsed < 5`
    and an `ru_maxrss` delta under 64 MiB. Neither can fail: MEASURED, the
    UNBOUNDED read of this 256 MiB fixture takes 0.45-0.88s, so the time ceiling is
    unreachable; and `ru_maxrss` is a process HIGH-WATER MARK, so once anything
    earlier in the run has peaked above the fixture size the delta is 0 and the
    memory ceiling passes regardless. CI runs the whole suite in ONE process, which
    is exactly the condition that makes it pass. A high-water-mark metric can never
    serve as a negative control.

    So this counts `os.read` calls instead: with the size gate in place the entry is
    refused before any read happens, and zero calls is a fact that cannot be
    masked by an earlier peak or a loaded machine.
    """
    import os

    import genesis.cc.invoker as inv_mod

    seal = tmp_path / "gh-sealed"
    seal.mkdir()
    big = seal / "config.yml"
    with open(big, "wb") as fh:
        fh.truncate(256 * 1024 * 1024)  # sparse: costs no disk
    big.chmod(0o400)
    seal.chmod(0o500)
    monkeypatch.setattr(inv_mod, "_SEALED_GH_CONFIG_DIR", seal)

    # CONTROL: the file really is that large, and the seal is otherwise correct.
    assert os.stat(big).st_size == 256 * 1024 * 1024
    assert os.stat(big).st_mode & 0o777 == 0o400

    reads: list[int] = []
    real_read = os.read

    def counting_read(fd, n):
        reads.append(n)
        return real_read(fd, n)

    monkeypatch.setattr(os, "read", counting_read)
    verdict = inv_mod._seal_matches(seal, {"config.yml": inv_mod._SEALED_GH_CONFIG_YML})

    assert verdict is False
    assert reads == [], (
        f"the oversized entry was READ before its size was compared "
        f"({len(reads)} call(s), first for {reads[0] if reads else 0} bytes) — that "
        f"is the unbounded read this size gate exists to prevent, and it runs at "
        f"bootstrap step one before the purge"
    )


def test_the_rewrite_VERIFIES_what_it_produced_before_reporting_success(
    monkeypatch, tmp_path
):
    """The rewrite used to be write-only, so success meant "I wrote", not "it is".

    An entry planted between the last create and the final chmod ends up INSIDE
    the finished 0500 seal, and `_sealed_gh_config_dir` returned the path as
    though it were clean — MEASURED with an extension subtree. `_seal_matches`
    catches it on the NEXT call, which is one dispatch too late: the session that
    should have been refused has already run with a seal that contains an
    extension directory, which is precisely what the seal exists to prevent.

    Driven by planting in the real window rather than by stubbing the verifier,
    so what is asserted is the behaviour and not the check. The seal is left
    holding the planted entry, and that is correct: refusing the LAUNCH is the
    contract, and repairing a directory somebody is actively writing to is not
    something this function can win.
    """
    import os

    import genesis.cc.invoker as inv_mod

    seal = tmp_path / "gh-sealed"
    seal.mkdir()
    (seal / "hosts.yml").write_text("github.com:\n  oauth_token: LEAKED\n")
    monkeypatch.setattr(inv_mod, "_SEALED_GH_CONFIG_DIR", seal)

    real_purge = inv_mod._purge_dir_entries
    planted: list[str] = []

    def purge_then_plant(dir_fd, label, depth=0):
        real_purge(dir_fd, label, depth)
        os.mkdir("gh", 0o700, dir_fd=dir_fd)
        planted.append("gh")

    monkeypatch.setattr(inv_mod, "_purge_dir_entries", purge_then_plant)

    result = inv_mod._sealed_gh_config_dir()

    assert planted == ["gh"], "the plant never happened — this test is inert"
    assert result is None, (
        "reported a prepared seal while an extension directory sat inside it; the "
        "caller reads a non-None return as 'this directory is safe to hand gh'"
    )


def test_a_deeply_nested_plant_answers_None_and_leaves_the_seal_SEALED(
    monkeypatch, tmp_path
):
    """Two failures in one, and the mode is the one that matters.

    The purge recursion was unbounded. MEASURED on a 1200-deep planted tree:
    `RecursionError` — which is not an `OSError` — escaped
    `_sealed_gh_config_dir`'s except clause and therefore its documented "answers
    None" contract, surfacing in the caller instead of the refusal. And on the way
    out it left the seal at **0700**, which is its only write protection, with
    nothing restoring it.

    Both are asserted, and the mode assertion is the load-bearing one: wrong
    CONTENTS after a failure are self-correcting, because `_seal_matches` refuses
    them and the next call rewrites. A WRITABLE seal is not — it stays writable
    until something else happens to rewrite it, and the whole mechanism rests on
    that directory being unwritable.

    The depth used here is far below Python's frame limit, so this fails on the
    BOUND rather than on recursion, which is the point: the bound converts the
    failure into one the contract covers.
    """
    import genesis.cc.invoker as inv_mod

    seal = tmp_path / "gh-sealed"
    seal.mkdir()
    (seal / "config.yml").write_text("wrong, so the rewrite runs\n")
    deep = seal / "nest"
    deep.mkdir()
    for _ in range(inv_mod._MAX_SEAL_PURGE_DEPTH + 40):
        deep = deep / "n"
        deep.mkdir()
    monkeypatch.setattr(inv_mod, "_SEALED_GH_CONFIG_DIR", seal)

    result = inv_mod._sealed_gh_config_dir()  # must not raise

    assert result is None, "a refused purge reported a prepared seal"
    assert seal.stat().st_mode & 0o777 == 0o500, (
        "the seal was left WRITABLE after the rewrite failed — its only write "
        "protection, gone, until something else rewrites it"
    )

    # And reconcile must not raise either, on the same tree.
    inv_mod.reconcile_gh_seal()

    # CONTROL: the bound is not so tight that a legitimate extension tree — the
    # thing the sweep exists for — stops being purged.
    shallow = tmp_path / "shallow"
    shallow.mkdir()
    (shallow / "config.yml").write_text("wrong\n")
    (shallow / "gh" / "extensions" / "gh-evil").mkdir(parents=True)
    (shallow / "gh" / "extensions" / "gh-evil" / "payload.sh").write_text("id\n")
    monkeypatch.setattr(inv_mod, "_SEALED_GH_CONFIG_DIR", shallow)
    assert inv_mod._sealed_gh_config_dir() == str(shallow)
    assert sorted(p.name for p in shallow.iterdir()) == ["config.yml"]


def test_a_HARDLINKED_seal_entry_is_not_written_through(monkeypatch, tmp_path):
    """The route that four path-level symlink checks could not see.

    `is_symlink()` is False for a hardlink, `is_file()` is True, and the name is
    in `desired` — so the old sweep's "keep a matching entry" branch KEPT it and
    the write landed on the other name's inode. MEASURED before the fix:
    `ln <victim> <seal>/config.yml` plus one junk entry, and the next
    reconciliation replaced the victim's contents with the seal constant and
    chmod'd it 0400. Byte for byte the effect the symlink round reports as closed.

    This is why the rewrite stopped enumerating link tricks: the seal is opened
    once with `O_DIRECTORY|O_NOFOLLOW` and everything runs against that
    descriptor, nothing is kept, and each file is created `O_EXCL|O_NOFOLLOW` —
    so the new `config.yml` is a fresh inode at `st_nlink == 1` by construction
    rather than by a check someone remembered to write.

    The nlink assertion is the one that matters. Content alone would pass against
    a sweep that wrote the right bytes through the planted link.
    """
    import os

    import genesis.cc.invoker as inv_mod

    victim = tmp_path / "victim.txt"
    victim.write_text("PRECIOUS\n")
    seal = tmp_path / "gh-sealed"
    seal.mkdir()
    os.link(victim, seal / "config.yml")
    (seal / "junk.txt").write_text("forces a mismatch so the rewrite runs\n")
    monkeypatch.setattr(inv_mod, "_SEALED_GH_CONFIG_DIR", seal)

    # CONTROL: the hardlink really is one, and really is invisible to is_symlink.
    assert victim.stat().st_nlink == 2
    assert not (seal / "config.yml").is_symlink()

    inv_mod.reconcile_gh_seal()

    assert victim.read_text() == "PRECIOUS\n", (
        "wrote through the hardlink — the seal constant landed on an inode that a "
        "writable name outside the seal also points at"
    )
    assert victim.stat().st_mode & 0o777 != 0o400, "chmod'd the victim to 0400"
    assert sorted(p.name for p in seal.iterdir()) == ["config.yml"]
    assert (seal / "config.yml").stat().st_nlink == 1, (
        "the seal's config.yml still shares its inode with a name outside the seal"
    )


def test_seal_matches_never_resolves_an_entry_NAME_TWICE(monkeypatch, tmp_path):
    """The TOCTOU lock, and it is deliberately structural rather than statistical.

    The first version of the file-type allowlist answered type, link count and
    mode from one `lstat` per path, then read the CONTENT with a fresh
    `read_text()` by path. Those are two resolutions of the same name, so the
    gates described one inode and the bytes came from whatever the name pointed at
    by the time of the read. MEASURED by an adversarial reviewer against a racing
    writer: 1 false-CLEAN verdict in 509,533 trials, in a setup where a clean
    verdict was only reachable by following a link the type gate had rejected.

    A RACE PROBE CANNOT PIN THIS, which is why the assertion below is not one. Run
    against the fixed code the same probe reports 0 in 600,000 — but its oracle arm
    reports 60,000 of 60,000 CLEAN when the bytes are genuinely right, meaning the
    swapping thread never once won the window. A run that never reproduces the
    race says nothing about whether the race is closed, and one hit in half a
    million is exactly the frequency a green run hides.

    So this asserts the PROPERTY instead: no entry name may be resolved a second
    time. `Path.read_text` and `Path.open` are booby-trapped for the duration, so
    reverting to a path-addressed read fails here immediately and deterministically
    rather than one time in 500,000. The positive control matters as much — the
    seal must still be judged CLEAN with the traps armed, or this would pass
    against a function that simply stopped working.
    """
    import genesis.cc.invoker as inv_mod

    seal = tmp_path / "gh-sealed"
    seal.mkdir()
    (seal / "config.yml").write_text(inv_mod._SEALED_GH_CONFIG_YML)
    (seal / "config.yml").chmod(0o400)
    seal.chmod(0o500)

    # Both fixtures FIRST. The traps below catch any path resolution, and
    # `write_text` is one — arming them before the fixtures exist makes the test
    # fail on its own setup, which is how the first version of it failed.
    (tmp_path / "other").mkdir()
    wrong = tmp_path / "other" / "gh-sealed"
    wrong.mkdir()
    (wrong / "config.yml").write_text("not the seal constant\n")
    (wrong / "config.yml").chmod(0o400)
    wrong.chmod(0o500)

    resolved: list[str] = []

    def trap(self, *a, **kw):
        resolved.append(str(self))
        raise AssertionError(
            f"_seal_matches resolved {self} by PATH after gating on a stat; that is "
            f"the time-of-check window the descriptor form exists to close"
        )

    monkeypatch.setattr(Path, "read_text", trap, raising=True)
    monkeypatch.setattr(Path, "open", trap, raising=True)

    # POSITIVE CONTROL: still answers CLEAN with the traps armed.
    assert inv_mod._seal_matches(seal, {"config.yml": inv_mod._SEALED_GH_CONFIG_YML}), (
        "the seal is no longer recognised as clean — this test would then pass for "
        "the wrong reason, since a function that always answers False resolves no "
        "paths either"
    )
    assert resolved == [], f"paths re-resolved: {resolved}"

    # And the negative direction reaches its verdict without touching a path
    # either — a mismatch must be decided from the descriptor too, or the window
    # is merely narrower rather than closed.
    assert not inv_mod._seal_matches(wrong, {"config.yml": inv_mod._SEALED_GH_CONFIG_YML})
    assert resolved == [], f"paths re-resolved on the mismatch path: {resolved}"


def test_seal_matches_rejects_a_HARDLINKED_entry(monkeypatch, tmp_path):
    """The worse half: the seal reporting itself CLEAN while somebody else owns it.

    `_seal_matches` compared name, mode and bytes. A hardlink satisfies all three,
    so a planted one made the fast path return True, the rewrite never ran, and a
    WRITABLE name outside the 0500 directory owned gh's `config.yml`. MEASURED: an
    alias was written through that outside name and read back from inside the seal
    — the `gh alias` escape this entire mechanism exists to close.

    The 0500 directory mode is the seal's only write protection, and a second link
    to the inode is a way around it that no content or mode check can detect. So
    the check belongs HERE as well as in the rewrite: this function decides whether
    the rewrite happens at all, and a check the rewrite has and this one lacks is a
    check that never runs.

    Paired with a positive control, because "reject everything" would also pass.
    """
    import os

    import genesis.cc.invoker as inv_mod

    desired = {"config.yml": inv_mod._SEALED_GH_CONFIG_YML}

    # POSITIVE CONTROL: a genuine single-link seal MUST match.
    good = tmp_path / "genuine"
    good.mkdir()
    (good / "config.yml").write_text(inv_mod._SEALED_GH_CONFIG_YML)
    (good / "config.yml").chmod(0o400)
    good.chmod(0o500)
    assert inv_mod._seal_matches(good, desired), (
        "a genuine seal does not match, so a False below would prove nothing"
    )

    # The hardlink: identical name, bytes and modes; only st_nlink differs.
    outside = tmp_path / "attacker-writable.yml"
    outside.write_text(inv_mod._SEALED_GH_CONFIG_YML)
    seal = tmp_path / "gh-sealed"
    seal.mkdir()
    os.link(outside, seal / "config.yml")
    (seal / "config.yml").chmod(0o400)
    seal.chmod(0o500)

    assert not inv_mod._seal_matches(seal, desired), (
        "a hardlinked entry reported the seal as already correct — the fast path "
        "returns early, the rewrite never runs, and a writable name outside the "
        "seal keeps ownership of gh's config.yml"
    )

    # And it is actually REPAIRED, not merely reported.
    monkeypatch.setattr(inv_mod, "_SEALED_GH_CONFIG_DIR", seal)
    inv_mod.reconcile_gh_seal()
    assert (seal / "config.yml").stat().st_nlink == 1


def test_the_purge_addresses_entries_BY_DESCRIPTOR_and_never_by_path():
    """A structural lock, because the behavioural difference is a RACE.

    `_purge_dir_entries` deliberately avoids `shutil.rmtree` and every other
    path-addressed removal: walking by PATH is the property that let the previous
    version write through planted links, and a component swapped DURING the walk
    redirects it. Substituting `shutil.rmtree` back in is behaviourally identical
    for every static case a test can set up — a mutation arm doing exactly that
    stayed GREEN — so the choice cannot be pinned by observing behaviour, only by
    asserting the code makes it.

    Asserted on the FUNCTION source rather than the module, so an unrelated
    `shutil.which` elsewhere in the file does not satisfy or break it. Every
    removal must carry `dir_fd=`, which is what makes it relative to the pinned
    descriptor instead of to a path that can be re-pointed.
    """
    import ast
    import inspect
    import re
    import textwrap

    import genesis.cc.invoker as inv_mod

    # The DOCSTRING names `shutil.rmtree` in order to say it is not used, so a
    # whole-source check reads the explanation as the offence. Strip the
    # docstring and assert on the code, which is what the claim is about.
    whole = textwrap.dedent(inspect.getsource(inv_mod._purge_dir_entries))
    fn = ast.parse(whole).body[0]
    body = fn.body[1:] if ast.get_docstring(fn) is not None else fn.body
    assert body, "the purge has no body left — this lock is inert"
    src = "\n".join(ast.unparse(node) for node in body)

    assert "shutil" not in src, (
        "the purge reaches for shutil again; rmtree walks by PATH, which is the "
        "property this helper exists to avoid"
    )
    for removal in ("os.unlink(", "os.rmdir("):
        calls = re.findall(re.escape(removal) + r"[^)]*\)", src)
        assert calls, f"{removal} disappeared from the purge — this lock is inert"
        for call in calls:
            assert "dir_fd=" in call, (
                f"{call!r} addresses an entry by path; a component of that path "
                f"can be replaced after the descriptor was opened"
            )

    # The ENUMERATION must also be descriptor-relative. Pinning only the
    # removals left the door open to `os.scandir(label)` or `label.iterdir()`,
    # which walks by path — the names would then come from a directory that may
    # no longer be the one the descriptor points at, and every "relative" removal
    # after it would be relative to the right descriptor but the wrong list.
    assert "os.scandir(dir_fd)" in src, (
        "the purge enumerates by PATH; the entry names must come from the same "
        "descriptor the removals are relative to"
    )
    # The list below is a DENYLIST and cannot be complete — it is a convenience
    # that names the spellings someone would actually reach for, so the failure
    # message points at the mistake. The positive assertion above is the real
    # check: the enumeration must BE `os.scandir(dir_fd)`, which no path walker
    # can satisfy.
    for walk in ("iterdir(", "os.walk(", "os.listdir(label", "glob("):
        assert walk not in src, f"the purge walks by path via {walk!r}"

    # And the recursion opens subdirectories relative to the parent descriptor,
    # with O_NOFOLLOW — not by joining a path.
    assert "dir_fd=dir_fd" in src, "the recursion no longer opens relative to the parent"
    assert "O_NOFOLLOW" in src and "O_DIRECTORY" in src, (
        "a subdirectory is opened without O_NOFOLLOW|O_DIRECTORY, so a symlinked "
        "one would be followed into"
    )


def test_O_EXCL_refuses_a_name_planted_between_the_purge_and_the_create(
    monkeypatch, tmp_path
):
    """The RACE arm, and the only thing that makes O_EXCL observable.

    Everything is unlinked before anything is created, so in a static seal
    `O_CREAT|O_WRONLY` and `O_CREAT|O_EXCL|O_WRONLY` behave identically — a
    mutation arm dropping `O_EXCL` stayed GREEN for exactly that reason, and the
    honest reading is that the flag guards the WINDOW between the two steps, not
    the static case. Rather than leave the flag unpinned, this forces the window
    open.

    The interleave is real, not stubbed: the genuine purge runs, and a hardlink is
    then planted under the name the create is about to use. Without `O_EXCL` the
    create opens that name and the write lands on the other inode — the same
    effect as the static hardlink defect, reachable by a writer that loses the
    static race but wins this one.

    `FileExistsError` is an OSError, so the caller answers None and the launch is
    REFUSED. The seal is left mid-repair, which is correct: refusing is the
    documented behaviour for a seal that cannot be prepared, and the alternative
    is writing through a link somebody else planted.
    """
    import os

    import genesis.cc.invoker as inv_mod

    victim = tmp_path / "victim.txt"
    victim.write_text("PRECIOUS\n")
    seal = tmp_path / "gh-sealed"
    seal.mkdir()
    (seal / "hosts.yml").write_text("github.com:\n  oauth_token: LEAKED\n")
    monkeypatch.setattr(inv_mod, "_SEALED_GH_CONFIG_DIR", seal)

    real_purge = inv_mod._purge_dir_entries
    planted: list[str] = []

    def purge_then_plant(dir_fd, label):
        real_purge(dir_fd, label)
        # The window: every name is gone, and nothing has been created yet.
        os.link(victim, "config.yml", dst_dir_fd=dir_fd)
        planted.append("config.yml")

    monkeypatch.setattr(inv_mod, "_purge_dir_entries", purge_then_plant)

    result = inv_mod._sealed_gh_config_dir()

    assert planted == ["config.yml"], "the interleave never ran — this test is inert"
    assert victim.read_text() == "PRECIOUS\n", (
        "the create wrote through a hardlink planted after the purge; O_EXCL is "
        "what refuses a name that exists at that point"
    )
    assert victim.stat().st_mode & 0o777 != 0o400, "the victim was chmod'd to 0400"
    assert result is None, (
        "the seal reported success despite being unable to create its own file"
    )


def test_a_real_nested_subtree_is_removed_without_following_links(monkeypatch, tmp_path):
    """`shutil.rmtree` is gone, so the subtree sweep needs its own coverage.

    The seal's purpose since the extension finding is that
    `<seal>/gh/extensions/...` does not exist. The old sweep used `shutil.rmtree`,
    which walks by PATH — the property that made the previous version write
    through planted links. The replacement recurses through descriptors, so both
    halves need asserting: a REAL nested subtree is removed, and a SYMLINKED
    subdirectory is unlinked as the link it is rather than followed into.
    """
    import genesis.cc.invoker as inv_mod

    # Arm 1 — a real nested subtree, several levels deep.
    seal = tmp_path / "gh-sealed"
    seal.mkdir()
    (seal / "config.yml").write_text(inv_mod._SEALED_GH_CONFIG_YML)
    deep = seal / "gh" / "extensions" / "gh-evil"
    deep.mkdir(parents=True)
    (deep / "payload.sh").write_text("#!/bin/sh\nid\n")
    monkeypatch.setattr(inv_mod, "_SEALED_GH_CONFIG_DIR", seal)

    inv_mod.reconcile_gh_seal()

    assert sorted(p.name for p in seal.iterdir()) == ["config.yml"], (
        "the extension subtree survived — which is what the seal exists to prevent"
    )

    # Arm 2 — a symlinked subdirectory must be unlinked, its target untouched.
    other = tmp_path / "somewhere-else"
    other.mkdir()
    (other / "precious.txt").write_text("keep\n")
    seal2 = tmp_path / "seal2"
    seal2.mkdir()
    (seal2 / "config.yml").write_text(inv_mod._SEALED_GH_CONFIG_YML)
    (seal2 / "gh").symlink_to(other)
    monkeypatch.setattr(inv_mod, "_SEALED_GH_CONFIG_DIR", seal2)

    inv_mod.reconcile_gh_seal()

    assert (other / "precious.txt").exists(), "recursed THROUGH the symlinked subdir"
    assert sorted(p.name for p in other.iterdir()) == ["precious.txt"]
    assert not (seal2 / "gh").exists()


def test_the_LOCK_FILE_is_not_a_symlink_truncation_primitive(monkeypatch, tmp_path):
    """A DIFFERENT spelling of the symlink class, missed by the first fix.

    The rewrite lock lives in the seal's PARENT as `<seal>.lock`, and it was
    opened `"w"` — which follows a symlink AND truncates whatever it lands on.
    Every symlink check added for the seal itself runs LATER than that line, so
    none of them covered it. MEASURED on the intermediate code: with
    `<seal>.lock` symlinked at an unrelated file, that file was truncated to zero
    bytes while the seal reconciled normally and nothing logged a problem.

    This test exists as much for the METHOD as for the bug. The first fix closed
    the three routes a finding named; asking "what else writes through a path an
    attacker controls?" found a fourth. The sweep cannot find these — a mutation
    only tests the guard someone already wrote.

    `O_NOFOLLOW` raises ELOOP on a symlinked final component, which
    `_sealed_gh_config_dir` catches as an OSError and answers None, so the caller
    REFUSES THE LAUNCH. Dropping `O_TRUNC` is the second half: a real lock file
    is reused rather than emptied, and flock needs no content.
    """
    import genesis.cc.invoker as inv_mod

    victim = tmp_path / "unrelated.txt"
    victim.write_text("PRECIOUS DATA\n")

    seal = tmp_path / "gh-sealed"
    seal.mkdir()
    (seal / "hosts.yml").write_text("github.com:\n  oauth_token: LEAKED\n")
    (tmp_path / "gh-sealed.lock").symlink_to(victim)
    monkeypatch.setattr(inv_mod, "_SEALED_GH_CONFIG_DIR", seal)

    inv_mod.reconcile_gh_seal()  # must not raise, even though the open fails

    assert victim.read_text() == "PRECIOUS DATA\n", (
        "the lock open truncated a symlink's target — an arbitrary-file "
        "truncation primitive reachable by anything that can write the seal's "
        "parent directory"
    )
    assert inv_mod._sealed_gh_config_dir() is None, (
        "the dispatch path continued despite being unable to take the lock"
    )


def test_an_existing_real_lock_file_is_reused_not_emptied(monkeypatch, tmp_path):
    """Dropping O_TRUNC is load-bearing, and this is what pins it.

    `O_NOFOLLOW` alone would still have truncated a REAL lock file on every
    rewrite. Harmless today — nothing reads the lock's bytes — but it is the
    reason the open is `O_CREAT|O_RDWR` rather than `"w"`, and without a test the
    next edit reaches for `"w"` again because it is shorter.

    The control is the seal itself reconciling: if the lock could not be taken at
    all, the credential would still be there and this test would pass for the
    wrong reason.
    """
    import genesis.cc.invoker as inv_mod

    seal = tmp_path / "gh-sealed"
    seal.mkdir()
    (seal / "config.yml").write_text(inv_mod._SEALED_GH_CONFIG_YML)
    (seal / "hosts.yml").write_text("github.com:\n  oauth_token: LEAKED\n")
    lock = tmp_path / "gh-sealed.lock"
    lock.write_text("pre-existing marker\n")
    monkeypatch.setattr(inv_mod, "_SEALED_GH_CONFIG_DIR", seal)

    inv_mod.reconcile_gh_seal()

    assert lock.read_text() == "pre-existing marker\n", "the lock file was truncated"
    assert not (seal / "hosts.yml").exists(), (
        "CONTROL: the credential survived, so the lock was never taken and the "
        "assertion above proves nothing"
    )


def test_a_symlinked_seal_DIRECTORY_is_refused_not_rewritten_through(monkeypatch, tmp_path):
    """The destructive one, and this PR is what made it reachable.

    Every operation the rewrite performs — `chmod`, `iterdir`, `rmtree`,
    `touch`, `write_text` — FOLLOWS symlinks. Before `reconcile_gh_seal` the
    sweep was reachable only from an allowlisted dispatch, of which this install
    has run zero, so a symlinked seal was a latent hazard. Wiring the sweep into
    startup makes it fire on EVERY boot, which is what turns "latent" into
    "runs tonight".

    MEASURED before the fix, driving this function against a seal symlinked at a
    directory holding three entries: it came back holding only `config.yml`, the
    subdirectory was `rmtree`'d, and the target was chmod'd 0500. The planter
    needs only same-uid write access — the adversary the seal already assumes,
    since its whole premise is a dispatched session that can be told what to do
    by the content it reads.

    Asserted on the TARGET, not on the seal: the point is that nothing outside
    the seal is touched.
    """
    import genesis.cc.invoker as inv_mod

    victim = tmp_path / "somebody-elses-config"
    victim.mkdir()
    (victim / "hosts.yml").write_text("github.com:\n  oauth_token: PRETEND\n")
    (victim / "unrelated.txt").write_text("keep me\n")
    (victim / "subdir").mkdir()
    (victim / "subdir" / "deep.txt").write_text("keep me too\n")
    before = sorted(p.name for p in victim.iterdir())
    before_mode = victim.stat().st_mode & 0o777

    seal = tmp_path / "gh-sealed"
    seal.symlink_to(victim)
    monkeypatch.setattr(inv_mod, "_SEALED_GH_CONFIG_DIR", seal)

    inv_mod.reconcile_gh_seal()

    assert sorted(p.name for p in victim.iterdir()) == before, (
        "the sweep reached THROUGH the symlink and modified a directory that is "
        "not the seal"
    )
    assert victim.stat().st_mode & 0o777 == before_mode, "the target was chmod'd"

    # And the dispatch path refuses rather than proceeding — None is what makes
    # the caller REFUSE THE LAUNCH, so this is not merely a skip.
    assert inv_mod._sealed_gh_config_dir() is None

    # RE-CHECK THE TARGET AFTERWARDS. The assertions above ran before that call,
    # so on their own they say nothing about what it did — and a mutation arm
    # dropping `O_NOFOLLOW` from the directory open passed this test for exactly
    # that reason: the post-write verification still answered None while the
    # rewrite had already emptied the symlink's target.
    assert sorted(p.name for p in victim.iterdir()) == before, (
        "the dispatch path reached THROUGH the symlink and modified the target, "
        "even though it correctly reported failure"
    )
    assert victim.stat().st_mode & 0o777 == before_mode


def test_reconcile_does_not_even_CALL_the_sealer_on_a_symlinked_seal(monkeypatch, tmp_path):
    """Pins the OUTER guard, which its sibling test cannot.

    Both layers refuse a symlinked seal — `reconcile_gh_seal` returns early, and
    `_sealed_gh_config_dir` refuses again under the lock. That redundancy is
    deliberate, and it is also why a mutation sweep deleting the OUTER guard left
    `test_a_symlinked_seal_DIRECTORY_is_refused_not_rewritten_through` GREEN: the
    inner one still held, so the observable outcome was identical. MEASURED —
    that arm was the one failure in an 8-arm sweep, and this test is the answer
    to it.

    A guard no test pins is a guard someone deletes as redundant. So this asserts
    the thing that differs between the layers rather than the outcome they share:
    on a symlinked seal, reconciliation must not reach the sealer AT ALL. The
    control matters as much as the assertion — on an ordinary seal it must reach
    it, or this would pass against a reconcile that never calls anything.
    """
    import genesis.cc.invoker as inv_mod

    calls: list[str] = []
    monkeypatch.setattr(
        inv_mod, "_sealed_gh_config_dir", lambda **_k: calls.append("called") or "x"
    )

    # CONTROL: an ordinary seal DOES reach the sealer.
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    monkeypatch.setattr(inv_mod, "_SEALED_GH_CONFIG_DIR", ordinary)
    inv_mod.reconcile_gh_seal()
    assert calls == ["called"], (
        "reconciliation did not reach the sealer even for an ordinary seal — the "
        "assertion below would then prove nothing"
    )

    # The symlink case: refused before the sealer is consulted.
    calls.clear()
    linked = tmp_path / "linked"
    linked.symlink_to(ordinary)
    monkeypatch.setattr(inv_mod, "_SEALED_GH_CONFIG_DIR", linked)
    inv_mod.reconcile_gh_seal()
    assert calls == [], (
        "reconciliation passed a symlinked seal through to the sealer; the inner "
        "guard would catch it today, but this boot-path check must not rely on "
        "that — remove one and the other becomes the only thing standing"
    )


def test_a_symlink_INSIDE_the_seal_is_unlinked_never_written_through(monkeypatch, tmp_path):
    """A link named `config.yml` is the arbitrary-write primitive.

    `stale.is_file()` follows the link, so a symlink named `config.yml` is both
    `is_file()` and in `desired` — the old sweep therefore KEPT it, and the
    write that follows landed on its target. MEASURED before the fix: an
    unrelated file's contents were replaced with the seal constant and the file
    was chmod'd 0400.

    `_seal_matches` had to be fixed alongside it, or this test could pass for
    the wrong reason: a link pointing at bytes that already match, at 0400,
    would have made the fast path report the seal ALREADY CORRECT and return
    before the sweep ever ran.

    The credential assertion is the CONTROL — a fix that refused the whole
    directory would pass the symlink half while silently dropping the migration.
    """
    import genesis.cc.invoker as inv_mod

    important = tmp_path / "important.txt"
    important.write_text("DO NOT OVERWRITE\n")

    seal = tmp_path / "gh-sealed"
    seal.mkdir()
    (seal / "config.yml").symlink_to(important)
    (seal / "hosts.yml").write_text("github.com:\n  oauth_token: LEAKED\n")
    monkeypatch.setattr(inv_mod, "_SEALED_GH_CONFIG_DIR", seal)

    inv_mod.reconcile_gh_seal()

    assert important.read_text() == "DO NOT OVERWRITE\n", (
        "wrote THROUGH the symlink — arbitrary-file overwrite with fixed content"
    )
    assert not (seal / "config.yml").is_symlink(), "the planted link survived"
    assert (seal / "config.yml").read_text() == inv_mod._SEALED_GH_CONFIG_YML
    assert not (seal / "hosts.yml").exists(), (
        "CONTROL: the credential survived — the symlink fix broke the migration"
    )


def test_a_symlinked_DIRECTORY_inside_the_seal_is_not_rmtree_followed(monkeypatch, tmp_path):
    """The extension-route sweep must not become an rmtree of somewhere else.

    The purge removes directory entries recursively, because `<seal>/gh/extensions`
    is exactly what the seal exists to prevent. It no longer uses `shutil.rmtree`
    at all — that walks by PATH, which is the property that let an earlier version
    write through planted links — so this asserts the replacement: a symlinked
    subdirectory is unlinked as the LINK it is, and its target is untouched.

    On ordering, stated correctly because an earlier version of this docstring had
    it backwards: `S_ISDIR` is tested FIRST and the symlink branch second. That is
    safe here only because the stat is an `lstat`, so a symlinked directory is
    never `S_ISDIR` and falls through to the link branch. The order is not the
    protection; `lstat` is.
    """
    import genesis.cc.invoker as inv_mod

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "precious.txt").write_text("keep\n")

    seal = tmp_path / "gh-sealed"
    seal.mkdir()
    (seal / "config.yml").write_text(inv_mod._SEALED_GH_CONFIG_YML)
    (seal / "gh").symlink_to(elsewhere)
    monkeypatch.setattr(inv_mod, "_SEALED_GH_CONFIG_DIR", seal)

    inv_mod.reconcile_gh_seal()

    assert (elsewhere / "precious.txt").exists(), "rmtree followed the link"
    assert sorted(p.name for p in elsewhere.iterdir()) == ["precious.txt"]
    assert not (seal / "gh").exists(), "the planted link survived the sweep"


def test_seal_matches_never_reports_a_symlinked_entry_as_correct(monkeypatch, tmp_path):
    """The fast path is what would have skipped the repair entirely.

    Driven at the exact shape that defeats a content-and-mode check: a symlink
    named `config.yml`, pointing at a file holding precisely the right bytes, at
    0400, inside a directory at 0500. Every value `_seal_matches` compares is
    correct; only the entry's TYPE is wrong.

    Paired with a positive control — the same seal with a real file rather than
    a link MUST match — so a `False` here cannot be about the modes or the bytes.
    """
    import genesis.cc.invoker as inv_mod

    desired = {"config.yml": inv_mod._SEALED_GH_CONFIG_YML}

    # POSITIVE CONTROL: a real file at the right bytes and modes matches.
    good = tmp_path / "good-seal"
    good.mkdir()
    (good / "config.yml").write_text(inv_mod._SEALED_GH_CONFIG_YML)
    (good / "config.yml").chmod(0o400)
    good.chmod(0o500)
    assert inv_mod._seal_matches(good, desired), (
        "the control seal does not match, so a False below would prove nothing"
    )

    # The symlink case: same bytes, same modes, different TYPE.
    src = tmp_path / "decoy.yml"
    src.write_text(inv_mod._SEALED_GH_CONFIG_YML)
    src.chmod(0o400)
    linked = tmp_path / "linked-seal"
    linked.mkdir()
    (linked / "config.yml").symlink_to(src)
    linked.chmod(0o500)
    assert not inv_mod._seal_matches(linked, desired), (
        "a symlinked entry reported the seal as already correct — the fast path "
        "returns early, the sweep never runs, and the link stays"
    )

    # And a symlinked seal DIRECTORY is never a match either.
    linked_dir = tmp_path / "linked-dir"
    linked_dir.symlink_to(good)
    assert not inv_mod._seal_matches(linked_dir, desired)


def test_reconcile_gh_seal_never_raises(monkeypatch, tmp_path):
    """It runs on a startup path where a failure must not block boot.

    TWO ARMS, because one of them was not testing what it claimed. The realistic
    arm drives a seal whose PARENT is unwritable, so the rewrite fails for real —
    but that failure is an OSError, which `_sealed_gh_config_dir` already catches
    and answers None for. MEASURED: this test still passed with
    `reconcile_gh_seal`'s own blanket guard DELETED, so it covered the inner
    function's contract and never the outer one.

    The second arm is what covers the guard: an exception that is NOT an OSError,
    from the call `reconcile_gh_seal` makes itself. Stubbing is right here
    precisely because the claim is about TYPE-INDIFFERENCE — "nothing escapes,
    whatever it is" cannot be driven by picking one realistic failure, and the
    route that actually raised in practice (an eager `os.readlink` in a log
    argument) was a plain OSError that the old reasoning also missed.
    """
    import genesis.cc.invoker as inv_mod

    parent = tmp_path / "ro"
    parent.mkdir()
    target = parent / "gh-sealed"
    target.mkdir()
    (target / "hosts.yml").write_text("github.com:\n  oauth_token: x\n", encoding="utf-8")
    monkeypatch.setattr(inv_mod, "_SEALED_GH_CONFIG_DIR", target)
    parent.chmod(0o500)
    try:
        inv_mod.reconcile_gh_seal()  # must not raise
    finally:
        parent.chmod(0o700)

    # ARM 2 — a NON-OSError out of the call this function makes. Only the blanket
    # guard stops this one; delete it and this arm fails while arm 1 still passes.
    def explode() -> str | None:
        raise RuntimeError("something nobody enumerated")

    monkeypatch.setattr(inv_mod, "_sealed_gh_config_dir", explode)
    inv_mod.reconcile_gh_seal()  # must not raise either


def test_every_dispatched_session_gets_gh_token_pinned_empty(invoker, monkeypatch):
    """The pin is UNCONDITIONAL — not gated on the gh allowlist.

    `bash_allowlist=` is assigned at exactly ONE call site in the tree, so every
    other dispatch takes the `()` default and `_build_env` skips the whole
    hardening block. Gating the pin there would have left those sessions holding
    the operator token, including ones that run Bash on external input.

    Both arms are asserted, because the no-allowlist arm is the one a
    hardening-only implementation fails.
    """
    monkeypatch.setenv("GH_TOKEN", "ghp_INHERITED_FROM_THE_OPERATOR")
    for inv in (
        CCInvocation(prompt="hi"),
        CCInvocation(prompt="hi", bash_allowlist=("gh",)),
        CCInvocation(prompt="hi", bash_allowlist=("jq",)),
    ):
        env = invoker._build_env(inv)
        assert env["GH_TOKEN"] == "", (
            f"an inherited GH_TOKEN survived into a dispatch with "
            f"bash_allowlist={inv.bash_allowlist!r} — gh resolves GH_TOKEN "
            f"ahead of hosts.yml, so this outranks the credential-free seal"
        )


def test_no_session_without_a_gh_allowlist_has_gh_pointed_anywhere(invoker, monkeypatch):
    """The SCOPE of the token pin, asserted from the other side.

    `GH_CONFIG_DIR` is pinned by `_gh_hardening` and NOWHERE else, so a dispatch
    with no gh allowlist must not acquire one however it is stamped. This is a
    guard against a specific REJECTED design, kept because the design looked
    right: pinning the seal for `origin == external_untrusted`, to close the
    ON-DISK route that the empty token demonstrably does not close.

    It was rejected on two MEASURED grounds. That origin is far wider than it
    reads — SIX dispatch profiles via `_PROFILE_ORIGIN` (campaign,
    community-responder, interact, mail, research, steward) plus every
    non-owner-attended conversation channel via `session_origin_for_channel`,
    dashboard included — and it would also have pinned `XDG_DATA_HOME`, a
    process-global base directory, at a read-only tree for those sessions. And
    pinning the SEAL is a fail-OPEN on exactly the installs that need it:
    REPRODUCED, a rewrite that fails before the stale sweep leaves a pre-heal
    seal still holding the copied credential, and a fallback that pins the path
    anyway points the session AT it.

    The arms below therefore include external-untrusted DELIBERATELY: if a later
    change reintroduces that pin, this fails and sends the reader to this
    docstring instead of to a rediscovery.

    `delenv` first — `_build_env` copies `os.environ` wholesale, so a developer
    with GH_CONFIG_DIR exported would otherwise see this fail for a reason that
    has nothing to do with the code.
    """
    from genesis.memory.provenance import ORIGIN_EXTERNAL_UNTRUSTED, ORIGIN_FIRST_PARTY

    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    for inv in (
        CCInvocation(prompt="hi", origin=ORIGIN_EXTERNAL_UNTRUSTED),
        CCInvocation(prompt="hi", origin=ORIGIN_FIRST_PARTY),
        CCInvocation(prompt="hi"),
        CCInvocation(prompt="hi", bash_allowlist=("jq",)),
    ):
        env = invoker._build_env(inv)
        assert "GH_CONFIG_DIR" not in env, (
            f"gh was pointed somewhere for a session with no gh allowlist "
            f"(origin={inv.origin!r}, allowlist={inv.bash_allowlist!r}) — read "
            f"this test's docstring before making that deliberate"
        )
        assert "XDG_DATA_HOME" not in env, (
            "XDG_DATA_HOME is a process-global base dir; pinning it at the "
            "read-only seal outside the allowlisted path is unmeasured"
        )
        # The TOKEN pin IS unconditional — the two have different scopes.
        assert env["GH_TOKEN"] == ""


# DELETED: test_the_gh_token_pin_is_enforced_fail_closed_not_merely_set.
#
# It asserted `_gh_hardening()["GH_TOKEN"] == ""` and drove the refusal through
# `_assert_hardening_present(..., ("gh",))` — i.e. it proved the enforcement for an
# ALLOWLISTED session, which is the only case that was never broken. No shipped
# profile grants an allowlist, so `_launch_env` never reached that check and the pin
# was set-but-unenforced on every real session. Keeping the test would have gone on
# reporting green over exactly that hole.
#
# Replaced by `test_env_overrides_cannot_restore_a_credential_on_ANY_session` (a
# session with NO allowlist, per variable) and by the absence assertion in
# `test_the_gh_confinement_pins_every_documented_program_route`, which pins that
# credentials are no longer part of the binary hardening at all.


@pytest.mark.parametrize("key", ["GH_CONFIG_DIR", "GH_PAGER"])
async def test_refuses_when_env_overrides_would_strip_the_gh_hardening(
    invoker, monkeypatch, tmp_path, key
):
    """env_overrides is applied LAST in _build_env and wins over everything.

    Defending `GENESIS_BASH_ALLOWLIST` by name was the narrow version of this:
    the same mechanism can replace the confinement of the allowlisted binary
    after every other check has passed, and the guard cannot see it — a command
    whose first token is `gh` is genuinely allowlisted.
    """
    _arm(monkeypatch, tmp_path, ["bash", str(_REAL_GUARD)])
    with pytest.raises(RuntimeError, match="not in the environment"):
        await _verify(
            invoker,
            CCInvocation(
                prompt="hi",
                bash_allowlist=("gh",),
                env_overrides={key: "/tmp/attacker-writable"},
            ),
        )


def test_a_seal_with_the_right_bytes_at_the_wrong_modes_is_not_a_seal(monkeypatch, tmp_path):
    """The modes ARE the mechanism, so the match test compares them too.

    A rewrite interrupted between its chmod-writable and its chmod-back leaves
    correct content at writable permissions. A content-only check would call
    that good and skip the repair, leaving a session pointed at a config it can
    write — which is the escape, restored quietly.
    """
    import genesis.cc.invoker as inv_mod

    sealed = Path(_seal(monkeypatch, tmp_path))
    assert inv_mod._seal_matches(sealed, {"config.yml": (sealed / "config.yml").read_text()})

    desired = {"config.yml": (sealed / "config.yml").read_text()}

    # DIRECTORY mode alone, files left at 0400. Asserted separately because the
    # file-mode check below would otherwise catch a combined case and the
    # directory check could be deleted without any test noticing — a writable
    # directory lets gh CREATE config.yml even when the existing files are
    # read-only, so this is the half that matters most.
    sealed.chmod(0o700)
    assert not inv_mod._seal_matches(sealed, desired), (
        "a writable seal DIRECTORY compared equal; gh could add a config file"
    )
    sealed.chmod(0o500)
    assert inv_mod._seal_matches(sealed, desired), "restoring the mode did not restore the match"

    sealed.chmod(0o700)
    (sealed / "config.yml").chmod(0o600)
    assert not inv_mod._seal_matches(sealed, desired), (
        "a writable seal compared equal — the repair would be skipped"
    )


def test_seal_rewrites_are_serialised_against_a_concurrent_writer(monkeypatch, tmp_path):
    """The rewrite is not atomic, so it must not interleave.

    It chmods the directory writable, unlinks, writes, then chmods back. Two
    dispatches arriving together — first run, or just after the operator's
    token changes — would otherwise collide, and the loser gets a
    PermissionError mid-write, returns None, and the caller refuses a launch
    that should have succeeded.

    Tested by holding the lock and asserting the rewrite BLOCKS rather than by
    asserting a lock file exists: the file existing says a lock was opened, not
    that anything waits on it.
    """
    import fcntl
    import threading

    import genesis.cc.invoker as inv_mod

    source = tmp_path / "src-gh"
    source.mkdir()
    (source / "hosts.yml").write_text("github.com:\n  oauth_token: x\n", encoding="utf-8")
    monkeypatch.setenv("GH_CONFIG_DIR", str(source))
    target = tmp_path / "sealed"
    monkeypatch.setattr(inv_mod, "_SEALED_GH_CONFIG_DIR", target)

    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.parent / f"{target.name}.lock"
    done = threading.Event()

    with open(lock_path, "w", encoding="utf-8") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        worker = threading.Thread(target=lambda: (inv_mod._sealed_gh_config_dir(), done.set()))
        worker.start()
        blocked = not done.wait(timeout=1.5)
        assert blocked, (
            "the rewrite completed while another writer held the lock — two "
            "concurrent dispatches can interleave mid-rewrite"
        )
    # Lock released: it should now finish on its own.
    assert done.wait(timeout=10), "the rewrite never completed after the lock was freed"
    worker.join(timeout=5)
    assert inv_mod._seal_matches(target, {"config.yml": inv_mod._SEALED_GH_CONFIG_YML})


# ── Settings-level env pins (#2385 round 4) ──────────────────────────


def test_settings_env_pins_empty_every_credential_for_every_session():
    """Every session pins all four gh credential variables to "" — the same
    values the launch env carries — with no allowlist required."""
    import genesis.cc.invoker as inv_mod

    pins = inv_mod._settings_env_pins(())
    assert pins == {var: "" for var in inv_mod._GH_CREDENTIAL_ENV}
    assert len(pins) == 4


def test_settings_env_pins_add_the_confinement_of_an_allowlisted_binary(monkeypatch):
    """An allowlisted gh session also pins its sealed config at the settings
    level, so a user or project settings GH_CONFIG_DIR cannot reopen it."""
    import genesis.cc.invoker as inv_mod

    sealed = {"GH_CONFIG_DIR": "/sealed", "XDG_DATA_HOME": "/sealed"}
    monkeypatch.setitem(inv_mod._BINARY_HARDENING, "gh", lambda: dict(sealed))

    pins = inv_mod._settings_env_pins(("gh",))
    assert pins["GH_CONFIG_DIR"] == "/sealed"
    assert pins["XDG_DATA_HOME"] == "/sealed"
    assert all(pins[var] == "" for var in inv_mod._GH_CREDENTIAL_ENV)


def test_settings_env_pins_refuse_when_hardening_cannot_be_prepared(monkeypatch):
    """A seal that cannot be prepared refuses the launch here. Writing a settings
    file without the seal pins would let a transient failure that clears before
    _build_env pass every launch check with the seal unpinned."""
    import genesis.cc.invoker as inv_mod

    monkeypatch.setitem(inv_mod._BINARY_HARDENING, "gh", lambda: None)
    with pytest.raises(RuntimeError):
        inv_mod._settings_env_pins(("gh",))


def test_cc_span_settings_path_with_pins_writes_a_sibling_env_file(monkeypatch, tmp_path):
    """Pins go to a content-named sibling with an `env` block; the legacy
    hooks-only file is neither written nor changed, so a process on older code
    rewriting it cannot strip the pins."""
    import genesis.cc.invoker as inv_mod

    fake_repo, _hook = _fake_genesis_hook_repo(tmp_path)
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(fake_repo))
    legacy = tmp_path / "settings.json"
    monkeypatch.setattr(inv_mod, "_CC_SPAN_SETTINGS_PATH", legacy)

    pins = {"GH_TOKEN": "", "GITHUB_TOKEN": ""}
    first = inv_mod.cc_span_settings_path(pins)
    again = inv_mod.cc_span_settings_path(dict(reversed(list(pins.items()))))
    other = inv_mod.cc_span_settings_path({"GH_TOKEN": "", "GH_CONFIG_DIR": "/sealed"})

    assert first == again, "the same pins must map to the same file"
    assert first != other and first != str(legacy)
    assert not legacy.exists()
    data = json.loads(Path(first).read_text())
    assert data["env"] == pins
    assert data["hooks"]["PostToolUse"], "the dispatch hooks must stay registered"


def test_build_args_passes_the_pinned_settings_file(invoker, monkeypatch, tmp_path):
    """The file handed to --settings is the one carrying the credential pins."""
    import genesis.cc.invoker as inv_mod

    fake_repo, _hook = _fake_genesis_hook_repo(tmp_path)
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(fake_repo))
    monkeypatch.setattr(inv_mod, "_CC_SPAN_SETTINGS_PATH", tmp_path / "settings.json")

    args = invoker._build_args(CCInvocation(prompt="hi"))
    data = json.loads(Path(args[args.index("--settings") + 1]).read_text())
    assert data["env"] == {var: "" for var in inv_mod._GH_CREDENTIAL_ENV}


# ── Restored from main: function-hook env pins (dropped by an earlier merge) ──


def test_build_env_pins_function_hooks_off(invoker):
    """A dispatched session never runs plugin JS modules: an opt-in inherited
    from the server's env is overridden to 0 (genesis.cc.child_env)."""
    with patch.dict("os.environ", {"CLAUDE_CODE_ENABLE_FUNCTION_HOOKS": "1"}):
        env = invoker._build_env()
    assert env["CLAUDE_CODE_ENABLE_FUNCTION_HOOKS"] == "0"


def test_launch_env_repins_function_hooks_after_an_explicit_override(invoker):
    """Review: env_overrides are merged after the builder's pin, and the login
    fallback after that, so the pin is re-applied at the last gate."""
    inv = CCInvocation(prompt="hi", env_overrides={"CLAUDE_CODE_ENABLE_FUNCTION_HOOKS": "1"})
    env = invoker._build_env(inv)
    assert invoker._launch_env(env, inv)["CLAUDE_CODE_ENABLE_FUNCTION_HOOKS"] == "0"


def test_build_env_pins_function_hooks_off_when_unset(invoker, monkeypatch):
    """Unset is not off: Claude Code falls back to a server-side default, so
    the pin must be written even when nothing was inherited."""
    monkeypatch.delenv("CLAUDE_CODE_ENABLE_FUNCTION_HOOKS", raising=False)
    env = invoker._build_env()
    assert env["CLAUDE_CODE_ENABLE_FUNCTION_HOOKS"] == "0"




# ── cc.invocation_failed: one central failure event per CCError, then re-raise ──


class _FakeBus:
    def __init__(self):
        self.emit = AsyncMock()

    @property
    def events(self) -> list[tuple]:
        return [c.args + (c.kwargs,) for c in self.emit.await_args_list]


@pytest.fixture
def fail_bus(monkeypatch):
    """Route the invoker's runtime-bus lookup to a fake; reset the coalescer."""
    import genesis.cc.invoker as inv_mod

    bus = _FakeBus()
    monkeypatch.setattr(inv_mod, "_runtime_event_bus", lambda: bus)
    inv_mod._reset_failure_event_state()
    yield bus
    inv_mod._reset_failure_event_state()


def _failing_invoker(monkeypatch, exc: BaseException | None, *, preflight_exc=None):
    """A CCInvoker whose inner run raises ``exc`` (or succeeds when None)."""
    from genesis.cc.types import CCOutput

    invoker = CCInvoker(claude_path="/usr/bin/claude")

    async def _preflight(_inv):
        if preflight_exc is not None:
            raise preflight_exc

    async def _inner(*_a, **_k):
        if exc is not None:
            raise exc
        return CCOutput(
            session_id="s",
            text="ok",
            model_used="sonnet",
            cost_usd=0.0,
            input_tokens=0,
            output_tokens=0,
            duration_ms=1,
            exit_code=0,
        )

    monkeypatch.setattr(invoker, "_network_preflight", _preflight)
    monkeypatch.setattr(invoker, "_run_inner", _inner)
    monkeypatch.setattr(invoker, "_run_streaming_inner", _inner)
    return invoker


async def _call(invoker, entry: str, inv: CCInvocation):
    if entry == "run":
        return await invoker.run(inv)
    return await invoker.run_streaming(inv)


def _error_cases():
    from genesis.cc.exceptions import (
        CCMCPError,
        CCNetworkOfflineError,
        CCOverloadedError,
        CCParsingError,
        CCQuotaExhaustedError,
        CCRateLimitError,
        CCSessionError,
    )

    return [
        (CCRateLimitError("limit"), "warning"),
        # Limit family: a provider-capacity overload is expected and recovers.
        (CCOverloadedError("overloaded"), "warning"),
        (CCQuotaExhaustedError("quota"), "warning"),
        (CCTimeoutError("slow"), "error"),
        (CCProcessError("exit 1"), "error"),
        (CCStreamTruncatedError("dropped"), "error"),
        (CCParsingError("bad json"), "error"),
        (CCSessionError("expired"), "error"),
        (CCMCPError("mcp down"), "error"),
        (CCNetworkOfflineError("offline"), "error"),
    ]


def _case_id(value):
    return type(value).__name__ if isinstance(value, Exception) else str(value)


@pytest.mark.parametrize("entry", ["run", "run_streaming"])
@pytest.mark.parametrize(("exc", "severity"), _error_cases(), ids=_case_id)
async def test_invocation_failed_event_per_error_class(
    monkeypatch,
    fail_bus,
    entry,
    exc,
    severity,
):
    invoker = _failing_invoker(monkeypatch, exc)
    inv = CCInvocation(prompt="x", caller_tag="unit.test")
    with pytest.raises(type(exc)) as raised:
        await _call(invoker, entry, inv)
    assert raised.value is exc, "the original error must be re-raised unchanged"
    assert len(fail_bus.events) == 1
    subsystem, sev, event_type, _msg, payload = fail_bus.events[0]
    assert event_type == "cc.invocation_failed"
    assert str(subsystem) == "providers"
    assert str(sev) == severity
    assert payload["error_class"] == type(exc).__name__
    assert payload["streaming"] is (entry == "run_streaming")
    assert payload["caller_tag"] == "unit.test"
    assert payload["model"] == str(inv.model)
    assert "session_id" in payload
    assert payload["coalesced"] == 0
    # Raw error text (CLI stderr/stdout) is never persisted — only its length.
    assert str(exc) not in _msg
    assert str(exc) not in {str(v) for v in payload.values()}
    assert payload["error_text_omitted_chars"] == len(str(exc))


@pytest.mark.parametrize("entry", ["run", "run_streaming"])
async def test_invocation_failed_event_covers_network_preflight(monkeypatch, fail_bus, entry):
    from genesis.cc.exceptions import CCNetworkOfflineError

    pre = CCNetworkOfflineError("offline")
    invoker = _failing_invoker(monkeypatch, None, preflight_exc=pre)
    with pytest.raises(CCNetworkOfflineError):
        await _call(invoker, entry, CCInvocation(prompt="x"))
    assert [e[2] for e in fail_bus.events] == ["cc.invocation_failed"]
    assert fail_bus.events[0][4]["error_class"] == "CCNetworkOfflineError"


@pytest.mark.parametrize("entry", ["run", "run_streaming"])
async def test_invocation_failed_no_event_on_success(monkeypatch, fail_bus, entry):
    invoker = _failing_invoker(monkeypatch, None)
    out = await _call(invoker, entry, CCInvocation(prompt="x"))
    assert out.text == "ok"
    assert fail_bus.events == []


@pytest.mark.parametrize("entry", ["run", "run_streaming"])
async def test_invocation_failed_no_event_on_cancel(monkeypatch, fail_bus, entry):
    invoker = _failing_invoker(monkeypatch, asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await _call(invoker, entry, CCInvocation(prompt="x"))
    assert fail_bus.events == []


@pytest.mark.parametrize("entry", ["run", "run_streaming"])
async def test_invocation_failed_no_event_for_probe(monkeypatch, fail_bus, entry):
    from genesis.cc.exceptions import CCRateLimitError
    from genesis.cc.types import PROBE_CALLER_TAG

    invoker = _failing_invoker(monkeypatch, CCRateLimitError("limit"))
    with pytest.raises(CCRateLimitError):
        await _call(invoker, entry, CCInvocation(prompt="x", caller_tag=PROBE_CALLER_TAG))
    assert fail_bus.events == []


@pytest.mark.parametrize("entry", ["run", "run_streaming"])
async def test_invocation_failed_probe_malfunction_still_emits(monkeypatch, fail_bus, entry):
    """Only the probe's EXPECTED answer (limit/quota) is exempt; a probe that
    times out or loses its binary is a malfunction and must surface."""
    from genesis.cc.types import PROBE_CALLER_TAG

    invoker = _failing_invoker(monkeypatch, CCTimeoutError("slow"))
    with pytest.raises(CCTimeoutError):
        await _call(invoker, entry, CCInvocation(prompt="x", caller_tag=PROBE_CALLER_TAG))
    assert len(fail_bus.events) == 1
    assert fail_bus.events[0][4]["caller_tag"] == PROBE_CALLER_TAG


async def test_invocation_failed_untagged_callers_never_coalesce(monkeypatch, fail_bus):
    """Untagged calls come from unrelated subsystems; sharing a (class, None)
    key would suppress one subsystem's failure behind another's."""
    invoker = _failing_invoker(monkeypatch, CCTimeoutError("slow"))
    for _ in range(3):
        with pytest.raises(CCTimeoutError):
            await invoker.run(CCInvocation(prompt="x"))
    assert len(fail_bus.events) == 3
    assert all(e[4]["caller_tag"] is None for e in fail_bus.events)


async def test_invocation_failed_emit_failure_does_not_open_a_window(monkeypatch, fail_bus):
    """A bus fault must not record an emission that never happened."""
    fail_bus.emit.side_effect = [RuntimeError("bus broke"), None]
    invoker = _failing_invoker(monkeypatch, CCTimeoutError("slow"))
    for _ in range(2):
        with pytest.raises(CCTimeoutError):
            await invoker.run(CCInvocation(prompt="x", caller_tag="a"))
    assert fail_bus.emit.await_count == 2, "the retry inside the window was suppressed"


async def test_invocation_failed_coalesced_count_is_in_the_message(monkeypatch, fail_bus):
    """health_errors returns the message, not details — the count must be visible there."""
    import genesis.cc.invoker as inv_mod

    clock = {"t": 1000.0}
    monkeypatch.setattr(inv_mod.time, "monotonic", lambda: clock["t"])
    invoker = _failing_invoker(monkeypatch, CCTimeoutError("slow"))
    for _ in range(3):
        with pytest.raises(CCTimeoutError):
            await invoker.run(CCInvocation(prompt="x", caller_tag="a"))
    clock["t"] += 61.0
    with pytest.raises(CCTimeoutError):
        await invoker.run(CCInvocation(prompt="x", caller_tag="a"))
    assert "2 similar failure(s) coalesced" in fail_bus.events[-1][3]
    assert "coalesced" not in fail_bus.events[0][3]


@pytest.mark.parametrize("entry", ["run", "run_streaming"])
async def test_invocation_failed_no_bus_still_reraises(monkeypatch, entry):
    import genesis.cc.invoker as inv_mod

    monkeypatch.setattr(inv_mod, "_runtime_event_bus", lambda: None)
    inv_mod._reset_failure_event_state()
    exc = CCTimeoutError("slow")
    invoker = _failing_invoker(monkeypatch, exc)
    with pytest.raises(CCTimeoutError) as raised:
        await _call(invoker, entry, CCInvocation(prompt="x"))
    assert raised.value is exc


@pytest.mark.asyncio
async def test_error_result_overload_carries_the_real_turn_count(invoker):
    """Exit 0 with an is_error result: the classifier gets the result prose as
    text, but the status code and turn count come from the raw stdout result
    object — so the overload retry's replay guard sees the real turn count."""
    from genesis.cc.exceptions import CCReplayUnsafeError

    result_line = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "api_error_status": 529,
            "num_turns": 6,
            "result": "API Error: 529 Overloaded. This is a server-side issue.",
            "session_id": "sess-529",
            "total_cost_usd": 0.0,
            "duration_ms": 10,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }
    )
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(result_line.encode(), b""))
    mock_proc.returncode = 0

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        pytest.raises(CCReplayUnsafeError) as raised,
    ):
        await invoker.run(CCInvocation(prompt="hello"))
    assert raised.value.__cause__.num_turns == 6


@pytest.mark.asyncio
async def test_streaming_error_result_overload_carries_the_real_turn_count(
    invoker,
    monkeypatch,
):
    from genesis.cc.exceptions import CCReplayUnsafeError

    _no_host_syscalls(monkeypatch)
    ev = _error_result_event("API Error: 529 Overloaded. This is a server-side issue.")
    ev["api_error_status"] = 529
    ev["num_turns"] = 9
    data = _make_stream_lines({"type": "system", "subtype": "init", "session_id": "s1"}, ev)
    proc = _streaming_proc(data)
    proc.pid = 424207

    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        pytest.raises(CCReplayUnsafeError) as raised,
    ):
        await invoker.run_streaming(CCInvocation(prompt="x"))
    assert raised.value.__cause__.num_turns == 9


@pytest.mark.parametrize("streaming", [False, True])
async def test_error_result_preserves_mcp_evidence_from_actual_stderr(invoker, monkeypatch, streaming):
    from genesis.cc.exceptions import CCReplayUnsafeError
    from genesis.cc.peer_availability import mentions_mcp

    _no_host_syscalls(monkeypatch)
    ev = _error_result_event("API Error: 529 Overloaded")
    ev.update(api_error_status=529, num_turns=1)
    stderr = b"MCP backend returned 529"
    if streaming:
        proc = _streaming_proc(_make_stream_lines(ev))
        proc.stderr = _make_mock_stderr(stderr)
        proc.pid = 424207
    else:
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(json.dumps(ev).encode(), stderr))
        proc.returncode = 0
    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        pytest.raises(CCReplayUnsafeError) as raised,
    ):
        await _call(invoker, "run_streaming" if streaming else "run", CCInvocation(prompt="x"))
    assert raised.value.__cause__.num_turns == 1
    assert mentions_mcp(raised.value.__cause__)


async def test_invocation_failed_emit_error_does_not_mask_original(monkeypatch, fail_bus):
    fail_bus.emit.side_effect = RuntimeError("bus broke")
    exc = CCProcessError("exit 1")
    invoker = _failing_invoker(monkeypatch, exc)
    with pytest.raises(CCProcessError) as raised:
        await invoker.run(CCInvocation(prompt="x"))
    assert raised.value is exc


async def test_invocation_failed_coalesces_per_class_and_tag(monkeypatch, fail_bus):
    import genesis.cc.invoker as inv_mod

    clock = {"t": 1000.0}
    monkeypatch.setattr(inv_mod.time, "monotonic", lambda: clock["t"])
    invoker = _failing_invoker(monkeypatch, CCTimeoutError("slow"))

    for _ in range(3):  # same (class, tag) inside the window -> one event
        with pytest.raises(CCTimeoutError):
            await invoker.run(CCInvocation(prompt="x", caller_tag="a"))
    assert len(fail_bus.events) == 1

    with pytest.raises(CCTimeoutError):  # different tag -> its own event
        await invoker.run(CCInvocation(prompt="x", caller_tag="b"))
    assert len(fail_bus.events) == 2

    clock["t"] += 61.0  # window elapsed -> emits again, declaring the 2 swallowed
    with pytest.raises(CCTimeoutError):
        await invoker.run(CCInvocation(prompt="x", caller_tag="a"))
    assert len(fail_bus.events) == 3
    assert fail_bus.events[-1][4]["caller_tag"] == "a"
    assert fail_bus.events[-1][4]["coalesced"] == 2


def test_failure_event_bus_lookup_never_constructs_a_runtime():
    """Observability must not lazily build a blank runtime singleton."""
    import genesis.cc.invoker as inv_mod
    from genesis.runtime import GenesisRuntime

    saved = GenesisRuntime._instance
    GenesisRuntime._instance = None
    try:
        assert inv_mod._runtime_event_bus() is None
        assert GenesisRuntime._instance is None
    finally:
        GenesisRuntime._instance = saved


@pytest.mark.parametrize("entry", ["run", "run_streaming"])
async def test_invocation_failed_attributes_and_keys_by_routed_model(monkeypatch, fail_bus, entry):
    """Two sessions with one caller_tag but different roster routes are
    different outages: neither may be coalesced behind the other, and the
    event names the routed model, not just the requested tier."""
    from dataclasses import replace as dc_replace

    import genesis.cc.invoker as inv_mod

    def _apply_active(inv):
        if inv.prompt == "peer":
            return dc_replace(inv, model_id_override="peer-model"), "peer-model"
        return inv, "claude"

    monkeypatch.setattr(inv_mod.roster, "apply_active", _apply_active)
    invoker = _failing_invoker(monkeypatch, CCTimeoutError("slow"))
    for prompt in ("native", "peer"):
        with pytest.raises(CCTimeoutError):
            await _call(
                invoker, entry, CCInvocation(prompt=prompt, caller_tag="direct_session.observe")
            )
    assert [e[4]["roster_model"] for e in fail_bus.events] == ["claude", "peer-model"]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"model_id_override": "peer-x"}, "peer-x"),
        ({"anthropic_base_url": "https://peer.invalid"}, "routed"),
        ({}, "claude"),
    ],
)
async def test_invocation_failed_attributes_pre_routed_non_roster_calls(
    monkeypatch,
    fail_bus,
    overrides,
    expected,
):
    """A call pre-stamped with peer overrides but roster_eligible=False (e.g. the
    fallback probe of a peer) is routed to that peer even though apply_active
    reports native; the event must attribute and key it to the peer."""
    invoker = _failing_invoker(monkeypatch, CCTimeoutError("slow"))
    with pytest.raises(CCTimeoutError):
        await invoker.run(CCInvocation(prompt="x", caller_tag="t", **overrides))
    assert fail_bus.events[0][4]["roster_model"] == expected


def test_settings_env_pins_pin_the_bash_allowlist_itself(monkeypatch):
    """The guard reads GENESIS_BASH_ALLOWLIST; a settings file must not be able
    to empty or widen it after the launch checks, so it is pinned with exactly
    the value _build_env exports."""
    import genesis.cc.invoker as inv_mod

    monkeypatch.setitem(inv_mod._BINARY_HARDENING, "gh", lambda: {"GH_CONFIG_DIR": "/sealed"})
    pins = inv_mod._settings_env_pins(("gh", "jq"))
    assert pins["GENESIS_BASH_ALLOWLIST"] == "gh,jq"
    assert "GENESIS_BASH_ALLOWLIST" not in inv_mod._settings_env_pins(())


def test_allowlist_pin_matches_the_launch_env(invoker, monkeypatch):
    """Pinned value and exported value come from one expression; assert they agree."""
    import genesis.cc.invoker as inv_mod

    monkeypatch.setitem(inv_mod._BINARY_HARDENING, "gh", lambda: {"GH_CONFIG_DIR": "/sealed"})
    inv = CCInvocation(prompt="hi", bash_allowlist=("gh", "jq"))
    env = invoker._build_env(inv)
    assert inv_mod._settings_env_pins(tuple(inv.bash_allowlist))["GENESIS_BASH_ALLOWLIST"] == env["GENESIS_BASH_ALLOWLIST"]


def test_build_args_uses_precomputed_pins_without_recomputing(invoker, monkeypatch, tmp_path):
    """The async run paths compute the pins in a worker thread and hand them in;
    _build_args must not redo that work on the event loop."""
    import genesis.cc.invoker as inv_mod

    fake_repo, _hook = _fake_genesis_hook_repo(tmp_path)
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(fake_repo))
    monkeypatch.setattr(inv_mod, "_CC_SPAN_SETTINGS_PATH", tmp_path / "settings.json")

    def _must_not_run(*_a, **_k):
        raise AssertionError("pins recomputed on the event loop")

    monkeypatch.setattr(inv_mod, "_settings_env_pins", _must_not_run)
    pins = {"GH_TOKEN": "", "PROBE": "x"}
    args = invoker._build_args(CCInvocation(prompt="hi"), settings_pins=pins)
    assert json.loads(Path(args[args.index("--settings") + 1]).read_text())["env"] == pins


async def test_run_paths_compute_pins_in_a_worker_thread(invoker, monkeypatch):
    """Both run paths hand _settings_env_pins to asyncio.to_thread."""
    import inspect

    import genesis.cc.invoker as inv_mod

    for name in ("_run_inner", "_run_streaming_inner"):
        src = inspect.getsource(getattr(inv_mod.CCInvoker, name))
        assert "asyncio.to_thread(_settings_env_pins" in src, name
        assert "settings_pins=pins" in src, name
        assert "verify_allowlist_enforceable(invocation, settings_pins=pins)" in src, name


def test_verify_checks_the_settings_file_built_from_the_launch_pins(invoker, monkeypatch):
    """The binding check must read the file built from the SAME pins the
    launch used, not recompute them (a recomputation can hash to another file)."""
    import genesis.cc.invoker as inv_mod

    def _must_not_run(*_a, **_k):
        raise AssertionError("pins recomputed in the binding check")

    seen = []
    monkeypatch.setattr(inv_mod, "_settings_env_pins", _must_not_run)
    monkeypatch.setattr(inv_mod, "cc_span_settings_path", lambda pins=None: seen.append(pins))
    pins = {"GH_TOKEN": "", "GENESIS_BASH_ALLOWLIST": "gh"}
    inv = CCInvocation(prompt="hi", bash_allowlist=("gh",))
    with pytest.raises(RuntimeError, match="could not be written"):
        invoker._verify_allowlist_enforceable_blocking(inv, pins)
    assert seen == [pins]


def test_reconcile_gh_seal_does_not_hang_boot_on_a_held_lock(monkeypatch, tmp_path, caplog):
    """Boot reconcile takes the seal lock non-blocking: a lock held by a stuck
    process must not hang server startup. It logs and leaves the seal as it was."""
    import fcntl
    import logging
    import threading

    import genesis.cc.invoker as inv_mod

    target = tmp_path / "gh-sealed"
    target.mkdir()
    (target / "config.yml").write_text(inv_mod._SEALED_GH_CONFIG_YML, encoding="utf-8")
    (target / "hosts.yml").write_text("github.com:\n  oauth_token: LEAKED\n", encoding="utf-8")
    monkeypatch.setattr(inv_mod, "_SEALED_GH_CONFIG_DIR", target)

    lock_path = target.parent / f"{target.name}.lock"
    with open(lock_path, "a+") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        done = threading.Event()
        worker = threading.Thread(target=lambda: (inv_mod.reconcile_gh_seal(), done.set()))
        with caplog.at_level(logging.WARNING, logger=inv_mod.logger.name):
            worker.start()
            finished = done.wait(timeout=10)
        assert finished, "boot reconcile blocked on a held seal lock"
        worker.join(timeout=5)
    assert (target / "hosts.yml").exists(), "nothing may change while another holder has the lock"
    assert "could not be reconciled" in caplog.text
