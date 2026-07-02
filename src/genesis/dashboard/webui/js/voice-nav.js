// voice-nav.js — reveal the optional "Genesis Voice" top-nav link only when the voice add-on
// is configured on this install. The link ships HIDDEN (display:none, [data-voice-nav]) on every
// page that renders the genesis-nav, so a stock clone with no ~/.genesis/ambient_remote.yaml never
// shows a voice surface. Plain fetch (no module import) so it also runs on the vanilla Neural Monitor page.
(async () => {
  try {
    const resp = await fetch("/api/genesis/voice/enabled", { credentials: "same-origin" });
    if (!resp.ok) return;
    const data = await resp.json();
    if (!data || !data.enabled) return;
    document.querySelectorAll("[data-voice-nav]").forEach((el) => { el.style.display = ""; });
  } catch (_) {
    // add-on absent / endpoint unreachable → leave the link hidden.
  }
})();
