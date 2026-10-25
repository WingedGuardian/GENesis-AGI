# Shared shell parser completeness

For a scanner call whose input contains no literal `#`, the unquoted scanner
consumes a backslash and its following non-newline character as word data,
retaining both source bytes for later shell decoding. Escaped quotes/operators
then cannot change scanner quote/operator state. Hash-containing calls retain
the earlier scanner behavior, avoiding a new comment/quote regression. A decoded
hash-free child may still gain the repair even when its parent contains `#`.
Backslash-newline retains the existing continuation handling. No input rewriting,
new tokenizer, larger limit or generic tokenization exemption is added.

This is a partial repair: #3257 and #3264 remain open for their broader
hash/escape and substitution classes. Validators separately refuse commands
containing both literal `#` and backslash before parsing, including harmless
quoted examples. Ordinary clients do not acquire that refusal. Use body files
for data; neither layer claims complete Bash grammar or process isolation.

`analyze_checked` withholds segments when a length/depth bound, continuation,
built escape, unreadable nested syntax or hidden heredoc suffix makes them
unreliable. Legacy `analyze` still returns its best-effort segments. Structural
and heredoc facts remain independent when a stronger diagnostic wins; recovery
can only add Git/GitHub refusals and never contributes on actual size bounds.

The heredoc traversal retains its existing narrow data proofs. Proof applies to
the current extracted fragment only and never passes into decoded scripts. An
empty hidden-suffix report is not a proof. A carried-body tokenization failure
keeps its heredoc remedy; an independent unreadable child keeps the structural
remedy. Root data excision remains root-only.

Accepted compatibility limit: some valid quoted heredocs with apostrophes are
explicitly refused when a substitution prefixes the receiver with another command
or redirection, such as `echo prefix; cat` or `cat 2>&1`. These lie outside the
canonical narrow proof. Use an editor/body file or separate simpler commands.
This is accident prevention, not a complete Bash interpreter or OS containment.

Tests retain both the current heredoc protections and nested-completeness cases,
plus combined-state recovery and the accepted extra-refusal boundary.
Real Bash shim tests cover escaped quotes, operators and word characters across
direct/nested scripts and command separators. The hash-free escaped-word
property is positive; the broader property retains its strict known hash-case
failure with an explicit witness. Comment/quote witnesses retain actual owned
execution receipts. One former escaped-double-quote substitution reproduction
also now surfaces its suffix; this does not establish that every substitution
quote case is fixed. The paired opposite-quote gap remains explicitly tracked.
