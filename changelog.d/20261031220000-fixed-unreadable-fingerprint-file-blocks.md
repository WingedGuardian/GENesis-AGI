- **The privacy scan no longer passes when the fingerprint file cannot be read.**
  When `~/.genesis/release-fingerprints.txt` (or `GENESIS_RELEASE_FINGERPRINTS`)
  existed but could not be read, the exact-value scan returned no findings and was
  recorded as having run, so text bound for a public issue or PR passed a check
  that never looked at it. It now blocks with a message naming the fix (repair the
  file's permissions or regenerate it). A missing file is unchanged: a warning for
  a PR diff, a block for an issue.
