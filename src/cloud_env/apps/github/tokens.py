from __future__ import annotations

import contextlib
import fcntl
import os
import re
import sys
import time
from pathlib import Path

from cloud_env.apps.github.prompts import GitHubApp
from cloud_env.config import LOG_FILE, SHM_DIR
from cloud_env.lib.fs import atomic_write
from cloud_env.lib.log import log
from cloud_env.lib.parse import clean_text, json_object
from cloud_env.secrets.broker import fetch_broker_env

# One file per owner with that owner's GitHub App token. Underscore names can't clash with
# owners: `_default` serves other owners, `_stamp` times the next refresh, `_lock` guards it.
TOKEN_DIR = SHM_DIR / "cloud-env-github"
DEFAULT_TOKEN = "_default"
# Installation tokens expire after an hour
TOKEN_REFRESH_AFTER = 50 * 60
TOKEN_RETRY_AFTER = 5 * 60
GITHUB_OWNER = re.compile(r"[a-z0-9][a-z0-9-]*")
# Identity for BOTICORN_TOKENS entries, which carry only a token
BOTICORN_NAME = "boticorn[bot]"
BOTICORN_EMAIL = "323198915+boticorn[bot]@users.noreply.github.com"


def github_apps(env: dict[str, str]) -> dict[str, GitHubApp]:
    """Returns the GitHub App of each owner from the broker env.

    GITHUB_APP_TOKENS maps owner to {"token", "name", "email"}. Without it, BOTICORN_TOKENS maps
    owner to a boticorn token.
    """
    if raw := env.get("GITHUB_APP_TOKENS"):
        source, parse = "GITHUB_APP_TOKENS", app_entry
    elif raw := env.get("BOTICORN_TOKENS"):
        source, parse = "BOTICORN_TOKENS", boticorn_entry
    else:
        return {}
    apps = {}
    for owner, entry in json_object(source, raw).items():
        app = parse(entry)
        if app and GITHUB_OWNER.fullmatch(owner.lower()):
            apps[owner.lower()] = app
        else:
            log(f"github: skipping invalid {source} entry {owner!r}")
    return apps


def app_entry(entry: object) -> GitHubApp | None:
    if not isinstance(entry, dict):
        return None
    fields = [entry.get(k) for k in GitHubApp._fields]
    return GitHubApp(*fields) if all(clean_text(f) for f in fields) else None


def boticorn_entry(token: object) -> GitHubApp | None:
    return GitHubApp(token, BOTICORN_NAME, BOTICORN_EMAIL) if clean_text(token) else None


def write_token(name: str, token: str, token_dir: Path = TOKEN_DIR) -> None:
    atomic_write(token_dir / name, token, mode=0o600)


def store_github_tokens(
    apps: dict[str, GitHubApp], default: str | None, token_dir: Path = TOKEN_DIR
) -> None:
    """Writes the token files. Tokens of owners missing from apps are kept: they still work
    until they expire."""
    token_dir.mkdir(mode=0o700, exist_ok=True)
    token_dir.chmod(0o700)
    for owner, app in apps.items():
        write_token(owner, app.token, token_dir)
    if default:
        write_token(DEFAULT_TOKEN, default, token_dir)
    if apps:
        (token_dir / "_stamp").touch()


def refresh_github_tokens(token_dir: Path = TOKEN_DIR) -> None:
    """Re-mints the GitHub App tokens before they expire. Runs on every token lookup, so the
    stamp file limits it to one attempt per TOKEN_REFRESH_AFTER, or TOKEN_RETRY_AFTER after a
    failure.
    """
    stamp = token_dir / "_stamp"
    lock_path = token_dir / "_lock"
    try:
        if time.time() - stamp.stat().st_mtime < TOKEN_REFRESH_AFTER:
            return
    except FileNotFoundError:
        return  # the session has no GitHub App tokens
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return  # a parallel lookup is refreshing
        if time.time() - stamp.stat().st_mtime < TOKEN_REFRESH_AFTER:
            return
        # Schedules the retry first: urlopen's timeout doesn't bound DNS or the whole response,
        # and if the caller gets killed, the next attempt still waits
        retry_at = time.time() - TOKEN_REFRESH_AFTER + TOKEN_RETRY_AFTER
        os.utime(stamp, (retry_at, retry_at))
        env = fetch_broker_env(timeout=4) or {}
        apps = github_apps(env)
        if not apps:
            log("github: token refresh failed, keeping the current tokens")
            return
        store_github_tokens(apps, env.get("GITHUB_TOKEN"), token_dir)
        log(f"github: refreshed tokens for {' '.join(sorted(apps))}")


def github_token(
    owner: str | None, token_dir: Path = TOKEN_DIR, log_file: Path = LOG_FILE
) -> str | None:
    """Returns the token for owner, or the default token, refreshing them when they are due."""
    # stdout belongs to git or gh, so the refresh logs to the session-start log
    try:
        with open(log_file, "a") as out, contextlib.redirect_stdout(out):
            refresh_github_tokens(token_dir)
    except Exception as e:  # a failed refresh falls back to the current tokens
        print(f"github: token refresh crashed: {e!r}", file=sys.stderr)
    names = [owner] if owner and GITHUB_OWNER.fullmatch(owner) else []
    for name in [*names, DEFAULT_TOKEN]:
        try:
            if token := (token_dir / name).read_text().strip():
                return token
        except OSError:
            continue
    return None
