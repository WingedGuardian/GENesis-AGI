#!/bin/bash
# Genesis pre-push hook — block ALL direct pushes to main on origin, and block
# publishing a `Deploy-rebuild:` commit to ANY remote.
# Private/fork remotes are otherwise unrestricted — users own their forks.
# Portable copy: lives in scripts/hooks/, installed by scripts/hooks/sync-hooks.sh

remote="$1"

# An all-zero object id means "no object" (a deletion, or a ref the remote does
# not have yet). Its length depends on the repository's hash: 40 for SHA-1, 64
# for SHA-256, so test the shape, never one fixed string.
is_zero_oid() {
    case "$1" in
        ""|*[!0]*) return 1 ;;
        *) return 0 ;;
    esac
}

while read local_ref local_oid remote_ref remote_oid; do
    # A `Deploy-rebuild:` commit exists only on `live`, the local integration
    # branch (origin/main plus unmerged candidate branches, rebuilt locally).
    # A branch that contains one was cut from `live`, so it carries other PRs'
    # unreviewed code; publishing it anywhere, fork included, is never right.
    # Deletions (local_oid all zeros) publish no commits.
    if ! is_zero_oid "$local_oid"; then
        # Walk exactly what this push adds when the remote's current tip is known
        # locally. Otherwise (a new ref, or a tip updated elsewhere and never
        # fetched) walk the WHOLE ancestry: remote-tracking refs record the last
        # fetch, not what the remote holds now, so excluding what they reach could
        # hide a rebuild commit the remote has since dropped. (MEASURED 0.18 s for
        # a whole-history walk of this repository.)
        if ! is_zero_oid "$remote_oid" && git cat-file -e "$remote_oid^{commit}" 2>/dev/null; then
            range_args=("$remote_oid..$local_oid")
        else
            range_args=("$local_oid")
        fi
        # The one exclusion that IS safe: origin's main. It is protected and only
        # moves forward, so even a stale local copy of it reaches only commits the
        # public repository already carries on main; publishing them again adds
        # nothing unreviewed. Without it, a single rebuild commit reaching main
        # (a squash commit whose message happens to end in the trailer) would
        # refuse every new branch cut from main, on every install.
        if git rev-parse -q --verify refs/remotes/origin/main >/dev/null 2>&1; then
            range_args+=(--not refs/remotes/origin/main)
        fi
        # Read the commits RAW and find the trailer here, never through git's
        # trailer formatting. Formatting obeys configuration, and settings kept
        # hiding the trailer one at a time: a key alias (`trailer.<key>.key`)
        # renames it, `i18n.logOutputEncoding=UTF-16` re-encodes the whole
        # message, a separator or comment character stops git recognising it
        # (MEASURED on git 2.43; `git interpret-trailers --parse` obeys the same
        # trailer settings, so it is no way out). `rev-list` only names commits
        # and `cat-file --batch` prints each object exactly as stored, so no
        # setting reaches what the scan reads. --no-replace-objects: a replace
        # ref (`git replace`) would otherwise show another commit's message, and
        # parents, in place of the real one the push sends.
        if ! pushed=$(git --no-replace-objects rev-list "${range_args[@]}" 2>/dev/null); then
            echo "BLOCKED: could not list the commits this push publishes to $remote_ref."
            echo "Refusing rather than risk publishing a Deploy-rebuild commit."
            exit 1
        fi
        hit=""
        if [ -n "$pushed" ]; then
            want=$(printf '%s\n' "$pushed" | grep -c .)
            # The scan is WIDER than git's trailer rules by construction rather
            # than a copy of them: ANY line after the message's first paragraph
            # (the subject, which git never reads as trailers) that starts with the
            # key counts, matched case-insensitively with optional whitespace
            # before the `:`. git finds the trailer block only after dropping
            # trailing comment lines, a `Conflicts:` block and a scissors line,
            # and a last-paragraph copy of that rule missed those shapes; every
            # trailer git can find lies after the subject, so this cannot. The cost
            # is a body line of prose starting with the key (MEASURED: 0 of 2,079
            # commits in this repository's history). The batch is
            # `<oid> commit <size>`, <size> bytes of object, then a newline;
            # LC_ALL=C makes length() count bytes. It prints exactly one verdict:
            # `HIT <oid>`, `OK`, or `ERR` when a header is not a commit or the
            # number of objects read differs from the number listed. Anything else
            # (no output at all included) refuses the push below.
            verdict=$(printf '%s\n' "$pushed" | git --no-replace-objects cat-file --batch 2>/dev/null | LC_ALL=C awk -v want="$want" '
                function blank(s) { return s ~ /^[ \t\r]*$/ }
                function scan(   i) {
                    i = 1
                    while (i <= n && blank(msg[i])) i++
                    while (i <= n && !blank(msg[i])) i++
                    for (; i <= n; i++)
                        if (tolower(msg[i]) ~ /^deploy-rebuild[ \t]*:/) { hit = oid; return }
                }
                remaining <= 0 {
                    if (NF == 3 && $2 == "commit" && $1 ~ /^[0-9a-f]+$/ && length($1) >= 40 && $3 ~ /^[0-9]+$/) {
                        oid = $1; remaining = $3 + 1; in_msg = 0; n = 0
                        next
                    }
                    bad = 1
                    exit
                }
                {
                    remaining -= length($0) + 1
                    if (in_msg) msg[++n] = $0
                    else if ($0 == "") in_msg = 1
                    if (remaining == 0) {
                        records++
                        scan()
                        if (hit != "") exit
                    } else if (remaining < 0) {
                        bad = 1
                        exit
                    }
                }
                END {
                    if (hit != "") print "HIT " hit
                    else if (bad || remaining != 0 || records != want) print "ERR"
                    else print "OK"
                }')
            case "$verdict" in
                "HIT "*) hit=${verdict#HIT } ;;
                OK) ;;
                *)
                    echo "BLOCKED: could not read the commits this push publishes to $remote_ref."
                    echo "Refusing rather than risk publishing a Deploy-rebuild commit."
                    exit 1
                    ;;
            esac
        fi
        if [ -n "$hit" ]; then
            echo "BLOCKED: this push to $remote_ref publishes a Deploy-rebuild commit ($hit)."
            echo ""
            echo "Deploy-rebuild commits exist only on 'live', the local integration branch,"
            echo "so this branch was cut from 'live' and carries other candidates' unmerged"
            echo "code. Recreate it from origin/main and carry only your own commits over:"
            echo "  git worktree add --no-track .claude/worktrees/<name> -b <scope>/<desc> origin/main"
            exit 1
        fi
    fi

    # Only protect the public repo (origin) for direct pushes to main.
    if [ "$remote" != "origin" ]; then
        continue
    fi
    if echo "$remote_ref" | grep -qE 'refs/heads/(main|master)$'; then
        # Block ALL direct pushes to main — force or fast-forward.
        # The only way to update main is via PR merge (gh pr merge).
        echo "BLOCKED: Direct push to $remote_ref on origin is not allowed."
        echo "Use a feature branch and PR instead: git push -u origin <branch>"
        exit 1
    fi
done

exit 0
