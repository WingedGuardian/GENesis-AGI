# Read-only inbox coverage report

Run `python3 scripts/inbox_coverage_report.py --inbox /path/to/inbox --db /path/to/genesis.db --out /path/to/reports`.
An existing `--out` directory receives a generated `inbox-coverage-*.md` file;
an ordinary file path keeps the existing report-overwrite behavior.

The destination is checked after choosing its final filename. It cannot name
the database (including its WAL, SHM and rollback-journal companions), any inbox
file the monitor may evaluate (of any type, not only markdown notes), or any response referenced by retained
inbox history. Protection includes unselected notes and response references
outside `--file` selection, resolved symlinks and existing hardlink identities.
An eligible-subtree enumeration, file metadata or identity lookup error refuses
publication; hidden and underscore-prefixed directories, which a recursive inbox scan
never reads below, are outside eligibility and cannot block a report;
a missing protected file keeps
its path protection. Unselected note bodies are not read for these checks.

The report is the only intended write. These checks prevent accidental
overwrites of stable owner-controlled files; they are not a hostile filesystem
race sandbox. A stale or interrupted report is inconclusive: require a
successful current invocation and reconcile its denominators. The report does
not change inbox approval, retry, baseline or evaluation state.

Known database-family destinations are refused before SQLite opens. A normal
read-only SQLite open can maintain WAL shared-memory bookkeeping; it is not a
guarantee that every SHM byte remains unchanged during a valid report run.
