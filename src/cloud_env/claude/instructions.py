from __future__ import annotations

from cloud_env.apps.github.prompts import github_identity_context
from cloud_env.apps.github.tokens import GitHubApp
from cloud_env.lib.fs import load_asset

CLAUDE_MD = load_asset(__file__, "CLAUDE.md")


def render_claude_md(apps: dict[str, GitHubApp], identity: tuple[str, str] | None) -> str:
    if not apps:
        return CLAUDE_MD + (
            "- Use `gh` for GitHub. To create issues, PRs or comments where its token can't "
            "write, use the `github` tools of the `dpn` MCP.\n"
        )
    acting, user, co_author, as_me = github_identity_context(apps, identity)
    return (
        CLAUDE_MD
        + acting
        + f"""\
- Commits in those repos are authored as their app automatically (`git config user.name` shows it). {co_author}
- `gh` picks the token of the owner in `--repo`, in a `gh api repos/<owner>/...` path, or of the current directory's `origin` remote. Pass `--repo` when working on a repo outside the current directory. Tokens refresh on their own.
- On repos of any other owner, create issues, PRs and comments with the `github` tools of the `dpn` MCP, which act as {user}. Commits there use {user}'s identity.
- When the user asks to act "as me" on one of those repos, post through the `dpn` MCP too, and {as_me}.
"""
    )
