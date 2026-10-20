- The merge gate no longer treats bounded legacy phrases such as “Hard blocks:
  None” and “no PII hard blocks” as findings. Both the automated-review and
  scheduled-review scans retain genuine findings elsewhere in the same body;
  these negative phrases do not clear earlier findings or replace an explicit
  clean verdict.
