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

# Every resolvable gitnexus binary, in a canonical order that does NOT depend on
# the caller's PATH. genesis-code-intel.service supplies its own PATH with
# ~/.npm-global/bin first while an interactive MCP client may resolve
# /usr/local/bin first — PATH-first resolution lets the analyzer and the
# launcher silently pick different binaries (and different storage formats).
_genesis_gitnexus_candidates() {
    local candidate="" prefix=""
    local -a found=()
    _genesis_gitnexus_add() {
        [ -n "$1" ] && [ -x "$1" ] || return 0
        local existing
        for existing in ${found[@]+"${found[@]}"}; do
            [ "$existing" = "$1" ] && return 0
        done
        found+=("$1")
    }
    _genesis_gitnexus_add "${GITNEXUS_BIN:-}"
    if command -v npm >/dev/null 2>&1; then
        prefix="$(npm config get prefix 2>/dev/null || true)"
        if [[ "$prefix" = /* ]]; then
            _genesis_gitnexus_add "${prefix%/}/bin/gitnexus"
        fi
    fi
    for candidate in \
        "${HOME:-}/.npm-global/bin/gitnexus" \
        "${HOME:-}/.local/bin/gitnexus" \
        /usr/local/bin/gitnexus \
        /usr/bin/gitnexus; do
        _genesis_gitnexus_add "$candidate"
    done
    _genesis_gitnexus_add "$(command -v gitnexus 2>/dev/null || true)"
    # EMIT NOTHING when nothing was found. `printf '%s\n'` with no arguments
    # still writes a newline, which the caller's `while read` turned into a
    # one-element list containing the empty string -- so "no GitNexus anywhere"
    # resolved SUCCESSFULLY to "". MEASURED: `genesis_gitnexus_ensure_pin` then
    # believed a binary was present, read an empty version, failed the semver
    # check and returned 3 instead of installing. A resolver that succeeds with
    # no result is a fail-OPEN, and every caller that trusts the exit code
    # inherits it.
    [ "${#found[@]}" -gt 0 ] || return 0
    printf '%s\n' "${found[@]}"
}

# The version of ONE candidate, WITHOUT executing it where that can be avoided.
#
# WHY NOT JUST `"$candidate" --version`. GitNexus is a Node script, so running it
# runs whatever `node` is first on PATH. Any caller that stubs node -- which the
# launcher's own node-version gate forces its tests to do -- makes every
# Node-based candidate report the STUB's output instead of its version. MEASURED:
# with a stubbed node echoing `v22.22.2`, the real install "reported" v22.22.2,
# the shadow scan read that as a conflict with the 1.6.12 candidate beside it,
# and the launcher refused to start with "GitNexus is not installed".
#
# Reading package.json cannot be hijacked that way, and npm always installs one
# next to the binary. The package NAME is checked so walking up cannot pick up an
# unrelated manifest. Executing the candidate remains the fallback, because a
# binary with no manifest (a test fake, a hand-built copy) still has to be
# readable -- but it is the last resort rather than the first.
_genesis_gitnexus_version_of() {
    local binary="${1:-}" real="" dir="" pkg="" name="" version=""
    [ -n "$binary" ] || return 1
    real="$(readlink -f "$binary" 2>/dev/null || printf '%s' "$binary")"
    dir="$(dirname "$real")"
    # Bounded walk: dist/cli/index.js -> dist/cli -> dist -> package root.
    local depth=0
    while [ "$depth" -lt 5 ] && [ -n "$dir" ] && [ "$dir" != "/" ]; do
        pkg="$dir/package.json"
        if [ -r "$pkg" ]; then
            name="$(sed -n 's/.*"name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$pkg" | head -1)"
            if [ "$name" = "gitnexus" ]; then
                version="$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$pkg" | head -1)"
                if [ -n "$version" ]; then
                    printf '%s' "$version"
                    return 0
                fi
            fi
        fi
        dir="$(dirname "$dir")"
        depth=$(( depth + 1 ))
    done
    "$binary" --version 2>/dev/null
}

genesis_gitnexus_resolve_binary() {
    # AN EXPLICIT OVERRIDE ENDS THE SEARCH, and therefore the shadow scan too.
    # The scan exists to resolve AMBIGUITY about which of several installations
    # a caller will get; naming one removes the ambiguity rather than adding to
    # it. Scanning anyway meant that on any machine with a real install beside
    # the named binary, the resolver refused a conflict the operator had already
    # settled -- and the launcher then reported "not installed", which is false
    # twice over.
    if [ -n "${GITNEXUS_BIN:-}" ] && [ -x "$GITNEXUS_BIN" ]; then
        printf '%s\n' "$GITNEXUS_BIN"
        return 0
    fi
    local -a candidates=()
    local candidate
    while IFS= read -r candidate; do
        # Skip a blank line rather than admitting "" as a candidate: the emitter
        # no longer produces one, and this makes that guarantee local to the
        # reader too, so a future change there cannot reopen the fail-open.
        [ -n "$candidate" ] || continue
        candidates+=("$candidate")
    done < <(_genesis_gitnexus_candidates)
    [ "${#candidates[@]}" -gt 0 ] || return 1
    [ "${#candidates[@]}" -eq 1 ] && { printf '%s\n' "${candidates[0]}"; return 0; }
    # Shadow scan: more than one installation exists. Canonical order already
    # makes every caller resolve the same one, but a second install at a
    # DIFFERENT version may have written a different storage format — refuse the
    # conflict instead of trusting whichever binary the canonical order chose.
    local first_version="" version=""
    for candidate in "${candidates[@]}"; do
        version="$(_genesis_gitnexus_version_of "$candidate" || true)"
        # AN UNREADABLE CANDIDATE ENDS THE SCAN. Letting it through was a
        # two-part bug: `first_version` tracked initialization by EMPTINESS, so
        # an unreadable candidates[0] left it empty and the SECOND candidate
        # initialized it — the conflict check then never compared against the
        # first — and the function returned candidates[0] anyway, handing the
        # caller the very binary whose version could not be read. The launcher
        # rejected it as unpinned and pin enforcement read the empty version as
        # unclassifiable. With more than one installation present there is no
        # basis for choosing, so refuse and name the file.
        if [ -z "$version" ]; then
            printf 'gitnexus: cannot determine the version of %s; remove or repair it\n' \
                "$candidate" >&2
            return 2
        fi
        if [ -z "$first_version" ]; then
            first_version="$version"
        elif [ "${version#v}" != "${first_version#v}" ]; then
            printf 'gitnexus: conflicting installations — %s reports %s, %s reports %s; remove one\n' \
                "${candidates[0]}" "${first_version:-unreadable}" "$candidate" "${version:-unreadable}" >&2
            return 2
        fi
    done
    printf '%s\n' "${candidates[0]}"
}

# Optional $1: a caller that has ALREADY resolved the binary passes it in, so
# the check runs against the file it will execute instead of re-resolving —
# the launcher is the user of this.
genesis_gitnexus_installed_version() {
    local binary="${1:-}"
    if [ -z "$binary" ]; then
        binary="$(genesis_gitnexus_resolve_binary)" || return 1
    fi
    # Same reasoning as the shadow scan: a stubbed `node` would otherwise make
    # this report the stub's output as GitNexus's version, and this value gates
    # the pin check.
    _genesis_gitnexus_version_of "$binary"
}

genesis_gitnexus_installed_is_pinned() {
    local version
    version="$(genesis_gitnexus_installed_version "${1:-}")" || return 1
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
    local actual="" resolved=0
    genesis_gitnexus_resolve_binary >/dev/null || resolved=$?
    # rc 2 from the resolver means installations EXIST but cannot be told apart
    # -- a version conflict, or a candidate whose version cannot be read. That is
    # the documented "cannot be classified safely" case, so it returns 3 rather
    # than falling through to the install: adding another copy does not resolve
    # an ambiguity between the copies already there, and the resolver would
    # refuse again immediately afterwards. rc 1 means nothing is installed at
    # all, which the install below is exactly the answer to.
    if [ "$resolved" = "2" ]; then
        return 3
    fi
    if [ "$resolved" = "0" ]; then
        actual="$(genesis_gitnexus_installed_version)" || actual=""
        genesis_gitnexus_installed_is_pinned && return 0
        [[ "${actual#v}" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]] || return 3
        genesis_gitnexus_version_is_newer_than_pin "$actual" && return 2
        # Two or more copies at the SAME old version resolve cleanly above —
        # but `npm install -g` upgrades only the configured prefix, and the
        # postcondition's shadow scan would then see the new copy beside the
        # untouched one and refuse on the version conflict WE just created.
        # Refuse the partial upgrade and name the copies instead.
        local -a _copies=() _c
        while IFS= read -r _c; do
            [ -n "$_c" ] && _copies+=("$_c")
        done < <(_genesis_gitnexus_candidates)
        if [ "${#_copies[@]}" -gt 1 ]; then
            printf 'gitnexus: %d installations report %s — npm can only upgrade its own prefix;\n' \
                "${#_copies[@]}" "$actual" >&2
            printf '  remove all but one of: %s\n' "${_copies[*]}" >&2
            return 3
        fi
        # A LONE install outside npm's prefix is the same trap one step earlier:
        # `npm install -g` cannot touch it, so the upgrade lands as a SECOND
        # copy at the prefix — the multi-copy conflict above, self-created.
        local _prefix_bin="" _pfx=""
        if command -v npm >/dev/null 2>&1; then
            _pfx="$(npm config get prefix 2>/dev/null || true)"
            [[ "$_pfx" = /* ]] && _prefix_bin="${_pfx%/}/bin/gitnexus"
        fi
        if [ -n "$_prefix_bin" ] && [ "${_copies[0]}" != "$_prefix_bin" ]; then
            printf 'gitnexus: %s is outside the npm prefix (%s) — upgrading would leave it as a stale shadow; remove it first\n' \
                "${_copies[0]}" "$_prefix_bin" >&2
            return 3
        fi
    fi

    npm install -g --engine-strict "gitnexus@${GENESIS_GITNEXUS_VERSION}" \
        >/dev/null 2>&1 || return 1
    genesis_gitnexus_installed_is_pinned
}
