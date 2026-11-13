# Spec: weekly digest of closed follow-ups

Synthetic fixture for `spec-analyze.md`. It is not a real spec and nothing
builds it.

## Problem

Follow-ups close quietly. The owner sees no summary of what was finished in a
week, so finished work is invisible and unfinished work looks the same.

## Behaviour

1. Every Monday at 08:00 in the user's timezone, a scheduled job collects the
   follow-ups closed in the previous seven days.
2. The job groups them by their `priority` and lists, for each, its `content`,
   its `completed_at` date and its `resolution_notes`.
3. The digest is sent to the owner through `outreach_send`, one message per
   week. A week with no closed follow-ups sends nothing.
4. Each digest is stored in a new `follow_up_digests` table and kept for 30
   days, then pruned by the daily disk-hygiene run.
5. To write the summary sentence at the top, the job launches a background
   Claude Code session that runs without asking for approval, so the digest
   still goes out when nobody is at the keyboard.
6. The dashboard gains a Digests panel that lists stored digests. The panel
   must load fast.
7. The job reads its send hour from the `digest_window` value in the routing
   table described under "Scheduling" below.

## Storage

The `follow_up_digests` table has one row per digest: an id, the week it
covers, the rendered text and the time it was sent. Rows are kept for 14 days.

## Acceptance

- A test with three follow-ups closed in one week produces one digest naming
  all three, grouped by priority.
- A test with no closed follow-ups produces no message.
- The prune removes digests older than the retention period.
