# shellcheck shell=bash
# Durable alert enqueue for shell scripts (F.3).
#
# Writes a schema-v1 JSON entry to ~/.genesis/alerts/queue/ (atomically),
# matching genesis.guardian.alert.queue. The container awareness tick drains it
# to Telegram via the outreach pipeline, so an alert raised while the channel is
# down survives instead of being lost to a log line.
#
# Best-effort by contract: every failure is swallowed so a queue problem can
# NEVER break the calling script (backup runs, the tmp watchgod).
#
#   queue_alert <severity> <source> <title> <body> [dedupe_key]
#
# severity: info|warning|critical|emergency   source: short id (e.g. backup)
# Title/body are passed via the environment (never interpolated into code), so
# arbitrary quotes/newlines are safe.

_ALERT_QUEUE_ROOT="${GENESIS_ALERT_QUEUE_ROOT:-$HOME/.genesis/alerts/queue}"

queue_alert() {
    local severity="${1:-warning}" source="${2:-shell}" title="${3:-}" body="${4:-}" dedupe="${5:-}"
    mkdir -p "$_ALERT_QUEUE_ROOT" 2>/dev/null || return 0
    ALERT_QUEUE_ROOT="$_ALERT_QUEUE_ROOT" \
    ALERT_SEVERITY="$severity" ALERT_SOURCE="$source" \
    ALERT_TITLE="$title" ALERT_BODY="$body" ALERT_DEDUPE="$dedupe" \
    python3 - <<'PY' 2>/dev/null || true
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
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
try:
    os.write(fd, json.dumps(entry, ensure_ascii=False).encode("utf-8"))
finally:
    os.close(fd)
os.replace(tmp, os.path.join(root, "%.6f-%s.json" % (ts, uuid.uuid4().hex)))
PY
}
