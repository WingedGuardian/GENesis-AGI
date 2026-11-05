# shellcheck shell=bash
# Make `timeout` the GNU one where the system's is uutils (Ubuntu 26.04).
#
# uutils' `timeout` sends a KILL to the direct child alone, so a child that
# ignored TERM and has a child of its own leaves that one running, holding whatever
# it holds (uutils issue 15768). Every bound in these scripts relies on the kill
# reaching the whole group. Ubuntu keeps GNU's as `gnutimeout`, so this defines a
# function of that name and the call sites stay as written.
#
# Runs at source time. It reaches what this shell runs itself, `$T cmd` included. It
# does not reach `command`, `exec`, `env`, `xargs` or a child `bash -c`. A function
# already defined wins, because a test defining its own means to use it.
# No `gnutimeout`: nothing changes, and the kill stays as weak as the system's.
if command -v gnutimeout >/dev/null 2>&1 && ! declare -F timeout >/dev/null; then
    case "$(timeout --version 2>/dev/null)" in
        *uutils*) timeout() { gnutimeout "$@"; } ;;
    esac
fi
