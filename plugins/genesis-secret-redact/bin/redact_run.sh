# shellcheck shell=bash
# redact_run.sh — SOURCED, never executed: source redact_run.sh <base64 command> [repo-root]
#
# Runs a Monitor or background Bash command with its stdout and stderr each passed through stream_filter.py,
# which replaces known secrets.env values with placeholders before Claude Code records them.
#
# Sourced into the shell Claude Code built for the command, and the command runs in a subshell of that shell
# via eval, so it keeps everything Claude Code set up there: its shell functions (a `pkill` that refuses to kill
# the Claude process, `find`/`grep` shims), options and exported variables. A separate `bash -c` would drop
# them. The command arrives base64-encoded so no quoting in it can break this wrapper.
#
# Exits with the command's own status as soon as the command exits and the filters have drained what it wrote;
# a background child the command left behind keeps a reader (the filter) and is never killed by the wrap.
# Wired by stream_wrap.py (the plugin's PreToolUse hook).
_sr_cmd=$(printf '%s' "${1:-}" | base64 -d) || { echo "secret-redact: undecodable command" >&2; exit 2; }
# Everything below is bash. Another shell (zsh, which Claude Code also supports) runs the command unwrapped.
if [ -z "${BASH_VERSION:-}" ]; then
    echo "secret-redact: not a bash shell; this command's output is NOT redacted" >&2
    ( set --; eval "$_sr_cmd" )
    exit $?
fi
# Claude Code's shell has job control on; without this its "[n]+ Exit N ..." notice for the jobs below lands in
# the command's own output (measured).
set +m
_sr_here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
_sr_root=()
_sr_py=python3
if [ -n "${2:-}" ]; then
    _sr_root=("$2")
    # The install's venv has python-dotenv, the parser the runtime itself reads the secrets file with.
    [ -x "$2/.venv/bin/python" ] && _sr_py="$2/.venv/bin/python"
fi
# Without a filter that can start, or a place for the FIFOs, the command still runs, unwrapped: the wrap is a
# privacy measure, never a reason to stop work. Say so where the session will see it. The filter is imported
# first because one that fails to start would leave the command writing into a FIFO with no reader, losing its
# output (measured: with no python3, both lines of output were lost and nothing said so).
_sr_dir=""
if ! "$_sr_py" -I -c 'import sys; sys.path.insert(0, sys.argv[1]); import stream_filter' "$_sr_here" 2>/dev/null \
    || ! _sr_dir=$(mktemp -d -p "${TMPDIR:-/tmp}" secret-redact.XXXXXX) || ! mkfifo "$_sr_dir/out" "$_sr_dir/err"; then
    [ -n "$_sr_dir" ] && { unlink "$_sr_dir/out"; unlink "$_sr_dir/err"; rmdir "$_sr_dir"; } 2>/dev/null
    echo "secret-redact: could not set up the output filter; this command's output is NOT redacted" >&2
    ( set --; eval "$_sr_cmd" )
    exit $?
fi
"$_sr_py" -I "$_sr_here/stream_filter.py" "$_sr_dir/done" "$_sr_dir/out.drained" "${_sr_root[@]}" < "$_sr_dir/out" &
"$_sr_py" -I "$_sr_here/stream_filter.py" "$_sr_dir/done" "$_sr_dir/err.drained" "${_sr_root[@]}" < "$_sr_dir/err" >&2 &
# `set --` so the command does not see this wrapper's own arguments as $1, $2.
( set --; eval "$_sr_cmd" ) > "$_sr_dir/out" 2> "$_sr_dir/err" &
_sr_pid=$!
# Every step from here is written so that a caller's `set -e` cannot end this file early: a non-zero status of
# the command is the normal case, and stopping before `done` exists would leave the filters waiting.
wait "$_sr_pid" && _sr_rc=0 || _sr_rc=$?
: > "$_sr_dir/done" || true
# The filters signal once the command's output is drained (at once when it left no writer behind); 5 s bounds a
# filter that died before signalling.
for _ in $(seq 1 100); do
    [ -e "$_sr_dir/out.drained" ] && [ -e "$_sr_dir/err.drained" ] && break
    sleep 0.05 || true
done
# The FIFOs stay open in the filters (and any lingering child), so removing the names now is safe.
for _sr_f in out err done out.drained err.drained; do unlink "$_sr_dir/$_sr_f" 2>/dev/null || true; done
rmdir "$_sr_dir" 2>/dev/null || true
exit "$_sr_rc"
