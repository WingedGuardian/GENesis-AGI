- **A delivery hiccup can no longer page the same alert twice.** When sending
  an alert to Telegram failed transiently, two independent recovery mechanisms
  both took ownership of the retry: the durable alert queue kept its copy for
  the next pass, and the delivery pipeline separately queued its own retry.
  Each then delivered — one alert, two pages, minutes apart, in the middle of
  exactly the kind of incident where you are reading the channel carefully.

  The durable queue is now the single owner: a caller that carries its own
  retry tells the pipeline not to queue a second one. The queue's fourteen-day
  patience is kept deliberately — the pipeline's own retry gives up after
  about an hour and a half, so handing the alert over would have traded the
  duplicate for a page silently lost in any longer outage. The same rule stops
  a failed retry from queueing a shadow copy of itself, which was quietly
  possible before and delivered doubles the same way.
