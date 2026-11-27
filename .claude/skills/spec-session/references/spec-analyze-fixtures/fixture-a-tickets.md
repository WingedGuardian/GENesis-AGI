# Tickets: weekly digest of closed follow-ups

Synthetic fixture for `spec-analyze.md`, the tickets for `fixture-a-spec.md`.

- T-1: Scheduled job that collects the follow-ups closed in the last seven
  days and groups them by priority (Behaviour 1, 2).
- T-2: Send the digest through `outreach_send`; send nothing for an empty week
  (Behaviour 3).
- T-3: Add the `follow_up_digests` table and its 30-day prune in the
  disk-hygiene run (Behaviour 4).
- T-4: Build the summary sentence from the counts (Behaviour 5).
- T-5: Digests panel with paging, and its query test (Behaviour 6).
- T-6: Add `config/follow_up_digest.yaml` with `send_hour: 8` and read it
  (Behaviour 7).
- T-7: Tests listed under Acceptance.
