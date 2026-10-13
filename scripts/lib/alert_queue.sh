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
# An entry whose dedupe_key is already waiting in the queue is not written
# again; the call reports success, since that alert is already durable.
# (genesis.guardian.alert.queue returns False there; a shell caller treats 0
# as "the page is durable", which it is.)
#
#   queue_alert <severity> <source> <title> <body> [dedupe_key]
#   queue_alert_try <same arguments>   → 0 queued, non-zero not queued
#   alert_stable_key <prefix> <message>   → prints <prefix>:<12 hex>
#
# severity: info|warning|critical|emergency   source: short id (e.g. backup)
# Title/body are passed via the environment (never interpolated into code), so
# arbitrary quotes/newlines are safe.

_ALERT_QUEUE_ROOT="${GENESIS_ALERT_QUEUE_ROOT:-$HOME/.genesis/alerts/queue}"

queue_alert() {
    queue_alert_try "$@" || true
    return 0
}

# A dedupe key for a message that carries per-run lines. Lines starting with
# "Time:", "Duration:" or "SQLite lines:" change on every run, so they are left
# out of the hash: a repeated identical failure keeps one key (the outreach
# dedup then collapses it within its window), and a different failure gets a
# different key. Never fails the caller, including under errexit and pipefail
# when every line is filtered out.
alert_stable_key() {
    local prefix="${1:-alert}" message="${2:-}" digest
    digest=$(printf '%s' "$message" \
        | { grep -vE '^(Time|Duration|SQLite lines):' || true; } \
        | md5sum 2>/dev/null | cut -c1-12) || digest=""
    printf '%s:%s\n' "$prefix" "$digest"
}

queue_alert_try() {
    local severity="${1:-warning}" source="${2:-shell}" title="${3:-}" body="${4:-}" dedupe="${5:-}"
    mkdir -p "$_ALERT_QUEUE_ROOT" 2>/dev/null || return 1
    ALERT_QUEUE_ROOT="$_ALERT_QUEUE_ROOT" \
    ALERT_SEVERITY="$severity" ALERT_SOURCE="$source" \
    ALERT_TITLE="$title" ALERT_BODY="$body" ALERT_DEDUPE="$dedupe" \
    python3 -I -S - <<'PY' 2>/dev/null
import json, math, os, time, uuid
root = os.environ["ALERT_QUEUE_ROOT"]
ts = time.time()


def text(name, default=""):
    # File names and /proc process names may hold bytes that are not UTF-8;
    # the environment hands them over as surrogates, which cannot be written
    # as UTF-8. Replace them, or the write fails and the alert never queues.
    return os.environ.get(name, default).encode("utf-8", "surrogateescape").decode("utf-8", "replace")


entry = {
    "schema": 1,
    "ts": ts,
    "severity": text("ALERT_SEVERITY", "warning"),
    "source": text("ALERT_SOURCE", "shell"),
    "title": text("ALERT_TITLE"),
    "body": text("ALERT_BODY"),
    "dedupe_key": text("ALERT_DEDUPE") or None,
    "meta": {},
}


def problem(e):
    # Mirror of genesis.guardian.alert.queue.v1_entry_problem (this script runs
    # isolated and cannot import it). A file the drainer would quarantine is not
    # a delivery, so it must not suppress this one. Keep the two in step:
    # tests/test_scripts/test_alert_queue_sh.py checks parity.
    if not isinstance(e, dict):
        return "entry"
    s = e.get("schema")
    if type(s) is not int or s != 1:
        return "schema"
    t = e.get("ts")
    if isinstance(t, bool) or not isinstance(t, (int, float)):
        return "ts"
    try:
        if not math.isfinite(t):
            return "ts"
    except OverflowError:
        return "ts"
    for f in ("title", "body", "severity", "source"):
        if f in e and not isinstance(e[f], str):
            return f
    if not (e.get("title") or e.get("body")):
        return "title"
    k = e.get("dedupe_key")
    if k is not None and not isinstance(k, str):
        return "dedupe_key"
    m = e.get("meta")
    if m is not None and not isinstance(m, dict):
        return "meta"
    for f in ("likely_cause", "proposed_action", "approval_url"):
        v = (m or {}).get(f)
        if v is not None and not isinstance(v, str):
            return "meta." + f
    return None


# The same identity already waiting to be delivered: a second copy adds
# nothing, and a caller that restarts in a loop while delivery is down would
# otherwise fill the queue with it. Already durable, so report success.
if entry["dedupe_key"]:
    for name in os.listdir(root):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                queued = json.load(fh)
            live = problem(queued) is None and queued.get("dedupe_key") == entry["dedupe_key"]
        except Exception:
            continue  # a file this scan cannot judge never suppresses a page
        if live:
            raise SystemExit(0)
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
