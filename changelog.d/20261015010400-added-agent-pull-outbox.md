Add a governed local agent channel and durable A2A pull outbox for explicitly
enrolled same-owner peers, with scoped idempotent collection ACKs and correlated
native replies. Preserve message identity through existing retry queues; add no
HTTP sender, listener or peer approval authority.
