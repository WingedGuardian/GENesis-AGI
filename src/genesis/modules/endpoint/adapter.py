"""WindowsEndpointAdapter — a MACHINE Genesis operates on, not a service it calls.

# GROUNDWORK(desktop-endpoint): nothing calls dispatch_mission yet. The transport
# is built and tested ahead of the capability-grant gate, which ships separately.

The external-module tier already gives us health, lifecycle, enable/disable and
an operations manifest. What it does not give us is a way to send a mission a
PAYLOAD: ``ExternalProgramAdapter.execute_operation`` passes ``data`` for a
SHELL operation, but ``SshIPCAdapter.send`` routes SHELL to ``_send_shell(path)``
and drops ``data`` entirely — so a manifest command is static by construction.

This adapter closes that gap the way the device already expects to be driven:
the payload is written to a file the device reads (``request.json``), the static
command is invoked, and the reply is read back from ``result.json``. That is the
protocol ``scripts/genesis-act.ps1`` already implements, not a new one.

WHY BASE64 BOTH WAYS. Outbound, base64's alphabet (A-Z a-z 0-9 + / =) contains
no cmd.exe or PowerShell metacharacter, so the payload needs no remote quoting —
which matters because the remote default shell is cmd.exe, where POSIX quoting
is actively wrong. Inbound, it is not a convenience but a correctness
requirement: MEASURED on hardware, reading the reply to the console CORRUPTS
non-ASCII before it reaches us (a payload containing an em dash, "ü" and a check
mark came back as bytes 2d 20 81 ... fb). That is data loss in the transport, so
no local decode strategy can recover it — ``errors="replace"`` would have
returned plausible-looking WRONG data instead of an error.

THE OUTBOUND COST, MEASURED: the payload rides the command line, and cmd.exe
caps that at 8191 characters. Binary-searched on hardware: an 8093-character
argument succeeded and 8125 failed, with ~98 characters of wrapper — the
documented 8191, confirmed rather than assumed. An oversized mission is REFUSED
with both numbers, never truncated: a silently clipped mission descriptor asks
for something other than what was requested. A larger payload needs a file
transport, which is a deliberate future step rather than a hidden limit.

WINDOWS IS BAKED IN, DELIBERATELY AND VISIBLY. The commands below are
PowerShell and the byte budget is a cmd.exe number, so this class is cloned per
WINDOWS machine by copying a YAML. It is registered as ``windows-endpoint``
rather than ``endpoint`` so a future POSIX dialect gets its own name instead of
silently inheriting this one's assumptions.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import json
import logging
import socket
import uuid
from datetime import UTC, datetime
from typing import Any

from genesis.modules.external.adapter import (
    _HEALTH_CACHE_TTL_S,
    ExternalProgramAdapter,
)
from genesis.modules.external.config import ProgramConfig
from genesis.modules.external.ipc import _HEALTH_PROBE_TIMEOUT

logger = logging.getLogger(__name__)

# cmd.exe's documented command-line ceiling. A PROTOCOL number, not a corpus
# observation. The 32-character haircut is deliberate slack: the budget is
# computed from the PowerShell text we build, while the string is actually
# executed by Windows sshd's `cmd.exe /c <cmd>`, and the top of the budget was
# only ever exercised against a fake transport.
_CMD_LINE_MAX = 8191 - 32

# A floor for the sanity check below, not a budget.
_MIN_USABLE_PAYLOAD = 256

# Tailscale's v4 space is RFC 6598 CGNAT, checked explicitly because
# `is_private`'s treatment of 100.64/10 is version-dependent. Its v6 space is a
# ULA (fc00::/7), which `is_private` already covers.
#
# genesis.cc.session_cap.classify_origin makes the same distinction for a
# DIFFERENT question — which network an inbound sshd client arrived from. Kept
# separate because the inputs and failure directions differ, but the v6-mapping
# and loopback handling are borrowed from it rather than re-derived.
_TAILNET_NET = ipaddress.ip_network("100.64.0.0/10")

# Characters that must never appear in a value we embed in a remote
# cmd.exe -> PowerShell single-quoted literal. Each one MEASURED on hardware
# rather than assumed:
#   '        terminates the literal.
#   \u2018-\u201b  PowerShell's grammar accepts these "smart quotes" AS quote
#            characters — verified: a U+2019 produced "The string is missing the
#            terminator: '." They survive a copy-paste from a doc or a chat.
#   "        closes cmd.exe's own quoting around the -Command argument, after
#            which cmd operators (&, |, &&) would run as separate commands.
#   %        cmd.exe expands %VAR% before PowerShell ever sees it — verified:
#            a path containing '%USERNAME%' arrived with the variable already
#            expanded to the account name. Not injection, but a silent rewrite
#            of the operator's path.
# Backtick and $ are deliberately NOT here: inside a PowerShell single-quoted
# literal both are inert, and refusing them would reject legitimate paths.
_FORBIDDEN_IN_LITERAL = "'\u2018\u2019\u201a\u201b\"%"


class EndpointNotReachable(RuntimeError):
    """The configured host is not on any network this endpoint permits."""


class MissionTooLarge(ValueError):
    """The mission payload exceeds what the remote command line can carry."""


class WindowsEndpointAdapter(ExternalProgramAdapter):
    """One Windows machine, addressed over the existing SSH SHELL transport.

    Written to be CLONED per Windows machine, not subclassed: a second machine
    is a second YAML in the install overlay with its own ``machine_id`` and
    network. A non-Windows endpoint is a different dialect and would need its
    own adapter — hence the explicit name.
    """

    def __init__(self, config: ProgramConfig) -> None:
        super().__init__(config)
        ep = config.endpoint
        if ep is None:
            raise ValueError(
                f"module '{config.name}' selects the windows-endpoint adapter but "
                "declares no `endpoint:` config block"
            )
        for required in ("state_dir", "mission_command", "machine_id"):
            if not getattr(ep, required, None):
                raise ValueError(f"module '{config.name}': endpoint.{required} is required")

        # Only state_dir is embedded in a remote string literal. mission_command
        # is passed VERBATIM to the remote shell (see dispatch_mission), so
        # quote-restricting it would reject a legitimate config such as
        # `powershell -File 'C:\my scripts\act.ps1'`.
        bad = sorted({c for c in ep.state_dir if c in _FORBIDDEN_IN_LITERAL or ord(c) < 0x20})
        if bad:
            raise ValueError(
                f"module '{config.name}': endpoint.state_dir may not contain "
                f"{[hex(ord(c)) for c in bad]} — it is embedded in a remote "
                "cmd.exe -> PowerShell string literal"
            )
        # N5: every command this class builds is a SHELL command. A config
        # pairing this adapter with an HTTP transport constructs cleanly today
        # and then fails every probe with an "Unsupported HTTP method: SHELL"
        # swallowed into _last_health_error. The constructor already refuses
        # unusable configs; this is one of them.
        if config.ipc.method != "ssh":
            raise ValueError(
                f"module '{config.name}': the windows-endpoint adapter requires "
                f"ipc.method 'ssh', not '{config.ipc.method}'"
            )

        self._ep = ep

        # Every command this adapter builds embeds state_dir, and the prepare
        # command embeds it TWICE — so its length is a CONFIG property, and a
        # config error belongs here rather than surfacing on the first mission.
        # Two distinct ways it can be too long, checked together:
        for label, cmd in (
            ("prepare", self._prepare_command()),
            ("read-result", self._read_result_command()),
        ):
            if len(cmd) > _CMD_LINE_MAX:
                raise ValueError(
                    f"module '{config.name}': endpoint.state_dir is too long — the "
                    f"{label} command is {len(cmd)} characters against a "
                    f"{_CMD_LINE_MAX} limit before any payload is added"
                )
        # BACKSTOP, and currently UNREACHABLE — said plainly rather than left to
        # look like live cover. MEASURED against the exact command strings in
        # this file: the prepare command embeds state_dir THREE times and
        # overflows at a state_dir length of 2617; read-result embeds it twice
        # and overflows at 4004; starving the payload to this floor needs 7707.
        # The check above therefore always fires first, by a wide margin.
        # Retained because that ordering is a property of the CURRENT command
        # shapes: shorten the prepare command and this becomes the binding
        # constraint, with no other guard behind it.
        if self.payload_budget() < _MIN_USABLE_PAYLOAD:  # pragma: no cover
            raise ValueError(
                f"module '{config.name}': endpoint.state_dir leaves only "
                f"{self.payload_budget()} payload bytes of the {_CMD_LINE_MAX}-character "
                f"remote command line (minimum {_MIN_USABLE_PAYLOAD}); shorten it"
            )

        # Serialises dispatches on THIS instance. Without it two concurrent
        # missions overwrite each other's request.json and both read the same
        # result.json — each returning the other's answer as its own, with no
        # signal at any layer above. The mission_id below covers what a
        # single-process lock cannot.
        self._dispatch_lock = asyncio.Lock()

    @property
    def machine_id(self) -> str:
        """Stable per-machine identity. NEVER a hostname — see EndpointConfig."""
        return self._ep.machine_id  # type: ignore[return-value]

    # ── reachability ────────────────────────────────────────────────────────
    def _host_address(self) -> str:
        return (self._config.ipc.ssh_host or "").split("@", 1)[-1]

    @staticmethod
    def _classify(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
        # A dual-stack host can be written as an IPv4-mapped v6 literal.
        # MEASURED py3.12: RFC1918 privateness DOES survive the mapping
        # (::ffff:192.168.0.1 -> is_private True), but the TAILNET range does
        # not (::ffff:100.x is is_private False and is not a member of a v4
        # network object), so a mapped tailnet endpoint would read as public.
        if getattr(ip, "ipv4_mapped", None) is not None:
            ip = ip.ipv4_mapped  # type: ignore[union-attr]
        if ip in _TAILNET_NET:
            return "tailnet"
        # Loopback counts as LAN: it is the one address that cannot be remote.
        if ip.is_loopback or ip.is_private:
            return "lan"
        return None

    async def resolve_network(self) -> str:
        """Classify the configured host as 'lan' or 'tailnet'.

        Async and non-blocking: the previous synchronous ``gethostbyname`` parked
        the whole event loop on a slow resolver — and did so precisely when DNS
        was unwell, since this is the fail-closed path. ``getaddrinfo`` also sees
        AAAA records, where ``gethostbyname`` is A-only and would refuse a
        perfectly reachable v6 endpoint as unresolvable.

        Fails CLOSED in three ways: an address on no permitted network raises, an
        unresolvable name raises, and a name resolving to addresses on MORE THAN
        ONE network raises rather than picking whichever came back first — that
        ordering is not stable, so allowing it would be fail-open by luck.

        NOTE a limit this cannot close: ``ssh`` resolves the name independently,
        so the address classified is not provably the address connected to, and
        an ``~/.ssh/config`` alias is not resolvable here at all. Prefer a
        literal address in config.
        """
        addr = self._host_address()
        if not addr:
            raise EndpointNotReachable(f"module '{self._config.name}': no ssh_host configured")

        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            try:
                infos = await asyncio.get_running_loop().getaddrinfo(
                    addr, None, proto=socket.IPPROTO_TCP
                )
            except OSError as exc:
                raise EndpointNotReachable(
                    f"module '{self._config.name}': cannot resolve '{addr}': {exc}"
                ) from exc
            networks = {self._classify(ipaddress.ip_address(i[4][0])) for i in infos}
            if len(networks) != 1:
                raise EndpointNotReachable(
                    f"module '{self._config.name}': '{addr}' resolves to addresses on "
                    f"more than one network ({sorted(str(n) for n in networks)}); "
                    "pin a literal address"
                ) from None
            network = networks.pop()
        else:
            network = self._classify(ip)

        if network is None:
            raise EndpointNotReachable(
                f"module '{self._config.name}': host {addr} is on neither the LAN nor "
                "the tailnet; an endpoint is a machine we operate, not a public address"
            )
        return network

    async def check_network_allowed(self) -> str:
        network = await self.resolve_network()
        allowed = self._ep.allowed_networks or []
        if network not in allowed:
            raise EndpointNotReachable(
                f"module '{self._config.name}': reachable over '{network}' but "
                f"allowed_networks is {allowed}"
            )
        return network

    # ── health ──────────────────────────────────────────────────────────────
    async def check_health(self) -> bool:
        """Liveness for a MACHINE, not for a Claude Code install.

        The inherited SSH health check runs ``<remote_claude_path> --version``,
        defaulting to ``claude``. On a Windows endpoint that returns non-zero,
        so every endpoint would register permanently unhealthy and
        ``execute_operation`` would then refuse everything — while the dashboard
        rendered the machine as an error. Worse, configuring a ``health_check:``
        block at all is what activates that path: with none configured the base
        class simply reports healthy.

        An endpoint is alive when we can REACH it and run a command on it. It is
        deliberately NOT "its state directory exists": the prepare step CREATES
        that directory, so making it a health precondition deadlocks a fresh
        machine — health reports unhealthy because the directory is absent, and
        the only thing that would create it is the dispatch the gate just
        refused. Found by a live run against a machine whose state dir had been
        deleted; no faked transport can produce it, because a fake answers every
        probe the same way.
        """
        probe = 'powershell -NoProfile -Command "exit 0"'
        try:
            # Its OWN short budget, not the module's work timeout. Without this
            # the modules page pays up to the work budget per endpoint, serially
            # — which is the stall _HEALTH_PROBE_TIMEOUT exists to prevent, and
            # my override made that probe mandatory where it used to be free.
            res = await self._ipc.send(
                probe, data={"timeout_s": _HEALTH_PROBE_TIMEOUT}, method="SHELL"
            )
        except Exception as exc:  # transport must never take the runtime down
            self._healthy = False
            self._last_health_error = str(exc)
            return False
        self._healthy = res.get("exit_code") == 0 and not res.get("error")
        self._last_health_error = (
            None
            if self._healthy
            else (res.get("error") or f"endpoint did not run a command (exit {res.get('exit_code')})")
        )
        return self._healthy

    async def check_health_cached(self) -> bool:
        """Override too, or the probe above is dead code that only tests reach.

        The base class's cached path calls ``self._ipc.health_check(...)``
        DIRECTLY rather than ``self.check_health()``, so overriding the latter
        alone changes nothing for any runtime caller — and every runtime caller
        uses the cached one (``dashboard/routes/modules.py`` and the
        career-outreach tick). Built-but-not-wired, exactly.

        The TTL behaviour is the base's, reusing its constant so the two cannot
        drift apart.
        """
        now = datetime.now(UTC)
        if (
            self._last_health_check_at is not None
            and (now - self._last_health_check_at).total_seconds() < _HEALTH_CACHE_TTL_S
        ):
            return self._healthy
        healthy = await self.check_health()
        self._last_health_check_at = now
        return healthy

    # ── payload budget ──────────────────────────────────────────────────────
    def _write_command(self, b64: str) -> str:
        path = f"{self._ep.state_dir}\\request.json"
        return (
            "powershell -NoProfile -Command "
            f"\"[IO.File]::WriteAllBytes('{path}',"
            f"[Convert]::FromBase64String('{b64}'))\""
        )

    def payload_budget(self) -> int:
        """Largest RAW payload (bytes) that fits, derived from the real command."""
        room = _CMD_LINE_MAX - len(self._write_command(""))
        return max(0, (room // 4) * 3)

    # ── mission dispatch ────────────────────────────────────────────────────
    async def dispatch_mission(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Write the payload, run the mission, read the reply.

        Returns the device's parsed ``result.json`` (plus ``mission_exit_code``)
        on success, or a dict with an ``error`` key. Raises only for a CALLER
        error — an oversized payload, or a host the config forbids.
        """
        # The base class gates every other execution path on these two
        # (handle_opportunity, record_outcome, execute_operation). This method is
        # the one that writes a file to a remote machine and runs a command
        # there; it does not get to opt out. `enabled` is the only operator lever
        # this adapter has today, and the dashboard toggle writes it.
        if not self._enabled:
            return {"error": f"Module '{self.name}' is disabled"}

        # CALLER errors first, before ANY I/O: a malformed request is malformed
        # whatever the machine's state, and diagnosing it should not depend on
        # whether some host happened to be reachable.

        if "mission_id" in payload:
            raise ValueError(
                f"module '{self._config.name}': payload carries its own 'mission_id'; "
                "that key is reserved for reply correlation and would be overwritten"
            )
        mission_id = uuid.uuid4().hex
        stamped = {**payload, "mission_id": mission_id}
        raw = json.dumps(stamped, separators=(",", ":")).encode("utf-8")

        # No floor check here: construction already refuses a state_dir that
        # leaves less than _MIN_USABLE_PAYLOAD, and a second copy would be
        # unreachable code pretending to be a guard.
        budget = self.payload_budget()
        if len(raw) > budget:
            raise MissionTooLarge(
                f"module '{self._config.name}': mission payload is {len(raw)} bytes but "
                f"only {budget} fit in the remote command line ({_CMD_LINE_MAX}-char "
                "cmd.exe limit, measured). Refusing rather than truncating — a clipped "
                "mission descriptor would ask for something other than what was requested."
            )

        # Then CONFIG: the network check is a refusal to contact this host AT
        # ALL, so it precedes the health probe — otherwise the probe reaches out
        # to a machine the config has already ruled out.
        await self.check_network_allowed()

        # Then LIVE state. With no health_check block configured (the shape the
        # template ships) register() sets _healthy True WITHOUT probing, and
        # nothing else on this path revises it — so without this refresh the gate
        # would only reflect a verdict some unrelated dashboard render happened
        # to collect, which reads as live and is not. Cached (60s TTL) and
        # bounded at _HEALTH_PROBE_TIMEOUT.
        await self.check_health_cached()
        if not self._healthy:
            return {"error": f"Module '{self.name}' is not healthy"}

        async with self._dispatch_lock:
            return await self._dispatch_locked(raw, mission_id)

    async def _dispatch_locked(self, raw: bytes, mission_id: str) -> dict[str, Any]:
        # Prepare FIRST: create the state dir and drop any previous reply.
        # Without the clear, a failed run leaves the last mission's result.json
        # in place and it reads as this mission's success — indistinguishable
        # from a real result at every layer above.
        #
        # exit_code is checked, not just error: `error` is set only on a
        # TRANSPORT failure (timeout / OSError), while a remote refusal surfaces
        # as a non-zero exit. MEASURED on hardware: deleting a locked file under
        # `-ErrorAction SilentlyContinue` prints nothing and still exits 1, so
        # the signal is there and only an unchecked caller loses it.
        cleared = await self._ipc.send(self._prepare_command(), method="SHELL")
        if cleared.get("error") or cleared.get("exit_code"):
            return {
                "error": "could not prepare the endpoint state directory",
                "detail": cleared.get("error") or cleared.get("stderr"),
                "exit_code": cleared.get("exit_code"),
            }

        b64 = base64.b64encode(raw).decode("ascii")
        written = await self._ipc.send(self._write_command(b64), method="SHELL")
        if written.get("error") or written.get("exit_code"):
            return {
                "error": "failed to write mission payload",
                "detail": written.get("error") or written.get("stderr"),
                "exit_code": written.get("exit_code"),
            }

        ran = await self._ipc.send(self._ep.mission_command, method="SHELL")
        if ran.get("error"):
            return {"error": f"mission dispatch failed: {ran['error']}"}

        reply = await self._ipc.send(self._read_result_command(), method="SHELL")
        if reply.get("error"):
            return {
                "error": f"mission ran but the reply could not be read: {reply['error']}",
                "mission_exit_code": ran.get("exit_code"),
            }

        encoded = (reply.get("output") or "").strip()
        if not encoded:
            return {
                "error": "mission produced no result.json",
                "mission_exit_code": ran.get("exit_code"),
                "mission_stderr": ran.get("stderr"),
            }
        try:
            raw_reply = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            return {
                "error": f"result.json did not come back as valid base64: {exc}",
                "mission_exit_code": ran.get("exit_code"),
            }
        try:
            # utf-8-sig: PowerShell 5.1's `Set-Content -Encoding utf8` writes a
            # BOM, and json.loads rejects it.
            parsed = json.loads(raw_reply.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return {
                "error": f"result.json is not valid UTF-8 JSON: {exc}",
                "mission_exit_code": ran.get("exit_code"),
            }

        # Correlate. A device that echoes mission_id proves the reply belongs to
        # THIS mission; one that does not (today's genesis-act.ps1) is accepted
        # unchanged, so this is additive rather than a flag day.
        if isinstance(parsed, dict):
            echoed = parsed.get("mission_id")
            if echoed is None and isinstance(parsed.get("echo"), dict):
                echoed = parsed["echo"].get("mission_id")
            if echoed is not None and echoed != mission_id:
                return {
                    "error": "result.json belongs to a different mission",
                    "expected": mission_id,
                    "got": echoed,
                }
            return {**parsed, "mission_exit_code": ran.get("exit_code")}
        return {"result": parsed, "mission_exit_code": ran.get("exit_code")}

    def _prepare_command(self) -> str:
        """Ensure the state dir exists, THEN drop any stale reply.

        Both halves in the first step, and in this order, because they interact:
        MEASURED on a fresh endpoint, `Remove-Item` against a path whose parent
        DIRECTORY is absent exits 1 — so a clear-then-create ordering makes the
        very first mission on a new machine fail, and the exit-code check
        (rightly) refuses it. Creating first also keeps the mkdir out of the
        write command, which is the one competing for the cmd.exe byte budget.

        The directory is created via .NET rather than ``New-Item``, for two
        measured reasons: ``New-Item`` has NO ``-LiteralPath`` parameter on
        PowerShell 5.1 (``(Get-Command New-Item).Parameters`` -> False), so the
        literal form simply throws; and ``-Path`` would treat ``* ? [ ]`` in the
        directory name as wildcards, which is the same class of bug the
        ``-LiteralPath`` on the other cmdlets exists to close.
        ``[IO.Directory]::CreateDirectory`` is literal by definition and
        idempotent on an existing directory (verified: exit 0 both times).

        The trailing Test-Path is NOT belt-and-braces; it is the only thing that
        makes the exit code mean anything. MEASURED on hardware, all four cells:

            Remove-Item -EA SilentlyContinue, file ABSENT   -> exit 1
            ... with the Test-Path postcondition, absent    -> exit 0
            ... with the postcondition, file LOCKED         -> exit 1
            ... with the postcondition, file deletable      -> exit 0

        So the bare exit code cannot distinguish "there was nothing to delete"
        (fine) from "I could not delete it" (fatal, because the stale reply then
        gets read as this mission's result). Only the postcondition separates
        them. `New-Item -Force` is idempotent on an existing directory.
        """
        path = f"{self._ep.state_dir}\\result.json"
        return (
            "powershell -NoProfile -Command "
            "\"$ErrorActionPreference='Stop'; try { "
            f"[void][IO.Directory]::CreateDirectory('{self._ep.state_dir}'); "
            f"Remove-Item -Force -ErrorAction SilentlyContinue "
            f"-LiteralPath '{path}'; "
            f"if (Test-Path -LiteralPath '{path}') {{ exit 1 }} else {{ exit 0 }} "
            "} catch { exit 2 }\""
        )

    def _read_result_command(self) -> str:
        """Read the reply as base64 — see the module docstring for why."""
        path = f"{self._ep.state_dir}\\result.json"
        return (
            "powershell -NoProfile -Command "
            f"\"if (Test-Path -LiteralPath '{path}') {{ "
            f"[Convert]::ToBase64String([IO.File]::ReadAllBytes('{path}')) }}\""
        )
