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

Local capture validation does not establish remote pool integrity. Pool transfer
uses remote names only to avoid initial uploads, then rereads every referenced
object and verifies its digest and size. A corrupt object is repairable only from
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
