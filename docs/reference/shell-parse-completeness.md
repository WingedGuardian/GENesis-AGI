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
hash/escape and substitution classes. A separate, later validator change is
planned to refuse commands containing both literal `#` and backslash before
parsing; nothing in this tree does so yet, and ordinary clients would not acquire
that refusal. Use body files for data; the parser does not claim complete Bash
grammar or process isolation.

One new refusal follows from the repair, in the fail-closed direction. The
substitution scanner (`_substitution_spans`) has no unquoted-backslash rule, so
a segment the scanner now keeps whole, such as `echo \`id\``, yields a
backtick body ending in a backslash that cannot be tokenized, and the call is
reported blind. Hash-free commands that pair an escaped quote with escaped
backticks, for example `echo It\'s; echo \`git push\``, parsed before and are
now refused; `echo \`id\`; curl x` alone was already refused. #3635 tracks
giving the substitution scanner the same backslash rule.

`analyze_checked` withholds segments when a length/depth bound, continuation,
built escape, unreadable nested syntax or hidden heredoc suffix makes them
unreliable. Legacy `analyze` still returns its best-effort segments. Structural
and heredoc facts remain independent when a stronger diagnostic wins; recovery
can only add Git/GitHub refusals and never contributes on actual size bounds.

The heredoc traversal retains its existing narrow data proofs. Proof applies to
the current extracted fragment only and never passes into decoded scripts. An
empty hidden-suffix report is not a proof. A nested level that cannot be tokenized
only because its own here-doc body is carried keeps the heredoc cause and remedy.
An independent unreadable child, including one extracted from a carried body,
reports the structural cause and wins; both are bounds-type and refuse the same
way. Root data excision remains root-only.

Accepted compatibility limit: some valid quoted heredocs with apostrophes are
explicitly refused when a substitution prefixes the receiver with another command
or redirection, such as `echo prefix; cat` or `cat 2>&1`. These lie outside the
canonical narrow proof. Use an editor/body file or separate simpler commands.

Accepted compatibility limit, broader: the substitution scan reads a quoted
heredoc's body lines as command text, so a body line holding a `$(...)` with an
apostrophe inside it (for example `$(echo don't)`, which shell prose often
contains) is an unreadable nested extraction. That makes the whole command
structurally uncertain, so a command that also mentions git is refused by the
push, commit and discard guards even when its git part is readable, such as
`git add f && cat <<'EOF' > notes` followed by that body. At the root, an
untokenizable command stays the discard guard's to leave alone; this applies to
nested extractions only. Telling body lines apart from real nested scripts needs
heredoc state this module does not model. Write such text with a file-writing
tool, or run the git command separately. The frequency of this shape on a live
install is unmeasured.
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
