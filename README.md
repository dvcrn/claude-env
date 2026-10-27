# claude-env

A signed bootstrap for Claude Code cloud environments. Every environment runs the same setup
stub, which verifies and runs `bootstrap.py` from this repo, so no environment's setup script
holds secrets or needs editing when the bootstrap changes.

| File                         | Runs                                         | Does                                                                                                                                                                                             |
| ---------------------------- | -------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `stub.sh`                    | as the environment's setup script            | Verifies `bootstrap.py` against `SIGNER` and runs `bootstrap.py setup`                                                                                                                           |
| `bootstrap.py setup`         | once, at setup                               | Installs mise and the tools in `GLOBAL_TOOLS` (op, Tailscale, gh, ...), installs itself as the SessionStart hook, and writes `~/.claude/settings.json` and `~/.claude/CLAUDE.md`. No secrets     |
| `bootstrap.py session-start` | on every session start and resume            | Updates itself to the newest signed copy, loads secrets from `https://cloud-env-broker.d.pn` and 1Password into the session env, and configures git, GitHub, Tailscale and the repo's mise tools |
| `bootstrap.py pre-tool-use`  | before GitHub MCP, Bash and file-write calls | Strips the Claude Code footer from GitHub MCP input and denies Bash commands and file writes that contain it. Setup denies the proxied `mcp__github__` write tools, so posts go through `gh`     |

`bootstrap.py` is a single Python script using only the standard library.

## Setting up an environment

1. In the Claude Code environment settings, paste [`stub.sh`](stub.sh) as the setup script. Its
   `SIGNER` is the key in `signing_key.pub`.
2. Give the environment a network secret for `cloud-env-broker.d.pn` carrying its broker
   credentials.

Each session then gets, from the broker and the 1Password Environment it points to:

- the 1Password variables and broker variables in the session env (`TS_AUTHKEY` excepted, it
  only goes to `tailscale up`)
- git author identity from `GIT_AUTHOR_NAME` / `GIT_AUTHOR_EMAIL`
- direct GitHub access with `GITHUB_TOKEN`, bypassing the agent proxy so any repo is reachable
- a Tailscale node named `claude-<session id>` if `TS_AUTHKEY` is set
- the repo's tools if it has a `mise.toml`

If the broker can't be reached, the session starts without secrets.

## Debugging a session

- `/tmp/session-start.log` has the hook's output, including mise and Tailscale.
- `/tmp/tailscaled.log` has the Tailscale daemon's output.
- The installed hook lives in `~/.claude/hooks/claude-env/`. Run it by hand with
  `python3 -I ~/.claude/hooks/claude-env/bootstrap.py session-start --no-update`.
- A resumed session skips everything unless Tailscale went down. Delete
  `/dev/shm/.cloud-env-initialized` to force a full run.

## Changing the bootstrap

1. Edit `bootstrap.py`. Tool versions live in `GLOBAL_TOOLS`, mise's in `MISE_VERSION`.
2. Raise `VERSION` if older signed copies should no longer run. Installed copies only update
   to an equal or higher `VERSION`, so a lower one is never picked up.
3. `mise run sign` signs `bootstrap.py` with `CLAUDE_ENV_SIGNING_KEY` from fnox and runs
   `mise run check`, which lints and verifies `bootstrap.py.sig` against `signing_key.pub`.
4. Commit `bootstrap.py` and `bootstrap.py.sig` together. `mise run precommit` verifies the
   staged pair; `mise run install-hooks` installs it as the git pre-commit hook. CI runs
   `mise run check` on every push and pull request.

Running sessions pick up the new copy at their next session start. New environments get it at
setup. Raise `MIN_VERSION` in the stub to stop older signed copies from being installed at
setup.
