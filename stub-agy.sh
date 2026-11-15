#!/bin/bash
# Setup script for an Antigravity cloud environment (/home/antigravity/env-setup.sh):
# verifies the signed bootstrap.py from claude-env, installs tools, and warms the session.
set -euo pipefail

BASE=https://raw.githubusercontent.com/dvcrn/claude-env/main
SIGNER='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILI+2eft+G5merFwPUtQobbra0i34484V5mFe7aqb4KS claude-env-signing'
MIN_VERSION=2

dir=$(mktemp -d)
trap 'rm -rf "$dir"' EXIT
curl -fsSL --retry 3 "$BASE/bootstrap.py" -o "$dir/bootstrap.py"
curl -fsSL --retry 3 "$BASE/bootstrap.py.sig" -o "$dir/bootstrap.py.sig"
echo "claude-env $SIGNER" > "$dir/allowed_signers"
ssh-keygen -Y verify -f "$dir/allowed_signers" -I claude-env -n claude-env \
  -s "$dir/bootstrap.py.sig" < "$dir/bootstrap.py"
MIN_VERSION=$MIN_VERSION SIGNER=$SIGNER python3 -I "$dir/bootstrap.py" setup
python3 -I "$HOME/.local/share/cloud-env/bootstrap.py" session-start --no-update
