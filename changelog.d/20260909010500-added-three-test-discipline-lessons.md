- **The development guide now names three ways a test can look rigorous and
  prove nothing.** A test that fails before the fix is the standard evidence
  that it caught something, but the failure has to come from the assertion that
  describes the defect — when it comes from setup instead, what it proves is
  that the test never built the situation it was written for. Related: a test
  of the seam between two components has to run both of them, because a value
  typed by hand in the middle is exactly where the defect hides, and a copy of
  the fix pasted into the test grades the copy rather than the shipped code.
  The guide also records that two reviewers pushing opposite values for the
  same field position a review apart is a signal that the code depends on
  something that is not fixed, not an invitation to pick a side.
