- **Development skills gain new rules and correct claims the code does not
  support.**
  - New rules:
    - a fix is measured against the original exploit and its neighbouring
      spellings, not just asserted;
    - a setting's effect scope is checked against the scope that actually
      loads it;
    - a root cause counts only once it is verified to certainty;
    - a separate PR never means a separate session;
    - a mutation harness refuses a non-unique anchor;
    - `/deep-review` verifies each finding before triage.
  - New guidance: several sigils go in one comment, because a second `#` ends
    the run.
  - Corrections:
    - a Python `finally:` does not run on SIGTERM or SIGHUP without a handler;
    - the gate-fix lane's round 1 is dual, the diff shrinks in scope after
      round 1, and the lane allows two discovery rounds plus one marked
      confirmation;
    - the retired `final-round-accept` sigil is named only when a gate is
      already asking for approval.
