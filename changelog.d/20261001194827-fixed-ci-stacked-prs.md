- **CI now runs on stacked pull requests and on retargets.** The CI workflow
  used to fire only for PRs targeting `main`, so a PR based on another PR's
  branch got no checks at all, and retargeting it after the parent merged did
  not re-run them either. CI now runs on every pull request regardless of base
  branch, and a base-branch change re-triggers it. Editing just a PR title or
  body still runs nothing. The checks that diff against the base (leak
  detector, CC pin-receipts) now compute the range against the PR's real base
  branch instead of assuming `main`.
