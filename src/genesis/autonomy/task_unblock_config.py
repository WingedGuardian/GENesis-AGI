"""Approval action type for releasing a BLOCKED background task.

Its own module, matching the house pattern for approval action types —
``EMAIL_GATE_ACTION_TYPE`` (``email_gate``), ``DESKTOP_GATE_ACTION_TYPE``
(``desktop_gate``), ``CONTRIBUTOR_ISSUE_ACTION_TYPE``
(``contributor_worklog_config``). It does NOT belong in
``executor/types.py``, whose scope is phases, step types, results and the
state transition table; the sole consumer is ``autonomy/dispatcher``.

Nothing PRODUCES this type yet — the producer is a separate change. This
module is where it should grow.
"""

from __future__ import annotations

#: ``action_type`` of the approval that releases a BLOCKED task.
#:
#: Named ``task_unblock`` rather than ``task_resume`` deliberately:
#: ``dashboard/routes/tasks.py`` already defines a ``task_resume`` view on
#: ``POST /api/genesis/tasks/<id>/resume``, which drives the PAUSED-only
#: ``resume_task`` path and does NOT release a BLOCKED task. Two different
#: mechanisms sharing one name in grep is how the wrong one gets called.
#:
#: Naming the type is load-bearing for the claim query, not decoration. The
#: ``$.task_id`` key is NOT a free namespace: ``executor/step_dispatcher.py``
#: builds ``context={"task_id": …, "step_idx": …}`` for an autonomous-CLI
#: fallback, and ``approval_gate`` nests it under ``$.extra`` — so that row's
#: task id lives one level down TODAY, by an undocumented nesting decision in
#: another module. MEASURED: the previous textual scan
#: (``context LIKE '%"task_id": "<id>"%'``) DID match that nested context, so
#: an unconsumed CLI-fallback approval could release a blocked task it was
#: never granted for. Matching on this type as well as the id means the
#: invariant no longer depends on that nesting staying put.
TASK_UNBLOCK_ACTION_TYPE = "task_unblock"
