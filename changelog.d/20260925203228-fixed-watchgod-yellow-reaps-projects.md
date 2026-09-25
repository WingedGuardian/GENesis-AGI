- The cc-tmp watchdog no longer deletes an entire project directory because
  that directory's own timestamp looks old. A project directory's timestamp
  only moves when a session is created or removed inside it, so working in one
  project for a week without starting a new session there made it look
  abandoned, and the cleanup took every session it held. Staleness is now
  judged per session from the files inside it, and a session whose contents
  cannot be read is kept rather than deleted.
