from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from cloud_env.apps.github.prompts import GitHubApp
from cloud_env.apps.github.tokens import GITHUB_OWNER, TOKEN_DIR, github_token
from cloud_env.config import HOME
from cloud_env.harness import SCRIPT
from cloud_env.lib.log import hook_command, log, run

# One git config file per owner with the commit identity of that owner's GitHub App
GIT_IDENTITY_DIR = HOME / ".config" / "git" / "github-apps"
# Identity file of version 1 copies, whose includes session start drops
LEGACY_IDENTITY_FILE = HOME / ".config" / "git" / "boticorn"


# The harness writes user.name=Claude into ~/.gitconfig. The user's identity from 1Password is
# the global default, and repos of an owner with a GitHub App commit as that app.
def set_git_identity(
    apps: dict[str, GitHubApp],
    identity_dir: Path = GIT_IDENTITY_DIR,
    legacy_file: Path = LEGACY_IDENTITY_FILE,
) -> None:
    name, email = os.environ.get("GIT_AUTHOR_NAME"), os.environ.get("GIT_AUTHOR_EMAIL")
    if name and email:
        run(["git", "config", "--global", "user.name", name], "[git] ")
        run(["git", "config", "--global", "user.email", email], "[git] ")
    else:
        log("git: no GIT_AUTHOR_NAME/GIT_AUTHOR_EMAIL, keeping the existing identity")
    # The harness signing key is registered to noreply@anthropic.com, and while signing is on its
    # Stop hook asks for every other committer's commits to be rewritten to that address
    run(["git", "config", "--global", "commit.gpgsign", "false"], "[git] ")

    if not apps:
        log("git: no GitHub App tokens, keeping the app identity config from earlier starts")
        return
    # Drops the includes of earlier starts, so an owner the broker no longer mints for stops matching
    found = subprocess.run(
        ["git", "config", "--global", "--get-regexp", r"^includeif\..*\.path$"],
        capture_output=True,
        text=True,
    )
    for line in found.stdout.splitlines():
        key, _, value = line.partition(" ")
        if Path(value).parent == identity_dir or Path(value) == legacy_file:
            subprocess.run(["git", "config", "--global", "--unset-all", key], capture_output=True)
    identity_dir.mkdir(parents=True, exist_ok=True)
    for owner, app in apps.items():
        path = identity_dir / owner
        path.unlink(missing_ok=True)
        # git config escapes the values, which come from the broker
        run(["git", "config", "--file", str(path), "user.name", app.name], "[git] ")
        run(["git", "config", "--file", str(path), "user.email", app.email], "[git] ")
        # `**/<owner>/**` also matches the agent proxy's http://127.0.0.1:<port>/git/<owner>/<repo>.
        # git only supports `remote.*.url` here, so any remote on the owner counts, upstream too.
        for pattern in (f"**/{owner}/**", f"git@github.com:{owner}/**"):
            key = f"includeIf.hasconfig:remote.*.url:{pattern}.path"
            run(["git", "config", "--global", key, str(path)], "[git] ")


def repo_owner(repo: str) -> str | None:
    """Returns the lowercased owner of a remote URL, OWNER/REPO or HOST/OWNER/REPO."""
    match = re.search(r"(?:^|[/:])([^/:]+)/[^/:]+?(?:\.git)?/?$", repo)
    owner = match.group(1).lower() if match else ""
    return owner if GITHUB_OWNER.fullmatch(owner) else None


def origin_owner(path: str) -> str | None:
    url = subprocess.run(
        ["git", "-C", path, "remote", "get-url", "origin"], capture_output=True, text=True
    ).stdout.strip()
    return repo_owner(url)


def configure_git_credentials(script: Path = SCRIPT) -> None:
    # The empty helper drops ones configured earlier
    key = "credential.https://github.com.helper"
    subprocess.run(["git", "config", "--global", "--unset-all", key], capture_output=True)
    run(["git", "config", "--global", "--add", key, ""], "[git] ")
    run(["git", "config", "--global", "--add", key, "!" + hook_command("git-credential", script)], "[git] ")  # fmt: skip
    run(
        ["git", "config", "--global", "credential.https://github.com.useHttpPath", "true"],
        "[git] ",
    )


def cmd_git_credential(args: list[str], token_dir: Path = TOKEN_DIR) -> int:
    """git credential helper: answers `get` with the token for the remote's owner."""
    if args[:1] != ["get"]:
        return 0
    fields = dict(line.partition("=")[::2] for line in sys.stdin.read().splitlines())
    owner = fields.get("path", "").lstrip("/").split("/", 1)[0].lower()
    if token := github_token(owner, token_dir):
        print(f"username=x-access-token\npassword={token}")
    return 0
