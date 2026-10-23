#!/bin/bash
# Setup script for a Claude Code cloud environment: verifies the signed bootstrap.py from
# claude-env and runs it. Identical in every environment; SIGNER is the key in signing_key.pub.
set -euo pipefail

# No-scope GitHub token: lifts setup's anonymous api.github.com rate limit (60/h per shared IP)
MISE_GITHUB_TOKEN=''
if [ -n "$MISE_GITHUB_TOKEN" ]; then export MISE_GITHUB_TOKEN; fi

BASE=https://raw.githubusercontent.com/dvcrn/claude-env/main
SIGNER='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILI+2eft+G5merFwPUtQobbra0i34484V5mFe7aqb4KS claude-env-signing'
MIN_VERSION=1

# The base image ships without openssh-client
if ! command -v ssh-keygen > /dev/null; then
  apt-get update -qq &&
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends openssh-client
fi

dir=$(mktemp -d)
trap 'rm -rf "$dir"' EXIT
curl -fsSL --retry 3 "$BASE/bootstrap.py" -o "$dir/bootstrap.py"
curl -fsSL --retry 3 "$BASE/bootstrap.py.sig" -o "$dir/bootstrap.py.sig"
echo "claude-env $SIGNER" > "$dir/allowed_signers"
ssh-keygen -Y verify -f "$dir/allowed_signers" -I claude-env -n claude-env \
  -s "$dir/bootstrap.py.sig" < "$dir/bootstrap.py"
MIN_VERSION=$MIN_VERSION SIGNER=$SIGNER python3 -I "$dir/bootstrap.py" setup
