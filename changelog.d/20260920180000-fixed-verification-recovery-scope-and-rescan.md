---
title: Fix verification-evidence recovery scope and quadratic rescan
---

- A verification "recovery" ("...but now passes") now rescues only outcome/inability qualifiers ("failed", "timed out", "could not"), never a non-execution admission: "the integration test did not run, but unit tests pass" still earns the verification reminder (Codex P2). The evidence scan also dedupes sentence spans, so a long single-line message no longer re-scans one sentence per evidence phrase (Codex P2).
