- **When work is killed for running out of memory, Genesis now says so.** A
  process the kernel reaps dies without a word, so a session that lost its work
  that way reported a bare failure — indistinguishable from a crash or a timeout,
  and typically followed by an immediate retry of the same memory-heavy work
  under the same pressure. Genesis now checks whether the kernel actually reaped
  anything while that work ran, and only then names the cause. It stays silent
  whenever the evidence is short of that: a different kind of exit, a counter it
  cannot read, or a process Genesis stopped itself. The attribution also stops
  the retry — the failure was the machine running out of memory, so re-running
  the same work, or handing it to a different model, only repeats it.
