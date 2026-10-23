#!/bin/bash
# VERSION=1
# claude-env SessionStart script, run by the hook loader on every session start and resume.
# Fetches credentials from the broker, loads the 1Password Environment into the session, then
# sets the git identity, routes GitHub past the agent proxy, joins Tailscale and installs repo
# tools. Each step fails on its own; without the broker the session starts without secrets.
set -uo pipefail

BROKER_URL=${CLOUD_ENV_BROKER_URL:-https://cloud-env-broker.d.pn}
INGRESS_TOKEN_FILE=${CLAUDE_SESSION_INGRESS_TOKEN_FILE:-/home/claude/.claude/remote/.session_ingress_token}
INIT_MARKER=/dev/shm/.cloud-env-initialized
TAILSCALE_MARKER=/dev/shm/.cloud-env-tailscale
TOOLS_PATH=$HOME/.local/bin:$HOME/.local/share/mise/shims
GITHUB_HOSTS=api.github.com,github.com,codeload.github.com,objects.githubusercontent.com,raw.githubusercontent.com,uploads.github.com

export PATH="$TOOLS_PATH:$PATH"

# Lines of the marked block this script owns in $CLAUDE_ENV_FILE
block=("export PATH=\"$TOOLS_PATH:\$PATH\"")
broker_exports=()
op_token=
op_environment_id=

log() { echo "$(date -u +%FT%TZ) $*"; }

# Turns the broker's {"env": {...}} into shell assignments. The 1Password values become plain
# shell variables so they reach op only; everything else is queued for the env block.
read -r -d '' PARSE_BROKER_ENV << 'PY'
import json, re, shlex, sys

env = json.load(sys.stdin)["env"]
private = {"OP_SERVICE_ACCOUNT_TOKEN": "op_token", "OP_ENVIRONMENT_ID": "op_environment_id"}
for name, value in env.items():
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or not isinstance(value, str):
        sys.exit(f"invalid entry {name!r}")
    if name in private:
        print(f"{private[name]}={shlex.quote(value)}")
    else:
        print(f"broker_exports+=({shlex.quote(f'export {name}={shlex.quote(value)}')})")
PY

# Prints <kid>.<sha256 of header.payload>.<signature> for the session ingress token. The
# payload itself never leaves the container, so the token can't be rebuilt from this.
ingress_sig_sample() {
  local token header payload signature kid digest
  token=$(cat "$INGRESS_TOKEN_FILE" 2> /dev/null) || return 1
  IFS=. read -r header payload signature <<< "${token#sk-ant-si-}"
  [ -n "$header" ] && [ -n "$payload" ] && [ -n "$signature" ] || return 1
  kid=$(python3 -I -c 'import base64, json, sys
h = sys.argv[1]
print(json.loads(base64.urlsafe_b64decode(h + "=" * (-len(h) % 4)))["kid"])' "$header" 2> /dev/null) || return 1
  digest=$(printf '%s.%s' "$header" "$payload" | sha256sum | cut -d' ' -f1)
  printf '%s.%s.%s' "$kid" "$digest" "$signature"
}

fetch_credentials() {
  local headers=(-H "X-Session-Id: ${CLAUDE_CODE_REMOTE_SESSION_ID:-}") sample body parsed
  if sample=$(ingress_sig_sample); then
    headers+=(-H "X-Ingress-Sig-Sample: $sample")
  fi
  if ! body=$(curl -fsS --proto =https --max-time 20 "${headers[@]}" "$BROKER_URL/v1/session-env"); then
    log "broker: $BROKER_URL unreachable or refused the request, continuing without secrets"
    return 1
  fi
  if ! parsed=$(python3 -I -c "$PARSE_BROKER_ENV" <<< "$body"); then
    log "broker: unexpected response, continuing without secrets"
    return 1
  fi
  eval "$parsed"
  log "broker: received ${#broker_exports[@]} variables plus 1Password access"
}

# Loads the 1Password Environment. op runs in an empty environment apart from the proxy
# settings it needs, so everything `export -p` reports came from 1Password.
load_1password() {
  local op_bin exports
  if [ -z "$op_token" ] || [ -z "$op_environment_id" ]; then
    log "1password: broker sent no token or environment ID, skipping"
    return 1
  fi
  op_bin=$(mise which op) || { log "1password: op is not installed"; return 1; }  # the real binary; the mise shim would add its own variables
  # --no-masking: op otherwise replaces secret values on stdout with a placeholder
  if ! exports=$(env -i HOME="$HOME" OP_SERVICE_ACCOUNT_TOKEN="$op_token" \
    HTTPS_PROXY="${HTTPS_PROXY:-}" https_proxy="${https_proxy:-}" \
    NO_PROXY="${NO_PROXY:-}" no_proxy="${no_proxy:-}" SSL_CERT_FILE="${SSL_CERT_FILE:-}" \
    "$op_bin" run --no-masking --environment "$op_environment_id" -- /bin/bash -c 'export -p' \
    | grep -Ev '^declare -x (HOME|PATH|PWD|OLDPWD|SHLVL|_|OP_SERVICE_ACCOUNT_TOKEN|HTTPS_PROXY|https_proxy|NO_PROXY|no_proxy|SSL_CERT_FILE)(=|$)' \
    | sed 's/^declare -x /export /'); then
    log "1password: op run failed"
    return 1
  fi
  eval "$exports"
  # One block line per variable, so single variables can be dropped later
  local names name
  names=$(sed -n 's/^export \([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' <<< "$exports")
  for name in $names; do
    block+=("export $name=$(printf '%q' "${!name-}")")
  done
  log "1password: loaded ${names//$'\n'/ }"
}

# Broker values follow the 1Password ones in the block, so a broker-minted token wins
add_broker_exports() {
  [ "${#broker_exports[@]}" -gt 0 ] || return 0
  local line
  for line in "${broker_exports[@]}"; do
    eval "$line"
    block+=("$line")
  done
}

# The harness writes user.name=Claude into ~/.gitconfig. GIT_AUTHOR_* only reach the Bash tool,
# so the identity from 1Password goes into git config too for anything else that commits.
set_git_identity() {
  if [ -z "${GIT_AUTHOR_NAME:-}" ] || [ -z "${GIT_AUTHOR_EMAIL:-}" ]; then
    log "git: no GIT_AUTHOR_NAME/GIT_AUTHOR_EMAIL, keeping the existing identity"
    return 0
  fi
  git config --global user.name "$GIT_AUTHOR_NAME"
  git config --global user.email "$GIT_AUTHOR_EMAIL"
}

# The agent proxy swaps GitHub credentials for a placeholder and only allows the session's repos.
# Sending GitHub traffic direct with the real GITHUB_TOKEN lets gh, git and mise reach any repo.
use_github_directly() {
  if [ -z "${GITHUB_TOKEN:-}" ]; then
    log "github: no GITHUB_TOKEN, leaving GitHub traffic on the agent proxy"
    return 0
  fi
  local exports="export NO_PROXY=\"\$NO_PROXY,$GITHUB_HOSTS\" no_proxy=\"\$no_proxy,$GITHUB_HOSTS\" GH_TOKEN=\"\$GITHUB_TOKEN\""
  eval "$exports"  # for this script's own mise install
  block+=("$exports")
  # The empty entry drops helpers configured earlier; ours reads the token at run time, so it never hits disk
  git config --global --unset-all credential.https://github.com.helper || true
  git config --global --add credential.https://github.com.helper ''
  # shellcheck disable=SC2016 # expanded by git at run time
  git config --global --add credential.https://github.com.helper \
    '!f() { echo username=x-access-token; echo "password=$GITHUB_TOKEN"; }; f'
}

# Replaces this script's marked block in $CLAUDE_ENV_FILE, keeping everything else in the file
write_env_block() {
  if [ -z "${CLAUDE_ENV_FILE:-}" ]; then
    log "env: CLAUDE_ENV_FILE is not set, the session won't see the loaded variables"
    return 1
  fi
  local rest
  rest=$(awk '/^# >>> cloud-env$/ { skip = 1 } !skip { print } /^# <<< cloud-env$/ { skip = 0 }' \
    "$CLAUDE_ENV_FILE" 2> /dev/null)
  touch "$CLAUDE_ENV_FILE" && chmod 600 "$CLAUDE_ENV_FILE"
  {
    [ -n "$rest" ] && printf '%s\n' "$rest"
    echo '# >>> cloud-env'
    printf '%s\n' "${block[@]}"
    echo '# <<< cloud-env'
  } > "$CLAUDE_ENV_FILE"
}

# tailscaled inherits HTTPS_PROXY from this script, which is what lets it reach the control plane.
# In-memory state makes the node ephemeral; it is named after the session.
join_tailscale() {
  if ! command -v tailscaled > /dev/null; then
    log "not installed, skipping"
    return 0
  fi
  if [ -z "${TS_AUTHKEY:-}" ]; then
    log "no TS_AUTHKEY, skipping"
    return 0
  fi
  if ! pgrep -x tailscaled > /dev/null; then
    setsid nohup tailscaled --state=mem: --no-logs-no-support >> /tmp/tailscaled.log 2>&1 < /dev/null &
    for _ in $(seq 1 20); do
      [ -S /var/run/tailscale/tailscaled.sock ] && break
      sleep 0.5
    done
  fi
  local name
  name="claude-$(echo "${CLAUDE_CODE_REMOTE_SESSION_ID#cse_}" | tr '[:upper:]_' '[:lower:]-')"
  tailscale up --auth-key="$TS_AUTHKEY" --hostname="$name" --timeout=60s || return 1
  tailscale status --peers=false
  touch "$TAILSCALE_MARKER"

  local kept=() line
  for line in "${block[@]}"; do
    [[ $line == "export TS_AUTHKEY="* ]] || kept+=("$line")
  done
  block=("${kept[@]}")
  write_env_block && log "removed TS_AUTHKEY from the env block"
}

install_repo_tools() {
  [ -n "${CLAUDE_PROJECT_DIR:-}" ] && [ -f "$CLAUDE_PROJECT_DIR/mise.toml" ] || return 0
  cd "$CLAUDE_PROJECT_DIR" && mise trust && mise install
}

# --- Main ----------------------------------------------------------------------------------

log "== session-start"

# A resumed VM keeps /dev/shm, its processes and the session env, so only Tailscale is checked
if [ -e "$INIT_MARKER" ]; then
  if [ ! -e "$TAILSCALE_MARKER" ] || pgrep -x tailscaled > /dev/null; then
    log "resume: state intact, nothing to do"
    exit 0
  fi
  log "resume: tailscaled is gone, running the full start to rejoin"
fi

if fetch_credentials && load_1password; then
  touch "$INIT_MARKER"
fi
add_broker_exports
set_git_identity
use_github_directly
write_env_block

{ join_tailscale || log "failed"; } 2>&1 | sed -u 's/^/[tailscale] /' &
{ install_repo_tools || log "repo install failed"; } 2>&1 | sed -u 's/^/[mise] /' &
wait

log "== session-start done"
exit 0
