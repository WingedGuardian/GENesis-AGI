# shellcheck shell=bash
# Genesis — where the credentials file lives. Sourced, not executed.
#
# genesis_secrets_path <repo_root> — print the secrets file this process reads
# or writes. Bash twin of src/genesis/secrets_location.py; same order, first
# match wins:
#   1. $SECRETS_PATH when set (~ and ~user expanded, as Python's expanduser)
#   2. $HOME/.config/genesis/secrets.env when it exists
#   3. <repo_root>/secrets.env, then $HOME/genesis/secrets.env, when one exists
#   4. otherwise $HOME/.config/genesis/secrets.env
# XDG_CONFIG_HOME is deliberately ignored (the systemd user manager lacks it).
# A candidate behind a directory this process cannot search is kept, as the
# Python resolver keeps it: the reader then fails loudly instead of silently
# loading an older legacy copy.
# tests/test_secrets_location.py keeps the twins in step.

# _genesis_secrets_present <path> — a regular file, or one that may exist but
# cannot be checked because the nearest existing directory is not searchable.
_genesis_secrets_present() {
    local dir
    [ -f "$1" ] && return 0
    [ -e "$1" ] && return 1
    # Walk up to the nearest directory that exists. A relative path ends at
    # ".", the directory it is relative to (a bare "${dir%/*}" never shortens
    # a slash-free name, so the loop must stop there itself).
    case "$1" in */*) dir="${1%/*}" ;; *) dir="." ;; esac
    while :; do
        [ -n "$dir" ] || dir="/"
        if [ -d "$dir" ]; then
            [ -x "$dir" ] && return 1
            return 0
        fi
        case "$dir" in
            */*) dir="${dir%/*}" ;;
            *) dir="." ;;
        esac
    done
}

genesis_secrets_path() {
    local repo_root="${1:?genesis_secrets_path: repo root required}" candidate
    if [ -n "${SECRETS_PATH:-}" ]; then
        case "$SECRETS_PATH" in
            \~/*) printf '%s\n' "$HOME/${SECRETS_PATH#\~/}" ;;
            \~) printf '%s\n' "$HOME" ;;
            \~*)
                # Python's Path.expanduser also supports ~user through passwd.
                local prefix="${SECRETS_PATH%%/*}" record user_home
                if ! record="$(getent passwd -- "${prefix#\~}")"; then
                    printf '%s\n' 'cannot resolve SECRETS_PATH user home' >&2
                    return 1
                fi
                if [ "${record%%:*}" != "${prefix#\~}" ]; then
                    printf '%s\n' 'cannot resolve SECRETS_PATH user home' >&2
                    return 1
                fi
                IFS=: read -r _ _ _ _ _ user_home _ <<< "$record"
                printf '%s%s\n' "$user_home" "${SECRETS_PATH#"$prefix"}"
                ;;
            *) printf '%s\n' "$SECRETS_PATH" ;;
        esac
        return 0
    fi
    for candidate in "$HOME/.config/genesis/secrets.env" "$repo_root/secrets.env" "$HOME/genesis/secrets.env"; do
        if _genesis_secrets_present "$candidate"; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    printf '%s\n' "$HOME/.config/genesis/secrets.env"
}
