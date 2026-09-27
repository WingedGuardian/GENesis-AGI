# shellcheck shell=bash
# Durable alert enqueue for shell scripts (F.3).
#
# Writes a schema-v1 JSON entry to ~/.genesis/alerts/queue/ (atomically),
# matching genesis.guardian.alert.queue. The container awareness tick drains it
# to Telegram via the outreach pipeline, so an alert raised while the channel is
# down survives instead of being lost to a log line.
#
# Best-effort by contract: queue_alert swallows every failure so a queue
# problem can NEVER break the calling script (backup runs, the tmp watchgod).
# queue_alert_try is the same write with its status returned, for a caller
# that must know whether the page is durable before recording it as sent (the
# watchgod's episode markers). It still never exits the caller.
#
#   queue_alert <severity> <source> <title> <body> [dedupe_key]
#   queue_alert_try <same arguments>   → 0 queued, non-zero not queued
#
# severity: info|warning|critical|emergency   source: short id (e.g. backup)
# Title/body are passed via the environment (never interpolated into code), so
# arbitrary quotes/newlines are safe.

_ALERT_QUEUE_ROOT="${GENESIS_ALERT_QUEUE_ROOT:-$HOME/.genesis/alerts/queue}"

queue_alert() {
    queue_alert_try "$@" || true
    return 0
}

queue_alert_try() {
    local severity="${1:-warning}" source="${2:-shell}" title="${3:-}" body="${4:-}" dedupe="${5:-}"
    mkdir -p "$_ALERT_QUEUE_ROOT" 2>/dev/null || return 1
    ALERT_QUEUE_ROOT="$_ALERT_QUEUE_ROOT" \
    ALERT_SEVERITY="$severity" ALERT_SOURCE="$source" \
    ALERT_TITLE="$title" ALERT_BODY="$body" ALERT_DEDUPE="$dedupe" \
    python3 - <<'PY' 2>/dev/null
import json, os, time, uuid
root = os.environ["ALERT_QUEUE_ROOT"]
ts = time.time()
entry = {
    "schema": 1,
    "ts": ts,
    "severity": os.environ.get("ALERT_SEVERITY", "warning"),
    "source": os.environ.get("ALERT_SOURCE", "shell"),
    "title": os.environ.get("ALERT_TITLE", ""),
    "body": os.environ.get("ALERT_BODY", ""),
    "dedupe_key": os.environ.get("ALERT_DEDUPE") or None,
    "meta": {},
}
tmp = os.path.join(root, ".%s.tmp" % uuid.uuid4().hex)
try:
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        data = json.dumps(entry, ensure_ascii=False).encode("utf-8")
        if os.write(fd, data) != len(data):
            raise OSError("short write")
    finally:
        os.close(fd)
    os.replace(tmp, os.path.join(root, "%.6f-%s.json" % (ts, uuid.uuid4().hex)))
except BaseException:
    # A full disk leaves a partial temp behind; never leave it to be drained.
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
PY
}
