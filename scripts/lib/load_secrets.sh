# shellcheck shell=bash
# Genesis — dotenv-safe secrets loader. Sourced, not executed.
#
# load_secrets_file <path> — export KEY=VALUE pairs from a secrets file
# WITHOUT shell-evaluating it. `set -a; source secrets.env` executes the
# file: a value containing $(...) or backticks RUNS that code with the
# caller's privileges. This reader treats the file strictly as data
# (2026-07-10 safety-gate remediation).
#
# Line grammar (kept in step with the escrow reader in
# src/genesis/guardian/credential_bridge.py::_read_dotenv):
#   - blank lines and full-line '#' comments are skipped
#   - an optional leading "export " is accepted
#   - KEY must match [A-Za-z_][A-Za-z0-9_]* — other lines are skipped
#   - value = everything after the first '='
#   - a value that STARTS with a single or double quote is the content up
#     to the matching closing quote; anything after it (e.g. an inline
#     comment) is discarded — matches `source` and the Python
#     _read_dotenv reader (contents verbatim, no escape processing)
#   - unquoted values drop a trailing inline comment (whitespace + '#'
#     onward) and trailing whitespace — matching what `source` yielded
#     for such lines, so the switch is behavior-identical
# Values are exported LITERALLY — never expanded, never executed.

load_secrets_file() {
    local file="$1" line key value
    [ -f "$file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        # Trim surrounding whitespace.
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        case "$line" in
            '' | '#'*) continue ;;
        esac
        case "$line" in
            'export '*) line="${line#export }" ;;
        esac
        case "$line" in
            *=*) ;;
            *) continue ;;
        esac
        key="${line%%=*}"
        value="${line#*=}"
        case "$key" in
            '' | [0-9]* | *[!A-Za-z0-9_]*) continue ;;
        esac
        case "$value" in
            \"*)
                # Value is up to the FIRST closing quote; drop everything
                # after it (a trailing inline comment). `%%\"*` removes the
                # longest suffix starting at the leftmost '"', leaving the
                # quoted content. Without this, `KEY="a b" # c` kept the
                # quotes and diverged from source → wrong backup passphrase
                # (2026-07-10 review).
                value="${value#\"}"
                value="${value%%\"*}"
                ;;
            \'*)
                value="${value#\'}"
                value="${value%%\'*}"
                ;;
            *)
                # sed keeps leftmost-match semantics: 'x #a #b' → 'x',
                # exactly what shell comment parsing gave under source.
                value="$(printf '%s' "$value" \
                    | sed -E 's/[[:space:]]+#.*$//; s/[[:space:]]+$//')"
                ;;
        esac
        export "$key=$value"
    done < "$file"
}
