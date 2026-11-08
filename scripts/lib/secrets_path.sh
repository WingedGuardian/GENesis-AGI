# shellcheck shell=bash
# Genesis — where the credentials file lives. Sourced, not executed.
#
# genesis_secrets_path <repo_root> — print the secrets file this process reads
# or writes. Bash twin of src/genesis/secrets_location.py, same order:
#   1. $SECRETS_PATH when set (~ and ~user expanded, as Python's expanduser)
#   2. otherwise <repo_root>/secrets.env
# tests/test_secrets_location.py keeps the twins in step.

genesis_secrets_path() {
    local repo_root="${1:?genesis_secrets_path: repo root required}"
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
    printf '%s\n' "$repo_root/secrets.env"
}
