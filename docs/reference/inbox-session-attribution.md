# Inbox invocation session attribution

Each approved inbox batch creates its own background manager session. While
the invoker and its safe overload retries run, `session_scope` supplies that
manager ID to observability, including the child environment's
`GENESIS_SESSION_ID` and invocation-failure events.

The scope begins after manager creation succeeds and ends before the existing
failure handlers or output postprocessing. Success, exceptions and cancellation
restore the previous task context. Concurrent evaluations keep their own IDs;
the scope does not change process-wide environment variables.

Manager IDs and reported CLI transcript IDs have different roles. The manager
ID identifies the invocation's operational events. Follow-ups retain their
explicit reported CLI ID, or an explicit missing value when none was reported.
The existing output-memory provenance fallback is unchanged. Postprocessing
and detached follow-up work do not inherit the new invocation scope.

Attribution grants no supervision or execution authority. Inbox input remains
`external_untrusted`; approval, replay holds and retry budgets keep their
existing behavior. Events outside the invocation boundary may use explicit
IDs or the prior context; this does not assign the new manager ID to every
event produced while an inbox scan is running.

`tests/test_inbox/test_session_attribution.py` exercises the real scan, retry
helper, child-environment builder, event persistence, response writer, baseline
and follow-up paths with inert invocation transport. It covers outer-context
restoration, distinct sequential/concurrent managers, failure/cancellation,
manager-creation failure and missing CLI identity. It does not qualify live
provider behavior or transcript collection/flush reliability.
