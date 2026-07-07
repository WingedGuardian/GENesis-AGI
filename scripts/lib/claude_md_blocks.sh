# Genesis — user-level CLAUDE.md sentinel-block helpers.
# Sourced by update.sh (in-container) and host-setup.sh (host-side); not
# executable on its own. Single source of truth for the network-identity
# block: the two writers previously drifted (update.sh dropped the
# Tailscale line and its `hostname -I | awk '{print $1}'` detection picked
# the tailscale0 address as the "Container IP" on Tailscale installs).

# detect_container_lan_ip — first global IPv4 that is NOT on a tailscale
# interface; falls back to the old hostname -I behavior if none found.
detect_container_lan_ip() {
    local lan_ip
    lan_ip=$(ip -4 -o addr show scope global 2>/dev/null \
        | awk '$2 !~ /^tailscale/ {print $4}' | cut -d/ -f1 | head -1 || true)
    if [ -z "$lan_ip" ]; then
        lan_ip=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
    fi
    printf '%s' "$lan_ip"
}

# detect_container_lan_ipv6 — first global IPv6 NOT on a tailscale
# interface (Tailscale's fd7a::/48 ULA shows up as scope global too).
detect_container_lan_ipv6() {
    ip -6 -o addr show scope global 2>/dev/null \
        | awk '$2 !~ /^tailscale/ {print $4}' | cut -d/ -f1 | head -1 || true
    return 0
}

# detect_tailscale_ip — Tailscale IPv4 if the daemon or interface is
# present; prints nothing (rc 0) when Tailscale is absent. Tailscale is
# the default-but-optional overlay, so absence is a normal configuration.
detect_tailscale_ip() {
    local ts=""
    if command -v tailscale >/dev/null 2>&1; then
        ts=$(tailscale ip -4 2>/dev/null | head -1 || true)
    fi
    if [ -z "$ts" ]; then
        ts=$(ip -4 -o addr show dev tailscale0 2>/dev/null \
            | awk '{print $4}' | cut -d/ -f1 | head -1 || true)
    fi
    [ -n "$ts" ] && printf '%s\n' "$ts"
    return 0
}

# build_network_identity_block <container_ip> <container_ipv6> <host_ip> <host_ipv6> <tailscale_ip>
# Prints the canonical block CONTENT (heading + bullets) to stdout, without
# sentinel markers. Empty arguments drop their line/suffix (Tailscale, v6),
# except the required IPs which fall back to "localhost". Callers detect
# values in their own environment (update.sh in-container, host-setup.sh
# host-side) and pass them in — the FORMAT lives only here.
build_network_identity_block() {
    local c_ip="$1" c_ipv6="$2" host_ip="$3" host_ipv6="$4" ts_ip="$5"
    echo "## Network Identity"
    echo ""
    printf -- "- **Container IP**: %s" "${c_ip:-localhost}"
    [ -n "$c_ipv6" ] && printf " (v6: %s)" "$c_ipv6"
    echo ""
    printf -- "- **Host VM IP**: %s" "${host_ip:-localhost}"
    [ -n "$host_ipv6" ] && printf " (v6: %s)" "$host_ipv6"
    echo ""
    [ -n "$ts_ip" ] && printf -- "- **Tailscale**: %s\n" "$ts_ip"
    printf -- "- **Dashboard**: http://%s:5000 (via proxy device)\n" "${host_ip:-localhost}"
}

# write_sentinel_block <file> <name>
# Replaces the <!-- begin:name --> .. <!-- end:name --> block in <file>
# with stdin wrapped in fresh markers. Appends at EOF, matching the
# long-standing writer behavior (block position in the file is not
# preserved; readers key on the markers, not the position).
write_sentinel_block() {
    local file="$1" name="$2"
    sed -i "/<!-- begin:${name} -->/,/<!-- end:${name} -->/d" "$file"
    {
        echo "<!-- begin:${name} -->"
        cat
        echo "<!-- end:${name} -->"
    } >> "$file"
}
