- **Peer conversation admission avoids a circular database wait with result persistence.**
  Database identity is still checked against the actual private connection before
  admission, but before acquiring its write lock. Persistence warnings identify
  exception classes and SQLite error names without logging private error values;
  regression tests use production database settings and bound their send wait.
