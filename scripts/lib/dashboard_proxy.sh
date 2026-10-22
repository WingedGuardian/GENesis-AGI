#!/bin/bash
# Verify that an Incus proxy device really serves the dashboard on every host
# address before host-setup.sh advertises a host URL (#3161).
#
# Sourced by host-setup.sh. One function:
#
#   dashboard_proxy_check <container> <port>
#
# Returns 0 when a proxy device on the container listens on <port> at the IPv4
# wildcard (tcp:0.0.0.0), binds on the host, sends no PROXY-protocol header,
# and connects to <port> at an IPv4 loopback address or an IPv4 address the
# container holds; it prints one line naming that device. Returns 1 otherwise
# and prints two lines: what was found, then "fix: <command>" naming every
# setting that differs. Never exits the caller.
#
# This checks the device CONFIGURATION, not a live listener: the server may not
# be running yet when host-setup reaches this point.
#
# IPv4 wildcard only, on purpose: host-setup's report offers routes through
# several IPv4 host addresses at once (the LAN or Tailscale IP, localhost, an
# SSH tunnel to the host). A device bound to one address answers only one of
# them, and tcp:[::] is the IPv6 wildcard, which need not accept IPv4.
#
# The connect target must be this container because the server listens on
# IPv4 0.0.0.0 inside it (genesis-server.service.template): a target on another
# address, or an IPv6 target, reaches some other service or nothing.
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
import ipaddress, json, sys

container, port = sys.argv[1], sys.argv[2]
want_listen = "tcp:0.0.0.0:%s" % port
want_connect = "tcp:127.0.0.1:%s" % port


def split(value):
    # "tcp:0.0.0.0:5000" -> ("0.0.0.0", "5000"); "tcp:[::]:5000" -> ("::", "5000")
    if not isinstance(value, str) or not value.startswith("tcp:"):
        return None
    host, sep, p = value[4:].rpartition(":")
    if not sep:
        return None
    return host.strip("[]"), p


def own_ipv4(state):
    # IPv4 addresses the container holds, from /1.0/instances/<c>/state.
    found = set()
    network = state.get("network") if isinstance(state, dict) else None
    for iface in (network or {}).values():
        for addr in (iface or {}).get("addresses") or []:
            if isinstance(addr, dict) and addr.get("family") == "inet":
                found.add(addr.get("address"))
    return found


def reaches_dashboard(connect, local_ips):
    # The server listens on IPv4 0.0.0.0 inside the container, so the target
    # must be an IPv4 loopback address or an IPv4 address this container holds.
    if connect is None or connect[1] != port:
        return False
    try:
        ip = ipaddress.IPv4Address(connect[0])
    except ValueError:
        return False
    return ip.is_loopback or connect[0] in local_ips


def problems(dev, local_ips):
    # Every way this device differs from one that serves the advertised routes,
    # paired with the key that repairs it.
    found = []
    listen = split(dev.get("listen"))
    if listen is None or listen != ("0.0.0.0", port):
        if listen is not None and listen[1] == port and listen[0] == "::":
            # [::] is the IPv6 wildcard; the routes host-setup prints are IPv4.
            why = "listens on the IPv6 wildcard only"
        elif listen is not None and listen[1] == port:
            why = "listens only on %s" % listen[0]
        else:
            why = "listen=%s" % dev.get("listen")
        found.append((why, "listen=%s" % want_listen))
    if dev.get("bind", "") not in ("", "host"):
        found.append(("bind=%s" % dev.get("bind"), "bind=host"))
    if str(dev.get("proxy_protocol", "")).lower() not in ("", "false", "0", "no", "off"):
        found.append(("proxy_protocol is on", "proxy_protocol=false"))
    if str(dev.get("nat", "")).lower() not in ("", "false", "0", "no", "off"):
        # NAT mode only starts with a connect address that is one of the
        # instance static NIC addresses; this check does not verify that, and
        # without NAT the loopback target works because the server listens on
        # 0.0.0.0.
        found.append(("nat mode is on, which this check does not verify", "nat=false"))
    if not reaches_dashboard(split(dev.get("connect")), local_ips):
        found.append(
            ("connect=%s is not this container on port %s" % (dev.get("connect"), port),
             "connect=%s" % want_connect)
        )
    return found


def fix_line(name, found):
    keys = " ".join(key for _, key in found)
    if name in local:
        return "fix: incus config device set %s %s %s" % (container, name, keys)
    # Inherited from a profile: override copies it onto this instance only,
    # where editing the profile would change every instance that uses it.
    return "fix: incus config device override %s %s %s" % (container, name, keys)


decoder = json.JSONDecoder()
try:
    text = sys.stdin.read().lstrip()
    data, end = decoder.raw_decode(text)
    devices = data.get("expanded_devices")
    local = data.get("devices") or {}
    if not isinstance(devices, dict) or not isinstance(local, dict):
        raise ValueError("no device maps")
except Exception:
    print("could not read the container device list")
    print("fix: check `incus query /1.0/instances/%s` by hand" % container)
    sys.exit(1)
try:
    rest = text[end:].lstrip()
    local_ips = own_ipv4(decoder.raw_decode(rest)[0]) if rest else set()
except Exception:
    local_ips = set()  # unreadable state: only loopback targets verify

rejected = []
fix = None
for name in sorted(devices):
    dev = devices[name]
    if not isinstance(dev, dict) or dev.get("type") != "proxy":
        continue
    listen = split(dev.get("listen"))
    if listen is None or listen[1] != port:
        continue
    found = problems(dev, local_ips)
    if not found:
        print("%s: listen=%s connect=%s" % (name, dev.get("listen"), dev.get("connect")))
        sys.exit(0)
    if fix is None:
        fix = fix_line(name, found)
    rejected.append("%s (%s)" % (name, "; ".join(why for why, _ in found)))

if rejected:
    print("no proxy device serves port %s on every address: %s" % (port, ", ".join(rejected)))
    print(fix)
else:
    print("no proxy device listens on port %s" % port)
    existing = local.get("dashboard-proxy")
    if isinstance(existing, dict) and existing.get("type") == "proxy":
        # The default name is taken by a proxy on another port: `add` would fail.
        print(fix_line("dashboard-proxy", problems(existing, local_ips)))
    else:
        dev_name = "genesis-dashboard-proxy" if existing is not None else "dashboard-proxy"
        print(
            "fix: incus config device add %s %s proxy listen=%s connect=%s"
            % (container, dev_name, want_listen, want_connect)
        )
sys.exit(1)
'

dashboard_proxy_check() {
    local container="$1" port="$2"
    local json="" state=""
    if ! json=$(incus query "/1.0/instances/$container" 2>/dev/null); then
        echo "could not query the container (incus query failed)"
        echo "fix: check that the container exists and incus is reachable"
        return 1
    fi
    # The container's own addresses, to recognise a connect target that is
    # this container. Unreadable: only loopback targets verify.
    state=$(incus query "/1.0/instances/$container/state" 2>/dev/null) || state=""
    local out="" rc=0
    out=$(printf '%s\n%s\n' "$json" "$state" | incus exec "$container" -T -- python3 -c "$_DASHBOARD_PROXY_PY" "$container" "$port" 2>/dev/null) || rc=$?
    if [ "$rc" -ne 0 ] && [ -z "$out" ]; then
        echo "could not run python3 in the container to read its device list"
        echo "fix: check \`incus query /1.0/instances/$container\` by hand"
        return 1
    fi
    printf '%s\n' "$out"
    return "$rc"
}
