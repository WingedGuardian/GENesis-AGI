"""WindowsEndpointAdapter — transport, reachability, refusals, and lifecycle.

Exercised against a FAKE implementing the real contract
(``send(path, data, method) -> {output, stderr, exit_code}``) so ordering and
refusal properties are provable without a machine on the other end.

What a fake CANNOT prove is recorded honestly rather than implied: it accepts
any remote path, so the state-dir creation and the encoding of the return path
were both found by a LIVE run, not here. Those live checks are a separate
harness; these are the properties that must hold on any install.
"""
from __future__ import annotations

import asyncio
import base64
import json

import pytest

from genesis.modules.endpoint.adapter import (
    EndpointNotReachable,
    MissionTooLarge,
    WindowsEndpointAdapter,
)
from genesis.modules.external.config import ProgramConfig

_STATE = r"C:\Users\X\AppData\Local\Genesis\desktop"
_MISSION = r"powershell -NoProfile -File C:\scripts\genesis-act.ps1"
# The reachability probe the adapter builds. Named here so a test cannot key on
# a substring that also matches an unrelated command.
_HEALTH_PROBE = 'powershell -NoProfile -Command "exit 0"'


def _cfg(**over):
    endpoint = {
        "state_dir": _STATE,
        "mission_command": _MISSION,
        "machine_id": "MACHINE-GUID-1",
    }
    endpoint.update(over.pop("endpoint", {}))
    data = {
        "name": over.pop("name", "Test Endpoint"),
        "ipc": {"method": "ssh", "ssh_host": over.pop("ssh_host", "u@100.64.0.1")},
        "endpoint": endpoint,
    }
    data.update(over)
    return ProgramConfig.from_dict(data)


def _b64_json(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")


class FakeIPC:
    """Implements the SshIPCAdapter contract used by the adapter."""

    def __init__(self, *, result=None, fail_on=None, exit_code_on=None, raw_result=None):
        self.calls: list[str] = []
        self._result = result
        self._raw = raw_result
        self._fail_on = fail_on
        self._exit_on = exit_code_on

    async def send(self, path, data=None, method="GET"):
        self.calls.append(path)
        if self._fail_on and self._fail_on in path:
            return {"error": f"boom in {self._fail_on}"}
        if self._exit_on and self._exit_on in path:
            return {"output": "", "stderr": "remote said no", "exit_code": 1}
        if "ToBase64String" in path:
            if self._raw is not None:
                return {"output": self._raw, "stderr": None, "exit_code": 0}
            body = "" if self._result is None else _b64_json(self._result)
            return {"output": body, "stderr": None, "exit_code": 0}
        if path == _HEALTH_PROBE:                          # the health probe
            return {"output": "", "stderr": None, "exit_code": 0}
        return {"output": "", "stderr": None, "exit_code": 0}


def _adapter(cfg=None, ipc=None, *, enabled=True, healthy=True, result=None):
    a = WindowsEndpointAdapter(cfg or _cfg())
    a._ipc = ipc or FakeIPC(result=result if result is not None else {"ok": True})
    a._enabled = enabled
    a._healthy = healthy
    return a


# ── construction refuses an unusable config ─────────────────────────────────
def test_missing_endpoint_block_is_refused():
    cfg = ProgramConfig.from_dict(
        {"name": "no-ep", "ipc": {"method": "ssh", "ssh_host": "u@10.0.0.1"}}
    )
    with pytest.raises(ValueError, match="no `endpoint:` config block"):
        WindowsEndpointAdapter(cfg)


@pytest.mark.parametrize("missing", ["state_dir", "mission_command", "machine_id"])
def test_each_required_endpoint_field_is_refused_when_absent(missing):
    with pytest.raises(ValueError, match=missing):
        WindowsEndpointAdapter(_cfg(endpoint={missing: None}))


@pytest.mark.parametrize(
    "bad_char",
    ["'", "\u2019", "\u2018", '"', "%", "\n", "\r", "\t"],
    ids=["apostrophe", "rsquo", "lsquo", "dquote", "percent", "lf", "cr", "tab"],
)
def test_dangerous_characters_in_state_dir_are_refused(bad_char):
    """Each measured on hardware, not guessed.

    U+2019 terminates a PowerShell single-quoted literal (verified: "The string
    is missing the terminator"); a double quote closes cmd.exe's own quoting
    around -Command; and cmd.exe expands %VAR% before PowerShell sees the path
    (verified: a path containing '%USERNAME%' arrived with the variable already
    expanded to the account name).
    """
    with pytest.raises(ValueError, match="state_dir"):
        WindowsEndpointAdapter(_cfg(endpoint={"state_dir": f"C:\\a{bad_char}b"}))


@pytest.mark.parametrize("ok_char", ["`", "$", " ", "&", "|"])
def test_characters_inert_inside_a_single_quoted_literal_are_allowed(ok_char):
    """Refusing these would reject legitimate paths for no gain.

    Backtick and $ are inert inside a PowerShell single-quoted literal; & and |
    are safe precisely BECAUSE the cmd.exe layer is quoted — which is why the
    double quote is the character that actually matters.
    """
    WindowsEndpointAdapter(_cfg(endpoint={"state_dir": f"C:\\a{ok_char}b"}))


def test_mission_command_is_not_quote_restricted():
    """It is passed VERBATIM to the remote shell, not embedded in a literal.

    Quote-restricting it would reject a legitimate config such as
    `powershell -File 'C:\\my scripts\\act.ps1'`.
    """
    a = WindowsEndpointAdapter(
        _cfg(endpoint={"mission_command": "powershell -File 'C:\\my scripts\\act.ps1'"})
    )
    assert "'" in a._ep.mission_command


# ── reachability fails closed ───────────────────────────────────────────────
async def test_tailnet_address_classifies_as_tailnet():
    assert await _adapter().resolve_network() == "tailnet"


async def test_rfc1918_address_classifies_as_lan():
    assert await _adapter(_cfg(ssh_host="u@192.168.1.10")).resolve_network() == "lan"


async def test_public_address_is_refused():
    with pytest.raises(EndpointNotReachable, match="neither the LAN nor the tailnet"):
        await _adapter(_cfg(ssh_host="u@8.8.8.8")).resolve_network()


async def test_unresolvable_host_is_refused_not_allowed_through():
    with pytest.raises(EndpointNotReachable, match="cannot resolve"):
        await _adapter(_cfg(ssh_host="u@no-such-host.invalid")).resolve_network()


async def test_a_name_on_two_different_networks_is_refused_not_guessed(monkeypatch):
    """getaddrinfo ordering is not stable, so picking the first is fail-open by luck."""
    async def fake_getaddrinfo(self, host, port, **kw):
        return [
            (0, 0, 0, "", ("192.168.1.5", 0)),
            (0, 0, 0, "", ("8.8.8.8", 0)),
        ]
    loop_cls = asyncio.get_running_loop().__class__
    monkeypatch.setattr(loop_cls, "getaddrinfo", fake_getaddrinfo, raising=False)
    a = _adapter(_cfg(ssh_host="u@ambiguous.example"))
    with pytest.raises(EndpointNotReachable, match="more than one network"):
        await a.resolve_network()


async def test_network_not_in_allowed_list_is_refused():
    a = _adapter(_cfg(ssh_host="u@192.168.1.10", endpoint={"allowed_networks": ["tailnet"]}))
    with pytest.raises(EndpointNotReachable, match="allowed_networks"):
        await a.check_network_allowed()


async def test_ipv4_mapped_v6_lan_address_classifies_as_lan():
    """Pins behaviour; does NOT prove the unwrap.

    MEASURED: this passes with or without the unwrap, because RFC1918
    privateness survives the v4-mapping. Labelled honestly — the tailnet case
    below is the one that flips, and treating this as evidence would be a false
    control.
    """
    assert await _adapter(_cfg(ssh_host="u@::ffff:192.168.1.10")).resolve_network() == "lan"


async def test_ipv4_mapped_v6_tailnet_address_classifies_as_tailnet():
    """The case the unwrap exists for — VERIFIED RED without it."""
    assert await _adapter(_cfg(ssh_host="u@::ffff:100.64.0.1")).resolve_network() == "tailnet"


async def test_loopback_counts_as_lan():
    assert await _adapter(_cfg(ssh_host="u@127.0.0.1")).resolve_network() == "lan"


# ── lifecycle gates: the base class's, which this method may not opt out of ──
async def test_a_disabled_module_does_not_touch_the_device():
    ipc = FakeIPC(result={"ok": True})
    out = await _adapter(ipc=ipc, enabled=False).dispatch_mission({"a": 1})
    assert "disabled" in out["error"]
    assert ipc.calls == []


async def test_a_module_whose_machine_fails_its_probe_does_not_dispatch():
    """The gate now REFRESHES before reading, so this needs a machine that fails.

    A stale True from registration is exactly what the refresh exists to catch:
    with no health_check block configured, register() sets healthy without ever
    probing.
    """
    ipc = FakeIPC(exit_code_on='Command "exit 0"')
    out = await _adapter(ipc=ipc, healthy=True).dispatch_mission({"a": 1})
    assert "not healthy" in out["error"]
    assert not any("FromBase64String" in c for c in ipc.calls), "no payload was written"


async def test_a_stale_healthy_flag_is_refreshed_rather_than_trusted():
    """register() sets _healthy True unprobed; dispatch must not rely on it."""
    ipc = FakeIPC(exit_code_on='Command "exit 0"')
    a = _adapter(ipc=ipc, healthy=True)
    assert a.healthy is True
    await a.dispatch_mission({"a": 1})
    assert a.healthy is False, "the gate read a stale flag instead of refreshing"


async def test_a_forbidden_network_is_refused_before_touching_the_device():
    """The gate must be consulted by the PAYLOAD path, not merely exist."""
    ipc = FakeIPC(result={"ok": True})
    a = _adapter(
        _cfg(ssh_host="u@192.168.1.10", endpoint={"allowed_networks": ["tailnet"]}), ipc=ipc
    )
    with pytest.raises(EndpointNotReachable):
        await a.dispatch_mission({"a": 1})
    assert ipc.calls == [], (
        "a host the config forbids must not be contacted at all — not even to "
        "health-check it"
    )


# ── health asks about the MACHINE, not about Claude Code ────────────────────
async def test_health_probes_reachability_not_claude_and_not_the_state_dir():
    """Reachability only.

    Not `claude --version` (the inherited probe, which asks about a Claude Code
    install rather than a machine), and NOT "does state_dir exist" — prepare
    creates that directory, so requiring it would deadlock a fresh machine.
    """
    ipc = FakeIPC(result={"ok": True})
    a = _adapter(ipc=ipc, healthy=False)
    assert await a.check_health() is True
    assert _HEALTH_PROBE in ipc.calls
    assert not any("--version" in c for c in ipc.calls)
    assert not any("Test-Path" in c for c in ipc.calls), (
        "health must not depend on the state dir the prepare step creates"
    )


async def test_health_is_false_when_the_machine_cannot_run_a_command():
    a = _adapter(ipc=FakeIPC(exit_code_on='Command "exit 0"'), healthy=True)
    assert await a.check_health() is False
    assert a.last_health_error


async def test_a_transport_exception_during_health_does_not_propagate():
    class Boom:
        async def send(self, *a, **k):
            raise RuntimeError("ssh exploded")
    a = _adapter(ipc=Boom(), healthy=True)
    assert await a.check_health() is False


# ── payload budget is derived, and enforced by REFUSAL ──────────────────────
def test_budget_is_derived_from_the_real_command_length():
    short = _adapter(_cfg(endpoint={"state_dir": r"C:\g"}))
    long = _adapter(_cfg(endpoint={"state_dir": "C:\\" + "d" * 200}))
    assert short.payload_budget() > long.payload_budget()


async def test_oversized_payload_is_refused_with_both_numbers():
    a = _adapter()
    budget = a.payload_budget()
    with pytest.raises(MissionTooLarge) as exc:
        await a.dispatch_mission({"blob": "x" * (budget + 100)})
    assert "Refusing rather than truncating" in str(exc.value)
    assert str(budget) in str(exc.value)


def test_a_state_dir_that_overflows_a_payload_free_command_is_refused_at_construction():
    """The prepare command embeds state_dir THREE times and carries no payload.

    So a state_dir short enough to leave payload room in the WRITE command (which
    embeds it once) can still overflow a fixed-size one. MEASURED against the
    exact command strings: prepare overflows at 2617, read-result at 4004.
    Caught at construction, where the message can name the real cause.
    """
    with pytest.raises(ValueError, match="state_dir is too long"):
        WindowsEndpointAdapter(_cfg(endpoint={"state_dir": "C:\\" + "d" * 3000}))


def test_a_constructible_endpoint_always_has_a_usable_budget():
    """The invariant that actually holds, swept rather than spot-checked.

    Deliberately NOT a test that the payload-floor backstop fires: MEASURED, it
    cannot be reached, because the prepare command embeds state_dir three times
    and overflows at 2617 while the floor needs 7707. Asserting an unreachable
    branch would be a test that passes for the wrong reason.

    Sweeping the range also means this keeps holding if the command shapes
    change, where a single boundary number would silently stop testing anything.
    """
    constructed = refused = 0
    for n in range(10, 3600, 100):
        try:
            a = _adapter(_cfg(endpoint={"state_dir": "C:\\" + "d" * n}))
        except ValueError:
            refused += 1
            continue
        constructed += 1
        assert a.payload_budget() >= 256, f"state_dir of {n} constructed but is unusable"
    assert constructed and refused, (
        f"the sweep must cover BOTH sides of the limit "
        f"(constructed={constructed}, refused={refused})"
    )


# ── dispatch ordering and failure reporting ─────────────────────────────────
async def test_stale_result_is_cleared_before_the_payload_is_written():
    ipc = FakeIPC(result={"ok": True})
    await _adapter(ipc=ipc).dispatch_mission({"a": 1})
    clear = next(i for i, c in enumerate(ipc.calls) if "CreateDirectory" in c)
    write = next(i for i, c in enumerate(ipc.calls) if "FromBase64String" in c)
    run = next(i for i, c in enumerate(ipc.calls) if c == _MISSION)
    read = next(i for i, c in enumerate(ipc.calls) if "ToBase64String" in c)
    assert clear < write < run < read


async def test_payload_round_trips_through_base64():
    ipc = FakeIPC(result={"ok": True})
    payload = {"action": "type", "text": "hello — ünicode ✓"}
    await _adapter(ipc=ipc).dispatch_mission(payload)
    blob = next(c for c in ipc.calls if "FromBase64String" in c)
    b64 = blob.split("FromBase64String('")[1].split("')")[0]
    sent = json.loads(base64.b64decode(b64).decode("utf-8"))
    assert sent["text"] == payload["text"]
    assert "mission_id" in sent


async def test_a_failed_clear_aborts_before_writing_anything():
    ipc = FakeIPC(fail_on="CreateDirectory")
    out = await _adapter(ipc=ipc).dispatch_mission({"a": 1})
    assert "could not prepare the endpoint state directory" in out["error"]
    assert not any("FromBase64String" in c for c in ipc.calls)


async def test_a_nonzero_clear_exit_code_aborts_too():
    """`error` is transport-only; a remote refusal arrives as an exit code.

    MEASURED: deleting a locked file under -ErrorAction SilentlyContinue prints
    nothing and still exits 1 — so the signal exists and only an unchecked
    caller loses it, leaving a stale result to be read as this mission's.
    """
    ipc = FakeIPC(exit_code_on="CreateDirectory")
    out = await _adapter(ipc=ipc).dispatch_mission({"a": 1})
    assert "could not prepare the endpoint state directory" in out["error"]
    assert not any("FromBase64String" in c for c in ipc.calls)


async def test_a_failed_write_does_not_run_the_mission():
    ipc = FakeIPC(fail_on="FromBase64String")
    out = await _adapter(ipc=ipc).dispatch_mission({"a": 1})
    assert out["error"] == "failed to write mission payload"
    assert _MISSION not in ipc.calls


async def test_a_nonzero_write_exit_code_does_not_run_the_mission():
    ipc = FakeIPC(exit_code_on="FromBase64String")
    out = await _adapter(ipc=ipc).dispatch_mission({"a": 1})
    assert out["error"] == "failed to write mission payload"
    assert _MISSION not in ipc.calls


async def test_a_failed_mission_run_is_reported():
    ipc = FakeIPC(fail_on=_MISSION)
    out = await _adapter(ipc=ipc).dispatch_mission({"a": 1})
    assert "mission dispatch failed" in out["error"]


async def test_a_failed_reply_read_is_reported():
    ipc = FakeIPC(fail_on="ToBase64String")
    out = await _adapter(ipc=ipc).dispatch_mission({"a": 1})
    assert "could not be read" in out["error"]


async def test_an_empty_result_is_an_error_not_a_silent_success():
    out = await _adapter(ipc=FakeIPC(result=None)).dispatch_mission({"a": 1})
    assert "no result.json" in out["error"]


async def test_a_non_base64_reply_is_reported():
    out = await _adapter(ipc=FakeIPC(raw_result="not base64!!")).dispatch_mission({"a": 1})
    assert "valid base64" in out["error"]


async def test_a_utf8_bom_in_the_reply_is_tolerated():
    """PowerShell 5.1's `Set-Content -Encoding utf8` writes one; json rejects it."""
    body = base64.b64encode(b"\xef\xbb\xbf" + json.dumps({"ok": True}).encode()).decode()
    out = await _adapter(ipc=FakeIPC(raw_result=body)).dispatch_mission({"a": 1})
    assert out["ok"] is True


async def test_unparseable_reply_is_reported_as_such():
    body = base64.b64encode(b"not json").decode()
    out = await _adapter(ipc=FakeIPC(raw_result=body)).dispatch_mission({"a": 1})
    assert "not valid UTF-8 JSON" in out["error"]


async def test_the_mission_exit_code_survives_onto_a_successful_reply():
    out = await _adapter(ipc=FakeIPC(result={"ok": True})).dispatch_mission({"a": 1})
    assert out["mission_exit_code"] == 0


# ── correlation and concurrency ─────────────────────────────────────────────
async def test_a_reply_from_a_different_mission_is_refused():
    ipc = FakeIPC(result={"ok": True, "mission_id": "some-other-mission"})
    out = await _adapter(ipc=ipc).dispatch_mission({"a": 1})
    assert "different mission" in out["error"]


async def test_a_device_that_does_not_echo_the_id_still_works():
    """Back-compat: today's device scripts predate mission_id."""
    out = await _adapter(ipc=FakeIPC(result={"ok": True})).dispatch_mission({"a": 1})
    assert out["ok"] is True


async def test_concurrent_dispatches_do_not_interleave():
    """Without the lock, two missions overwrite each other's request.json and
    both read the same result.json — each returning the other's answer."""
    order: list[str] = []

    class SlowIPC(FakeIPC):
        async def send(self, path, data=None, method="GET"):
            if "FromBase64String" in path:
                order.append("write-start")
                await asyncio.sleep(0.02)
                order.append("write-end")
            return await super().send(path, data, method)

    a = _adapter(ipc=SlowIPC(result={"ok": True}))
    await asyncio.gather(a.dispatch_mission({"n": 1}), a.dispatch_mission({"n": 2}))
    # Perfectly nested (start,end,start,end) — never interleaved.
    assert order == ["write-start", "write-end", "write-start", "write-end"]


def test_machine_id_is_exposed_and_is_not_a_hostname():
    assert _adapter().machine_id == "MACHINE-GUID-1"


# ── the health override must be REACHABLE from the runtime, not just present ──
async def test_the_cached_health_path_uses_the_endpoint_probe():
    """Every runtime caller uses check_health_cached, never check_health.

    The base class's cached path calls ``self._ipc.health_check(...)`` directly,
    so overriding only ``check_health`` leaves the endpoint probe unreachable —
    present, tested in isolation, and never once executed by the dashboard or
    the career-outreach tick. This test is the difference between built and
    wired.
    """
    ipc = FakeIPC(result={"ok": True})
    a = _adapter(ipc=ipc, healthy=False)
    assert await a.check_health_cached() is True
    assert _HEALTH_PROBE in ipc.calls, "the endpoint probe never ran"
    assert not any("--version" in c for c in ipc.calls)


async def test_the_cached_health_path_still_caches():
    ipc = FakeIPC(result={"ok": True})
    a = _adapter(ipc=ipc, healthy=False)
    await a.check_health_cached()
    first = len(ipc.calls)
    await a.check_health_cached()
    assert len(ipc.calls) == first, "a second call inside the TTL must not re-probe"


# ── deliberate asymmetries and config refusals, pinned so they stay deliberate ──
async def test_a_nonzero_mission_exit_still_reads_the_reply():
    """Unlike prepare and write, a non-zero MISSION exit is NOT fatal.

    The device can legitimately exit non-zero AND write a result.json explaining
    why — a refusal is a result, not a transport failure — and the code is
    carried up as mission_exit_code. Asserted because the two steps immediately
    above DO treat a non-zero exit as fatal, so a consistency pass would
    otherwise "fix" this into silence.
    """
    ipc = FakeIPC(result={"ok": False, "why": "no such window"}, exit_code_on=_MISSION)
    out = await _adapter(ipc=ipc).dispatch_mission({"a": 1})
    assert out["why"] == "no such window"
    assert out["mission_exit_code"] == 1


async def test_the_health_probe_carries_its_own_short_budget():
    """Not the module's work timeout: the modules page probes serially."""
    seen = {}

    class BudgetIPC(FakeIPC):
        async def send(self, path, data=None, method="GET"):
            if path == _HEALTH_PROBE:
                seen["timeout_s"] = (data or {}).get("timeout_s")
            return await super().send(path, data, method)

    a = _adapter(ipc=BudgetIPC(result={"ok": True}), healthy=False)
    await a.check_health()
    assert seen["timeout_s"] == 30


async def test_a_caller_supplied_mission_id_is_refused_not_clobbered():
    """The key is reserved for reply correlation; silently overwriting it would
    make a caller's own correlation vanish with no signal."""
    with pytest.raises(ValueError, match="mission_id"):
        await _adapter().dispatch_mission({"mission_id": "mine", "a": 1})


def test_a_non_ssh_transport_is_refused_at_construction():
    """Every command this adapter builds is a SHELL command."""
    cfg = ProgramConfig.from_dict({
        "name": "http-endpoint",
        "ipc": {"method": "http", "url": "http://x"},
        "endpoint": {"state_dir": _STATE, "mission_command": _MISSION, "machine_id": "G"},
    })
    with pytest.raises(ValueError, match="requires ipc.method 'ssh'"):
        WindowsEndpointAdapter(cfg)


@pytest.mark.parametrize("wild", ["[1]", "*", "?"])
def test_wildcard_characters_in_state_dir_use_literal_paths(wild):
    """PowerShell's -Path treats * ? [ ] as WILDCARDS; .NET file APIs do not.

    MEASURED on hardware with a state_dir containing '[1]': Test-Path -Path
    returned False for a file that demonstrably existed, so the prepare step
    exited 0 with the stale result.json still on disk — the exact silent-stale
    -read the postcondition exists to prevent. -LiteralPath fixes the class
    without having to forbid the characters.
    """
    a = _adapter(_cfg(endpoint={"state_dir": f"C:\\g{wild}x"}))
    for cmd in (a._prepare_command(), a._read_result_command()):
        assert "-LiteralPath" in cmd
        assert "Test-Path '" not in cmd, "a wildcard-interpreting Test-Path remains"


def test_the_prepare_step_distinguishes_a_permission_failure():
    """MEASURED: a failing New-Item is NON-terminating, so without the try/catch
    the trailing `exit 0` masks it and the operator sees a write error for what
    is a permissions problem on the parent directory."""
    cmd = _adapter()._prepare_command()
    assert "$ErrorActionPreference='Stop'" in cmd
    assert "catch { exit 2 }" in cmd
    assert "CreateDirectory" in cmd, "New-Item has no -LiteralPath on PS 5.1"
