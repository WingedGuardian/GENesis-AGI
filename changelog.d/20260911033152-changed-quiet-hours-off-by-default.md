- Quiet hours are now OFF by default, so a message you schedule for 01:30 arrives at
  01:30. Previously outreach shipped with a 22:00–07:00 window that silently held every
  non-urgent category until morning, with no way to turn it off or to override it for a
  specific send. To re-enable it, set real times under `quiet_hours` in
  `config/outreach.yaml` (or your local overlay); `start` equal to `end` means disabled.
