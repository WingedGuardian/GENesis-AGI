# Desktop Takeover — Design

Genesis captures a window on the operator's own machine, reasons over it, and
injects mouse and keyboard input back into it. This document specifies the
authorization design and records what has been measured, built, and deliberately
left unbuilt.

**Status.** The gate is built and has no caller. The act leg and abort watcher
are built and inert. The loop, transport and tool that would connect them are
not built. That ordering is the design's central commitment: an actuator with a
caller and no gate *is* the ungated capability, so the gate ships first and
alone.

| piece | state |
|---|---|
| capability-cell promotion closed to an allowlist | built |
| act leg + abort watcher, device-side, no caller | built on a separate branch, unmerged, inert |
| authorization gate, container-side, no caller | built |
| loop, transport, tool, consent path | not built |
| device-side lease enforcement | deliberately not built |

**Device-generic by construction.** No machine name, address, account, or
location appears here. "The device" is any machine the operator runs the agent
on; "the container" is the machine Genesis runs on.

---

## 1. Threat model

**Assets.** The device's state, meaning anything irreversible an input can
cause. The operator's screen content, which leaves the device. And Genesis's own
trust posture, which is spent if this goes wrong once.

**The realistic adversary is Genesis being wrong,** not an attacker. A confused
loop clicking confidently is the expected failure, and it is the one the gate is
sized for.

**One genuinely new surface: the screen is untrusted input.** Everything
rendered in the captured window becomes tokens in Genesis's context. A hostile
page can render text addressed to the agent rather than the reader — telling it
to disregard its instructions and take some action of the page's choosing — and
unlike a document being summarised, this context is wired to an actuator. Browser
automation mostly dodges this because accessibility trees are structured; a
screenshot is not.

`config/content_sanitization.yaml` already detects injection patterns but sits
at Layer 3 of the enforcement spectrum, which is detection and log-only. The
capture path is the strongest case for promoting that to enforcement: it is the
only place where untrusted rendered text reaches a loop that can act on the
operator's own machine. That promotion is a separate decision and is not bundled
here.

**Out of the threat model, stated so it is not silently assumed:** a compromised
container. Per the repository's standing posture, container compromise is device
compromise. Nothing here defends against that, and no claim below should be read
as if it does. Section 11 states the narrower version of this that applies to
the gate specifically.

---

## 2. The approval unit

**Session consent plus abort.** One grant per takeover session, plus instant
abort. Not per-action, and not a lease.

One grant covers exactly one target window, one stated mission and one time
budget. It does not cover switching windows, launching applications, or anything
in the inherited tier below. An iteration cap belongs to the loop and does not
exist yet; it is listed in §13, not claimed here.

### 2.1 What the session grant is, concretely

A grant is a row in `approval_requests` that clears six independent bars. Each
exists because its absence was a defect found in review, not because it seemed
prudent.

**It must belong to this session.** The session id rides in the row's `context`
and is filtered in SQL. It is deliberately *not* folded into the approval key
(§2.2).

**It must be a grant and not a hold.** Session grants and per-action holds share
one `action_type`, deliberately: a single action type is what keeps every
batch-approval exclusion to one entry rather than a set someone can half-update.
The price is that `context.kind` becomes load-bearing. Without it, an approved
hold satisfies the grant predicate exactly, so approving one click grants the
whole session with a fresh expiry. That was measured on the first implementation
before it was fixed.

**It must be for this window.** The grant names the target window the operator
was shown, and the action must be in it, compared case- and
whitespace-insensitively. An absent or empty window on either side is a refusal
rather than a wildcard, mirroring the rule the device applies to a target it
cannot resolve — a grant naming no window is not a narrower grant, it is an
unbounded one. Review caught this bar missing: `window_title` was carried in the
grant and read back to the operator on the consent card while nothing compared
it, so a grant for a text editor authorised actions in an unrelated chat or
banking window. Carrying a scope field without comparing it is worse than not
carrying it, because the card makes a promise the code does not keep.

Window titles are not stable identifiers — a document name changes the title of
the same window — so this is a strict bar by choice. A title that drifts refuses
and the loop re-asks against the window the operator can actually see named. A
stable window identity carried from the resolve step is the better long-term
answer and belongs with the loop that resolves it.

**It must be approved and unconsumed.** Consumption *will be* how a session's
grant is retired at teardown; the teardown is part of the unbuilt loop, so today
the TTL is the only thing that retires a grant.

**It must have been resolved through an allowlisted channel.** The set is
narrower than the codebase's canonical "human resolver" class, which answers
"was this written by a human-operated channel?" — the right question for metrics
and the wrong one here. A dashboard resolution is stamped unconditionally by a
route reachable by any local process holding the internal token, and the default
resolver value means only "nobody recorded who". Neither is proof a person
acted. What remains are channels whose *messages* originate outside the
container. This is an allowlist on purpose: a resolver cannot mint desktop
authority until someone decides it may, in code.

**It must be unexpired, bounded in both directions.** An upper bound alone
accepts a negative age forever, so a backwards clock step or a hand-edited row
would mint a permanent grant.

### 2.2 Three earlier claims that turned out to be wrong

The first draft of this design named three things that "must be built, not
inherited" from the existing CLI approval gate. Reading the code falsified all
three, and recording that is more useful than quietly deleting it.

| claimed | what the code actually does |
|---|---|
| a session id must be added to the approval key | Unnecessary. A **fresh row per session** gives one-grant-per-session by construction — no key and no dedup. The id still travels: it rides in the row's `context` and is filtered in SQL, which is not the same as folding it into the approval key. |
| `mark_consumed` must be built | It already exists, over an atomic single-flip CRUD. It belongs at session *teardown*, not session start. |
| the global `manual_approval_required` bypass needs a carve-out | It does not apply. That bypass lives inside `AutonomousCliApprovalGate.ensure_approval`; the enforcing-gate template calls `ApprovalManager.request_approval` directly and never touches it. |

The lesson generalises past this feature: the two gates in this codebase have
different topologies, and "the approval gate" is not one object. Which one a new
gate inherits from decides which of its properties are yours to build.

### 2.3 The inherited tier

Session consent cannot pre-approve a purchase, an account creation, a credential
entry, or a message send. The standing rule that every transaction needs
explicit approval every time, and that prior approval never carries forward,
governs those independently.

Each of those four has a mechanism behind it rather than only a sentence:
purchase and payment terms, account-creation terms, the secret-field family, and
send terms all sit in the identity or financial pattern sets. Account creation
was added after review found it classified as ordinary input — it had been held
only when a button happened to read "Submit" or "Sign up", which is exactly the
accidental coverage §3 warns about. "Create" alone is deliberately excluded, so
that creating a folder or a document stays ordinary.

In the gate this appears as a risk gradient. Ordinary control manipulation is
allowed under a live grant. Anything crossing the identity bar or touching money
holds for its own decision, every time, even under a live grant. A secret field
is refused outright with no approval path at all, because there is no version of
this capability that types into a password box, and offering the operator a
button that implies otherwise would be worse than useless.

---

## 3. Classification reads observation, not self-report

This is the design's one substantive departure from the obvious approach, and it
replaces the weakness the first draft named as unavoidable.

That draft concluded: the decide step must emit a semantic description of what
it believes it is about to do, and the gate classifies that description — so the
gate's coverage depends on the loop's own honesty about its intent. That is a
real weakness, and it is the same weakness a prompt-level gate has.

**The gate does not do that.** Its inputs are the target the actuator
*resolved*: element name, control type and the password flag as read from the
machine's accessibility tree, plus the window title and any text to be typed. A
loop that means to click "Save" and resolves "Delete account" is classified on
what it resolved. Nothing the model writes about its own plan reaches the
classifier.

Which input feeds which bar is a decision rather than an oversight:

- **Identity** reads the control — window, element name, control type. Clicking
  "Send" is the identity act; typing the word is not. A gate that holds the most
  ordinary desktop action there is teaches people to wave it through.
- **Financial** additionally reads the typed text, and matches card, IBAN and
  SSN *shapes* rather than only labels. "Reads the typed text" was an empty
  promise while every pattern was label-shaped: a real card number typed into a
  field labelled "Confirmation" matched nothing.
- **Password** matches the resolved target alone, never the window title, so a
  window called "Password Manager" does not make its controls unreachable. It
  does not trust the accessibility flag by itself, because custom controls
  routinely do not set it, and it covers the whole secret-field family rather
  than the single word "password" — one-time codes and security answers are
  credentials too.

**The limit, stated at its real size.** Identity and financial classification
are keyword matchers over text the screen supplies, and in this threat model the
screen is hostile. A malicious page controls its own window title and control
names: it can label a destructive control "Yes", use a synonym, use another
language, or split a matched phrase, and the action then classifies as ordinary
and executes under a live grant. So the classifier raises the bar on ordinary
software and is **not** a boundary against adversarial UI. What holds there is
the bounded, revocable, window-scoped grant and the operator watching their own
screen.

Observation beats self-report. It does not replace the grant.

---

## 4. A hold queues nothing

There is no pending-action table and no drain. Approving a held action does not
replay it, because by the time the approval arrives the screen it targeted has
moved; the loop re-captures and re-plans instead. The approval row says so, in
the sentence the operator reads.

The invariant that buys is worth stating plainly: **every action executes
against an observation taken after the last approval.**

It also removes a table, a migration, a schema-allowlist entry and a drain job
from the design. A design that needs fewer moving parts to state an invariant is
usually the right one.

**Known gap for the loop.** Holds are not deduplicated. A loop retrying the same
blocked click every tick would leave one permanent approval row per attempt.
Harmless while nothing calls the gate; the loop must either back off on a hold
or reuse the existing content-stable approval key.

---

## 5. The arming lever

Two keys, both affirmative, neither reachable through the settings API or the
dashboard. Arming real input is a conscious edit of a gitignored local overlay,
never one unconfirmed API call.

Every degradation path moves toward less authority. A non-boolean master switch
reads as off, including the YAML string `"false"`, which is truthy in Python. An
invalid mode degrades to shadow, which is observable, rather than to off, which
is silent. And the environment kill switch is read *before* any configuration is
parsed, so a file the process cannot read is not a way past the stop.

The shipped default is shadow: classify, record the capability cell, log the
full verdict including a missing grant, and refuse. That last clause matters
more than it looks. An observer that only reports on sessions already holding a
grant observes nothing at all, because a shadow install has no grants — nobody
asks for keyboard consent for a capability that cannot act.

---

## 6. Capture is egress

A screenshot leaving the device to a vision provider is external egress and gets
the seriousness this repository already gives outbound channels.

Full-screen capture must be **available but not the default**. "Find the window
showing X", "what is on my screen", and any multi-window task are legitimate and
impossible under a window-only rule. So capture is two modes with separate
grants, not one policy:

| | window capture | screen capture |
|---|---|---|
| scope | one named window | the whole virtual desktop |
| default | yes | **no** |
| grant | covered by the session grant | **its own explicit consent, per session** |
| inherited from a window grant? | — | **never** |
| denylist | abort if a denied window is the target | abort if a denied window is anywhere in frame |

The asymmetry is the point. A window grant says "you may look at this one
thing." A screen grant says "you may look at everything I have open", which on a
real desktop means mail, messages and documents in the same frame. It is a
strictly larger disclosure and must be asked for as one, every session, never
silently upgraded into.

Both modes share the rest:

- **The denylist aborts; it does not redact.** Redaction is a promise about
  pixels that nobody can audit.
- **Fail closed.** An occluded, minimised, moved, resized or unidentifiable
  target means stop and re-ask, never capture anyway.
- **The denylist is operator-configured, and categories that must never be
  captured are the operator's to name.** A capture carrying a denied window is
  an exfiltration, not a feature.
- **Validate the content, never trust the file** — see §9.1.

---

## 7. Abort carries the safety load

Because the approval unit is coarse by design, abort is where the safety budget
goes. Four properties, none optional:

1. **Human presence yields.** Any physical input on the device suspends the loop
   immediately. The operator is sitting there; the system must not fight them
   for the pointer.
2. **A device-local abort that needs no network.** A control that halts the
   agent without a round trip. Every other channel fails exactly when it is most
   needed.
3. **Dead-man, not kill-switch.** Heartbeat lapse, network drop or container
   death means the agent stops accepting work. Silence must mean stop.
4. **Bounded blast radius per command.** Abort cannot beat a command already in
   flight, so commands carry short expiries and the gate stamps one on every
   allowed action. The device refuses a request past its expiry, so an action
   that sat in transit dies at the machine rather than landing on a screen that
   has moved.

Property 3 necessarily runs device-side. That is a liveness requirement rather
than a lease holder, so it is compatible with container-side enforcement rather
than a violation of it.

---

## 8. Transport and the device agent

**Enforcement lives in the container; the device agent is a dumb executor.**
Device-side lease enforcement is explicitly deferred.

That is sound only with the right transport. The existing enforcing capability
gate in this codebase works because the thing it guards is an in-process call
with no other caller: convergence is a property of the process boundary, not of
caller discipline. A device agent with an open port has no such property — the
"chokepoint" would be a convention among the container's callers, and anything
else on the network could bypass it.

**Therefore the agent dials out and pulls work. It never listens.**

| | reverse-connect (chosen) | agent listens |
|---|---|---|
| new inbound door | none | one |
| chokepoint claim | true for "who can send commands" | aspirational |
| NAT, sleep, roaming | traverses without config | needs rules, breaks on roam |
| abort under partition | agent stops receiving, halts | keeps its last instruction |

The last two rows decide it on engineering grounds alone, independent of any
security argument.

**What deferring device-side enforcement gives up,** stated plainly: a command
already in flight can outrun a container-side abort. Short expiries bound that
window; they do not remove it. That is the accepted cost and should be revisited
if the surface grows.

**Agent shape.** It runs as the interactive user, not as a service: a service
account runs in a separate session and cannot inject input into the interactive
desktop at all. It has no execution time limit and restarts on crash, because a
long-lived agent under a default task time limit is killed mid-session. It is
battery-aware, since a laptop task that does not opt in simply never runs. It is
installed by one pasted elevated command, because there is no remote-execution
path to an operator's own machine and building a push installer is not
warranted. And "agent absent" is a first-class, well-messaged state: the device
may be asleep, so reachability is never assumed.

---

## 9. What was measured

**Provenance, because these numbers are load-bearing and cannot be re-derived
from this repository.** Measured in a single spike session on 2026-09-06, on one
Windows 11 host, n=1 per row. They have not been re-run since, and nothing in CI
re-derives them. Read them as one careful observation each rather than as
repeatable measurements — the qualitative findings (structure-driven targeting
is viable; an elevated window returns nothing) are what the design rests on, and
those are robust to the precision the percentages imply but do not have.

### 9.1 On Windows, every access failure presents as empty success

Three distinct failure modes, each observed directly:

| failure | how it presents |
|---|---|
| running in the wrong session | UI Automation enumerates **0 windows** — reads as an empty desktop |
| capture from the wrong session | throws, but still writes a **valid all-black PNG** — reads as a successful capture |
| UIPI blocking an elevated window | **empty element tree** — reads as an app with no structure |

None of these raise anything a caller notices if it wraps the call in a `try`.
The third nearly inverted this design: an elevated window returned 3 elements
and 0% actionable, which read as "native apps have no structure" until a
non-elevated control returned 47 elements and 53% actionable from the same class
of app.

**Therefore a saved file is not evidence of a capture, and a returned tree is
not evidence of enumeration.** Every capture validates its own content and
refuses rather than returning a confident blank. On this platform, "succeeded
with nothing in it" is the expected failure.

### 9.2 The accessibility tree carries enough structure to act on

| target | elements | AutomationId | actionable | generic |
|---|---|---|---|---|
| modern native app, non-elevated | 47 | 55.3% | 53.2% | 25.5% |
| browser, chrome + page | 108 | 73.1% | 75.9% | 22.2% |
| content-heavy page in that browser | 43 | 34.9% | 44.2% | 51.2% |
| **elevated window** | **3** | **0%** | **0%** | **66.7%** |

Structure-driven targeting is viable and should be the primary path. Pixels are
the fallback for content-heavy surfaces, where the generic-container share
roughly doubles. The last row is not an application property — see §9.4.

### 9.3 SSH reaches the machine but not the desktop

An SSH login lands in session 0; the desktop is session 1, and the two have
separate window stations. From session 0 the desktop is invisible and
uncapturable, so SSH alone is useless for capture or input even as the same
user.

The working mechanism, verified end to end: a scheduled task registered with an
interactive logon type, triggered from the SSH session, whose body executes in
the user's session. This confirms that a service principal cannot do this and
extends it — an SSH login cannot either.

One registration detail worth recording because it costs a debugging cycle: on a
workgroup-joined host the environment reports a domain that does not match the
real principal, and registration fails with an account-mapping error. Register
against the current token's SID rather than a constructed name.

### 9.4 Elevation is a wall, and must be a refusal

A non-elevated agent cannot read an elevated window's tree. UIPI denies it, and
denies it silently by returning an empty tree (§9.1). Two consequences:

- The agent must detect elevation and **refuse the target**, saying so. Falling
  back to pixels would be worse than refusing: it would drive a window it cannot
  introspect, using coordinates it cannot verify.
- Running the agent elevated to "solve" this is the wrong answer. It would grant
  the takeover loop administrative reach over the whole machine to fix a narrow
  visibility problem, and every argument in §1 gets worse.

### 9.5 Still open

Lock screen and UAC secure-desktop behaviour: a user-mode agent cannot reach the
secure desktop at all and must detect and stop rather than silently no-op.
Per-iteration vision cost, now that §9.2 says structure carries most of the load.

---

## 10. Placement on the enforcement spectrum

This capability registers on the repository's existing taxonomy rather than
inventing one.

| element | layer | why |
|---|---|---|
| session grant | 5 — proposal gate | the operator approves before it happens |
| capture denylist | 7 — hard block | no prompt may override it |
| human-presence yield | 7 — hard block | mechanical, not a judgement |
| password target | 7 — hard block | refused outright, no approval path |
| irreversible actions inside a session | existing irreversible rule | inherited, not new |
| no takeover from a dispatched session | **code, not rule data** | see below |

**The last row was wrong in the first draft, and the correction is not the one
the second draft made either.** The original claimed a background session could
be excluded by a data rule, since the rule engine has a `context` condition. The
second draft said such a rule could never fire. Both are wrong, in opposite
directions.

The real reason a data rule cannot carry this invariant is simpler: **the
desktop gate never calls the rule engine at all.** It classifies in code and
reads the grant directly, so there is no `RuleEngine.evaluate` call site for a
rule to attach to. A rule with no evaluation point is not a weak mechanism; it
is not a mechanism.

The context-condition fragility is real but is a separate observation, and it is
narrower than the second draft claimed. `RuleContext.context_category` is written
by exactly one production site, which writes `"background"`; the classification
path leaves it unset, and a `context` condition cannot match a context-less
evaluation. So of the two shipped rules carrying a `context` condition, one
(`critical_path_block`) survives precisely because it happens to list
`background`, and only the other (`sensitive_path_propose`, which requires a
value nothing writes) is dead. One dead rule, not two.

The invariant is therefore enforced in code, as the resolver-allowlist
requirement in §2.1: a dispatched session has no way to produce a resolution
from an allowlisted channel, because those channels carry messages that
originate outside the container.

---

## 11. What this design does not guarantee

Three limits, each stated because the obvious reading of the sections above is
stronger than the truth.

**The grant predicate is not a complete authorization boundary.** Every bar in
§2.1 is a column value in a database file owned by the same OS user every
Genesis process runs as. Anything with same-uid code execution can write a row
satisfying all of them, including the resolver allowlist, by typing an allowed
prefix into it. What closes the *application-layer* path — no Genesis component
using the sanctioned approval APIs can mint itself desktop authority — is the
allowlist **together with** three explicit exclusions, and the distinction
matters. `ApprovalManager.resolve` validates no resolver string, so an
in-process component could stamp an allowlisted prefix on its own resolution;
what actually stops it is that no surface can reach a desktop row: the batch
sweep, the generic per-item resolver and the dashboard queue each exclude the
action type by name, and the voice and most-recent resolvers are allowlists that
never contained it. The property is therefore maintained by enumeration, and a
new resolution surface that forgets its exclusion re-opens the path — the
allowlist will not catch it. Closing the lower, database-level path needs
provenance a SQL predicate cannot express, such as a signature only the real
resolver can produce, or an OS-level identity split. This is a
property of the whole approval substrate rather than of this gate, and it is
recorded here so that whoever wires the first caller does not mistake the
predicate for a boundary.

**The classifier is not a boundary against adversarial UI** (§3).

**The capability cell can deny but never grant.** A desktop cell is permanently
non-promotable: no amount of banked evidence converts session consent into
standing autonomy. Its only authority over the gate is negative — a permanent
denial outranks a live grant. That asymmetry is deliberate, and it is what keeps
the cell from being decoration.

---

## 12. Prior art, and the one difference that matters

The closest external analogue's safety gate is entirely prompt-level, measured
by reading its actual prompt pack rather than its marketing. The user is told to
paste ground rules into their agent's instruction file: drive only on an
explicit takeover instruction; screenshot, show a short plan and wait before
acting; one step at a time; stop instantly on the word stop; and refuse without
exception for payments, card or bank details, passwords, file deletion, or
sending any message without showing the exact text first.

**That is the same gate shape specified here** — session consent, instant abort,
an irreversible tier — arrived at independently. Convergence is evidence the
shape is right, and this document should not claim novelty it does not have.

**The difference is where the rules live.** Those are text the model is *asked*
to respect, which is Layer 4 on §10's spectrum. A takeover loop reads its
instructions and its screen through the same context window, so content rendered
in the captured window competes with the rules on equal footing. That is exactly
the untrusted-input problem in §1, and exactly what a pasted rule cannot solve.
This design puts the identical rules below the tool call, in code, where no
rendered text can reach them.

There is a second difference, and it is the one §3 is about: a prompt-level gate
classifies the model's *description* of its intent, because that is the only
thing available to it. Reading the resolved target instead is what a code-level
gate can do and a pasted rule cannot.

Two further observations from the same source, both narrowing the unknowns
rather than resolving them: it is macOS-only, and its Windows guidance amounts
to pasting the same prompts anyway — so §9's Windows questions are genuinely
open rather than solved elsewhere and waiting to be copied. And it captures the
full screen, where §6's window-scoped, abort-on-denylist capture is strictly
stricter on the egress axis. That difference is deliberate.

---

## 13. Explicitly not covered

Unattended operation. Multi-window sequences. Non-visual actions. Device-side
lease enforcement. Anything on a device other than the operator's own. Promoting
content sanitization from detection to enforcement on the capture path (§1),
which is a separate decision and deliberately not bundled into a security-gate
change.

Three things belong to the loop rather than the gate, and do not exist yet: an
**iteration cap** per session; **hold deduplication** (§4); and **grant
consumption at teardown** (§2.1), without which the TTL is the only thing
retiring a grant. A **stable window identity** carried from the resolve step,
replacing the title comparison in §2.1, belongs there too.

One question was raised in review and has been answered. A spoken
challenge-response **may** open a session-length grant; `voice:` stays an
allowlisted channel. That settles the policy, and it is not reopened on
ambient-audio or replay grounds.

It does not assert that the voice pipeline resists replayed audio, which is an
engineering property rather than a decision, and the distinction is worth
keeping straight. The mitigation the design relies on is structural: the consent
path names the target window back to the operator and waits for a specific
answer, which is materially harder to trigger by accident or by replay than a
bare "approve" — an attacker would need the window name — and the resulting
grant is bounded, revocable and scoped to that one window regardless. The loop
must therefore build the challenge-response as specified rather than accept a
naked "yes".
