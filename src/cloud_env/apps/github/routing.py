from __future__ import annotations

import os
from pathlib import Path

from cloud_env.apps.github.gh import GH_SHIM, install_gh_shim
from cloud_env.apps.github.git import configure_git_credentials
from cloud_env.apps.github.prompts import GitHubApp
from cloud_env.apps.github.tokens import TOKEN_DIR, store_github_tokens
from cloud_env.harness import HARNESS, SCRIPT
from cloud_env.lib.log import log

GITHUB_HOSTS = (
    "api.github.com,github.com,codeload.github.com,objects.githubusercontent.com,"
    "raw.githubusercontent.com,uploads.github.com"
)


def bypass_proxy_for_github() -> None:
    for var in ("NO_PROXY", "no_proxy"):
        os.environ[var] = ",".join(filter(None, [os.environ.get(var), GITHUB_HOSTS]))


# Claude Code's agent proxy swaps GitHub credentials for a placeholder and only allows the session's
# repos, so traffic goes direct there. Antigravity's proxy only swaps agy-h-* handles and blocks
# direct egress, so GitHub stays on HTTPS_PROXY while git's credential helper and the gh shim pick
# the token by the repo's owner. Returns the env file lines.
def use_github_directly(
    apps: dict[str, GitHubApp],
    default: str | None,
    *,
    bypass_proxy: bool | None = None,
    token_dir: Path = TOKEN_DIR,
    shim: Path = GH_SHIM,
    script: Path = SCRIPT,
) -> list[str]:
    if not apps and not default:
        log("github: no GitHub token, leaving GitHub traffic on the agent proxy")
        return []
    bypass = HARNESS.bypass_github_proxy if bypass_proxy is None else bypass_proxy
    if bypass:
        bypass_proxy_for_github()
    store_github_tokens(apps, default, token_dir)
    install_gh_shim(shim, script)
    configure_git_credentials(script)
    log(f"github: app tokens for {' '.join(sorted(apps)) or 'no owners'}")
    lines = []
    if bypass:
        lines += [
            f'export NO_PROXY="${{NO_PROXY:+$NO_PROXY,}}{GITHUB_HOSTS}"',
            f'export no_proxy="${{no_proxy:+$no_proxy,}}{GITHUB_HOSTS}"',
        ]
    # gh prefers these over the token the shim passes
    lines.append("unset GH_TOKEN GITHUB_TOKEN")
    return lines
