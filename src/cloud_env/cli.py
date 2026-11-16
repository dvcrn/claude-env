"""Setup and SessionStart bootstrap for Claude Code and Antigravity cloud environments.

`setup` runs once from stub.sh or stub-agy.sh after verifying this file's signature. It installs
mise, the global tools, and a copy of this file for session start. Holds no secrets.

`session-start` runs on every session start and resume. It updates itself to the newest signed
copy, loads secrets from the broker and 1Password into the session environment, and configures
git, GitHub, Tailscale, and the repo's mise tools. Without the broker the session starts without
secrets. Logs to /tmp/session-start.log.

`pre-tool-use` runs from the Claude Code PreToolUse hook. It strips the Claude Code footer from
GitHub MCP tool input, and denies Bash commands and file writes that contain it.

`git-credential` is git's credential helper for github.com. It returns the GitHub App token of
the remote's owner, or the default token.

`gh` runs from the gh shim. It runs the real gh with the GitHub App token of the owner gh acts
on, or the default token.

Both re-mint the GitHub App tokens from the broker when they near expiry.
"""

from __future__ import annotations

import sys

from cloud_env.apps.github.gh import cmd_gh
from cloud_env.apps.github.git import cmd_git_credential
from cloud_env.claude.pre_tool_use import cmd_pre_tool_use
from cloud_env.session import cmd_session_start
from cloud_env.setup import cmd_setup


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["setup"]:
        return cmd_setup()
    if args[:1] == ["session-start"]:
        return cmd_session_start(update="--no-update" not in args)
    if args[:1] == ["pre-tool-use"]:
        return cmd_pre_tool_use()
    if args[:1] == ["git-credential"]:
        return cmd_git_credential(args[1:])
    if args[:1] == ["gh"]:
        return cmd_gh(args[1:])
    sys.exit(
        "usage: bootstrap.py setup | session-start [--no-update] | pre-tool-use | git-credential | gh"
    )


if __name__ == "__main__":
    sys.exit(main())
