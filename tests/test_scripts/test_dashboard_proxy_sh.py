"""Behavioural tests for dashboard_proxy_check (scripts/lib/dashboard_proxy.sh).

host-setup.sh used to advertise the host's dashboard URL whether or not any
proxy device served it (#3161): an existing device was accepted by name alone,
and a failed ``device add`` only warned. It now advertises host routes only
when this check passes.

The function is sourced from the REAL lib and run under host-setup's
``set -euo pipefail``, with a stub ``incus`` on PATH: ``incus query`` answers
fixture JSON shaped like ``/1.0/instances/<name>`` (measured shape on incus
6.0.0: ``devices`` holds the instance's own devices, ``expanded_devices`` adds
the profile's), and ``incus exec <c> -T -- python3 …`` runs the local python3
with the same arguments and stdin, standing in for the container's interpreter.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "scripts" / "lib" / "dashboard_proxy.sh"
HOST_SETUP = REPO_ROOT / "scripts" / "host-setup.sh"

_INCUS_STUB = r"""#!/bin/bash
echo "$*" >> "$INCUS_LOG"
case "$1" in
  query)
    [ "${INCUS_QUERY_RC:-0}" != "0" ] && exit "$INCUS_QUERY_RC"
    cat "$INCUS_QUERY_FILE"; exit 0 ;;
  exec)
    shift
    while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do shift; done
    shift
    exec "$@" ;;
esac
exit 0
"""

LAN = "198.51.100.7"  # TEST-NET-2


def _proxy(listen, connect="tcp:127.0.0.1:5000", **extra):
    return {"type": "proxy", "listen": listen, "connect": connect, **extra}


def _run(tmp_path, devices, *, local=None, query_rc=0, raw=None):
    """Run the check; return (verdict, first line, fix line or None)."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "incus"
    stub.write_text(_INCUS_STUB)
    stub.chmod(0o755)
    local = devices if local is None else local
    payload = (
        raw
        if raw is not None
        else json.dumps({"name": "c1", "devices": local, "expanded_devices": devices})
    )
    qfile = tmp_path / "query.json"
    qfile.write_text(payload)
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "INCUS_LOG": str(tmp_path / "incus.log"),
        "INCUS_QUERY_FILE": str(qfile),
        "INCUS_QUERY_RC": str(query_rc),
        "HOME": str(tmp_path),
    }
    script = (
        f'set -euo pipefail; source "{LIB}"; '
        'if out=$(dashboard_proxy_check c1 5000); then printf "OK\\n%s\\n" "$out"; '
        'else printf "NO\\n%s\\n" "$out"; fi; echo __DONE__'
    )
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    assert "__DONE__" in res.stdout, f"check aborted the caller: {res.stdout!r} {res.stderr!r}"
    lines = res.stdout.splitlines()
    verdict, first = lines[0], lines[1]
    fix = next((ln[len("fix: ") :] for ln in lines[2:] if ln.startswith("fix: ")), None)
    return verdict, first, fix


def test_lib_parses():
    assert subprocess.run(["bash", "-n", str(LIB)]).returncode == 0


@pytest.mark.parametrize(
    "devices",
    [
        {"dashboard-proxy": _proxy("tcp:0.0.0.0:5000")},  # host-setup's own default
        {"dashboard-proxy": _proxy("tcp:[::]:5000")},  # IPv6 wildcard
        {
            "web": _proxy("tcp:0.0.0.0:5000", connect="tcp:10.0.0.5:5000")
        },  # other name, bridge connect
        {"dashboard-proxy": _proxy("tcp:0.0.0.0:5000", bind="host", proxy_protocol="false")},
        {"dashboard-proxy": _proxy("tcp:0.0.0.0:5000", proxy_protocol="0")},
    ],
)
def test_serving_device_passes(tmp_path, devices):
    verdict, first, fix = _run(tmp_path, devices)
    assert verdict == "OK", first
    assert "listen=" in first and "connect=" in first
    assert fix is None


@pytest.mark.parametrize(
    ("devices", "why"),
    [
        ({}, "no proxy device listens on port 5000"),
        (
            {"codebase-viz-proxy": _proxy("tcp:0.0.0.0:9749", "tcp:127.0.0.1:9749")},
            "no proxy device listens",
        ),
        (
            {"dashboard-proxy": _proxy("tcp:127.0.0.1:5000")},
            "listens only on 127.0.0.1",
        ),  # loopback-only
        # Bound to one address: it may answer the LAN URL, but never the
        # localhost / tunnel / Tailscale routes the report also prints.
        ({"web": _proxy(f"tcp:{LAN}:5000")}, f"listens only on {LAN}"),
        ({"dashboard-proxy": _proxy("tcp:0.0.0.0:5000", bind="instance")}, "bind=instance"),
        (
            {"dashboard-proxy": _proxy("tcp:0.0.0.0:5000", proxy_protocol="true")},
            "proxy_protocol is on",
        ),
        (
            {"dashboard-proxy": _proxy("tcp:0.0.0.0:5000", connect="tcp:127.0.0.1:8080")},
            "connect=tcp:127.0.0.1:8080",
        ),
        ({"eth0": {"type": "nic", "network": "br0"}}, "no proxy device listens"),
    ],
)
def test_non_serving_device_fails_with_reason(tmp_path, devices, why):
    verdict, first, fix = _run(tmp_path, devices)
    assert verdict == "NO"
    assert why in first
    assert fix, "a failed check must say how to fix it"


def test_one_good_device_among_bad_ones_passes(tmp_path):
    devices = {
        "a-bad": _proxy("tcp:127.0.0.1:5000"),
        "b-good": _proxy("tcp:0.0.0.0:5000"),
    }
    verdict, first, _ = _run(tmp_path, devices)
    assert verdict == "OK"
    assert first.startswith("b-good:")


# ── The fix line fits the state it found ──


def test_fix_adds_a_device_when_none_listens(tmp_path):
    _, _, fix = _run(tmp_path, {})
    assert (
        fix
        == "incus config device add c1 dashboard-proxy proxy listen=tcp:0.0.0.0:5000 connect=tcp:127.0.0.1:5000"
    )


def test_fix_sets_listen_on_an_existing_local_device(tmp_path):
    # `device add` would fail here: the device already exists. Only `listen` is
    # changed; the device's own connect target is left alone.
    _, _, fix = _run(tmp_path, {"web": _proxy(f"tcp:{LAN}:5000", connect="tcp:10.0.0.5:5000")})
    assert fix == "incus config device set c1 web listen=tcp:0.0.0.0:5000"


@pytest.mark.parametrize(
    ("dev", "keys"),
    [
        (_proxy("tcp:0.0.0.0:5000", bind="instance"), "bind=host"),
        (_proxy("tcp:0.0.0.0:5000", proxy_protocol="true"), "proxy_protocol=false"),
        (_proxy("tcp:0.0.0.0:5000", connect="tcp:127.0.0.1:8080"), "connect=tcp:127.0.0.1:5000"),
        (
            _proxy(
                "tcp:127.0.0.1:5000",
                bind="instance",
                proxy_protocol="true",
                connect="tcp:127.0.0.1:1",
            ),
            "listen=tcp:0.0.0.0:5000 bind=host proxy_protocol=false connect=tcp:127.0.0.1:5000",
        ),
    ],
)
def test_fix_sets_every_failing_key(tmp_path, dev, keys):
    # A fix that changed only `listen` would exit 0 and leave the device broken.
    _, _, fix = _run(tmp_path, {"dashboard-proxy": dev})
    assert fix == f"incus config device set c1 dashboard-proxy {keys}"


def test_fix_reuses_the_default_name_when_it_is_a_proxy_on_another_port(tmp_path):
    # `device add dashboard-proxy` would fail: the name exists (as a proxy on 6000).
    _, first, fix = _run(
        tmp_path, {"dashboard-proxy": _proxy("tcp:0.0.0.0:6000", "tcp:127.0.0.1:6000")}
    )
    assert "no proxy device listens on port 5000" in first
    assert fix == (
        "incus config device set c1 dashboard-proxy listen=tcp:0.0.0.0:5000 connect=tcp:127.0.0.1:5000"
    )


def test_fix_picks_another_name_when_the_default_name_is_not_a_proxy(tmp_path):
    _, _, fix = _run(tmp_path, {"dashboard-proxy": {"type": "disk", "path": "/x"}})
    assert fix.startswith("incus config device add c1 genesis-dashboard-proxy proxy ")


def test_fix_points_at_the_profile_for_an_inherited_device(tmp_path):
    devices = {"dashboard-proxy": _proxy("tcp:127.0.0.1:5000")}
    _, _, fix = _run(tmp_path, devices, local={})
    assert "comes from a profile" in fix
    assert "incus profile device set" in fix
    assert "incus config device" not in fix


def test_container_python_failure_is_reported_not_fatal(tmp_path):
    # The stub's exec runs `python3 -c …`; break it by shadowing python3.
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    broken = bindir / "python3"
    broken.write_text("#!/bin/bash\nexit 127\n")
    broken.chmod(0o755)
    verdict, first, fix = _run(tmp_path, {"d": _proxy("tcp:0.0.0.0:5000")})
    assert verdict == "NO"
    assert "could not run python3 in the container" in first
    assert fix


def test_query_failure_is_reported_not_fatal(tmp_path):
    verdict, first, fix = _run(tmp_path, {}, query_rc=1)
    assert verdict == "NO"
    assert "incus query failed" in first
    assert fix


@pytest.mark.parametrize(
    "raw", ["not json", "[]", '{"expanded_devices": "x"}', '{"expanded_devices": {}, "devices": 3}']
)
def test_unreadable_device_list_is_reported_not_fatal(tmp_path, raw):
    verdict, first, _ = _run(tmp_path, {}, raw=raw)
    assert verdict == "NO"
    assert "could not read the container device list" in first


def test_container_python_gets_json_non_interactively(tmp_path):
    _run(tmp_path, {"d": _proxy("tcp:0.0.0.0:5000")})
    log = (tmp_path / "incus.log").read_text()
    assert "query /1.0/instances/c1" in log
    assert "exec c1 -T -- python3 -c" in log


# ── host-setup.sh wiring (extraction: the script itself needs a real host) ──


def _host_setup() -> str:
    return HOST_SETUP.read_text()


def test_host_setup_sources_the_lib_and_gates_on_the_check():
    code = _host_setup()
    assert "lib/dashboard_proxy.sh" in code
    assert 'dashboard_proxy_check "$CONTAINER_NAME" 5000)' in code
    assert "PROXY_OK=1" in code


def test_host_setup_prints_the_checks_fix_line():
    code = _host_setup()
    block = code[code.index("PROXY_OK=0") : code.index("PROXY_OK=0") + 1200]
    assert 'Fix: ${_proxy_report#*"$_nl"fix: }' in block


def test_host_ip_dashboard_line_is_gated():
    code = _host_setup()
    line = "(dashboard: http://$HOST_IPV4:5000)"
    idx = code.index(line)
    guard = code.rfind('if [ "$PROXY_OK" = "1" ]', 0, idx)
    assert guard != -1 and idx - guard < 300, (
        "the host-IP dashboard line must sit under the PROXY_OK guard"
    )


def test_step3_proxy_routes_only_when_verified():
    code = _host_setup()
    step3 = code[code.index('echo "  STEP 3 — Dashboard:"') :]
    step3 = step3[: step3.index("GUARDIAN (host-side health monitor")]
    first_branch = step3.index('if [ "$PROXY_OK" != "1" ]')
    # Every line that rides the host proxy comes after the unverified branch.
    for proxied in (
        "From this host:    http://localhost:5000",
        "ssh -L 5000:localhost:5000",
        "<tailscale-ip>:5000",
    ):
        assert step3.index(proxied) > first_branch
    unverified = step3[first_branch : step3.index("elif", first_branch)]
    assert "$CONTAINER_IP:5000/genesis" in unverified
    assert "ssh -L 5000:$CONTAINER_IP:5000" in unverified
    # The final report says WHY the host routes are missing, and how to fix it,
    # rather than leaving that to a warning hundreds of lines earlier.
    assert 'Host routes are not offered: ${_proxy_report%%"$_nl"*}' in unverified
    assert 'To enable them: ${_proxy_report#*"$_nl"fix: }' in unverified
    assert "localhost:5000/genesis  " not in unverified.replace(
        "Then open: http://localhost:5000/genesis", ""
    )


def test_failed_check_does_not_exit_setup():
    code = _host_setup()
    block = code[code.index("PROXY_OK=0") : code.index("# ── Run install.sh inside container")]
    assert "exit" not in block
