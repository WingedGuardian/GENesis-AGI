- **Sessions that read untrusted incoming messages can no longer reach tools they
  were never meant to have.** Genesis runs email replies and community messages
  through deliberately restricted sessions, because the content is written by
  someone else and may try to give Genesis instructions. Those restrictions were
  written as a list of what to block, which means anything nobody thought to add
  to the list was allowed by default — so the restricted sessions could read the
  queue of pending messages, and could ask for a disk to be grown or a backup to
  be taken. Both of those last two still needed explicit approval before anything
  happened, so nothing could have changed the machine on its own; the problem was
  that they were reachable at all. The community-message session was missing the
  restrictions entirely, where the email one had them. Both are now covered, and
  the rule is checked the other way round: every tool on that server must be
  explicitly either blocked or justified in writing, so a newly added tool fails
  the check until someone decides which side of the line it belongs on.
