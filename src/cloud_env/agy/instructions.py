from __future__ import annotations

import contextlib
from pathlib import Path

from cloud_env.apps.github.prompts import github_identity_context
from cloud_env.apps.github.tokens import GitHubApp
from cloud_env.lib.fs import atomic_write, load_asset
from cloud_env.lib.log import log

GEMINI_BASE_MD = load_asset(__file__, "GEMINI.md")


def render_gemini_md(apps: dict[str, GitHubApp], identity: tuple[str, str] | None) -> str:
    plugin_gh = 'GH_TOKEN=$(jq -r .access_token "$GITHUB_TOKEN_FILE") gh'
    if not apps:
        return GEMINI_BASE_MD + (
            f"- Use `gh` for GitHub. Where its default token can't write, run `{plugin_gh} ...` "
            "(when `$GITHUB_TOKEN_FILE` is set) or use the `dpn` MCP if connected.\n"
        )
    acting, user, co_author, as_me = github_identity_context(apps, identity)
    return (
        GEMINI_BASE_MD
        + acting
        + f"""\
- Commits in those repos are authored as their app automatically (`git config user.name` shows it). {co_author}
- `gh` picks the token of the owner in `--repo`, in a `gh api repos/<owner>/...` path, or of the current directory's `origin` remote. Pass `--repo` when working on a repo outside the current directory. Tokens refresh on their own.
- On repos of any other owner, commits use {user}'s identity; to create issues, PRs, or comments where `gh`'s default token can't write, run `{plugin_gh} ...` (when `$GITHUB_TOKEN_FILE` is set) or use the `dpn` MCP if connected.
- When the user asks to act "as me" on one of those repos, post via `{plugin_gh} ...` (or the `dpn` MCP if connected), and {as_me}.
"""
    )


def write_gemini_md(content: str, path: Path) -> None:
    """Writes ~/.gemini/GEMINI.md atomically when changed, since ~/.gemini is shared across VMs."""
    with contextlib.suppress(OSError):
        if path.read_text() == content:
            return
    # /shared/user/.gemini is virtiofs mapped to nobody:nogroup, so mode stays None to skip chmod
    try:
        atomic_write(path, content)
    except OSError as e:
        log(f"gemini-md: write failed ({e})")
