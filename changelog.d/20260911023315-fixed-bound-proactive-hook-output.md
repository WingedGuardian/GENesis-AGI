- The per-prompt memory hook can no longer lose its own output. Claude Code
  silently files a hook's stdout past a size threshold and shows only a short
  preview, so an oversized injection costs that prompt its recalled memories,
  its list of concurrent sessions, and the safety line that tells Claude not to
  treat another session's text as your input — with no error anywhere. Four
  parts of the injection had no size limit; the worst was a pasted hash or token
  in your prompt, which the hook kept whole and then repeated on every later
  prompt in the session. Each part is now bounded where it is built: over-long
  pasted tokens are dropped rather than cut (a cut one collides with other ids),
  the session-trail line drops whole old topics instead of ending mid-topic, a
  long code signature is shortened while its file path is kept intact, and the
  concurrent-session list is capped with an explicit "+N more not shown" so a
  short list is never mistaken for a quiet machine.
