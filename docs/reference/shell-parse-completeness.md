# Shared shell parser completeness

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
