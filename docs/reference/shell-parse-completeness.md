# Shared shell parser completeness

For a scanner call whose input contains no literal `#`, the unquoted scanner
consumes a backslash and its following non-newline character as word data,
retaining both source bytes for later shell decoding. Escaped quotes/operators
then cannot change scanner quote/operator state. Hash-containing calls keep
the earlier backslash handling, avoiding a new comment/quote regression. Comments
are modelled separately: a `#` that starts a word at bash's top level (outside
every quote, open bracket and backtick) runs to end of line and changes no scanner
state, except in a command containing `<<`, which keeps the earlier scan. A decoded
hash-free child may still gain the repair even when its parent contains `#`.
Backslash-newline retains the existing continuation handling. No input rewriting,
new tokenizer, larger limit or generic tokenization exemption is added.

This is a partial repair, completed for the escaped-quote case by refusal: in
hash-bearing text an escaped quote outside quotes now refuses (#3713, below),
so it no longer hides the next command. The broader substitution class (#3264)
is refused rather than read until the shared lexer lands. A separate, later
validator change is planned to refuse commands containing both literal `#` and
backslash before parsing; nothing in this tree does so yet, and ordinary clients
would not acquire that refusal. Use body files for data; the parser does not claim complete Bash
grammar or process isolation.

One new refusal follows from the repair, in the fail-closed direction. The
substitution scanner (`_substitution_spans`) has no unquoted-backslash rule, so
a segment the scanner now keeps whole, such as `echo \`id\``, yields a
backtick body ending in a backslash that cannot be tokenized, and the call is
reported blind. Hash-free commands that pair an escaped quote with escaped
backticks, for example `echo It\'s; echo \`git push\``, parsed before and are
now refused; `echo \`id\`; curl x` alone was already refused. That refusal is
kept on purpose (#3713, below): outside double quotes a backslash before `$(`,
before a backtick when the segment holds another backtick, or before a quote in
a segment holding a substitution, refuses. So does an escaped quote outside quotes
in any command that contains a `#`, because the segmenter models escapes only in
`#`-free text: there the quote it opens can swallow a separator and the command
after it (`echo \' ; git push … "'" #`). A lone escaped backtick stays readable.

## Unproven comment and substitution boundaries (#3713, PR 1 of 2)

The segmenter, the comment stripper and the override scan each track quotes on
their own and none of them models substitutions, so where they disagree with bash
a command could run while the parse showed neither it nor a blind spot.
`_segment_unproven` names the boundaries that cannot be trusted, and
`analyze_checked` then withholds the parse with `_BLIND_STRUCTURAL`:

- a segment whose comment has a newline after it: a quote in the comment carried
  the scan past the line end (commands containing `<<` or an escaped quote, where
  the top-level comment skip does not run);
- a double-quoted substitution whose body holds a `"`, when the text the legacy
  pairing then reads as unquoted holds a separator, redirection, `#`, newline,
  backtick, backslash or unbalanced paren, or the pairing ends outside the outer
  quote. `"$(basename "$f")"` stays readable; `"$(printf "x # note")"` does not;
- a backtick body holding a backslash, and a decoded `-c` script holding `\$`
  or `` \` ``.

It is refusal only. Substitution extraction is unchanged, so a substitution inside
a comment is still parsed; it never hides one. The withholding applies only where
`analyze_checked` would otherwise report nothing or an unresolved verb: every
existing cause keeps its class and rank, including the untokenizable cause the
discard guard exempts. Segments inside a here-doc body the heredoc check verified
are data and are not checked, but only when bash reads the opener as one too: at
its top level by the bash-like pass, with no `#` before it on its line. A `<<`
after an escaped quote or in a comment opens no body to bash, so its lines are
checked like any others. A fragment's own inert proof clears only that fragment. A substitution that never closes is not refused: bash runs nothing past
it.

Two comment scans run side by side: main's, and a strict one that reads bash's
blanks (space, tab, newline; not VT, FF or NBSP) and treats a backslash outside
quotes as an escape. Neither is bash, so neither alone decides anything
permissive:

- argv keeps main's cut exactly, in both directions. Cutting earlier drops words
  bash passes (`--force` after `${x:+ \' # }`), and cutting later keeps words bash
  never passes (a `--help` that skips a gate).
- Where main's cut drops text the strict scan says bash reads, and that text could
  carry a flag or an expansion (`-`, `$`, a backtick), the segment refuses
  (`git commit -m x\ # --no-verify` passes `--no-verify`).
- An override counts only when both scans read it, so the override scan can only
  switch an override off. The strict scan also skips a `#` inside a substitution
  and waives nothing after an escaped quote.

A `<<` inside a here-doc body already kept is that body's data and opens no body
of its own.

Still open: #3778 (a `#` inside process substitution, `${…}` or a `case`
pattern), and the shared lexer that would let these boundaries be proven rather
than refused (#3713, PR 2).

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
property is positive, and the broader hash-bearing property now holds too: such
text is refused rather than read (#3713). Comment/quote witnesses retain actual owned
execution receipts. One former escaped-double-quote substitution reproduction
also now surfaces its suffix; this does not establish that every substitution
quote case is fixed. The paired opposite-quote case is now refused rather than
parsed (#3713), and the Bash differential's generator draws quote-bearing words
again: across 10 seeds of 1,500 commands it finds no command bash runs that the
parse neither shows nor refuses.
