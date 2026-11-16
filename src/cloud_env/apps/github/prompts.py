from __future__ import annotations

import shlex
from typing import NamedTuple


class GitHubApp(NamedTuple):
    """An owner's GitHub App installation token and the identity its commits use."""

    token: str
    name: str
    email: str


# The proxied GitHub MCP server appends the footer after this hook runs, so these tools are
# denied and posts go through gh
PROXIED_GITHUB_WRITES = tuple(
    f"mcp__github__{name}"
    for name in (
        "add_comment_to_pending_review",
        "add_issue_comment",
        "add_reply_to_pull_request_comment",
        "create_pull_request",
        "issue_write",
        "pull_request_review_write",
        "update_issue_comment",
        "update_pull_request",
    )
)


def github_identity_context(
    apps: dict[str, GitHubApp], identity: tuple[str, str] | None
) -> tuple[str, str, str, str]:
    owners_by_app: dict[str, list[str]] = {}
    for owner, app in sorted(apps.items()):
        owners_by_app.setdefault(app.name, []).append(f"`{owner}`")
    acting = "".join(
        f"- On repos owned by {', '.join(owners)}, `gh` and `git` act as the `{name}` GitHub App.\n"
        for name, owners in owners_by_app.items()
    )
    if identity:
        user = f"{identity[0]} <{identity[1]}>"
        co_author = (
            f"End every commit message in those repos with the trailer `Co-authored-by: {user}`."
        )
        as_me = (
            f"commit with `git -c user.name={shlex.quote(identity[0])} "
            f"-c user.email={shlex.quote(identity[1])} commit ...` and without the trailer"
        )
    else:
        user, co_author, as_me = "the user", "", "commit with the user's identity"
    return acting, user, co_author, as_me
