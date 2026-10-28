"""Shared-MCP rollout switch: which command a Genesis MCP registration runs.

Every Claude Code registration of a Genesis MCP server (the interactive
``.mcp.json`` and each dispatched session's rendered config, both from
``config/mcp.json.template``) runs ``.claude/mcp/run-mcp-server``. When an
install has opted in, that launcher hands its argv here, and this decides per
server between the per-session stdio server (the argv it was given, today's
behaviour) and the thin adapter to a shared service.

The EFFECTIVE stage is the lowest of three: the stage requested
(``GENESIS_MCP_SHARED_STAGE``, else the config), the code ceiling
``MAX_STAGE``, and what the server's unit can serve (installed, enabled,
running). A server outside the effective stage, or whose unit is not ready,
runs stdio. Codex (``--external-client``) and the kill switch
``GENESIS_MCP_SHARED_DISABLED=1`` always run stdio: Codex's switch is its own.

Fail direction: any error here (an unreadable overlay, an unknown stage)
launches the stdio server and says why on stderr. Stdio is not a degraded mode;
it is what every session runs today.

Stdlib only: the launcher runs it on every opted-in spawn and the deploy
scripts run it with ``python3 -I -S``. Subcommands: ``launch`` (exec the
chosen command), ``units`` (the configured unit names, for the deploy),
``status`` (read-only JSON report: what a NEW session would launch, per server).
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

STAGES = ("off", "memory-floor", "health-reads", "writes")
STAGE_SERVERS = {
    "off": (),
    "memory-floor": ("memory",),
    "health-reads": ("memory", "health"),
    "writes": ("memory", "health", "outreach", "recon"),
}
# The highest stage this code allows, whatever any config says. It stays "off"
# until the shared service (P4), the adapter (P5) and per-request connection
# isolation (P2b) are on main: even the memory floor writes (procedure_recall
# records invocation counters). Raising it is a reviewed code change, never a
# configuration one.
# GROUNDWORK(mcp-shared-rollout): with the ceiling at "off" the adapter branch of
# decide() is unreachable on purpose; the unit names, URLs, Type=notify readiness
# and adapter argv placeholders are provisional contracts the shared service and
# the adapter own, and may override in config.
MAX_STAGE = "off"

KILL_SWITCH = "GENESIS_MCP_SHARED_DISABLED"
STAGE_ENV = "GENESIS_MCP_SHARED_STAGE"
CONFIG_NAME = "mcp_shared.json"


class RolloutError(Exception):
    """The rollout configuration cannot be used as written."""


class Decision(NamedTuple):
    argv: list
    shared: bool
    reason: str


def _stage(value, where):
    if not isinstance(value, str) or value not in STAGES:
        raise RolloutError(f"{where}: stage {value!r} is not one of {', '.join(STAGES)}")
    return value


def overlay_path(env):
    home = env.get("GENESIS_HOME") or str(Path(env.get("HOME") or Path.home()) / ".genesis")
    path = Path(home).expanduser()
    if not path.is_absolute():
        raise RolloutError(f"GENESIS_HOME must be an absolute path (got {home!r})")
    return path / "config" / CONFIG_NAME


def _read(path):
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RolloutError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RolloutError(f"{path}: expected a JSON object")
    return data


def load_config(root, env):
    """The tracked defaults under <root>/config, with the user overlay merged per key."""
    config = {"stage": "off", "adapter": {"argv": []}, "servers": {}}
    for layer in (_read(Path(root) / "config" / CONFIG_NAME), _read(overlay_path(env))):
        if not layer:
            continue
        if "stage" in layer:
            config["stage"] = layer["stage"]
        if isinstance(layer.get("adapter"), dict):
            config["adapter"] = {**config["adapter"], **layer["adapter"]}
        for name, spec in (layer.get("servers") or {}).items():
            if not isinstance(spec, dict):
                raise RolloutError(f"servers.{name}: expected an object")
            config["servers"][name] = {**config["servers"].get(name, {}), **spec}
    return config


def _run(argv):
    env = dict(os.environ)
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    env.setdefault("XDG_RUNTIME_DIR", runtime)
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime}/bus")
    # No timeout of its own: this runs only at an opted-in MCP spawn, and Claude
    # Code's MCP startup window already bounds a spawn that hangs on the bus.
    try:
        done = subprocess.run(argv, capture_output=True, text=True, env=env, check=False)
    except OSError:
        return 127, ""
    return done.returncode, done.stdout


# A shared-MCP unit is a genesis-mcp-*.service user unit: a closed naming rule, so
# a config value can never name an option, another host or a unit the deploy
# scripts manage themselves (genesis-server, the watchdogs).
_UNIT_RE = re.compile(r"genesis-mcp-[A-Za-z0-9_.:@-]+\.service")


def valid_unit(unit):
    return isinstance(unit, str) and _UNIT_RE.fullmatch(unit) is not None


def unit_ready(unit, runner=_run):
    """(ready, detail): installed, enabled, and running per systemd."""
    rc, out = runner(
        [
            "systemctl",
            "--user",
            "show",
            "-p",
            "LoadState",
            "-p",
            "UnitFileState",
            "-p",
            "ActiveState",
            "-p",
            "SubState",
            "--",
            unit,
        ]
    )
    if rc != 0:
        return False, f"{unit}: systemctl could not be asked (exit {rc})"
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    state = (
        props.get("LoadState"),
        props.get("UnitFileState"),
        props.get("ActiveState"),
        props.get("SubState"),
    )
    ready = (
        state[0] == "loaded"
        and state[1] in ("enabled", "enabled-runtime")
        and state[2:] == ("active", "running")
    )
    return ready, f"{unit}: " + "/".join(s or "?" for s in state)


def _server(argv):
    """The server name when <argv> is EXACTLY the form config/mcp.json.template
    registers (<python> <script> --server <name>), else None. A closed form, not a
    parse: genesis_mcp_server.py's argparse accepts abbreviations and other flags
    (Codex's --external-client among them), and any argv this does not recognise
    stays on the stdio server it already names."""
    if len(argv) == 4 and argv[2] == "--server" and argv[3] in STAGE_SERVERS["writes"]:
        return argv[3]
    return None


def kill_switch_on(env):
    return env.get(KILL_SWITCH, "") not in ("", "0")


def decide(argv, *, config, env, ready, ceiling, python, root):
    """The command for one spawn. <argv> is the stdio command the launcher would run."""
    stdio = list(argv)
    if "--external-client" in stdio:
        return Decision(stdio, False, "external client: never routed by this switch")
    if kill_switch_on(env):
        return Decision(stdio, False, f"{KILL_SWITCH} is set")
    requested = (
        _stage(env[STAGE_ENV], STAGE_ENV)
        if env.get(STAGE_ENV) is not None
        else _stage(config.get("stage"), CONFIG_NAME)
    )
    stage = min(requested, _stage(ceiling, "ceiling"), key=STAGES.index)
    capped = f" (requested {requested}, capped by the code ceiling)" if stage != requested else ""
    server = _server(stdio)
    if server is None:
        return Decision(stdio, False, "not the registered --server <name> form")
    if server not in STAGE_SERVERS[stage]:
        return Decision(stdio, False, f"{server} is not shared at stage {stage}{capped}")
    template = config.get("adapter", {}).get("argv") or []
    if not isinstance(template, list) or not all(isinstance(a, str) for a in template):
        raise RolloutError("adapter.argv must be a list of strings")
    if not template:
        return Decision(stdio, False, "no adapter configured")
    spec = config.get("servers", {}).get(server) or {}
    unit, url = spec.get("unit"), spec.get("url")
    if not (valid_unit(unit) and isinstance(url, str) and url):
        raise RolloutError(f"servers.{server} needs a genesis-mcp-*.service unit and a url")
    ok, detail = ready(unit)
    if not ok:
        return Decision(stdio, False, f"shared service not ready ({detail})")
    values = {"python": python, "root": str(root), "server": server, "url": url, "stage": stage}
    try:
        adapter = [a.format(**values) for a in template]
    except (KeyError, IndexError, ValueError) as exc:
        raise RolloutError(f"adapter.argv: {exc!r}") from exc
    return Decision(adapter, True, f"{server} shared at stage {stage} via {unit}{capped}")


def units(config):
    """The configured shared-MCP units, each once, for the deploy scripts. A name
    outside the genesis-mcp-*.service rule is a configuration error rather than
    something to pass to systemctl."""
    found = set()
    for name, spec in sorted(config["servers"].items()):
        unit = spec.get("unit")
        if unit is None or unit == "":
            continue
        if not valid_unit(unit):
            raise RolloutError(f"servers.{name}.unit {unit!r} is not a shared MCP .service unit")
        found.add(unit)
    return sorted(found)


def _say(message):
    print(f"genesis mcp rollout: {message}", file=sys.stderr)


def launch(root, stdio):
    shared = False
    try:
        env = dict(os.environ)
        chosen = decide(
            stdio,
            config=load_config(root, env),
            env=env,
            ready=unit_ready,
            ceiling=MAX_STAGE,
            python=stdio[0],
            root=root,
        )
        _say(chosen.reason)
        shared = chosen.shared
        argv = chosen.argv
    except Exception as exc:  # noqa: BLE001 - every failure launches today's stdio server
        _say(f"{exc}; launching the per-session server")
        argv = stdio
    if shared:
        try:
            os.execv(argv[0], argv)  # noqa: S606 - argv from the adapter config
        except OSError as exc:
            _say(f"cannot start the adapter {argv[0]!r} ({exc}); launching the per-session server")
    os.execv(stdio[0], stdio)  # noqa: S606 - the launcher's own stdio command


def status(root):
    env = dict(os.environ)
    config = load_config(root, env)
    python = str(Path(root) / ".venv" / "bin" / "python")
    report = {
        "requested": env[STAGE_ENV] if env.get(STAGE_ENV) is not None else config.get("stage"),
        "ceiling": MAX_STAGE,
        "kill_switch": kill_switch_on(env),
        "servers": {},
    }
    for server in STAGE_SERVERS["writes"]:
        unit = (config["servers"].get(server) or {}).get("unit") or ""
        if valid_unit(unit):
            ok, detail = unit_ready(unit)
        else:
            ok, detail = False, f"not a genesis-mcp-*.service unit: {unit!r}"
        stdio = [python, str(Path(root) / "scripts" / "genesis_mcp_server.py"), "--server", server]
        try:
            chosen = decide(
                stdio,
                config=config,
                env=env,
                ready=lambda _u, ok=ok, detail=detail: (ok, detail),
                ceiling=MAX_STAGE,
                python=python,
                root=root,
            )
            launches, reason = ("adapter" if chosen.shared else "stdio"), chosen.reason
        except RolloutError as exc:
            launches, reason = "stdio", str(exc)
        report["servers"][server] = {
            "unit": unit,
            "ready": ok,
            "unit_state": detail,
            "a_new_session_launches": launches,
            "reason": reason,
        }
    print(json.dumps(report, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="mcp_rollout")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("launch", "units", "status"):
        sub.add_parser(name).add_argument("--root", required=True)
    args, rest = parser.parse_known_args(argv)
    root = Path(args.root)
    if args.cmd == "launch":
        if not rest or rest[0] != "--" or len(rest) < 2:
            parser.error("launch needs -- <stdio command>")
        launch(root, rest[1:])
    elif rest:
        parser.error(f"unexpected arguments: {rest}")
    elif args.cmd == "units":
        for unit in units(load_config(root, dict(os.environ))):
            print(unit)
    else:
        status(root)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RolloutError as exc:
        _say(str(exc))
        sys.exit(1)
