# Snapshot-bound transcript restore

`python -m genesis restore` selects a complete off-site snapshot and pins that
selection across retries. Changing the backend requires an explicit new
selection. `--refresh-snapshot` selects the latest complete snapshot instead of
resuming the pinned selection. An explicit local backup directory remains
supported and takes precedence over configured off-site backends for full,
database-only and dry-run recovery. An empty explicit directory fails rather
than selecting a remote snapshot. In local mode, `--refresh-snapshot` reports
that it applies only to off-site recovery. Failed inventory or transfer does not authorize unrelated cached
files or silently switch to another recovery point.

Hook merge-override audit records are a separate, self-contained Tier-1 source.
Off-site recovery uses them only from the recognized backup repository, logs
their separate provenance and adds missing records without overwriting live
ones, including under `--force`. Other state remains bound to the selected
snapshot; audit-looking files in an unrecognized ambient directory are ignored.
Unpublished selection staging is removed on ordinary exceptions. Published
workspaces survive later failures for retry; interrupted-process cleanup remains
part of selection recovery. Every pointer and workspace read validates the
supported version and the backend/snapshot identity encoded by its directory.
Component records must contain a supported state and an explicit mapping of
safe payload basenames; missing entries cannot stand for an empty inventory.
Invalid metadata is refused before cached bytes or inventory can authorize a
restore, including refresh and direct helper commands. Inactive, validated
workspaces are renamed to private `.retired-<selection-id>` tombstones and the
rename is fenced before deletion. A retry resumes partial tombstone deletion
without requiring metadata that deletion may already have removed; cleanup
failure retains the newly published active selection and reports failure.
An older unmarked partial directory without valid metadata is retained and
refused; it is not automatically treated as a tool-owned tombstone. If optional extra-directory separation from core
paths cannot be inspected, backup skips that extra and reports the existing
extras gap while continuing the core backup.

The reader supports explicitly recognized legacy plaintext transcripts,
legacy encrypted transcripts and v2 encrypted archives. All `.gpg` payloads use
the shared authenticated backup decoder. Unencrypted packets disguised as
`.gpg`, failed passphrase decryption and damaged integrity are rejected before
files replace their destinations. Pool manifests are authenticated on each
invocation; ciphertext objects are checked against the selected manifest's
digest and size before reuse. Missing or corrupt objects are fetched atomically.

Where legacy and v2 archives conflict for a transcript, choose explicitly with
the repeatable
`--transcript-preference RELATIVE_PATH=legacy|legacy-plain|legacy-encrypted|v2`
option in either entry point. When both supported legacy encodings exist for
one destination, byte-identical decoded copies collapse without a freshness
claim. Differing copies require `legacy-plain` or `legacy-encrypted`; the
existing `legacy` family choice alone does not choose between different bytes.
All supplied candidates are still validated, so an invalid unchosen copy keeps
the restore incomplete. Explicit encoding choice does not imply `--force`.
For a project name beginning with `-`, pass the Python CLI option with its
value attached, for example `--transcript-preference=-project/session.jsonl=legacy`;
the wrapper forwards it to the shell script as two separate arguments.
Legacy captures do not record a trustworthy source timestamp: an existing
destination is retained unless `--force` is explicit. V2 captures compare the
recorded source timestamp, never the downloaded ciphertext timestamp. The restore reports
missing or rejected requested payloads as incomplete. An unfinished run retains
its private selected workspace for retry.

This reader delivery keeps capture on the existing format. New-format capture
and persistent analytics settings recovery arrive in dependent changes.
Reserved analytics dataset archives are reported unavailable until their
settings-aware decoder is installed. For recovery runtime requirements and
credential handling, see
[recovery and portability](recovery-and-portability-workflow.md).
