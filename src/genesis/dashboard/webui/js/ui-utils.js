// ui-utils.js — shared UI helpers for all Genesis dashboard surfaces.
//
// ES module importable by the main dashboard (via dashboard.js) AND the
// standalone pages (neural monitor, event log, error log, voice), which are
// static-served and can't share Jinja macros:
//   import { fmtAge, chipState, chipClass, chipGlyph } from "/js/ui-utils.js";
//
// Spec: ~/.genesis/output/specs/dashboard-ui-spec.md §3.3–3.4.

/** Milliseconds after which a healthy-looking item must render as stale. */
export const STALE_AFTER_MS = 7 * 24 * 60 * 60 * 1000; // 7 days

/**
 * Human age string for a timestamp: "just now" (<90s), "Nm" (<1h),
 * "Nh" (<48h), "Nd" otherwise. Accepts Date, epoch ms, epoch s, or
 * ISO-8601 string; returns "—" for null/invalid input.
 *
 * Callers should put the absolute time in a `title` attribute for hover.
 */
export function fmtAge(ts, nowMs = Date.now()) {
  const ms = _toMs(ts);
  if (ms === null) return "—";
  const delta = nowMs - ms;
  if (delta < 0) return "just now"; // clock skew: never show negative ages
  const sec = delta / 1000;
  if (sec < 90) return "just now";
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m`;
  const hr = Math.round(min / 60);
  if (hr < 48) return `${hr}h`;
  return `${Math.round(hr / 24)}d`;
}

/**
 * Resolve the 5-state chip state: ok / warn / err / stale / off.
 * `health` is the item's own status; `lastRun` (optional) demotes anything
 * older than 7 days to "stale" REGARDLESS of health — a green light on a
 * 64-day-old run is a lie.
 */
export function chipState(health, lastRun = null, nowMs = Date.now()) {
  const ms = _toMs(lastRun);
  if (ms !== null && nowMs - ms > STALE_AFTER_MS) return "stale";
  const h = String(health ?? "").toLowerCase();
  if (["ok", "healthy", "active", "normal", "pass", "success"].includes(h)) return "ok";
  if (["warn", "warning", "degraded", "partial", "fallback"].includes(h)) return "warn";
  if (["err", "error", "critical", "failed", "fail", "down"].includes(h)) return "err";
  // "idle"/"unknown" render neutral-gray today (semanticStateColor #888) — keep
  // that meaning: quiet, not alarming.
  if (["off", "disabled", "inactive", "paused", "idle", "unknown"].includes(h)) return "off";
  return "warn"; // genuinely novel health value: surface it, don't hide it
}

/** CSS class for a chip state (pair with the base `chip` class). */
export function chipClass(state) {
  return `chip chip--${state}`;
}

/** Shape glyph per state — color-blind support without icon fonts.
 * ("info" is not a health state — chipState never returns it — but chips
 * using .chip--info via static class can still ask for its glyph.) */
export function chipGlyph(state) {
  return { ok: "●", warn: "▲", err: "✕", stale: "◔", info: "ℹ", off: "○" }[state] ?? "●";
}

function _toMs(ts) {
  if (ts === null || ts === undefined || ts === "") return null;
  if (ts instanceof Date) return isNaN(ts.getTime()) ? null : ts.getTime();
  if (typeof ts === "number") {
    if (!isFinite(ts) || ts <= 0) return null;
    return ts < 1e12 ? ts * 1000 : ts; // epoch seconds vs milliseconds
  }
  const parsed = Date.parse(ts);
  return isNaN(parsed) ? null : parsed;
}
