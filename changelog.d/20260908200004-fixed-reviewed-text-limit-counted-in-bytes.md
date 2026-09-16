- **The size limit on reviewed text now bounds what it claims to.** It counted
  characters, so text in a non-Latin script or heavy in emoji could be several
  times larger than the limit implied, with no combined bound across the message
  and the reply together. Counting bytes bounds it for any script. Ordinary
  English text is unaffected — for it the two counts are the same number.
