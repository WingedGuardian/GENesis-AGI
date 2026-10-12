#!/bin/bash
# Verify that an Incus proxy device really serves the dashboard on every host
# address before host-setup.sh advertises a host URL (#3161).
#
# Sourced by host-setup.sh. One function:
#
#   dashboard_proxy_check <container> <port>
#
# Returns 0 when a proxy device on the container listens on <port> at a
# WILDCARD address (tcp:0.0.0.0 or tcp:[::]), binds on the host, sends no
# PROXY-protocol header, and connects to <port> inside the container; it prints
# one line naming that device. Returns 1 otherwise and prints two lines: what
# was found, then "fix: <command>" with the command that would make it serve.
# Never exits the caller.
#
# Wildcard only, on purpose: host-setup's report offers routes through several
# host addresses at once (the LAN or Tailscale IP, localhost, an SSH tunnel to
# the host), and a device bound to one address answers only one of them.
#
# The device is found by its LISTEN PORT, not its name: installs name it
# differently (host-setup creates `dashboard-proxy`; other setups use their own
# names), and a device inherited from a profile cannot be read per instance with
# `incus config device get` ("Device from profile(s) cannot be retrieved for
# individual instance", measured on incus 6.0.0). `incus query` returns
# `expanded_devices`, which includes profile devices, and `devices`, which holds
# only the instance's own, so the two together say which is which.
#
# The JSON is parsed by the CONTAINER's python3 (installed earlier by
# host-setup.sh), because python3 on the bare host is not something host-setup
# provisions.

_DASHBOARD_PROXY_PY='
import json, sys

container, port = sys.argv[1], sys.argv[2]
want = "tcp:0.0.0.0:%s" % port


def split(value):
    # "tcp:0.0.0.0:5000" -> ("0.0.0.0", "5000"); "tcp:[::]:5000" -> ("::", "5000")
    if not isinstance(value, str) or not value.startswith("tcp:"):
        return None
    host, sep, p = value[4:].rpartition(":")
    if not sep:
        return None
    return host.strip("[]"), p


try:
    data = json.load(sys.stdin)
    devices = data.get("expanded_devices")
    local = data.get("devices") or {}
    if not isinstance(devices, dict) or not isinstance(local, dict):
        raise ValueError("no device maps")
except Exception:
    print("could not read the container device list")
    print("fix: check `incus query /1.0/instances/%s` by hand" % container)
    sys.exit(1)

rejected = []
fix_name = None
fix_keys = []
for name in sorted(devices):
    dev = devices[name]
    if not isinstance(dev, dict) or dev.get("type") != "proxy":
        continue
    listen = split(dev.get("listen"))
    if listen is None or listen[1] != port:
        continue
    problems, keys = [], []
    if listen[0] not in ("0.0.0.0", "::"):
        problems.append("listens only on %s" % listen[0])
        keys.append("listen=%s" % want)
    if dev.get("bind", "") not in ("", "host"):
        problems.append("bind=%s" % dev.get("bind"))
        keys.append("bind=host")
    if str(dev.get("proxy_protocol", "")).lower() not in ("", "false", "0", "no", "off"):
        problems.append("proxy_protocol is on")
        keys.append("proxy_protocol=false")
    connect = split(dev.get("connect"))
    if connect is None or connect[1] != port:
        problems.append("connect=%s" % dev.get("connect"))
        keys.append("connect=tcp:127.0.0.1:%s" % port)
    if not problems:
        print("%s: listen=%s connect=%s" % (name, dev.get("listen"), dev.get("connect")))
        sys.exit(0)
    if fix_name is None:
        fix_name, fix_keys = name, keys
    rejected.append("%s (%s)" % (name, "; ".join(problems)))

if rejected:
    print("no proxy device serves port %s on every address: %s" % (port, ", ".join(rejected)))
    if fix_name in local:
        print("fix: incus config device set %s %s %s" % (container, fix_name, " ".join(fix_keys)))
    else:
        print(
            "fix: device %s comes from a profile; set %s on it with `incus profile device set`"
            % (fix_name, " ".join(fix_keys))
        )
else:
    print("no proxy device listens on port %s" % port)
    full = "listen=%s connect=tcp:127.0.0.1:%s" % (want, port)
    existing = local.get("dashboard-proxy")
    if isinstance(existing, dict) and existing.get("type") == "proxy":
        # The default name is taken by a proxy on another port: `add` would fail.
        print("fix: incus config device set %s dashboard-proxy %s" % (container, full))
    else:
        dev_name = "genesis-dashboard-proxy" if existing is not None else "dashboard-proxy"
        print("fix: incus config device add %s %s proxy %s" % (container, dev_name, full))
sys.exit(1)
'

dashboard_proxy_check() {
    local container="$1" port="$2"
    local json=""
    if ! json=$(incus query "/1.0/instances/$container" 2>/dev/null); then
        echo "could not query the container (incus query failed)"
        echo "fix: check that the container exists and incus is reachable"
        return 1
    fi
    local out="" rc=0
    out=$(printf '%s' "$json" | incus exec "$container" -T -- python3 -c "$_DASHBOARD_PROXY_PY" "$container" "$port" 2>/dev/null) || rc=$?
    if [ "$rc" -ne 0 ] && [ -z "$out" ]; then
        echo "could not run python3 in the container to read its device list"
        echo "fix: check \`incus query /1.0/instances/$container\` by hand"
        return 1
    fi
    printf '%s\n' "$out"
    return "$rc"
}
