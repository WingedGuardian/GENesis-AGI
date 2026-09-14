- **A code review that finds an unrelated bug now files it and lets the pull
  request through.** Reviews regularly turn up real defects that already existed
  and have nothing to do with the change being reviewed. The rule is one
  question: does the change work without that fix? If it does, the finding
  becomes a tracked issue and the change merges; only a defect that actually
  breaks the change gets folded into it. This keeps a review from widening into
  an open-ended cleanup, which is what turns a small change into a long series
  of review rounds. A security defect is the exception and is never posted
  publicly before it is fixed.
