from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor

from cloud_env.apps.github.git import set_git_identity
from cloud_env.apps.github.routing import use_github_directly
from cloud_env.apps.github.tokens import GitHubApp, github_apps
from cloud_env.apps.mise.mise import install_repo_tools, trust_repo
from cloud_env.apps.tailscale import TAILSCALE_ONLY, join_tailscale, tailscale_needs_rejoin
from cloud_env.config import INIT_MARKER, LOG_FILE, VERSION, tools_path
from cloud_env.harness import HARNESS
from cloud_env.lib.log import log
from cloud_env.secrets.loader import SESSION_ENV_EXCLUDED, load_secrets
from cloud_env.update import self_update


def unpack_session_secrets(
    raw_secrets: Mapping[str, str],
) -> tuple[dict[str, str], str | None, dict[str, GitHubApp]]:
    secrets = dict(raw_secrets)
    tailscale_authkey = secrets.pop(TAILSCALE_ONLY, None)
    apps = github_apps(secrets)
    secrets.pop("GITHUB_APP_TOKENS", None)
    secrets.pop("BOTICORN_TOKENS", None)
    return secrets, tailscale_authkey, apps


def format_session_env(
    secrets: Mapping[str, str],
    preamble: list[str],
    github_lines: list[str],
) -> list[str]:
    return [
        *preamble,
        *(
            f"export {var}={shlex.quote(value)}"
            for var, value in secrets.items()
            if var not in SESSION_ENV_EXCLUDED
        ),
        *github_lines,
    ]


def session_start(update: bool) -> None:
    """Prepares the session. Every step logs its failure and the session starts regardless."""
    log("== session-start")
    # Child processes must not trigger ~/.bashrc's session-start check on Antigravity
    os.environ.pop("BASH_ENV", None)
    os.environ["PATH"] = f"{tools_path()}:{os.environ.get('PATH', '')}"
    trust_repo()

    resuming = INIT_MARKER.exists()
    if resuming:
        if not tailscale_needs_rejoin():
            log("resume: state intact, nothing to do")
            return
        log("resume: tailscaled is gone, running the full start to rejoin")

    if update:
        self_update()
    log(f"running version {VERSION}")

    raw_secrets, complete = load_secrets()
    secrets, tailscale_authkey, apps = unpack_session_secrets(raw_secrets)
    os.environ.update(secrets)

    set_git_identity(apps)
    name = os.environ.get("GIT_AUTHOR_NAME")
    email = os.environ.get("GIT_AUTHOR_EMAIL")
    identity = (name, email) if name and email else None
    HARNESS.write_instructions(apps, identity, resuming=resuming)
    github_lines = use_github_directly(apps, secrets.get("GITHUB_TOKEN"))
    lines = format_session_env(secrets, HARNESS.env_preamble(), github_lines)
    if resuming and not complete:
        # The session already has the env from its first start; rewriting would drop secrets
        log("env: secrets incomplete, keeping the env file from the last start")
    elif HARNESS.write_env(lines) and complete:
        INIT_MARKER.touch()

    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(install_repo_tools)]
        # Antigravity's sandbox netns only exposes lo, so tailscaled's netmon pauses control login
        if HARNESS.supports_tailscale:
            tasks.append(pool.submit(join_tailscale, tailscale_authkey))
        else:
            log("unsupported in Antigravity sandbox, skipping", "[tailscale] ")
    for task in tasks:
        if error := task.exception():
            log(f"step failed: {error!r}")
    log("== session-start done")


def cmd_session_start(update: bool) -> int:
    # Hook stdout is shown to the model, so everything (including child output) goes to the log
    fd = os.open(LOG_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    os.close(fd)
    try:
        with HARNESS.session_context():
            session_start(update)
    except Exception as e:  # the session must start even if this script breaks
        log(f"session-start failed: {e!r}")
    return 0
