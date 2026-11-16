from __future__ import annotations

import os

from cloud_env.apps.github.gh import install_gh_shim
from cloud_env.apps.github.git import configure_git_credentials
from cloud_env.apps.github.tokens import GitHubApp, store_github_tokens
from cloud_env.harness import HARNESS
from cloud_env.lib.log import log

GITHUB_HOSTS = (
    "api.github.com,github.com,codeload.github.com,objects.githubusercontent.com,"
    "raw.githubusercontent.com,uploads.github.com"
)


def bypass_proxy_for_github() -> None:
    for var in ("NO_PROXY", "no_proxy"):
        os.environ[var] = ",".join(filter(None, [os.environ.get(var), GITHUB_HOSTS]))


def github_env_lines(*, bypass_proxy: bool) -> list[str]:
    lines = []
    if bypass_proxy:
        lines += [
            f'export NO_PROXY="${{NO_PROXY:+$NO_PROXY,}}{GITHUB_HOSTS}"',
            f'export no_proxy="${{no_proxy:+$no_proxy,}}{GITHUB_HOSTS}"',
        ]
    # gh prefers these over the token the shim passes
    lines.append("unset GH_TOKEN GITHUB_TOKEN")
    return lines


# Claude Code's agent proxy swaps GitHub credentials for a placeholder and only allows the session's
# repos, so traffic goes direct there. Antigravity's proxy only swaps agy-h-* handles and blocks
# direct egress, so GitHub stays on HTTPS_PROXY while git's credential helper and the gh shim pick
# the token by the repo's owner.
def use_github_directly(apps: dict[str, GitHubApp], default: str | None) -> list[str]:
    if not apps and not default:
        log("github: no GitHub token, leaving GitHub traffic on the agent proxy")
        return []
    if HARNESS.bypass_github_proxy:
        bypass_proxy_for_github()
    store_github_tokens(apps, default)
    install_gh_shim()
    configure_git_credentials()
    log(f"github: app tokens for {' '.join(sorted(apps)) or 'no owners'}")
    return github_env_lines(bypass_proxy=HARNESS.bypass_github_proxy)
