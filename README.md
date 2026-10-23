# claude-env

Signed bootstrap scripts for Claude Code cloud environments. Every environment runs the same
setup stub, which verifies and runs `bootstrap.sh` from this repo, so no environment's setup
script holds secrets or needs editing when the scripts change.

| File | Runs | Does |
|---|---|---|
| `stub.sh` | as the environment's setup script | Verifies `bootstrap.sh` against `SIGNER` and runs it |
| `bootstrap.sh` | at setup | Installs mise, the tools in `tools/`, Tailscale and the SessionStart hook loader. No secrets |
| hook loader | on every session start | Runs the latest signed `session-start.sh`, or the last verified copy |
| `session-start.sh` | from the loader | Loads secrets from `https://cloud-env-broker.d.pn` and 1Password, and configures git, GitHub, Tailscale and repo tools. Logs to `/tmp/session-start.log` |

If the broker can't be reached, the session starts without secrets and the log says so.

## Signing

Both scripts are signed with an SSH Ed25519 key and carry `# VERSION=<n>` on line 2. After changing either script:

1. Bump its `# VERSION=` if old signed copies should no longer run.
2. `mise run sign` signs both with `CLAUDE_ENV_SIGNING_KEY` from fnox, writing the `.sig` files.
3. `mise run check` lints the scripts, checks the `tools/` pins, and verifies the signatures
   against `signing_key.pub`.
4. Commit the scripts and their `.sig` files together.

After changing `tools/mise.toml`, regenerate the lockfile with
`mise lock --platform linux-x64,linux-arm64` in `tools/`, then update `TOOLS_TOML_SHA256` and
`TOOLS_LOCK_SHA256` in `bootstrap.sh` to the values `mise run check` prints.

## Using the stub

1. Paste `stub.sh` as the environment's setup script. Its `SIGNER` is the key in `signing_key.pub`.
2. Give the environment a network secret for `cloud-env-broker.d.pn` carrying its broker
   credentials.

Raise `MIN_VERSION` in the stub to stop older signed copies of `bootstrap.sh` from running.
