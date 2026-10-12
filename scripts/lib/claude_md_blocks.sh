# shellcheck shell=bash
# (sourced fragment, not an executable script — no shebang)
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
# interface (Tailscale's IPv6 ULA range shows up as scope global too).
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
#
# A marker is a WHOLE line (surrounding whitespace and a CR ignored). Every
# well-formed begin..end pair is removed before the fresh block is appended,
# so a duplicated block heals itself. A begin with no end, an end with no
# begin, or a begin inside an open block leaves the file UNTOUCHED, prints
# the shape and line numbers on stderr and returns 1. The sed range this
# replaces deleted everything from an unmatched begin to the end of the
# file, the user's own notes included.
#
# The same rule lives in src/genesis/infra_profile/claude_md.py (one fixture
# table, tests/test_scripts/test_claude_md_blocks.py, drives both).
# host-setup.sh ships this function into the container with `declare -f`,
# so it may call commands only, never other functions of this file.
write_sentinel_block() {
    local file="$1" name="$2" body dir tmp rc
    # Read all of stdin first: a refusal must not SIGPIPE the producer.
    body=$(cat; printf x)
    body=${body%x}
    if [ -n "$body" ] && [ "${body: -1}" != $'\n' ]; then
        body+=$'\n'
    fi
    dir=$(dirname -- "$file")
    tmp=$(mktemp "$dir/.claude-md-block.XXXXXX") || return 1
    rc=0
    if [ -e "$file" ]; then
        # ENVIRON, not -v: -v processes backslash escapes in the value.
        # Exit 3 = unbalanced markers; any other failure is not a marker problem.
        _SENTINEL_NAME="$name" LC_ALL=C awk '
            BEGIN {
                name = ENVIRON["_SENTINEL_NAME"]
                b = "<!-- begin:" name " -->"; e = "<!-- end:" name " -->"; open = 0
            }
            {
                t = $0
                gsub(/^[[:space:]]+|[[:space:]]+$/, "", t)
                if (t == b) {
                    if (open) { bad = "a second begin at line " NR " inside the block opened at line " start; exit 3 }
                    open = 1; start = NR; next
                }
                if (t == e) {
                    if (!open) { bad = "an end at line " NR " with no begin before it"; exit 3 }
                    open = 0; next
                }
                if (!open) print
            }
            END {
                if (bad == "" && open) bad = "the begin at line " start " has no end"
                if (bad != "") { print bad > "/dev/stderr"; exit 3 }
            }
        ' "$file" > "$tmp" 2> "$tmp.err" || rc=$?
    fi
    if [ "$rc" -eq 3 ]; then
        echo "CLAUDE.md block '$name' left unchanged in $file: $(cat "$tmp.err" 2>/dev/null)." \
            "Each <!-- begin:$name --> needs one <!-- end:$name --> on a later line;" \
            "fix the markers by hand and re-run." >&2
        rm -f "$tmp" "$tmp.err"
        return 1
    elif [ "$rc" -ne 0 ]; then
        echo "CLAUDE.md block '$name' left unchanged: could not read $file" \
            "($(cat "$tmp.err" 2>/dev/null))." >&2
        rm -f "$tmp" "$tmp.err"
        return 1
    fi
    rm -f "$tmp.err"
    {
        printf '%s\n' "<!-- begin:${name} -->"
        printf '%s' "$body"
        printf '%s\n' "<!-- end:${name} -->"
    } >> "$tmp"
    if [ -e "$file" ]; then
        chmod --reference="$file" "$tmp" 2>/dev/null || true
    fi
    mv -f "$tmp" "$file"
}
