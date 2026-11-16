# Antigravity Cloud Environment Learnings

Reference notes on how Antigravity's remote Linux VM differs from Claude Code cloud environments, and why `bootstrap.py` / `stub-agy.sh` handle Antigravity the way they do.

## 1. Architecture: Orchestrator Outside the VM

Unlike Claude Code Cloud (where the `claude` CLI process runs inside the container alongside the repo), the Antigravity agent loop runs **outside** the VM on Google's backend and talks to the VM over an internal socket.

Inside the VM, only two platform components run:

1. **Shell & file daemon (`/app/server.py` on `/tmp/shell.sock`):** FastAPI service exposing `/execute_bash`, `/execute_bash_stream`, `/processes/*`, and file RPCs (`/save_file`, `/fetch_file`, `/stat`, `/listdir`, `/find_files`).
2. **Sandbox & credential proxy (`/usr/local/bin/agy-plugin-proxy`):** Wraps every `run_command` call inside `exebox` as `/bin/bash -O expand_aliases -c <cmd>` and proxies outbound HTTP/HTTPS on `127.0.0.1:<port>`.

| Concept | Claude Code Cloud | Antigravity Cloud |
| :--- | :--- | :--- |
| **Where agent loop runs** | Inside container (`claude` CLI) | Outside VM (calls `/app/server.py` RPCs) |
| **Persistent dir across VMs** | None (7-day setup snapshot only) | `~/.gemini/` (`/shared/user/.gemini` via `virtiofs`) |
| **Setup script** | Web UI paste (`stub.sh`) | `/home/antigravity/env-setup.sh` + `<repo>/.agents/environment.json` |
| **Broker auth** | Platform network secret injected outside VM | `~/.gemini/.broker/token` (`CF-Access-Client-Id` / `Secret`) |
| **Session env injection** | `$CLAUDE_ENV_FILE` from `SessionStart` hook | `BASH_ENV=~/.bashrc` sourcing `/dev/shm/cloud-env-session.sh` |
| **Global instructions** | `~/.claude/CLAUDE.md` | `~/.gemini/GEMINI.md` + repo `AGENTS.md` / `.agents/rules/*.md` |
| **Skills** | `~/.claude/skills/...` | `~/.gemini/config/skills/<name>/SKILL.md` (`save_skill`) + `<repo>/.agents/skills/` |
| **Tool hooks / footers** | `PreToolUse` in `~/.claude/settings.json` | No JSON tool hooks; no AI attribution footers added by default |
| **MCP servers** | Local `stdio` or remote MCP in container | Remote HTTP/SSE MCPs only (in Web UI **Customizations**); use CLIs + `SKILL.md` in VM |

## 2. Filesystem, Sandbox, and Persistence

- **Read-only `/` and unprivileged `exebox`:** Commands run as non-root `antigravity` (`HOME=/home/antigravity`) with no passwordless `sudo` or `apt-get`. `/` is read-only (`EROFS` on `/etc/profile.d`, `/var/run`, etc.); only `/home/antigravity` and `/tmp` (plus `/dev/shm`) are writable.
- **Cross-VM persistence (`~/.gemini`):** `/home/antigravity/.gemini` symlinks to `/shared/user/.gemini` (`virtiofs`), which persists across **all** conversations and VMs for the user. Files there map to `nobody:nogroup`, so `chmod()` raises `EPERM` even though writes and `os.replace` succeed. Store long-lived bootstrap inputs like `~/.gemini/.broker/token` and `~/.gemini/GEMINI.md` there (using unique temp filenames when replacing), and keep per-VM locks and markers on local disk (`~/.local/share/cloud-env/`, `/dev/shm/`) since multiple VMs can mount `~/.gemini` concurrently.
- **Idle stop/wake lifecycle:** When an idle VM stops and later wakes up, `/` and `/dev/shm` reset while `/home/antigravity` persists. Tools in `~/.local/` stay installed, while `/dev/shm/cloud-env-*` state disappears and re-initializes on wakeup.
- **Workspace layout:** Attached GitHub repos clone to `/home/antigravity/<owner>/<repo>`. Zero, one, or multiple repos can be attached at once (`PWD` defaults to `/home/antigravity` when 0 or 2+ repos are attached), so `bootstrap.py` discovers repo `mise` configs via `HOME.glob("*/*/.git")` rather than a single `$CLAUDE_PROJECT_DIR`.
- **Base image packages (Debian 12):**
  - `/usr/bin/python3` is **Python 3.11.2**, which predates `TarFile.extract(..., filter="data")` (added in 3.11.4 / 3.12). Use `tar.extractfile(...)` in stdlib scripts.
  - `/usr/bin/ssh-keygen` and `/usr/bin/git` (2.39.5, supporting `includeIf.hasconfig:remote.*.url:`) are preinstalled.

## 3. Startup & `BASH_ENV` Shell Lifecycle

- **Startup scripts:** On new/woken VMs, `/opt/antigravity/bin/run-repo-env.sh` runs `<repo>/.agents/environment.json` (`{"install": "..."}`) in each attached repo first, and `/home/antigravity/env-setup.sh` runs on top. Writing `/home/antigravity/env-setup.sh` during a session causes the platform to save it to the project's configuration automatically when the turn ends.
- **`BASH_ENV=~/.bashrc` on every command:** `/app/server.py` sets `BASH_ENV=~/.bashrc` and invokes `/bin/bash -O expand_aliases -c <cmd>` on every `run_command`.
  - **Strip non-interactive early returns:** Debian's default `case $- in *i*) ;; *) return;; esac` guard in `~/.bashrc` exits non-interactive `BASH_ENV` shells early if left above custom exports.
  - **Guard subshells with `_CLOUD_ENV_LOADED=1`:** Because `BASH_ENV` is inherited by child processes, any nested `bash -c` (e.g., `mise` tasks with `shell = "bash -c"`, Makefiles, wrapper scripts) re-sources `~/.bashrc`. Re-sourcing `/dev/shm/cloud-env-session.sh` unconditionally would overwrite per-command overrides (`FNOX_PROFILE=default mise run ...`) and re-run `unset GH_TOKEN GITHUB_TOKEN`. Exporting `_CLOUD_ENV_LOADED=1` before sourcing ensures subshells keep their inherited env and caller overrides intact.
  - **Prevent recursion & retry storms:** `bootstrap.py session-start` pops `BASH_ENV` from `os.environ` before spawning child processes, serializes concurrent shells with `flock` on `/dev/shm/.cloud-env-session.lock`, and touches `/dev/shm/.cloud-env-attempted` in `finally` so a broker outage never stalls every subsequent command.

## 4. Network Proxy, GitHub App Routing, and Tailscale

- **Stay on `HTTPS_PROXY`:** Outbound traffic must route through `HTTP_PROXY` / `HTTPS_PROXY` (`agy-plugin-proxy` on `127.0.0.1:<port>`). The proxy's local CA is installed in `/etc/ssl/certs/ca-certificates.crt`, so `curl`, `git`, `op`, and `python3 -I` (`urllib.request`) trust it out of the box. Direct egress bypassing the proxy is blocked: **do not** add `github.com` or `api.github.com` to `NO_PROXY`.
- **GitHub token pass-through:** `agy-plugin-proxy` only swaps credentials when it sees its own `agy-h-*` stand-in handles (`GH_TOKEN` / `GITHUB_TOKEN_FILE`), which are restricted to repos attached to the project. Real `ghs_...` GitHub App installation tokens pass straight through the proxy untouched, unlocking access across all repos installed on the app.
- **Shadow `/opt/antigravity/bin/gh`:** `/opt/antigravity/bin/gh` is a shell launcher that reads `GITHUB_TOKEN_FILE` and rewrites `credential.https://github.com.helper` in `~/.gitconfig` whenever `TOKEN_FROM_FILE=1`. Prepending `~/.local/bin:~/.local/share/mise/shims` ahead of `/opt/antigravity/bin` lets `~/.local/bin/gh` (`GH_SHIM`) exec `mise`'s upstream `gh` binary directly so `~/.gitconfig` is never overwritten.
- **Tailscale cannot join inside `exebox`:** First, the sandbox network namespace exposes only `lo` (`v4=false, v6=false`), causing `tailscaled`'s network monitor to pause the control client (`setPaused(true)`). Second, even when overriding netmon (`TS_ASSUME_NETWORK_UP_FOR_TEST=true` + `--tun=userspace-networking`), `agy-plugin-proxy` is a TLS-terminating HTTP proxy rather than a raw TCP `CONNECT` tunnel, so `/machine/register` fails with `502 Bad Gateway` when `tailscaled` upgrades to its `ts2021` Noise stream. `bootstrap.py` skips Tailscale on Antigravity.

## 5. Instructions, Skills, and MCP Servers

- **Instructions (`CLAUDE.md` equivalents):** The external orchestrator injects `<repo>/AGENTS.md`, `<repo>/.agents/rules/*.md`, and global `~/.gemini/GEMINI.md` into `<user_rules>`. `bootstrap.py` renders `~/.gemini/GEMINI.md` atomically (only rewriting when changed, since `~/.gemini` is shared across VMs) with quality, PR, and per-owner GitHub App identity/`Co-authored-by` rules. Antigravity does not append AI attribution footers to commits or PRs, so no `PreToolUse` footer-stripping hook is needed.
- **MCP servers vs. CLIs + `SKILL.md`:**
  - **Local `stdio` MCP servers are not wired up:** `/app/server.py` has no MCP `stdio` bridge into the container, so `npx`/`uvx` MCP servers inside the VM cannot register as native agent tools. Even Antigravity's built-in plugins (`github`, `gdrive`, `gdocs`, `gcalendar`, `gsheets`, `gslides` under `/opt/antigravity/plugins/`) are implemented as CLI scripts + `SKILL.md` instructions rather than MCP servers.
  - **Remote HTTP/SSE MCP servers** can be connected in the Antigravity Web UI (**Customizations → MCP Servers**) and are called directly by the external orchestrator outside the VM.
  - **Preferred VM pattern:** Install CLIs (`gh`, `linear`, `op`, `fnox`) via `src/cloud_env/apps/mise/global_tools.toml`, authenticate them via `/dev/shm/cloud-env-session.sh`, and pair them with repo skills (`.agents/skills/<name>/SKILL.md`) or persistent global skills (`~/.gemini/config/skills/<name>/SKILL.md` / `save_skill`).
