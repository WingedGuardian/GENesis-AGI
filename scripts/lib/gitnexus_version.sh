# shellcheck shell=bash
# shellcheck disable=SC2034  # sourced constant; consumers use it
# GitNexus version verified against Genesis's index and query workflows.
GENESIS_GITNEXUS_VERSION="1.6.12"

# gitnexus@1.6.12 declares: ^22.18.0 || >=24.11.0. Keep this exact shape:
# Node 23 is intentionally excluded by upstream's semver range.
genesis_gitnexus_node_version_supported() {
    local version="${1#v}" major minor
    [[ "$version" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]] || return 1
    major="${BASH_REMATCH[1]}"
    minor="${BASH_REMATCH[2]}"

    if (( major == 22 )); then
        (( minor >= 18 ))
    elif (( major == 24 )); then
        (( minor >= 11 ))
    else
        (( major > 24 ))
    fi
}

genesis_gitnexus_node_supported() {
    local version
    command -v node >/dev/null 2>&1 || return 1
    version="$(node --version 2>/dev/null)" || return 1
    genesis_gitnexus_node_version_supported "$version"
}

genesis_gitnexus_resolve_binary() {
    local candidate="" prefix=""
    if [ -n "${GITNEXUS_BIN:-}" ] && [ -x "$GITNEXUS_BIN" ]; then
        printf '%s\n' "$GITNEXUS_BIN"
        return 0
    fi
    candidate="$(command -v gitnexus 2>/dev/null || true)"
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
        printf '%s\n' "$candidate"
        return 0
    fi
    if command -v npm >/dev/null 2>&1; then
        prefix="$(npm config get prefix 2>/dev/null || true)"
        candidate="${prefix%/}/bin/gitnexus"
        if [[ "$prefix" = /* ]] && [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    fi
    for candidate in \
        "${HOME:-}/.npm-global/bin/gitnexus" \
        "${HOME:-}/.local/bin/gitnexus" \
        /usr/local/bin/gitnexus \
        /usr/bin/gitnexus; do
        if [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

genesis_gitnexus_installed_version() {
    local binary
    binary="$(genesis_gitnexus_resolve_binary)" || return 1
    "$binary" --version 2>/dev/null
}

genesis_gitnexus_installed_is_pinned() {
    local version
    version="$(genesis_gitnexus_installed_version)" || return 1
    [[ "${version#v}" == "$GENESIS_GITNEXUS_VERSION" ]]
}

genesis_gitnexus_version_is_newer_than_pin() {
    local version="${1#v}" major minor patch pin_major pin_minor pin_patch
    [[ "$version" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)(-[0-9A-Za-z.-]+)?$ ]] || return 1
    major="${BASH_REMATCH[1]}"
    minor="${BASH_REMATCH[2]}"
    patch="${BASH_REMATCH[3]}"
    IFS=. read -r pin_major pin_minor pin_patch <<< "$GENESIS_GITNEXUS_VERSION"

    (( major > pin_major )) && return 0
    (( major < pin_major )) && return 1
    (( minor > pin_minor )) && return 0
    (( minor < pin_minor )) && return 1
    (( patch > pin_patch ))
}

# Return 0 only when the reviewed pin is active. Return 2 when a newer version
# is installed: never auto-downgrade it, because it may already have written a
# newer storage format. Return 3 for an installed version whose output cannot
# be classified safely. Other failures return 1.
genesis_gitnexus_ensure_pin() {
    local actual=""
    if genesis_gitnexus_resolve_binary >/dev/null; then
        actual="$(genesis_gitnexus_installed_version)" || actual=""
        genesis_gitnexus_installed_is_pinned && return 0
        [[ "${actual#v}" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]] || return 3
        genesis_gitnexus_version_is_newer_than_pin "$actual" && return 2
    fi

    npm install -g --engine-strict "gitnexus@${GENESIS_GITNEXUS_VERSION}" \
        >/dev/null 2>&1 || return 1
    genesis_gitnexus_installed_is_pinned
}
