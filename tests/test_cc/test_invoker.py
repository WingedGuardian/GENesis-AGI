"""Tests for CCInvoker."""

import asyncio
import json
import logging
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.cc.exceptions import CCProcessError, CCStreamTruncatedError, CCTimeoutError
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
        lambda: "/tmp/cc-span-settings.json",
    )
    args = invoker._build_args(CCInvocation(prompt="hi"))
    assert "--settings" in args
    assert args[args.index("--settings") + 1] == "/tmp/cc-span-settings.json"


def test_build_args_omits_span_settings_when_unavailable(invoker, monkeypatch):
    """No --settings when the span-hook file can't be generated (None)."""
    import genesis.cc.invoker as inv_mod

    monkeypatch.setattr(inv_mod, "cc_span_settings_path", lambda: None)
    args = invoker._build_args(CCInvocation(prompt="hi"))
    assert "--settings" not in args


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
        assert ["-p", prop] == probe_argv[
            probe_argv.index(prop) - 1 : probe_argv.index(prop) + 1
        ]
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
        return real_subprocess.CompletedProcess(
            args[0], seq[len(calls) - 1], b"", b"bus error"
        )

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

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
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
            self._i += 1                      # consumed BEFORE raising
            self.reads.append(idx)
            if idx in raise_on:
                raise ValueError(
                    "Separator is not found, and chunk exceed the limit"
                )
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
    invoker, monkeypatch,
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
    assert 1 in proc.stdout.reads, proc.stdout.reads   # the fault was reached


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
    proc = _streaming_proc(data, raise_on=(1,))   # the RESULT line is dropped

    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        pytest.raises(CCStreamTruncatedError, match="NO result event"),
    ):
        await asyncio.wait_for(
            invoker.run_streaming(CCInvocation(prompt="x")), timeout=10
        )

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
    proc = _streaming_proc(data, raise_on=(1,))   # drop the assistant line
    fired = []

    async def _spy(*a, **k):
        fired.append(a)

    invoker._fire_empty_output_callback = _spy

    with (
        patch("asyncio.create_subprocess_exec", return_value=proc),
        pytest.raises(CCStreamTruncatedError),
    ):
        await invoker.run_streaming(CCInvocation(prompt="x", expect_output=True))

    assert 1 in proc.stdout.reads, proc.stdout.reads   # the fault was reached
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
    def _killpg_gone(*a):
        raise ProcessLookupError  # vacant group — never live-fire a real probe

    monkeypatch.setattr("genesis.util.proc_kill.os.killpg", _killpg_gone)
    data = _make_stream_lines(
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "oversized"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "keep me"}]}},
    )
    # index 1 = the oversized tool-result trace; no result event ever arrives.
    proc = _streaming_proc(data, raise_on=(1,))
    proc.stderr = _make_mock_stderr(
        b"Background tasks still running after 600s; terminating.\n"
    )
    proc.pid = 4242

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        output = await invoker.run_streaming(CCInvocation(prompt="x", expect_output=True))

    assert 1 in proc.stdout.reads, proc.stdout.reads  # the drop really happened
    assert output.text == "keep me", "the partial deliverable was discarded"
    assert output.bg_truncated is True, "the truncation notice was lost"


@pytest.mark.asyncio
async def test_an_empty_result_without_a_drop_is_still_the_cap_signature(
    invoker, monkeypatch
):
    """CLAUSE COVER for `oversized_dropped` in the result guard.

    Empty output with NO drop is the unexplained-empty shape the silent-cap
    detector exists to aggregate. Only a DROP explains it away. Without this,
    deleting that clause — so any empty result raises — passes the suite, and
    the cap detector goes permanently silent behind an exception.
    """
    def _killpg_gone(*a):
        raise ProcessLookupError  # vacant group — never live-fire a real probe

    monkeypatch.setattr("genesis.util.proc_kill.os.killpg", _killpg_gone)
    data = _make_stream_lines(
        {"type": "system", "subtype": "init", "session_id": "s1"},
        _result_event(""),
    )
    proc = _streaming_proc(data)  # nothing dropped
    proc.pid = 4242  # never leave pid to a mock default near killpg
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
    def _killpg_gone(*a):
        raise ProcessLookupError  # vacant group — never live-fire a real probe

    monkeypatch.setattr("genesis.util.proc_kill.os.killpg", _killpg_gone)
    data = _make_stream_lines(
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "answer"}]}},
    )
    proc = _streaming_proc(data)  # nothing dropped, no result event
    proc.pid = 4242  # never leave pid to a mock default near killpg

    with patch("asyncio.create_subprocess_exec", return_value=proc):
        output = await invoker.run_streaming(CCInvocation(prompt="x"))

    assert output.text == "answer"


@pytest.mark.asyncio
async def test_a_drop_with_surviving_text_still_raises_without_bg_truncation(
    invoker, monkeypatch
):
    """CLAUSE COVER for the `bg_truncated` conjunct of the exemption.

    Surviving partial text is NOT on its own a reason to forgive a missing
    result — the exemption exists for a background run killed at the wait
    ceiling, which is what makes the absence explainable. Drop the
    ``bg_truncated`` conjunct and any run with leftover text goes quiet.
    """
    def _killpg_gone(*a):
        raise ProcessLookupError  # vacant group — never live-fire a real probe

    monkeypatch.setattr("genesis.util.proc_kill.os.killpg", _killpg_gone)
    data = _make_stream_lines(
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "oversized"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "keep me"}]}},
    )
    proc = _streaming_proc(data, raise_on=(1,))
    proc.pid = 4242  # never leave pid to a mock default near killpg

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
    def _killpg_gone(*a):
        raise ProcessLookupError

    monkeypatch.setattr("genesis.util.proc_kill.os.killpg", _killpg_gone)
    data = _make_stream_lines(
        {"type": "system", "subtype": "init", "session_id": "s1"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "the answer"}]}},
    )
    proc = _streaming_proc(data, raise_on=(1,))  # the only text line is dropped
    proc.stderr = _make_mock_stderr(
        b"Background tasks still running after 600s; terminating.\n"
    )
    proc.pid = 4242

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
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "tool_use", "name": "Read", "input": {}},
        ]}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "hi"},
            {"type": "tool_use", "name": "Bash", "input": {}},
        ]}},
        {
            "type": "result", "subtype": "success", "is_error": False,
            "result": "done", "session_id": "s9", "total_cost_usd": 0.01,
            "duration_ms": 100, "usage": {"input_tokens": 1, "output_tokens": 1},
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
        {"type": "assistant", "message": {"content": [
            {"type": "redacted_thinking", "data": "x"},
            {"type": "text", "text": "hi"},
        ]}},
        {
            "type": "result", "subtype": "success", "is_error": False,
            "result": "done", "session_id": "s10", "total_cost_usd": 0.01,
            "duration_ms": 100, "usage": {"input_tokens": 1, "output_tokens": 1},
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
