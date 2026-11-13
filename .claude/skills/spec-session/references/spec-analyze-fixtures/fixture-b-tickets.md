# Tickets: weekly digest of closed follow-ups

Synthetic fixture for `spec-analyze.md`, the tickets for `fixture-b-spec.md`.

- T-1: Scheduled job that collects the follow-ups closed in the last seven
  days and groups them by priority (Behaviour 1, 2).
- T-2: Send the digest through `outreach_send`; send nothing for an empty week
  (Behaviour 3).
- T-3: Add the `follow_up_digests` table and its prune in the disk-hygiene run
  (Behaviour 4).
- T-4: Summary sentence written by a background session (Behaviour 5).
- T-5: Read the send hour from the routing table (Behaviour 7).
- T-6: Tests listed under Acceptance.
