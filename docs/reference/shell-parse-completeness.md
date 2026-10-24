# Shared shell parser completeness

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
