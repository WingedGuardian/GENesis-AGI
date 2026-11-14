#!/usr/bin/env bash
# Install ripgrep from its official GitHub release, pinned by version and sha256.
#
# Used by CI jobs that need `rg`: the test job (locate's rg backend, the branch
# leak-scan workflow tests, the portability checks) and the leak detector. The
# release asset replaces `apt-get install ripgrep`, which fails the job whenever
# the runner's apt mirror is unreachable (#4376), and it pins the exact binary
# the tests run against.
#
# To bump: change VERSION, then take SHA256 from the release's own
# `<asset>.tar.gz.sha256` file (https://github.com/BurntSushi/ripgrep/releases).
set -euo pipefail

# HOME may be unset (a sandboxed or `env -i` caller): resolve it from the passwd
# entry for this uid, the same source Path.home() uses, and fail closed if that
# is unresolvable too.
if [ -z "${HOME:-}" ]; then
    HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6)" || HOME=""
    [ -n "$HOME" ] || { echo "install_ripgrep: HOME is unset and could not be resolved from passwd." >&2; exit 1; }
    export HOME
fi

VERSION="15.1.0"
SHA256="1c9297be4a084eea7ecaedf93eb03d058d6faae29bbc57ecdaf5063921491599"  # pragma: allowlist secret
ASSET="ripgrep-${VERSION}-x86_64-unknown-linux-musl"
URL="https://github.com/BurntSushi/ripgrep/releases/download/${VERSION}/${ASSET}.tar.gz"

if [[ "$(uname -m)" != "x86_64" ]]; then
  echo "install_ripgrep: only the x86_64 release is pinned (this machine is $(uname -m))" >&2
  exit 1
fi

work="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/ripgrep.XXXXXX")"
trap 'rm -rf "$work"' EXIT

curl --fail --location --silent --show-error \
  --retry 5 --retry-all-errors --retry-delay 3 \
  --output "$work/rg.tar.gz" "$URL"
echo "${SHA256}  $work/rg.tar.gz" | sha256sum --check --strict -

tar -xzf "$work/rg.tar.gz" -C "$work" "${ASSET}/rg"
bindir="$HOME/.local/bin"
mkdir -p "$bindir"
install -m 0755 "$work/${ASSET}/rg" "$bindir/rg"
if [[ -n "${GITHUB_PATH:-}" ]]; then
  echo "$bindir" >> "$GITHUB_PATH"
elif [[ ":$PATH:" != *":$bindir:"* ]]; then
  echo "install_ripgrep: $bindir is not on PATH; add it to use rg" >&2
fi
"$bindir/rg" --version | head -1
