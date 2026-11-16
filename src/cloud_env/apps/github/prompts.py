from __future__ import annotations

import shlex

from cloud_env.apps.github.tokens import GitHubApp


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
