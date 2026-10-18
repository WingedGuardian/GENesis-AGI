# Encrypted transcript capture validation

The capture helper in `scripts/lib/transcript_archive.py` keeps the authenticated
capture inventory separately from its current validation authority. An existing
format-version-1 index can retain its source and capture entries, but its old
ciphertext digest alone does not authorize reuse under the current restore
validator.

The encrypted index records a `validation_contract` and a `validated_captures`
map binding capture names to their validated ciphertext digests. The contract
includes the restore-validator revision, platform `time_t` width, and filesystem
encoding/error policy. The current revision also requires passphrase-based,
integrity-checked decryption; certificates from the earlier `restore-v2`
development validator must be revalidated rather than trusted by digest alone. A missing or different contract clears validation
certificates, preserving the known capture inventory. Each retained object must
pass decryption and the current dry-run restore validation, remain unchanged
through that check, and complete its durability fences before its validation
certificate is checkpointed. A completed certificate avoids repeating capture
decryption on the next run while the contract and digest still match.

A missing or invalid retained capture leaves backup coverage incomplete. An
extant source can repair its own invalid capture only when that source belongs
to the current selected population. Sources outside that population are not
recaptured merely to make an older archive valid. Retained names remain visible
as missing or unvalidated evidence. Coverage and publication authority require
current validation for every retained capture, rather than only captures written
in the latest run.

This migration applies to a readable authenticated prior index. The owner-approved
history policy requires an existing unreadable index to remain untouched and the
backup to remain incomplete until recovery or explicit retirement. A truly absent
index can bootstrap from the current object inventory. The strict unreadable-index
correction is assembled here and has passed disposable GPG checks. Full combined
backup/restore and post-review end-to-end checks remain required before delivery.

Interrupted or deadline-limited runs retain completed validation checkpoints for
resumption. Fresh index reads and direct attestation retry the exact capture-directory fence
before reusing cached authority. A retry fence failure stops the operation
without replacing retained history with an empty index.

File and directory `fsync` failures report incomplete work; a visible
replacement is not proof that its parent-directory fence succeeded. Synthetic
fault checks and disposable GPG fixtures test those paths without promising
recovery from arbitrary physical storage failures.

Local capture validation does not establish remote pool integrity. Pool publication freezes a dated epoch and verifies every referenced remote
object against its validated ciphertext before COMPLETE. A pending epoch retains
earlier verification receipts across runs; restore independently verifies the
bytes it reads. A corrupt object is repairable only from
the exact validated local ciphertext, followed by another verified read. Failed
transport remains incomplete. Marker and manifest readback are also required.
This candidate's final combined tests remain pending; local certificates alone
must not be described as proof of off-site recoverability.

`GENESIS_BACKUP_TRANSCRIPT_SCOPE` defaults to `main`, which captures direct
`.jsonl` sessions for the current Genesis project. `all` includes every project
and nested subagent transcript or metadata under `~/.claude/projects`. Other
values report incomplete coverage and prevent a COMPLETE snapshot. An absent
source directory means no new transcripts; retained archive inventory still
requires validation, including captures from projects outside the selected scope.
Only a missing path establishes this empty-source case. Permission, symlink-loop
and other inspection failures report incomplete coverage. Pool host labels accept
letters, digits, dots, underscores and hyphens in a single component, including
non-alphanumeric prefixes; the special components `.` and `..` are rejected.


## Publication across backup runs

`scripts/lib/transcript_publication.py` stores one authenticated pending epoch
under `~/backups/.genesis-backup-publication/pending`, outside the private Git
clone and temporary storage. Required core files are copied and fenced before
the initial journal becomes authoritative. Transcript copies then advance from
the accepted inventory. Mutable producer outputs never replace frozen inputs.
Keep this directory when interrupting a backup; deleting it discards publication
progress and is an explicit operator action, not automatic recovery.

The epoch binds the installation, backend root/share, host, selected source
scope, current validation contract and capture-index digest. A changed key,
unreadable history, or incompatible binding reports incomplete and retains the
epoch for recovery or explicit resolution. Do not remove it to hide a coverage
gap. Finished epochs retire by a fenced rename followed by owned cleanup.

Timer and manual backups resume pending publication before fresh producers.
Update-triggered backups first produce and verify current SQL; completion of an
older off-site epoch never substitutes for that update prerequisite. All runs
retain the two-hour whole-backup window. The original snapshot timestamp and
age remain visible in stdout/journald and `backup_status.json`'s
`pending_publication` object (phase, byte/object totals and verified progress).
`snapshot_id` remains the original timestamp. The public dashboard keeps its
existing safe projection; it does not display the new raw progress object.
Newer transcript source changes wait for the next epoch.

Initial inventory generation and all-core freezing must fit an allowed initial
run. An interrupted unpublished freeze is reclaimed and that initial phase is
repeated; cross-run row/receipt progress starts after pending becomes durable.
There is no promise that arbitrarily large initial inventories or core inputs
can finish within one two-hour window.

SMB rejects quotes, semicolons, backslashes and CR/LF in its command path arguments.
Required remote names and local transfer workspace roots are checked before a
new frozen epoch becomes durable. Correcting an unsupported managed filename in
the backups clone allows a later initialization attempt instead of retaining that
unusable target. Renaming a plaintext producer alone does not remove an older
encrypted filename already retained in that clone.
An older authenticated pending epoch with an unusable required path is retained
and reports incomplete for explicit resolution; it is never automatically
discarded. Unsupported new optional extras become explicit gaps. Existing failed
optional extras remain terminal gaps and do not acquire new core requirements.
The local backend retains its ordinary filesystem pathname support.

Uploads retry whole files; no upload-resume transport was added. If one upload
cannot fit, that file remains incomplete for a later run. Downloads append via
SMB `reget` or a local byte suffix. After a child finishes, including a failed
transfer, exact comparison may checkpoint its verified prefix. Fsync precedes
journal authority. Retry retains only checkpointed received bytes and continues
unverified comparison before requesting more data; replaced downloads lose only
their own progress. A partial prefix never grants an object receipt.
Each publication backend call retains its configured operation timeout and also
ends before the whole-run deadline, reserving time for checkpointing. Automatic
expiry keeps the existing ten-second force margin. The whole-run wrapper does
not start an earlier force timer on first cancellation; existing per-operation
limits remain. Backup helper timers stay in the whole-run process group, while
restore retains its standalone timer groups.

Initialization publishes `TRANSCRIPT_POOL` and its authenticated manifest before
pool objects, preserving conservative GC protection. Existing metadata must
match frozen bytes; unexpected remote content refuses adoption. COMPLETE needs
all required receipts, a verified staged marker and exact final-name
acknowledgement. An interrupted acknowledgement resumes the same recovery point.
Optional extras preserve the existing core-complete/extras-incomplete contract.

Initial source capture, encryption and core freezing still need to fit one
allowed run. An indivisible local input or whole-file upload exceeding the window
is reported incomplete; resumability does not promise universal convergence.
Ordinary readbacks use an installation-specific directory under the chosen large
temp location; cleanup and the next lock owner reclaim only its named files.
Both durable pending and retired spool directories are excluded from extra-dir
capture through the existing overlap guard.

The frozen epoch also preserves the existing payment-vault producer state. A
failed or missing vault capture keeps its last-good archive under the explicit
last-good remote name and records the original gap in COMPLETE, as before. A
resumed epoch never relabels that older archive fresh; local status describes the
current run, while the publication progress object records the frozen vault
state. Restore continues to stage accepted fresh vaults rather than activate
them, and reports a gap instead of installing a last-good fallback.
