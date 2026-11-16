from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

from cloud_env.apps.github.git import origin_owner, repo_owner
from cloud_env.apps.github.tokens import github_token
from cloud_env.config import HOME
from cloud_env.harness import SCRIPT
from cloud_env.lib.fs import atomic_write
from cloud_env.lib.log import hook_command

# Ahead of the mise shim on TOOLS_PATH
GH_SHIM = HOME / ".local" / "bin" / "gh"


def install_gh_shim(shim: Path = GH_SHIM, script: Path = SCRIPT) -> None:
    atomic_write(shim, f'#!/bin/sh\nexec {hook_command("gh", script)} "$@"\n', mode=0o755)


def gh_target_owner(args: list[str]) -> str | None:
    """Returns the owner gh acts on: from --repo, a `gh api repos/<owner>/...` path, GH_REPO, or
    the current directory's origin remote."""
    for i, arg in enumerate(args):
        if arg in ("-R", "--repo") and i + 1 < len(args):
            return repo_owner(args[i + 1])
        if arg.startswith("--repo="):
            return repo_owner(arg.removeprefix("--repo="))
    if args[:1] == ["api"]:
        for arg in args[1:]:
            if match := re.match(r"/?repos/([^/]+)/", arg):
                return match.group(1).lower()
    if repo := os.environ.get("GH_REPO"):
        return repo_owner(repo)
    return origin_owner(os.getcwd())


def cmd_gh(args: list[str]) -> int:
    """Runs the real gh with the token for the owner it acts on, unless a token is already set."""
    path = os.pathsep.join(
        p for p in os.environ.get("PATH", "").split(os.pathsep) if p and Path(p) != GH_SHIM.parent
    )
    real = shutil.which("gh", path=path)
    if not real:
        print("gh: not installed", file=sys.stderr)
        return 127
    env = dict(os.environ)
    explicit = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN")
    if not explicit and (token := github_token(gh_target_owner(args))):
        env["GH_TOKEN"] = token
    os.execve(real, [real, *args], env)
