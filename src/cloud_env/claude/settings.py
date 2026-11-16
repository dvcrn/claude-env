from __future__ import annotations

import json
import sys
from pathlib import Path

from cloud_env.apps.github.prompts import PROXIED_GITHUB_WRITES
from cloud_env.apps.linear import DENIED_LINEAR_MCP
from cloud_env.lib.log import hook_command

PRE_TOOL_USE_MATCHER = r"^(Bash|Write|Edit|MultiEdit|NotebookEdit|mcp__.*[Gg]it[Hh]ub.*)$"

ALLOWED_COMMANDS = (
    "Bash(op:*)",
    "Bash(jq:*)",
    "Bash(gh pr view:*)",
    "Bash(gh pr diff:*)",
    "Bash(gh pr checks:*)",
    "Bash(gh pr comment:*)",
    "mcp__claude-code-remote__*",
)
# The default External System Writes rule excludes thread replies and resolves, so
# addressing review feedback through gh api gets blocked in auto mode
AUTO_MODE_ALLOW = (
    "PR Review Thread Work: When the user's own message asked the agent to work on, babysit, "
    "or address review feedback on a specific PR in the repository this session was started "
    "for, these writes on that PR are allowed: replying to its review comments or threads "
    "(`gh api` POST `repos/{o}/{r}/pulls/{n}/comments/{id}/replies`, GraphQL "
    "`addPullRequestReviewThreadReply`), resolving or unresolving its review threads (GraphQL "
    "`resolveReviewThread`/`unresolveReviewThread`), posting PR comments, editing comments the "
    "agent posted in this session, and re-requesting review from existing reviewers. Does not "
    "cover merging, closing, approving or requesting changes, label or base changes, or writes "
    "to any other PR or repository."
)


def set_hook(hooks: dict, event: str, group: dict, script: Path) -> None:
    """Replaces this script's hook group for event with group."""
    target = str(script)
    groups = hooks.setdefault(event, [])
    groups[:] = [
        existing
        for existing in groups
        if not any(target in hook.get("command", "") for hook in existing.get("hooks", []))
    ]
    groups.append(group)


# Merges into the existing settings so keys set by the image survive
def write_claude_settings(claude_dir: Path, script: Path) -> None:
    path = claude_dir / "settings.json"
    try:
        settings = json.loads(path.read_text())
    except FileNotFoundError:
        settings = {}
    except ValueError:
        print(f"bootstrap: {path} is not valid JSON, replacing it", file=sys.stderr)
        settings = {}

    settings["attribution"] = {"commit": "", "pr": "", "sessionUrl": False}
    permissions = settings.setdefault("permissions", {})
    allow = permissions.setdefault("allow", [])
    allow += [rule for rule in ALLOWED_COMMANDS if rule not in allow]
    deny = permissions.setdefault("deny", [])
    deny += [rule for rule in (*PROXIED_GITHUB_WRITES, *DENIED_LINEAR_MCP) if rule not in deny]

    auto_allow = settings.setdefault("autoMode", {}).setdefault("allow", [])
    auto_allow += [rule for rule in ("$defaults", AUTO_MODE_ALLOW) if rule not in auto_allow]

    hooks = settings.setdefault("hooks", {})
    session_start = {
        "type": "command",
        "command": hook_command("session-start", script),
        "timeout": 600,
    }
    set_hook(hooks, "SessionStart", {"hooks": [session_start]}, script)
    pre_tool_use = {
        "type": "command",
        "command": hook_command("pre-tool-use", script),
        "timeout": 10,
    }
    set_hook(
        hooks,
        "PreToolUse",
        {"matcher": PRE_TOOL_USE_MATCHER, "hooks": [pre_tool_use]},
        script,
    )

    path.write_text(json.dumps(settings, indent=2) + "\n")
