# shellcheck shell=bash
# The `live` readiness marker (#2978).
#
# scripts/deploy_candidates_gate.py (PR_C_MARKER) reads this file's PRESENCE in
# the server's base: merge-base(the commit genesis-server booted from,
# origin/main). While it is absent, `deploy_candidates add` and `rebuild` refuse
# on every install and the engine stays inert. Once this file is in the deployed
# base, AND (where a host guardian is configured) the host guardian's deployed
# commit carries its `live` ban (#3546; the same readiness check), an install may
# run candidates on `live`.
#
# What already protects a `live` checkout when this file arrives:
#   - update.sh and deploy_code_only.sh rebuild `live` through the engine and
#     refuse to merge onto it;
#   - bootstrap's crash recovery never rolls `live` back;
#   - the boot repair (live_move_repair.py, run by the server unit) heals a
#     checkout move killed partway;
#   - the host guardian never runs `git revert` on `live`: it proposes a
#     container restart instead.
#
# The gap, accepted by the owner (2026-11-20) until the Guardian's drop lands
# (#4841's rework, PRs B1 and B2): nothing removes a candidate that keeps the
# server down. Remove it with
#   scripts/deploy_candidates drop <branch> --wait 0
# then scripts/deploy_code_only.sh restart (launched detached).
#
# Nothing sources this file. It is a marker, so deleting it disarms `live`;
# admission refuses a candidate that changes it (REFUSAL_FILES).
