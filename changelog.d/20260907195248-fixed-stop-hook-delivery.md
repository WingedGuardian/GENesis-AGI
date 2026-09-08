- **Two end-of-turn checks were firing correctly and reaching nobody.** When
  Claude finishes responding, Genesis checks the reply for work handed back to
  you that it could have done itself, and for a claim of completion with no
  evidence behind it. Both checks worked; both then wrote their reminder to a
  channel the assistant cannot read, while the file said in as many words that
  the text reached the next turn.

  Claude Code shows a model a hook's plain output for only a few kinds of
  event, and the end of a turn is not one of them. What that channel does
  instead is worth stating plainly, because it is not what the name suggests:
  handing text back there does not leave a message for later, it declines to
  end the turn and says why. Claude keeps working and takes the note into
  account. That is the right shape for these two — being told you have handed
  work back, or claimed something is done without checking, is precisely a case
  for not stopping yet — but it is a change in behaviour and not only in
  plumbing, so it is bounded: the reminder speaks once and then stays quiet,
  costing at most one extra step rather than looping.

  Both stay quiet when a reply ends by asking you something. A turn that hands
  control back is already stopping correctly, and declining to end it would
  have Claude talking past the person it is waiting on — including at the point
  where it asks whether to open a pull request. That mattered only once the
  reminders started arriving; while they went nowhere, firing on the wrong turn
  cost nothing.

  A third check, for code changed without a review, deliberately stays out of
  this path. It depends on a condition rather than on something in the reply,
  so it would repeat until the condition cleared; it also already arrives by
  another route, at the start of each of your messages, and always did.

  The one other end-of-turn check Genesis runs — the deliverable gate, which
  refuses to end a session that rendered a document nobody verified — keeps
  repeating on purpose, and is unchanged. A reminder said twice adds nothing; a
  gate that gives up after being acknowledged once was never a gate. It still
  lets go the moment the document is verified or the draft is abandoned.
