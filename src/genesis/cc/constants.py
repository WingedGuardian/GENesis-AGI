"""CC-layer shared constants.

Home for CC-domain constants that must be referenced from OUTSIDE the cc package
(e.g. genesis.awareness.loop) without importing cc.contingency — importing
contingency from awareness created a package-level import cycle
(cc.contingency <-> awareness.loop). Keep this a LEAF module: stdlib-only, no
genesis imports.
"""

# How long deferred CC work persists before expiry. Matches the typical CC Max
# rate-limit reset window. Referenced by cc.contingency, awareness.loop, and
# cc.reflection_bridge._bridge.
RATE_LIMIT_DEFERRAL_TTL_S = 14400  # 4 hours
