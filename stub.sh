#!/bin/bash
# Setup script for a Claude Code cloud environment: verifies the signed bootstrap.sh from
# claude-env and runs it. Identical in every environment; set SIGNER to the signing key.
set -euo pipefail

BASE=https://raw.githubusercontent.com/dvcrn/claude-env/main
SIGNER='ssh-ed25519 REPLACE_ME claude-env'
MIN_VERSION=1

case "$SIGNER" in
  *REPLACE_ME*) echo "stub: SIGNER is still the placeholder, refusing to run" >&2; exit 1 ;;
esac

# The base image ships without openssh-client
if ! command -v ssh-keygen > /dev/null; then
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssh-client
fi

dir=$(mktemp -d)
curl -fsSL --retry 3 "$BASE/bootstrap.sh" -o "$dir/bootstrap.sh"
curl -fsSL --retry 3 "$BASE/bootstrap.sh.sig" -o "$dir/bootstrap.sh.sig"
echo "claude-env $SIGNER" > "$dir/allowed_signers"
ssh-keygen -Y verify -f "$dir/allowed_signers" -I claude-env -n claude-env-bootstrap \
  -s "$dir/bootstrap.sh.sig" < "$dir/bootstrap.sh"
MIN_VERSION=$MIN_VERSION SIGNER=$SIGNER bash "$dir/bootstrap.sh"
