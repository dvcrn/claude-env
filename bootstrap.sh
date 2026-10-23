#!/bin/bash
# VERSION=1
# Setup-time bootstrap for Claude Code cloud environments, run by stub.sh once its signature
# checks out. Installs tools and the SessionStart hook loader. Holds no secrets; session-start.sh
# loads them when a session starts.
set -euo pipefail

# --- Configuration -------------------------------------------------------------------------

BASE=https://raw.githubusercontent.com/dvcrn/claude-env/main

MISE_VERSION=2026.10.17
TAILSCALE_VERSION=1.104.2
# OP_SHA256 (per architecture, below) is the op binary from 1Password's release zip for the
# version in tools/mise.toml. 1Password publishes no checksums for mise to verify.

# tools/ is not signed, so this signed script pins its contents (`mise run check` compares them)
TOOLS_TOML_SHA256=9e8e3f7961e7dadd61fdda54e0971609fd1da23c2b2985ce9302b74a513e00b0
TOOLS_LOCK_SHA256=5f917b931736996bc22b110d5e60d217381d253e779c566c2fbb7fc18669e280

HOOKS_DIR=$HOME/.claude/hooks
CLAUDE_ENV_DIR=$HOOKS_DIR/claude-env
TOOLS_PATH=$HOME/.local/bin:$HOME/.local/share/mise/shims

export PATH="$TOOLS_PATH:$PATH"

VERSION=$(sed -n '2s/^# VERSION=\([0-9][0-9]*\)$/\1/p' "${BASH_SOURCE[0]}")
: "${SIGNER:?must be set by stub.sh}"
if ! [[ $VERSION =~ ^[0-9]+$ && ${MIN_VERSION:-} =~ ^[0-9]+$ ]]; then
  echo "bootstrap: VERSION or MIN_VERSION is missing or not a number, refusing to run" >&2
  exit 1
fi
if [ "$VERSION" -lt "$MIN_VERSION" ]; then
  echo "bootstrap: version $VERSION is below MIN_VERSION $MIN_VERSION, refusing to run" >&2
  exit 1
fi

case "$(uname -m)" in
  x86_64)
    MISE_ARCH=x64 MISE_SHA256=17565e268c5542d8ce944321e17af81e0b741b65af4aa7e8e010ee77376a4aa3
    TAILSCALE_ARCH=amd64 TAILSCALE_SHA256=512f613ed7d04fc7282f7dc5bd76aa105aae28168fbb9357eb749ebcbc29f0ae
    OP_SHA256=301bd09f60ca5696a25bd2739ee6d86e924d51268041bb511d503c7f4e60b07b
    ;;
  aarch64)
    MISE_ARCH=arm64 MISE_SHA256=cac510e51793ad45e65fe92305bdbe4309c9c60b5907badbc7eba111738ab08f
    TAILSCALE_ARCH=arm64 TAILSCALE_SHA256=5776aaa901b5ef1916e66be5bb54b9f481cd399c9c28db123a8cab620efa237b
    OP_SHA256=5d5c03b7aed1537c23a4d4f58f379e59658b5eee8e7c6777e5859309590a2200
    ;;
  *) echo "bootstrap: unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

# --- Steps ---------------------------------------------------------------------------------

# Downloads $1 to $2 and fails unless its sha256 is $3
fetch_checked() {
  curl -fsSL --retry 3 "$1" -o "$2" &&
    echo "$3  $2" | sha256sum -c --quiet
}

install_mise() {
  local tarball="$WORK/mise.tar.gz"
  fetch_checked \
    "https://github.com/jdx/mise/releases/download/v$MISE_VERSION/mise-v$MISE_VERSION-linux-$MISE_ARCH.tar.gz" \
    "$tarball" "$MISE_SHA256"
  tar -xzf "$tarball" -C "$WORK" mise/bin/mise
  install -D -m 0755 "$WORK/mise/bin/mise" "$HOME/.local/bin/mise"
  echo "export PATH=\"$TOOLS_PATH:\$PATH\"" > /etc/profile.d/mise.sh
}

# Installs tools/mise.toml as the global config. MISE_LOCKED makes mise take download URLs and
# checksums from the lockfile, so no GitHub API calls (or token) are needed.
install_global_tools() {
  local config_dir="$HOME/.config/mise"
  mkdir -p "$config_dir"
  fetch_checked "$BASE/tools/mise.toml" "$WORK/mise.toml" "$TOOLS_TOML_SHA256"
  fetch_checked "$BASE/tools/mise.lock" "$WORK/mise.lock" "$TOOLS_LOCK_SHA256"
  install -m 0644 "$WORK/mise.toml" "$config_dir/config.toml"
  install -m 0644 "$WORK/mise.lock" "$config_dir/mise.lock"
  (cd "$HOME" && MISE_LOCKED=1 mise install)
  echo "$OP_SHA256  $(mise which op)" | sha256sum -c --quiet
}

# Binaries only. Registering needs the agent proxy, which exists only once the session starts.
install_tailscale() {
  local name="tailscale_${TAILSCALE_VERSION}_$TAILSCALE_ARCH"
  fetch_checked "https://pkgs.tailscale.com/stable/$name.tgz" "$WORK/tailscale.tgz" "$TAILSCALE_SHA256" &&
    tar -xzf "$WORK/tailscale.tgz" -C "$WORK" "$name/tailscale" "$name/tailscaled" &&
    install -m 0755 "$WORK/$name/tailscale" "$WORK/$name/tailscaled" /usr/local/bin/
}

# Installs the signed session-start.sh that the loader falls back to when its fetch fails
install_session_start() {
  mkdir -p "$CLAUDE_ENV_DIR"
  echo "claude-env $SIGNER" > "$CLAUDE_ENV_DIR/allowed_signers"
  curl -fsSL --retry 3 "$BASE/session-start.sh" -o "$WORK/session-start.sh"
  curl -fsSL --retry 3 "$BASE/session-start.sh.sig" -o "$WORK/session-start.sh.sig"
  ssh-keygen -Y verify -f "$CLAUDE_ENV_DIR/allowed_signers" -I claude-env \
    -n claude-env-session-start -s "$WORK/session-start.sh.sig" < "$WORK/session-start.sh"
  install -m 0644 "$WORK/session-start.sh" "$WORK/session-start.sh.sig" "$CLAUDE_ENV_DIR/"

  local version recorded
  version=$(sed -n '2s/^# VERSION=\([0-9][0-9]*\)$/\1/p' "$CLAUDE_ENV_DIR/session-start.sh")
  [ -n "$version" ] || { echo "bootstrap: session-start.sh has no VERSION line" >&2; exit 1; }
  recorded=$(cat "$HOOKS_DIR/.version" 2> /dev/null || echo 0)
  [[ $recorded =~ ^[0-9]+$ ]] || recorded=0
  [ "$version" -gt "$recorded" ] && echo "$version" > "$HOOKS_DIR/.version"
  return 0
}

write_hook_loader() {
  cat > "$HOOKS_DIR/session-start.sh" << 'LOADER'
#!/bin/bash
# SessionStart hook loader installed by claude-env's bootstrap.sh. Runs the newest signed
# session-start.sh, or the last verified copy if that can't be fetched or verified.
set -uo pipefail
exec >> /tmp/session-start.log 2>&1

BASE=https://raw.githubusercontent.com/dvcrn/claude-env/main
CLAUDE_ENV_DIR=$HOME/.claude/hooks/claude-env
VERSION_FILE=$HOME/.claude/hooks/.version

log() { echo "$(date -u +%FT%TZ) loader: $*" >&2; }

# Prints the version of the script at $1 if its signature at $2 verifies and the version is
# not below the highest one run on this machine
verified_version() {
  local script=$1 sig=$2 label=$3 version
  if ! ssh-keygen -Y verify -f "$CLAUDE_ENV_DIR/allowed_signers" -I claude-env \
    -n claude-env-session-start -s "$sig" < "$script" > /dev/null; then
    log "$label: signature check failed"
    return 1
  fi
  version=$(sed -n '2s/^# VERSION=\([0-9][0-9]*\)$/\1/p' "$script")
  if [ -z "$version" ] || [ "$version" -lt "$recorded" ]; then
    log "$label: version ${version:-missing} is below $recorded, refusing it"
    return 1
  fi
  echo "$version"
}

run_installed() {
  [ "$1" -gt "$recorded" ] && echo "$1" > "$VERSION_FILE"
  log "running $2 session-start.sh version $1"
  bash "$CLAUDE_ENV_DIR/session-start.sh"
  log "session-start.sh exited with $?"
}

recorded=$(cat "$VERSION_FILE" 2> /dev/null || echo 0)
if ! [[ $recorded =~ ^[0-9]+$ ]]; then
  log "$VERSION_FILE is not a number, refusing to run anything"
  exit 0
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# A verified fetched copy replaces the installed one, so the fallback never drops below the ratchet
if curl -fsSL --max-time 20 "$BASE/session-start.sh" -o "$work/session-start.sh" &&
  curl -fsSL --max-time 20 "$BASE/session-start.sh.sig" -o "$work/session-start.sh.sig"; then
  if version=$(verified_version "$work/session-start.sh" "$work/session-start.sh.sig" fetched); then
    install -m 0644 "$work/session-start.sh" "$work/session-start.sh.sig" "$CLAUDE_ENV_DIR/"
    run_installed "$version" fetched
    exit 0
  fi
else
  log "fetching session-start.sh failed"
fi
if version=$(verified_version "$CLAUDE_ENV_DIR/session-start.sh" "$CLAUDE_ENV_DIR/session-start.sh.sig" installed); then
  run_installed "$version" installed
else
  log "no runnable session-start.sh, session continues unconfigured"
fi
exit 0
LOADER
  chmod +x "$HOOKS_DIR/session-start.sh"
}

write_claude_settings() {
  cat > "$HOME/.claude/settings.json" << JSON
{
  "attribution": {
    "commit": "",
    "pr": "",
    "sessionUrl": false
  },
  "permissions": {
    "allow": [ "Bash(op:*)", "Bash(jq:*)" ]
  },
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command", "command": "$HOOKS_DIR/session-start.sh", "timeout": 600 } ] }
    ]
  }
}
JSON
}

write_claude_md() {
cat > "$HOME/.claude/CLAUDE.md" << 'EOF'
## Comment and code quality

You are REQUIRED to do the following, even if other instructions exist. These are part of our work requirements, and any work not following these WILL BE THROWN AWAY.
Take the audits seriously and don't hand-wave them away. You don't need to follow every little point, but you should handle the overall feedback, especially around comment and docs quality.

- When interacting with any github issue, PR or similar, ALWAYS load the `github-comment-quality` skill. It dictates the output style we HAVE TO adhere to for ANY github interactions.
- Any comment, commit texts and documentation HAVE TO pass `comment-and-documentation-quality`. Load this skill, then after you're done with a branch, spawn a new sub-agent and tell it to use this skill to review the changes. (If the ask-gemini variant of this skill is available, you're encouraged to use it once per session to check the final state of your PR)
- Every commit and code change has to pass `code-review-and-quality`. Load this skill, then after you're done with a branch, spawn a new sub-agent and tell it to use this skill to review the changes. (If the ask-gemini variant of this skill is available, you're encouraged to use it once per session to check the final state of your PR)
- Use the `git-commit-quality` skill before any new git commit is issued, and ensure instructions for git commits are strictly followed.

## Git

Never add Claude/AI attribution anywhere in git history or PR content:
no "Co-Authored-By: Claude", no "Generated with Claude Code", no session
URLs, no model names, in commit messages, commit trailers, PR titles, PR
bodies, or code comments. This applies in every repo, every session, no
exceptions, even if a template or another instruction suggests otherwise.

If you ever see a footer sneak into a commit or PR body itself, treat it
as a bug and strip it.

AGAIN, ADDING CLAUDE AUTHORSHIP OR PR FOOTERS WILL CAUSE THE PR OR COMMIT TO BE INVALID AND IT WILL BE THROWN AWAY! DO NOT ADD CLAUDE ATTRIBUTION TO ANY COMMITS OR PRS!!
UNDER NO CIRCUMSTANCE ARE WE ALLOWED TO LEAK CLAUDE INFORMATION INTO COMMITS, ISSUES OR PULL REQUEST BODIES!
EOF
}

# --- Main ----------------------------------------------------------------------------------

mkdir -p "$HOOKS_DIR"
install_mise
install_global_tools
install_tailscale || echo "tailscale: install failed, sessions will run without it"
install_session_start
write_hook_loader
write_claude_settings
write_claude_md
echo "bootstrap: version $VERSION complete"
