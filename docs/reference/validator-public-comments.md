# Privacy-scanned issue and PR comments

`scripts/file_tracker_issue.py --comment-on <positive-number> --body-file <file>`
adds a timeline comment on an issue or PR in the resolved shared tracker.
It does not create reviews, inline comments, edits or reactions. Issue creation
keeps its existing title/area/difficulty arguments; mixing them with comment
mode refuses. `--dry-run` performs the checks and submits nothing.

The caller owns authorization and privacy judgment. Never publish an unfixed
security defect or private finding. The scanner detects supported patterns;
passing it is not a universal privacy guarantee or permission to publish.
Validators must not use comments to request development reviews or trigger paid
agents. This Python entry point is not the direct-gh review-budget classifier.

Comment mode reads and scans the complete UTF-8 body before any GitHub request,
then makes a private immutable snapshot of those bytes. Later draft edits do
not change what is submitted. It uses the existing four prose detectors and
fails closed when their required dependencies/fingerprint floor are unavailable.
Lookup failures do not print raw API stderr, draft paths or exception messages.

The existing fork-root resolution, write-permission check and per-tracker lock
apply. Target preflight checks the canonical GitHub host, repository and number;
a transferred or mismatched target refuses. Comments are listed exhaustively
using pagination; all page and comment identities are checked before absence
is claimed, including pages after a matching body. Comparison is exact, without
whitespace, newline or Unicode normalization. It proves neither semantic
uniqueness nor authorship. An identical existing body produces duplicate exit3.

One POST uses argv file input, with no shell interpolation. A confirmed result
must match the target, comment id and raw body. Timeout, nonzero exit,
interruption or malformed confirmation triggers only a read-only exact-body
lookup. A matching comment establishes existence; otherwise the outcome stays
unknown, even if the read completed and found none. There is no second POST.
After exit4, read the target before considering a retry. A confirmed URL stays
confirmed through reporting, lock-release or cleanup failures. Private snapshot
cleanup and the existing signal-restoration behavior still apply.

Exit codes are0 for confirmed existence or a valid dry run,2 for a refusal
before submission,3 for an existing exact duplicate,4 for an uncertain
submission, and1 for an unexpected pre-submission error. The lock covers this
install; another host can race it. Preflight is not atomic with a later remote
transfer, rename or edit, and GitHub may normalize a body that was sent exactly.
Those limits must not be reported as proof that nothing was posted.

`.agents/skills/validating-merges` links to the canonical `.claude/skills/`
doctrine. Its Running under Codex section maps the dedicated validator MCP
tools, keeps progress outside Genesis session lifecycle, requires scanned
publishing and forbids deployment/fixes/merge/push. The verification writer
dependency and full dedicated-launcher qualification are still required before
using that validator flow. Skill discovery alone activates nothing.
