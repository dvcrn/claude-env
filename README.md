# claude-env

Signed bootstrap scripts for Claude Code cloud environments. Every environment runs the same
setup stub, which verifies and runs `bootstrap.sh` from this repo, so no environment's setup
script holds secrets or needs editing when the scripts change.

| File | Runs | Does |
|---|---|---|
| `stub.sh` | as the environment's setup script | Verifies `bootstrap.sh` against `SIGNER` and runs it |
| `bootstrap.sh` | at setup (snapshotted for up to 7 days) | Installs mise, the tools pinned in `tools/`, Tailscale and the SessionStart hook loader. No secrets |
| hook loader | on every session start | Fetches and verifies the current `session-start.sh`, falling back to the copy verified at setup. Refuses versions below the highest it has run |
| `session-start.sh` | from the loader | Fetches credentials from `https://cloud-env-broker.d.pn`, loads the 1Password Environment into the session, sets the git identity, routes GitHub past the agent proxy, joins Tailscale and runs `mise install` for the repo. Logs to `/tmp/session-start.log` |

If the broker can't be reached, the session starts without secrets and the log says so.

## Signing

Both scripts are signed with an SSH Ed25519 key, and each carries `# VERSION=<n>` on line 2 so
the version is covered by the signature. After changing either script:

1. Bump its `# VERSION=` if old signed copies should no longer run.
2. `mise run sign` signs both with `~/.ssh/id_ed25519`, writing the `.sig` files.
3. `mise run check` lints the scripts and confirms the pins and signatures match. Set
   `SIGNER_PUBKEY` if the public key isn't `~/.ssh/id_ed25519.pub`.
4. Commit the scripts and their `.sig` files together.

After changing `tools/mise.toml`, regenerate the lockfile with
`mise lock --platform linux-x64,linux-arm64` in `tools/`, then update `TOOLS_TOML_SHA256` and
`TOOLS_LOCK_SHA256` in `bootstrap.sh` to the values `mise run check` prints.

## Using the stub

1. Replace `SIGNER` in `stub.sh` with the signing public key, e.g. the contents of
   `~/.ssh/id_ed25519.pub` (the comment part is ignored).
2. Paste `stub.sh` as the environment's setup script.
3. Give the environment a network secret for `cloud-env-broker.d.pn` carrying its broker
   credentials.

Raise `MIN_VERSION` in the stub to stop older signed copies of `bootstrap.sh` from running.
