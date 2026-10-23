#!/bin/bash
# VERSION=1
# Setup-time bootstrap for Claude Code cloud environments, run by stub.sh once its signature
# checks out. Installs tools and the SessionStart hook loader. Holds no secrets and makes no
# broker calls; everything secret is loaded by session-start.sh when a session starts.
set -euo pipefail

# --- Configuration -------------------------------------------------------------------------

BASE=https://raw.githubusercontent.com/dvcrn/claude-env/main

MISE_VERSION=2026.10.17
TAILSCALE_VERSION=1.104.2

# tools/ is not signed, so this signed script pins its contents (`mise run check` compares them)
TOOLS_TOML_SHA256=56315206bade456490481f45baf3800a49f3f5ef5adfd25cd85e087148816784
TOOLS_LOCK_SHA256=2eb138594319a7debef8e2db4131363f22ffe4800cf843c864bb518ff4614f33

HOOKS_DIR=$HOME/.claude/hooks
CLAUDE_ENV_DIR=$HOOKS_DIR/claude-env
TOOLS_PATH=$HOME/.local/bin:$HOME/.local/share/mise/shims

export PATH="$TOOLS_PATH:$PATH"

VERSION=$(sed -n '2s/^# VERSION=//p' "${BASH_SOURCE[0]}")
: "${MIN_VERSION:?must be set by stub.sh}"
: "${SIGNER:?must be set by stub.sh}"
if [ "$VERSION" -lt "$MIN_VERSION" ]; then
  echo "bootstrap: version $VERSION is below MIN_VERSION $MIN_VERSION, refusing to run" >&2
  exit 1
fi

case "$(uname -m)" in
  x86_64)
    MISE_ARCH=x64 MISE_SHA256=17565e268c5542d8ce944321e17af81e0b741b65af4aa7e8e010ee77376a4aa3
    TAILSCALE_ARCH=amd64 TAILSCALE_SHA256=512f613ed7d04fc7282f7dc5bd76aa105aae28168fbb9357eb749ebcbc29f0ae
    ;;
  aarch64)
    MISE_ARCH=arm64 MISE_SHA256=cac510e51793ad45e65fe92305bdbe4309c9c60b5907badbc7eba111738ab08f
    TAILSCALE_ARCH=arm64 TAILSCALE_SHA256=5776aaa901b5ef1916e66be5bb54b9f481cd399c9c28db123a8cab620efa237b
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

# Installs tools/mise.toml as the global config. MISE_LOCKED makes mise take every download
# URL and checksum from the lockfile, so no GitHub API calls (or token) are needed.
install_global_tools() {
  local config_dir="$HOME/.config/mise"
  mkdir -p "$config_dir"
  fetch_checked "$BASE/tools/mise.toml" "$WORK/mise.toml" "$TOOLS_TOML_SHA256"
  fetch_checked "$BASE/tools/mise.lock" "$WORK/mise.lock" "$TOOLS_LOCK_SHA256"
  install -m 0644 "$WORK/mise.toml" "$config_dir/config.toml"
  install -m 0644 "$WORK/mise.lock" "$config_dir/mise.lock"
  (cd "$HOME" && MISE_LOCKED=1 mise install)
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
  recorded=$(cat "$HOOKS_DIR/.version" 2> /dev/null || echo 0)
  [ "$version" -gt "$recorded" ] && echo "$version" > "$HOOKS_DIR/.version"
  return 0
}

write_hook_loader() {
  cat > "$HOOKS_DIR/session-start.sh" << 'LOADER'
#!/bin/bash
# SessionStart hook loader installed by claude-env's bootstrap.sh. Runs the newest signed
# session-start.sh, or the copy verified at setup time if that can't be fetched or verified.
# Refuses any version below the highest one run on this machine. Logs to /tmp/session-start.log.
set -uo pipefail
exec >> /tmp/session-start.log 2>&1

BASE=https://raw.githubusercontent.com/dvcrn/claude-env/main
CLAUDE_ENV_DIR=$HOME/.claude/hooks/claude-env
VERSION_FILE=$HOME/.claude/hooks/.version

log() { echo "$(date -u +%FT%TZ) loader: $*"; }

# Verifies the script at $1 against the signature at $2, ratchets the recorded version and
# runs it. Returns 1 without running anything if any check fails.
run_verified() {
  local script=$1 sig=$2 label=$3 version recorded
  if ! ssh-keygen -Y verify -f "$CLAUDE_ENV_DIR/allowed_signers" -I claude-env \
    -n claude-env-session-start -s "$sig" < "$script" > /dev/null; then
    log "$label: signature check failed"
    return 1
  fi
  version=$(sed -n '2s/^# VERSION=\([0-9][0-9]*\)$/\1/p' "$script")
  recorded=$(cat "$VERSION_FILE" 2> /dev/null || echo 0)
  if [ -z "$version" ] || [ "$version" -lt "$recorded" ]; then
    log "$label: version ${version:-missing} is below $recorded, refusing it"
    return 1
  fi
  [ "$version" -gt "$recorded" ] && echo "$version" > "$VERSION_FILE"
  log "running $label session-start.sh version $version"
  bash "$script"
  log "session-start.sh exited with $?"
}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

if curl -fsSL --max-time 20 "$BASE/session-start.sh" -o "$work/session-start.sh" &&
  curl -fsSL --max-time 20 "$BASE/session-start.sh.sig" -o "$work/session-start.sh.sig"; then
  run_verified "$work/session-start.sh" "$work/session-start.sh.sig" fetched && exit 0
else
  log "fetching session-start.sh failed"
fi
run_verified "$CLAUDE_ENV_DIR/session-start.sh" "$CLAUDE_ENV_DIR/session-start.sh.sig" installed ||
  log "no runnable session-start.sh, session continues unconfigured"
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
