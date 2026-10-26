# Scheduled user jobs

`user_job_create` saves a cron definition and attempts to register it in the
running UserJobScheduler. `success: true` means that registration attempt
succeeded. `saved: true` and `job_id` identify the retained definition even
when registration returns false or throws. Do not recreate a saved job after
a registration failure: use `user_job_control(job_id, action="resume")` on
that same ID.

MCP and dashboard lists expose two independent observations:

- `status: active` is saved scheduling intent. Paused/disabled definitions
  are not loaded by startup or public add.
- `registered` is current membership in a running scheduler. It becomes
  false when the scheduler stops. A failed replacement attempt can leave an
  older registration present, so `success: false, registered: true` is possible.

The dashboard labels active, unregistered jobs as registration pending and
offers **Retry registration**. Controls read the JSON operation result;
HTTP 200 alone does not mean an action succeeded. A registration observation
does not guarantee future execution. Startup reloads active definitions;
there is no automatic in-process registration retry loop. Genesis can retry
the same ID through its existing tools, without an operator logging in.

Cron uses the configured user timezone. There is no per-job timezone or
definition-update endpoint. For an owner-approved replacement:

1. Validate the replacement's cron/profile/model/effort before pausing the old job.
2. Pause the old job, then create/register the replacement. Retain both IDs.
3. If registration fails after save, pause the saved replacement and verify it
   is paused and unregistered **before** resuming the old job. Otherwise both
   active definitions will register at the next startup.
4. Resume the old ID and verify its active intent and registration. If either
   control fails, report both jobs' current state and retry the same IDs;
   do not claim restoration or create another replacement. Retain definitions
   and history until verification is complete.

Controls span SQLite and an in-process scheduler; concurrent control calls
are not a serializable transaction across both. `run_now` preserves its
existing behavior independently of cron registration. A run marked `passed`
records successful session dispatch, not the eventual session result. Verify
the session's terminal result and any required delivery separately.
