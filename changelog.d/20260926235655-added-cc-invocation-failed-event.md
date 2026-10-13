- **Every failed Claude Code invocation now emits one `cc.invocation_failed`
  event.** Previously a CC error surfaced only in whatever the calling
  subsystem chose to log, so failures across the many CC call sites had no
  common signal. The invoker now emits the event on both its entry points —
  including the pre-spawn offline check — and then re-raises the original
  error unchanged. It carries the error class, streaming flag, requested model,
  the roster model the call was actually routed to, session id and, where the
  call site is tagged, a caller tag naming the dispatching subsystem, and
  appears in
  `health_errors`. The raw error text is not stored in the event (it is built
  from unbounded CLI output); only its length is recorded. Rate-limit and
  quota errors are WARNING; everything else is ERROR. Repeats of the same error
  from the same tagged caller on the same route are coalesced to one event
  per minute, and the
  next event's message reports how many were folded into it; untagged callers
  are never pooled together. Cancellations emit nothing, and the fallback
  liveness probe is exempt only for its expected rate-limit answer — a probe
  that times out or fails otherwise is reported. The event does not wake a
  reactive ego cycle, since that cycle would itself need Claude Code.
