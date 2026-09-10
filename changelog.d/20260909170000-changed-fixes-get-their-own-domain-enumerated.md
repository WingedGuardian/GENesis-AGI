- **The development skill now names the six axes review findings actually fall
  along.** Genesis classified 31 review findings from two pull requests,
  attributing each to the commit that introduced the line it points at. Of the 25
  distinct findings left after removing duplicates and one that later proved
  unfounded, 24 fall into six shapes: a signal that does not change when the
  property it stands for changes; a scope or lifetime narrower than the operation
  it guards; an input the type permits but nobody pictured; an equivalence
  claimed but never checked; a failure path that leaves work behind; and a
  caller's explicit option silently dropped. The 25th was a test whose identity
  comparison was unsound in both directions. Those six are now a checklist to run
  against new code before the first review rather than after it.

  The same measurement settled where findings live, which was worth knowing
  because two successive intuitions about it were both wrong. Counting every
  round, 21 of the 31 sat in the original implementation — but a first review has
  no fix code to find anything in, so that number is partly arithmetic. Counting
  only the rounds that actually loop, it is nearly even: 11 findings were already
  in the original code and 10 were in code written to answer an earlier round.
  Neither writing the feature nor answering the review is the dominant source,
  and the checklist applies to both.

  The entry records what would disprove it, with a rate and a sample size rather
  than an adjective, and a note on the attribution method — an earlier version of
  this measurement was inverted by treating a review comment's recorded commit as
  the commit that introduced the line, when it is the branch head at the moment
  the review was posted.
