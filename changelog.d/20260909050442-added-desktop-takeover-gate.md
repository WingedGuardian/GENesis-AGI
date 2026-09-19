- **The desktop-takeover authorization gate, shipped before anything can call
  it.** `autonomy/desktop_gate.py` is the deterministic check that would stand
  between Genesis and your own keyboard, mouse and screen. Nothing calls it: the
  actuator is inert and the loop lands later. An actuator with a caller and no
  gate *is* the ungated capability, so the gate goes first and alone.

  It decides from what the actuator RESOLVED — the operation, the key chord, the
  target window's handle, and the element name, control type and `IsPassword`
  the accessibility tree reported — never from the acting model's prose about
  its own intent. Consent inferred from a model's self-report is the weakness
  the whole gate exists to avoid. A call that omits any of that is REFUSED with
  a reason naming the missing field, rather than held: a hold would ask you to
  approve an action nobody can describe, and a missing field is a caller defect,
  not a judgement to delegate.

  Authority is per SESSION, MISSION and WINDOW, and every field it carries is
  compared. The window is identified by `(handle, process id)` rather than by
  title, because two browser tabs are both called "New Tab" and a title changes
  when the document inside it does — so a title bar authorises the wrong window
  while looking strict, and stops matching the right one. The mission binds an
  id, never the mission text, which is model-generated and gets rewritten on
  every re-plan. A grant must also be a grant and not a hold (both are rows of
  the same action type, so a `kind` marker is what stops approving one held
  click from handing over the session), must be approved and unconsumed, must
  carry a wire format this version knows, must have been resolved through a
  channel whose messages originate outside this box — Telegram or voice,
  deliberately narrower than `classify_resolver`'s "human" class, because
  `dashboard` is stamped by a route any local process can reach with the
  internal bearer token and `user` is merely a default — and must be unexpired
  against a config TTL bounded in both directions, so a backwards clock step
  cannot mint a permanent grant.

  Risk comes from four independent signals combined highest-wins, because the
  ways an action can be dangerous are not the same shape. Labels are one of
  them: "Send", "Delete", "Sign up" cross the identity bar. But `Pay` and
  `Remove account` are on no desktop label list while the reversibility
  classifier has always called them irreversible, so that verdict now counts
  too. A drag acts on geometry and no label describes its effect, so it holds. A
  key chord is checked against an allowlist of chords that only navigate or edit
  text — `ctrl+enter` sends the mail in most clients and appears on no denylist
  anyone finished writing, so an unrecognised chord holds. That polarity is the
  point: a denylist's misses on a safety boundary are vulnerabilities, an
  allowlist's are one hold apiece.

  Ordinary work stays ordinary, which is a safety property and not a
  convenience. Typing "please delete the old draft" into a text editor is not an
  irreversible action, clicking in a window called "Messages" is not sending
  one, and a commit hash is not a bank transfer — money is matched by card, IBAN
  and SSN *shapes* as well as labels, with the IBAN validated by its ISO 7064
  checksum rather than its shape, because a country-code pattern alone held
  roughly one git SHA in every hundred and ten. A gate that stops the most
  ordinary thing you do teaches you to wave it through.

  Secret fields are outside all of it: `IsPassword`, or a target merely *named*
  like one, is a refusal with no approval path — custom controls routinely do
  not set the flag, and the list covers the family (OTP, one-time and
  verification codes, 2FA, recovery keys, passkeys, security questions, SSNs).
  That match is scoped to the target alone, so a window called "Password
  Manager" does not make its controls unreachable. "Pin" gets its own rule
  rather than a bare keyword, because a case-insensitive match made "Pin to
  taskbar" a secret field — and a secret-field match is a refusal with no way
  to proceed, so a false one is the most obstructive mistake this gate can
  make.

  That "nothing types into a password box" is an absolute, and keeping it true
  is why paste is not on the inert-chord list. A keypress has no resolved
  target, so `IsPassword` is always false for one and the refusal has nothing
  to match against; with `tab` inert and `ctrl+v` inert, the loop could move
  focus into a credential box and paste into it. Copy, undo and backspace stay,
  because none of them can put chosen content anywhere.

  The approval a person reads is newline-delimited, one field per line, rather
  than quoting the window and control inline. Screen text cannot contain a
  newline by the time it reaches the card, but it can contain an apostrophe: an
  element named `Cancel' in 'Notepad` produced a card that read as well-formed
  and named the wrong window. Structure closes that; stripping quotes would
  have corrupted every legitimate title.

  Screen-supplied text is treated as hostile where it reaches a person: window
  titles and element names pass through the canonical control-character strip
  and a declared length bound before they reach the approval you read, so a
  malicious window cannot forge extra lines, reorder the text with bidi
  overrides, or conceal part of it — while the full value is kept verbatim in
  the row's context.

  `shadow` — the shipped default — now reports the verdict the live path
  computes, from the same function, instead of recomputing it by hand. A shadow
  that silently disagrees with live is worse than no shadow, because shadow is
  the mode installs actually run and the only thing telling you what live would
  do.

  Arming needs both `mode: live` and `live_opt_in: true` in
  `config/desktop_takeover.yaml` (default `shadow`; env kill
  `GENESIS_DESKTOP_TAKEOVER_DISABLED`, checked before any config read, and now
  honouring `true`/`yes`/`on` rather than only the literal `1` — it previously
  ignored every spelling but one, silently, on the control documented as the
  one thing config damage cannot get around). Both time windows have an upper
  bound as well as a lower one: the lever is described as bounded and was
  bounded on one side, so a mistyped `grant_ttl_minutes: 30000` was a 20-day
  grant. Anything past the maximum falls back to the default rather than
  clamping to the ceiling, because every degradation here moves toward less
  authority. And the
  domain is deliberately absent from the settings MCP so arming is a conscious
  file edit rather than one unconfirmed API call. If your local overlay is
  present but unparseable the capability goes to `off` rather than falling back
  to defaults, because the base ships `enabled: true` and your disable can only
  live in the overlay — degrading it would quietly re-enable something you
  switched off. The capability cell can permanently DENY but can never GRANT, so
  no amount of banked evidence converts session consent into standing autonomy.
  Desktop rows are withheld from the generic dashboard approvals queue and from
  the unified comms feed, refused by the per-item resolve path, excluded from
  `approve_all_pending`, and sit outside the voice-gated types: neither a batch
  "approve all" tap nor a bare spoken "approve" aimed at something else can hand
  over the machine. That set is maintained by enumeration rather than by an
  allowlist, so it was checked by listing every reader of the approvals table —
  the comms feed was the one that had been missed.

  Three limits stated plainly rather than left to be discovered. The grant
  predicate is not a complete authorization boundary — the database is writable
  by the uid every Genesis process runs as, so the allowlist closes the
  app-layer path and nothing more. The classifier raises the bar on ordinary
  software but is not a defence against adversarial UI, since a hostile page
  names its own controls. And approving a held action currently authorizes
  nothing: the row is recorded, no surface can resolve it yet, and what an
  approval should buy is a new authorization surface that lands with the consent
  path in a later change.
