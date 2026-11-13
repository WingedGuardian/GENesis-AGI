# Spec: weekly digest of closed follow-ups

Synthetic fixture for `spec-analyze.md`. It is not a real spec and nothing
builds it.

## Problem

Follow-ups close quietly. The owner sees no summary of what was finished in a
week, so finished work is invisible and unfinished work looks the same.

## Behaviour

1. Every Monday at 08:00 in the user's timezone (`genesis.env.user_timezone()`),
   a scheduled job collects the follow-ups closed in the previous seven days.
2. The job groups them by their `priority` and lists, for each, its `content`,
   its `completed_at` date and its `resolution_notes`.
3. The digest is sent to the owner through `outreach_send`, one message per
   week. A week with no closed follow-ups sends nothing.
4. Each digest is stored in a new `follow_up_digests` table and kept for 30
   days, then pruned by the daily disk-hygiene run.
5. The summary sentence at the top is plain text built from the counts: "<n>
   follow-ups closed this week across <k> priorities." No model call is made.
6. The dashboard gains a Digests panel that lists stored digests, newest first,
   20 per page. With 1,000 stored digests the first page renders in under one
   second on the test fixture database.
7. The send hour is the `send_hour` key in `config/follow_up_digest.yaml`,
   created by this change with a default of 8; a local overlay may change it.

## Storage

The `follow_up_digests` table has one row per digest: an id, the week it
covers, the rendered text and the time it was sent. Retention is the 30 days
in Behaviour 4.

## Acceptance

- A test with three follow-ups closed in one week produces one digest naming
  all three, grouped by priority.
- A test with no closed follow-ups produces no message.
- The prune removes digests older than 30 days and keeps a 29-day-old one.
- A test of the panel's query returns 20 rows, newest first, from 1,000.
