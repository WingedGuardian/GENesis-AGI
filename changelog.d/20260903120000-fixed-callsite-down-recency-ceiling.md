- **A broken critical call site no longer falls silent after a day.** The
  dashboard's `callsite:down` view only counted failures from the last 24
  hours, so a slow-cadence critical site (like the weekly strategic
  reflection) that failed its last run went quiet after a day and stayed
  broken-and-invisible until its next attempt — the longer something was
  broken, the quieter Genesis got. Critical sites now stay red for up to 35
  days after a failed last run (matching the job-health floor), while
  non-critical sites keep the tight 24h window so long-abandoned one-off
  sites still don't nag.
