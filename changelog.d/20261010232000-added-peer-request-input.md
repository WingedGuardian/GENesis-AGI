- Standalone hosting provides bounded request-body ingestion for the dependent
  peer send/cancel API: an absolute body deadline, decoded and framing byte caps,
  and bounded unread-body cleanup. The existing listener and network topology are
  preserved; peer routes and task execution are not activated by this prerequisite.
