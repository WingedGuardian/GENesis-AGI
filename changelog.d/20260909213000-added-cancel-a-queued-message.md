- **A queued message can now be cancelled before it sends.** Scheduling a
  reminder or notification for later used to be one-way: once queued, the only
  states the message could reach were "sent" or "sent". Changing your mind meant
  either letting a stale message arrive and sending a correction after it, or
  marking the original as delivered — which writes a record saying you were told
  something you never were, and a later session reads that record and believes
  it. Cancelling is now its own state, so a retracted message is recorded as
  retracted rather than disguised as delivered. Two new tools: one lists what is
  currently queued and when each item is due, the other cancels one by id. A
  cancel that finds nothing to cancel — an unknown id, a message already sent,
  or one cancelled a moment ago — says so plainly instead of reporting success.
