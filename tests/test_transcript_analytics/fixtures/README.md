# Genuine historical store fixtures

These synthetic Parquet files were emitted by the unmodified producers below,
using `store.ingest` and each producer's own shared scrubber. Their footers were
not manufactured or restamped. Only the generated Parquet files and the original
relative `current` symlink are included; projects, inventory and absolute
projects-root bindings are deliberately absent.

| Fixture | Producer commit | Stored schema | Extraction signature |
|---|---|---|---|
| `flat-v1` | `27a1513ba4badcdb1e9dda3523f9214f83f8afe3` | 1 | `a61bb523389105b68dd9a7a29f461f0633b9572d66460de79e8f8bc4d0232a31` |
| `record-v5` | `f693174b7d301382af4f0e677b9801795742ab8c` | 5 | `5c864e56212e8cd3970105505fc5fc4de7299558b4764f7b0edda5cfef1ce376` |

The source, `historical.jsonl`, held one assistant Bash call (`echo historical`,
session `historical-session`, message `historical-message`, tool ID
`historical-tool`, input/output tokens 1/2, timestamp `2026-01-01T00:00:00Z`)
followed by its user result at `2026-01-01T00:00:01Z`, `is_error: true`, content
`[scrub-error: content withheld]`. This literal public placeholder deliberately
exercises the real producer's withholding path: the persisted tool row has both
`is_error` and `scrub_failed` true. No genuine credential or personal transcript
was used. The source transcript was then deleted.

The schema definitions and extraction/scrubber policies can be independently
audited at those commits. Tests copy these stores without originals and verify
all existing values, flags and original Arrow metadata, typed nulls for every
missing column, strict non-current status, and exclusion of unknown signatures.
Dependent query/cache tests use the same fixtures rather than silently
re-extracting them with the current parser.
