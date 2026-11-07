# shellcheck shell=bash
# Genesis — where the credentials file lives. Sourced, not executed.
#
# genesis_secrets_path <repo_root> — print the secrets file this process reads.
# Bash twin of src/genesis/secrets_location.py; same order, first match wins:
#   1. $SECRETS_PATH when set (a leading ~/ is expanded)
#   2. $HOME/.config/genesis/secrets.env when it exists
#   3. <repo_root>/secrets.env, then $HOME/genesis/secrets.env, when one exists
#   4. otherwise $HOME/.config/genesis/secrets.env
# XDG_CONFIG_HOME is deliberately ignored (the systemd user manager lacks it).
# tests/test_secrets_location.py keeps the twins in step.

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
        if [ -f "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    printf '%s\n' "$HOME/.config/genesis/secrets.env"
}
