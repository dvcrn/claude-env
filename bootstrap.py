#!/usr/bin/env python3
"""Setup and SessionStart bootstrap for Claude Code and Antigravity cloud environments.

`setup` runs once from stub.sh or stub-agy.sh after verifying this file's signature. It installs
mise, the global tools, and a copy of this file for session start. Holds no secrets.

`session-start` runs on every session start and resume. It updates itself to the newest signed
copy, loads secrets from the broker and 1Password into the session environment, and configures
git, GitHub, Tailscale, and the repo's mise tools. Without the broker the session starts without
secrets. Logs to /tmp/session-start.log.

`pre-tool-use` runs from the Claude Code PreToolUse hook. It strips the Claude Code footer from
GitHub MCP tool input, and denies Bash commands and file writes that contain it.

`git-credential` is git's credential helper for github.com. It returns the GitHub App token of
the remote's owner, or the default token.

`gh` runs from the gh shim. It runs the real gh with the GitHub App token of the owner gh acts
on, or the default token.

Both re-mint the GitHub App tokens from the broker when they near expiry.
"""

# Generated from src/cloud_env by scripts/build.py: do not edit directly.

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple, Protocol

# Raise to stop older signed copies from running. Installed copies only ever move forward.
VERSION = 2

BASE_URL = "https://raw.githubusercontent.com/dvcrn/claude-env/main"
SIGNING_NAMESPACE = "claude-env"

HOME = Path.home()
SHM_DIR = Path("/dev/shm")

LOG_FILE = Path("/tmp/session-start.log")
INIT_MARKER = SHM_DIR / ".cloud-env-initialized"


def tools_path(home: Path = HOME) -> str:
    return f"{home}/.local/bin:{home}/.local/share/mise/shims"


# --- apps/github/prompts ------------------------------------------------------------------


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


# --- lib/fs -------------------------------------------------------------------------------


def atomic_write(path: Path, content: str, *, mode: int | None = None) -> None:
    """Writes content to a sibling temp file and replaces path atomically.

    Leaves permissions untouched when mode is None so writes to virtiofs mounts
    (where chmod raises EPERM) still succeed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        if mode is not None:
            tmp.chmod(mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def replace_marked_block(existing: str, lines: list[str]) -> str:
    kept = re.sub(r"(?ms)^# >>> cloud-env\n.*?^# <<< cloud-env(\n|\Z)", "", existing)
    if kept and not kept.endswith("\n"):
        kept += "\n"
    return kept + "\n".join(["# >>> cloud-env", *lines, "# <<< cloud-env"]) + "\n"


# --- lib/log ------------------------------------------------------------------------------


_log_lock = threading.Lock()


def log(message: str, prefix: str = "") -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _log_lock:
        print(f"{stamp} {prefix}{message}", flush=True)


def run(cmd: list[str], prefix: str = "", cwd: str | None = None) -> bool:
    """Runs cmd, logging its combined output line by line under prefix."""
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError as e:
        log(f"{cmd[0]}: {e}", prefix)
        return False
    with proc:
        assert proc.stdout
        for line in proc.stdout:
            log(line.rstrip("\n"), prefix)
        code = proc.wait()
    if code:
        log(f"{cmd[0]} exited with {code}", prefix)
    return code == 0


def hook_command(subcommand: str, script: Path) -> str:
    return f"{shlex.quote(sys.executable)} -I {shlex.quote(str(script))} {subcommand}"


# --- lib/parse ----------------------------------------------------------------------------


def clean_text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and not any(ord(c) < 0x20 for c in value)


def json_object(name: str, raw: str) -> dict:
    try:
        parsed = json.loads(raw)
    except ValueError:
        log(f"github: {name} is not JSON")
        return {}
    if not isinstance(parsed, dict):
        log(f"github: {name} is not an object")
        return {}
    return parsed


# --- agy/auth -----------------------------------------------------------------------------


def broker_token_headers(token_file: Path) -> dict[str, str]:
    """Reads Cloudflare Access service-token headers when the VM has no proxy network secret."""
    if not token_file.is_file():
        return {}
    try:
        raw = json.loads(token_file.read_text())
    except (OSError, ValueError):
        log(f"broker: unreadable token file {token_file}")
        return {}
    if not isinstance(raw, dict):
        log(f"broker: {token_file} is not a JSON object")
        return {}
    client_id = raw.get("CF-Access-Client-Id")
    client_secret = raw.get("CF-Access-Client-Secret")
    if not (
        isinstance(client_id, str)
        and isinstance(client_secret, str)
        and clean_text(client_id)
        and clean_text(client_secret)
    ):
        log(f"broker: missing CF-Access-Client-Id or CF-Access-Client-Secret in {token_file}")
        return {}
    return {"CF-Access-Client-Id": client_id, "CF-Access-Client-Secret": client_secret}


# --- agy/bashrc ---------------------------------------------------------------------------


# Antigravity runs every command in a non-interactive bash shell with BASH_ENV=~/.bashrc
def write_antigravity_bashrc(
    home: Path,
    script: Path,
    attempt_marker: Path,
    env_file_path: Path,
) -> None:
    path = home / ".bashrc"
    existing = path.read_text() if path.exists() else ""
    # Debian's default non-interactive early return would stop BASH_ENV before our block
    existing = re.sub(r"(?ms)^case \$- in\n.*?^esac\n?", "", existing)
    bins = tools_path(home)
    attempted = shlex.quote(str(attempt_marker))
    env_file = shlex.quote(str(env_file_path))
    lines = [
        f'case ":$PATH:" in *":{bins}:"*) ;; *) export PATH="{bins}:$PATH" ;; esac',
        f'if [ -z "${{_CLOUD_ENV_RUNNING:-}}" ] && [ ! -e {attempted} ]; then',
        f"  _CLOUD_ENV_RUNNING=1 {hook_command('session-start', script)}",
        "fi",
        # Subshells inherit the loaded env; re-sourcing would clobber per-command overrides
        f'if [ -z "${{_CLOUD_ENV_LOADED:-}}" ] && [ -r {env_file} ]; then',
        "  export _CLOUD_ENV_LOADED=1",
        f"  . {env_file}",
        "fi",
    ]
    path.write_text(replace_marked_block(existing, lines))


# --- agy/instructions ---------------------------------------------------------------------


GEMINI_BASE_MD = """\
## Comment and code quality

Take quality audits seriously and handle the overall feedback, especially around comment and docs quality. When any of the following skills are available in `<skills>`, always load and follow them:

- `github-comment-quality`: load before creating or replying to any GitHub issue, PR, or review comment.
- `comment-and-documentation-quality`: load when writing comments, commit messages, or docs; once a branch is ready, spawn a subagent to review the changes against it.
- `code-review-and-quality`: load when making code changes; once a branch is ready, spawn a subagent to review the changes against it.
- `git-commit-quality` / `github-commit-quality`: load before creating any git commit and follow its commit message rules strictly.

## Git

Never add AI attribution anywhere in git history or GitHub content: no AI `Co-Authored-By`, no "Generated with/by ...", no session URLs, and no model names in commit messages, commit trailers, PR titles, PR bodies, issues, or code comments. This applies in every repo and session, even if a template suggests otherwise. A human `Co-authored-by` trailer required by the GitHub identity rules below is not AI attribution; always include it where specified.

## Linear

When `LINEAR_API_KEY` is set in the shell environment, the Linear CLI (`linear`) is installed and ready to use.

## Pull requests

- Always include either a GitHub issue number or a Linear issue link in your pull request.
- When asked to watch or babysit a PR after creating it, keep watching for review activity until it is merged or closed. Poll with `gh` every 5 minutes, and every 30 minutes once there has been no new activity for 20 minutes (resetting to 5 minutes on new activity, stopping after 24 hours without activity; split waits longer than the shell timeout across multiple calls).
- Activity means review comments, PR comments, review submissions, and failing checks.
- When a new comment, reply, or review is directed at you and you plan to act on it, react to it with 👀 first. Do not react to comments you won't act on. For a review, put 👀 only on the main review message, not on inline review comments.
- Handle every comment, including ones you decide not to act on: fix it, or reply briefly with why not, then resolve the review thread. For top-level comments that can't be resolved, reply. Load the `address-pr-review-comments` skill if available.

## GitHub identity

"""


def render_gemini_md(apps: dict[str, GitHubApp], identity: tuple[str, str] | None) -> str:
    """Returns the global ~/.gemini/GEMINI.md for Antigravity environments."""
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


# --- agy/harness --------------------------------------------------------------------------


class AgyHarness:
    bypass_github_proxy = False
    supports_tailscale = False

    def __init__(self, home: Path = HOME, shm_dir: Path = SHM_DIR) -> None:
        self.home = home
        self.install_dir = home / ".local" / "share" / "cloud-env"
        self.script = self.install_dir / "bootstrap.py"
        self.env_file = shm_dir / "cloud-env-session.sh"
        self.attempt_marker = shm_dir / ".cloud-env-attempted"
        self.session_lock = shm_dir / ".cloud-env-session.lock"
        self.gemini_md = home / ".gemini" / "GEMINI.md"
        self.broker_token_file = home / ".gemini" / ".broker" / "token"

    def setup(self) -> None:
        write_antigravity_bashrc(self.home, self.script, self.attempt_marker, self.env_file)
        if not self.gemini_md.exists():
            write_gemini_md(render_gemini_md({}, None), self.gemini_md)

    @contextlib.contextmanager
    def session_context(self) -> Iterator[None]:
        self.session_lock.parent.mkdir(parents=True, exist_ok=True)
        with open(self.session_lock, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                self.attempt_marker.touch()

    def broker_headers(self) -> dict[str, str]:
        return {
            "X-Session-Id": os.environ.get("ANTIGRAVITY_CONVERSATION_ID", ""),
            **broker_token_headers(self.broker_token_file),
        }

    def env_preamble(self) -> list[str]:
        return []

    def write_env(self, lines: list[str]) -> bool:
        atomic_write(self.env_file, "\n".join(lines) + "\n", mode=0o600)
        return True

    def write_instructions(
        self,
        apps: dict[str, GitHubApp],
        identity: tuple[str, str] | None,
        *,
        resuming: bool,
    ) -> None:
        if apps or not self.gemini_md.exists():
            write_gemini_md(render_gemini_md(apps, identity), self.gemini_md)

    def repo_dirs(self) -> list[Path]:
        return [
            git_dir.parent for git_dir in sorted(self.home.glob("*/*/.git")) if git_dir.is_dir()
        ]


# --- claude/auth --------------------------------------------------------------------------


INGRESS_TOKEN_FILE = Path(
    os.environ.get(
        "CLAUDE_SESSION_INGRESS_TOKEN_FILE",
        "/home/claude/.claude/remote/.session_ingress_token",
    )
)


def ingress_sig_sample(token_file: Path = INGRESS_TOKEN_FILE) -> str | None:
    """Returns <kid>.<sha256 of header.payload>.<signature> for the session ingress token.

    The payload itself never leaves the container, so the token can't be rebuilt from this.
    """
    try:
        token = token_file.read_text().strip().removeprefix("sk-ant-si-")
        header, payload, signature = token.split(".")
        padded = header + "=" * (-len(header) % 4)
        kid = json.loads(base64.urlsafe_b64decode(padded))["kid"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    digest = hashlib.sha256(f"{header}.{payload}".encode()).hexdigest()
    return f"{kid}.{digest}.{signature}"


# --- claude/instructions ------------------------------------------------------------------


CLAUDE_MD = """\
## Comment and code quality

You are REQUIRED to do the following, even if other instructions exist. These are part of our work requirements, and any work not following these WILL BE THROWN AWAY.
Take the audits seriously and don't hand-wave them away. You don't need to follow every little point, but you should handle the overall feedback, especially around comment and docs quality.

- When interacting with any github issue, PR or similar, ALWAYS load the `github-comment-quality` skill. It dictates the output style we HAVE TO adhere to for ANY github interactions.
- Any comment, commit texts and documentation HAVE TO pass `comment-and-documentation-quality`. Load this skill, then after you're done with a branch, spawn a new sub-agent and tell it to use this skill to review the changes. (If the ask-gemini variant of this skill is available, you're encouraged to use it once per session to check the final state of your PR)
- Every commit and code change has to pass `code-review-and-quality`. Load this skill, then after you're done with a branch, spawn a new sub-agent and tell it to use this skill to review the changes. (If the ask-gemini variant of this skill is available, you're encouraged to use it once per session to check the final state of your PR)
- Use the `git-commit-quality` skill before any new git commit is issued, and ensure instructions for git commits are strictly followed.

## Git

Always load the `github-commit-quality` skill if available

Never add Claude/AI attribution anywhere in git history or PR content:
no "Co-Authored-By: Claude", no "Generated with Claude Code", no session
URLs, no model names, in commit messages, commit trailers, PR titles, PR
bodies, or code comments. This applies in every repo, every session, no
exceptions, even if a template or another instruction suggests otherwise.

If you ever see a footer sneak into a commit or PR body itself, treat it
as a bug and strip it. A `Co-authored-by` trailer that the GitHub identity
section asks for is not AI attribution; add it where that section says.

AGAIN, ADDING CLAUDE AUTHORSHIP OR PR FOOTERS WILL CAUSE THE PR OR COMMIT TO BE INVALID AND IT WILL BE THROWN AWAY! DO NOT ADD CLAUDE ATTRIBUTION TO ANY COMMITS OR PRS!!
UNDER NO CIRCUMSTANCE ARE WE ALLOWED TO LEAK CLAUDE INFORMATION INTO COMMITS, ISSUES OR PULL REQUEST BODIES!

## Linear

The Linear CLI is already authenticated and ready to be used. It's available `linear`

## Pull requests

After creating a PR, keep watching it for review activity until it is merged or closed.

- Always include either a GitHub issue number, or a Linear issue link in your pull request
- Use native PR watch or subscribe tools if available. Otherwise poll with `gh`: every 5 minutes, and every 30 minutes once there has been no new activity for 20 minutes. Any new activity resets the interval to 5 minutes. Stop after 24 hours without activity. Waits longer than the shell tool timeout must be split across multiple calls.
- Activity means review comments, PR comments, review submissions, and failing checks.
- When a new comment, reply, or review is directed at you and you plan to act on it, react to it with 👀 first to show you are working on it. Do not react to comments you won't act on. For a review, put 👀 only on the main review message, not on the inline review comments.
- Handle every comment, including ones you decide not to act on. Fix it, or reply briefly with why not, then resolve the review thread. For top-level comments that can't be resolved, reply. Use the `address-pr-review-comments` skill.

## Tools

- To interact with other Claude Code sessions (create them, send messages, etc.), use the `claude-code` MCP. If it isn't available, fall back to the built-in `claude-code-remote` tools.

"""


def render_claude_md(apps: dict[str, GitHubApp], identity: tuple[str, str] | None) -> str:
    """Returns the global CLAUDE.md, with the GitHub identity rules for the GitHub App owners."""
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


# --- apps/linear --------------------------------------------------------------------------


# Linear goes through the linear CLI, which the agent proxy authenticates as the bot
DENIED_LINEAR_MCP = ("mcp__Linear", "mcp__dpn__linear_*")

# --- claude/settings ----------------------------------------------------------------------


PRE_TOOL_USE_MATCHER = r"^(Bash|Write|Edit|MultiEdit|NotebookEdit|mcp__.*[Gg]it[Hh]ub.*)$"

ALLOWED_COMMANDS = (
    "Bash(op:*)",
    "Bash(jq:*)",
    "Bash(gh pr view:*)",
    "Bash(gh pr diff:*)",
    "Bash(gh pr checks:*)",
    "Bash(gh pr comment:*)",
    "mcp__claude-code-remote__*",
)
# The default External System Writes rule excludes thread replies and resolves, so
# addressing review feedback through gh api gets blocked in auto mode
AUTO_MODE_ALLOW = (
    "PR Review Thread Work: When the user's own message asked the agent to work on, babysit, "
    "or address review feedback on a specific PR in the repository this session was started "
    "for, these writes on that PR are allowed: replying to its review comments or threads "
    "(`gh api` POST `repos/{o}/{r}/pulls/{n}/comments/{id}/replies`, GraphQL "
    "`addPullRequestReviewThreadReply`), resolving or unresolving its review threads (GraphQL "
    "`resolveReviewThread`/`unresolveReviewThread`), posting PR comments, editing comments the "
    "agent posted in this session, and re-requesting review from existing reviewers. Does not "
    "cover merging, closing, approving or requesting changes, label or base changes, or writes "
    "to any other PR or repository."
)


def set_hook(hooks: dict, event: str, group: dict, script: Path) -> None:
    """Replaces this script's hook group for event with group."""
    target = str(script)
    groups = hooks.setdefault(event, [])
    groups[:] = [
        existing
        for existing in groups
        if not any(target in hook.get("command", "") for hook in existing.get("hooks", []))
    ]
    groups.append(group)


# Merges into the existing settings so keys set by the image survive
def write_claude_settings(claude_dir: Path, script: Path) -> None:
    path = claude_dir / "settings.json"
    try:
        settings = json.loads(path.read_text())
    except FileNotFoundError:
        settings = {}
    except ValueError:
        print(f"bootstrap: {path} is not valid JSON, replacing it", file=sys.stderr)
        settings = {}

    settings["attribution"] = {"commit": "", "pr": "", "sessionUrl": False}
    permissions = settings.setdefault("permissions", {})
    allow = permissions.setdefault("allow", [])
    allow += [rule for rule in ALLOWED_COMMANDS if rule not in allow]
    deny = permissions.setdefault("deny", [])
    deny += [rule for rule in (*PROXIED_GITHUB_WRITES, *DENIED_LINEAR_MCP) if rule not in deny]

    auto_allow = settings.setdefault("autoMode", {}).setdefault("allow", [])
    auto_allow += [rule for rule in ("$defaults", AUTO_MODE_ALLOW) if rule not in auto_allow]

    hooks = settings.setdefault("hooks", {})
    session_start = {
        "type": "command",
        "command": hook_command("session-start", script),
        "timeout": 600,
    }
    set_hook(hooks, "SessionStart", {"hooks": [session_start]}, script)
    pre_tool_use = {
        "type": "command",
        "command": hook_command("pre-tool-use", script),
        "timeout": 10,
    }
    set_hook(
        hooks,
        "PreToolUse",
        {"matcher": PRE_TOOL_USE_MATCHER, "hooks": [pre_tool_use]},
        script,
    )

    path.write_text(json.dumps(settings, indent=2) + "\n")


# --- claude/harness -----------------------------------------------------------------------


class ClaudeHarness:
    bypass_github_proxy = True
    supports_tailscale = True

    def __init__(self, home: Path = HOME, ingress_token_file: Path = INGRESS_TOKEN_FILE) -> None:
        self.home = home
        self.claude_dir = home / ".claude"
        self.install_dir = self.claude_dir / "hooks" / "claude-env"
        self.script = self.install_dir / "bootstrap.py"
        self.ingress_token_file = ingress_token_file

    def setup(self) -> None:
        self.claude_dir.mkdir(parents=True, exist_ok=True)
        write_claude_settings(self.claude_dir, self.script)
        (self.claude_dir / "CLAUDE.md").write_text(render_claude_md({}, None))

    def session_context(self) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()

    def broker_headers(self) -> dict[str, str]:
        headers = {"X-Session-Id": os.environ.get("CLAUDE_CODE_REMOTE_SESSION_ID", "")}
        if sample := ingress_sig_sample(self.ingress_token_file):
            headers["X-Ingress-Sig-Sample"] = sample
        return headers

    def env_preamble(self) -> list[str]:
        return [f'export PATH="{tools_path(self.home)}:$PATH"']

    def write_env(self, lines: list[str]) -> bool:
        if not (env_file := os.environ.get("CLAUDE_ENV_FILE")):
            log("env: CLAUDE_ENV_FILE is not set, the session won't see the loaded variables")
            return False
        path = Path(env_file)
        existing = path.read_text() if path.exists() else ""
        path.touch(mode=0o600)
        path.chmod(0o600)  # before writing, so secrets never sit in a readable file
        path.write_text(replace_marked_block(existing, lines))
        return True

    def write_instructions(
        self,
        apps: dict[str, GitHubApp],
        identity: tuple[str, str] | None,
        *,
        resuming: bool,
    ) -> None:
        if apps or not resuming:
            (self.claude_dir / "CLAUDE.md").write_text(render_claude_md(apps, identity))

    def repo_dirs(self) -> list[Path]:
        if project := os.environ.get("CLAUDE_PROJECT_DIR"):
            return [Path(project)]
        return []


# --- harness ------------------------------------------------------------------------------


class Harness(Protocol):
    install_dir: Path
    script: Path
    bypass_github_proxy: bool
    supports_tailscale: bool

    def setup(self) -> None: ...
    def session_context(self) -> contextlib.AbstractContextManager[None]: ...
    def broker_headers(self) -> dict[str, str]: ...
    def env_preamble(self) -> list[str]: ...
    def write_env(self, lines: list[str]) -> bool: ...
    def write_instructions(
        self,
        apps: dict[str, GitHubApp],
        identity: tuple[str, str] | None,
        *,
        resuming: bool,
    ) -> None: ...
    def repo_dirs(self) -> list[Path]: ...


ANTIGRAVITY = os.environ.get("ANTIGRAVITY_AGENT") == "1" or HOME.name == "antigravity"
HARNESS: Harness = AgyHarness() if ANTIGRAVITY else ClaudeHarness()
INSTALL_DIR = HARNESS.install_dir
SCRIPT = HARNESS.script

# --- secrets/broker -----------------------------------------------------------------------


BROKER_URL = os.environ.get("CLOUD_ENV_BROKER_URL", "https://broker.d.pn")
ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")
# Never taken from the broker or 1Password: they would change how this script or the shell runs
RESERVED_NAMES = {
    "PATH", "HOME", "BASH_ENV", "ENV", "LD_PRELOAD", "LD_LIBRARY_PATH",
    "CLAUDE_ENV_FILE", "CLAUDE_PROJECT_DIR", "GITHUB_TOKEN_FILE",
}  # fmt: skip


def fetch_broker_env(
    timeout: float = 20,
    *,
    broker_url: str = BROKER_URL,
    auth_headers: Mapping[str, str] | None = None,
) -> dict[str, str] | None:
    headers = {
        # Cloudflare's Browser Integrity Check rejects urllib's default User-Agent (error 1010)
        "User-Agent": f"claude-env-bootstrap/{VERSION}",
        **(HARNESS.broker_headers() if auth_headers is None else auth_headers),
    }
    request = urllib.request.Request(f"{broker_url}/v1/session-env", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            env = json.load(resp)["env"]
        if not isinstance(env, dict):
            raise TypeError
    except OSError as e:
        log(f"broker: request to {broker_url} failed ({e})")
        return None
    except (ValueError, KeyError, TypeError):
        log("broker: unexpected response")
        return None
    # The broker is remote input, so one bad entry rejects the whole response
    for name, value in env.items():
        if not ENV_NAME.fullmatch(name) or name in RESERVED_NAMES or not isinstance(value, str):
            log(f"broker: invalid entry {name!r}")
            return None
    return env


# --- apps/github/tokens -------------------------------------------------------------------


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


# --- apps/github/git ----------------------------------------------------------------------


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


# --- apps/github/gh -----------------------------------------------------------------------


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


# --- claude/pre_tool_use ------------------------------------------------------------------


# Claude Code footer on GitHub posts, including any --- separator above it
FOOTER = re.compile(
    r"\n*(?:-{3,}[ \t]*\n+)?(?:🤖 )?_?Generated (?:by|with) "
    r"\[Claude Code\]\(https://claude\.(?:ai/code|com/claude-code)[^)]*\)_?[ \t]*"
)


def strip_footer(value: object) -> object:
    if isinstance(value, str):
        # Keeps one line break where the footer sat mid-text, so paragraphs don't merge
        return FOOTER.sub(lambda m: "" if m.end() == len(m.string) else "\n", value)
    if isinstance(value, list):
        return [strip_footer(item) for item in value]
    if isinstance(value, dict):
        return {k: strip_footer(v) for k, v in value.items()}
    return value


def has_footer(value: object) -> bool:
    if isinstance(value, str):
        return bool(FOOTER.search(value))
    if isinstance(value, list):
        return any(has_footer(item) for item in value)
    if isinstance(value, dict):
        # old_string has to match the file as it is, footer included
        return any(has_footer(v) for k, v in value.items() if k != "old_string")
    return False


def cmd_pre_tool_use() -> int:
    """Strips the footer from MCP tool input through updatedInput. Bash and file writes are
    denied instead, since rewriting shell or file content behind Claude's back is not safe.
    Malformed events pass through unchanged."""
    try:
        event = json.load(sys.stdin)
        tool, tool_input = event["tool_name"], event["tool_input"]
    except (ValueError, KeyError, TypeError):
        return 0
    if not isinstance(tool, str) or not isinstance(tool_input, dict):
        return 0
    if tool.startswith("mcp__"):
        cleaned = strip_footer(tool_input)
        if cleaned == tool_input:
            return 0
        output = {"updatedInput": cleaned}
    elif has_footer(tool_input):
        reason = "Remove the 'Generated by Claude Code' footer and retry."
        output = {"permissionDecision": "deny", "permissionDecisionReason": reason}
    else:
        return 0
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", **output}}))
    return 0


# --- apps/github/routing ------------------------------------------------------------------


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


# --- lib/http -----------------------------------------------------------------------------


def download(url: str, dest: Path, timeout: float = 60) -> None:
    # Cloudflare (in front of mise.jdx.dev) rejects urllib's default User-Agent with 403
    request = urllib.request.Request(url, headers={"User-Agent": "claude-env-bootstrap"})
    with urllib.request.urlopen(request, timeout=timeout) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


# --- apps/mise/mise -----------------------------------------------------------------------


MISE_VERSION = "2026.10.17"
GLOBAL_TOOLS = """\
[tools]
"aqua:1password/cli" = "2.41.1"
"aqua:tailscale/tailscale" = "1.104.2"
"npm:@schpet/linear-cli" = "3.1.0"
age = "1.3.2"
fnox = "1.42.0"
gh = "2.103.0"
trzsz-ssh = "0.1.26"
"""
REPO_MISE_CONFIGS = ("mise.toml", ".mise.toml", ".config/mise.toml", ".tool-versions")


def install_mise(work: Path, arch: str, home: Path = HOME) -> None:
    dest = home / ".local" / "bin" / "mise"
    if dest.is_file():
        current = subprocess.run([str(dest), "--version"], capture_output=True, text=True)
        if current.returncode == 0 and MISE_VERSION in current.stdout:
            return
    name = f"mise-v{MISE_VERSION}-linux-{'x64' if arch == 'x86_64' else 'arm64'}.tar.gz"
    tarball = work / name
    # mise.jdx.dev mirrors the GitHub release and stays up when GitHub 504s anonymous downloads
    urls = [
        f"https://mise.jdx.dev/v{MISE_VERSION}/{name}",
        f"https://github.com/jdx/mise/releases/download/v{MISE_VERSION}/{name}",
    ]
    for attempt in range(3):
        url = urls[attempt % len(urls)]
        try:
            download(url, tarball)
            break
        except OSError as e:
            log(f"mise: {url} failed ({e})")
            if attempt == 2:
                raise
            time.sleep(5)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball) as tar, open(dest, "wb") as out:
        member = tar.extractfile("mise/bin/mise")
        assert member is not None
        shutil.copyfileobj(member, out)
    dest.chmod(0o755)
    if os.access("/etc/profile.d", os.W_OK):
        Path("/etc/profile.d/mise.sh").write_text(f'export PATH="{tools_path(home)}:$PATH"\n')


def install_global_tools(home: Path = HOME) -> None:
    config = home / ".config" / "mise" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(GLOBAL_TOOLS)
    subprocess.run(["mise", "install", "--yes"], cwd=home, check=True)


def repo_mise_projects(candidates: list[Path] | None = None) -> list[Path]:
    dirs = HARNESS.repo_dirs() if candidates is None else candidates
    return [repo for repo in dirs if any((repo / name).is_file() for name in REPO_MISE_CONFIGS)]


# The hook runs inside the repo, and every mise call there (including the op and tailscale
# shims) fails until the repo's mise config is trusted, so this runs before anything else.
def trust_repo() -> None:
    for project in repo_mise_projects():
        run(["mise", "trust"], "[mise] ", cwd=str(project))


def install_repo_tools() -> None:
    for project in repo_mise_projects():
        run(["mise", "install", "--yes"], "[mise] ", cwd=str(project))


# --- apps/tailscale -----------------------------------------------------------------------


TAILSCALED_LOG = Path("/tmp/tailscaled.log")
TAILSCALED_SOCKET = Path("/var/run/tailscale/tailscaled.sock")
TAILSCALE_MARKER = SHM_DIR / ".cloud-env-tailscale"
# TS_AUTHKEY only goes to `tailscale up`, never into the session env
TAILSCALE_ONLY = "TS_AUTHKEY"


def tailscaled_running() -> bool:
    return subprocess.run(["pgrep", "-x", "tailscaled"], capture_output=True).returncode == 0


def tailscale_needs_rejoin(marker: Path = TAILSCALE_MARKER) -> bool:
    """Whether this VM joined Tailscale earlier and tailscaled has since died."""
    return marker.exists() and not tailscaled_running()


# tailscaled inherits HTTPS_PROXY, which is what lets it reach the control plane. In-memory
# state makes the node ephemeral.
def start_tailscaled(prefix: str) -> None:
    with open(TAILSCALED_LOG, "ab") as out:
        proc = subprocess.Popen(
            ["tailscaled", "--state=mem:", "--no-logs-no-support"],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            start_new_session=True,
        )
    for _ in range(20):
        if TAILSCALED_SOCKET.exists():
            return
        if proc.poll() is not None:
            log(f"tailscaled exited with {proc.returncode}, see {TAILSCALED_LOG}", prefix)
            return
        time.sleep(0.5)
    log(f"tailscaled socket did not appear after 10s, see {TAILSCALED_LOG}", prefix)


def join_tailscale(authkey: str | None, *, supported: bool | None = None) -> None:
    prefix = "[tailscale] "
    # Antigravity's sandbox netns only exposes lo, so tailscaled's netmon pauses control login
    if not (HARNESS.supports_tailscale if supported is None else supported):
        log("unsupported in Antigravity sandbox, skipping", prefix)
        return
    if not shutil.which("tailscaled"):
        log("not installed, skipping", prefix)
        return
    if not authkey:
        log("no TS_AUTHKEY, skipping", prefix)
        return
    if not tailscaled_running():
        start_tailscaled(prefix)
    session = os.environ.get("CLAUDE_CODE_REMOTE_SESSION_ID", "unknown").removeprefix("cse_")
    hostname = "claude-" + session.lower().replace("_", "-")
    up = [
        "tailscale",
        "up",
        f"--auth-key={authkey}",
        f"--hostname={hostname}",
        "--ssh",
        "--timeout=60s",
    ]
    if run(up, prefix):
        run(["tailscale", "status", "--peers=false"], prefix)
        TAILSCALE_MARKER.touch()


# --- secrets/onepassword ------------------------------------------------------------------


# Passed through to op, which needs the agent proxy to reach 1Password
OP_PASSTHROUGH = ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy", "SSL_CERT_FILE")
OP_TIMEOUT = 30


def load_1password(token: str, environment_id: str) -> dict[str, str] | None:
    """Returns the variables of the 1Password Environment.

    op runs `env -0` in an empty environment apart from what op itself needs, so every other
    variable in its output came from 1Password. NUL separators keep multiline values intact.
    """
    # The real binary; the mise shim would add its own variables
    op_bin = subprocess.run(["mise", "which", "op"], capture_output=True, text=True).stdout.strip()
    if not op_bin:
        log("1password: op is not installed")
        return None
    base_env = {"HOME": str(HOME), "OP_SERVICE_ACCOUNT_TOKEN": token}
    base_env |= {k: os.environ[k] for k in OP_PASSTHROUGH if k in os.environ}
    # --no-masking: op otherwise replaces secret values on stdout with a placeholder
    cmd = [op_bin, "run", "--no-masking", "--environment", environment_id, "--", "/usr/bin/env", "-0"]  # fmt: skip
    try:
        result = subprocess.run(
            cmd, env=base_env, capture_output=True, text=True, timeout=OP_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        log(f"1password: op run timed out after {OP_TIMEOUT}s")
        return None
    if result.returncode != 0:
        log(f"1password: op run failed: {result.stderr.strip()}")
        return None

    variables = {}
    for entry in filter(None, result.stdout.split("\0")):
        name, _, value = entry.partition("=")
        if name in base_env or name in ("PWD", "SHLVL", "_"):
            continue
        if not ENV_NAME.fullmatch(name) or name in RESERVED_NAMES:
            log(f"1password: skipping invalid name {name!r}")
            continue
        variables[name] = value
    log(f"1password: loaded {' '.join(variables) or 'nothing'}")
    return variables


# --- secrets/loader -----------------------------------------------------------------------


# Kept out of the secrets exported to the session: git identity variables override the per-owner
# identity in git config, and gh and git get their GitHub tokens from this script
SESSION_ENV_EXCLUDED = {
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
    "GITHUB_TOKEN", "GH_TOKEN", "GITHUB_APP_TOKENS", "BOTICORN_TOKENS",
}  # fmt: skip


def load_secrets() -> tuple[dict[str, str], bool]:
    """Returns the session's secrets, and whether both the broker and 1Password delivered."""
    broker = fetch_broker_env()
    if broker is None:
        log("broker: continuing without secrets")
        return {}, False
    token = broker.pop("OP_SERVICE_ACCOUNT_TOKEN", None)
    environment_id = broker.pop("OP_ENVIRONMENT_ID", None)
    log(f"broker: received {len(broker)} variables")
    if not (token and environment_id):
        log("1password: broker sent no token or environment ID, skipping")
        return broker, False
    onepassword = load_1password(token, environment_id)
    if onepassword is None:
        return broker, False
    # Broker values win, so a broker-minted token replaces a long-lived one from 1Password
    return onepassword | broker, True


# --- update -------------------------------------------------------------------------------


def install_self(
    signer: str,
    *,
    source_script: Path | None = None,
    install_dir: Path = INSTALL_DIR,
) -> None:
    install_dir.mkdir(parents=True, exist_ok=True)
    (install_dir / "allowed_signers").write_text(f"claude-env {signer}\n")
    here = (source_script or Path(__file__)).resolve()
    dest = install_dir / "bootstrap.py"
    shutil.copy(here, dest)
    shutil.copy(here.with_name("bootstrap.py.sig"), dest.with_name("bootstrap.py.sig"))


def script_version(source: bytes) -> int | None:
    match = re.search(rb"^VERSION = (\d+)$", source, re.MULTILINE)
    return int(match.group(1)) if match else None


def verify_signature(script: Path, sig: Path, allowed_signers: Path) -> bool:
    cmd = [
        "ssh-keygen", "-Y", "verify", "-f", str(allowed_signers),
        "-I", "claude-env", "-n", SIGNING_NAMESPACE, "-s", str(sig),
    ]  # fmt: skip
    try:
        with open(script, "rb") as f:
            result = subprocess.run(cmd, stdin=f, capture_output=True, text=True)
    except OSError as e:
        log(f"update: can't run ssh-keygen ({e})")
        return False
    if result.returncode != 0:
        log(f"update: signature check failed: {result.stderr.strip()}")
    return result.returncode == 0


def self_update(
    install_dir: Path = INSTALL_DIR,
    *,
    base_url: str = BASE_URL,
    current_version: int = VERSION,
) -> None:
    """Replaces the installed copy with a newer signed one from the repo and re-runs it.

    Only the installed copy runs as the hook and it is only ever replaced by an equal or newer
    version, so its VERSION is the floor: an old signed copy can't be replayed. Any failure
    keeps the installed copy.
    """
    installed = install_dir / "bootstrap.py"
    allowed_signers = install_dir / "allowed_signers"
    # Inside install_dir so os.replace stays on one filesystem and is atomic
    with tempfile.TemporaryDirectory(dir=install_dir) as tmp:
        script, sig = Path(tmp) / "bootstrap.py", Path(tmp) / "bootstrap.py.sig"
        try:
            download(f"{base_url}/bootstrap.py", script, timeout=5)
            download(f"{base_url}/bootstrap.py.sig", sig, timeout=5)
        except OSError as e:
            log(f"update: fetch failed ({e}), running the installed copy")
            return
        source = script.read_bytes()
        if source == installed.read_bytes() or not verify_signature(script, sig, allowed_signers):
            return
        version = script_version(source)
        if version is None or version < current_version:
            log(f"update: fetched version {version} is below {current_version}, refusing it")
            return
        os.replace(sig, installed.with_name("bootstrap.py.sig"))
        os.replace(script, installed)
    log(f"update: installed version {version}, re-running")
    os.execv(
        sys.executable,
        [sys.executable, "-I", str(installed), "session-start", "--no-update"],
    )


# --- session ------------------------------------------------------------------------------


def session_start(
    update: bool,
    *,
    harness: Harness = HARNESS,
    init_marker: Path = INIT_MARKER,
) -> None:
    """Prepares the session. Every step logs its failure and the session starts regardless.

    1. On resume, stop unless Tailscale needs rejoining: the VM kept everything else.
    2. Update to the newest signed copy of this script.
    3. Load secrets from the broker and 1Password.
    4. Configure git and GitHub, and write the session env.
    5. Join Tailscale and install the repo's mise tools in parallel.
    """
    log("== session-start")
    # Child processes must not trigger ~/.bashrc's session-start check on Antigravity
    os.environ.pop("BASH_ENV", None)
    os.environ["PATH"] = f"{tools_path()}:{os.environ.get('PATH', '')}"
    trust_repo()

    resuming = init_marker.exists()
    if resuming:
        if not tailscale_needs_rejoin():
            log("resume: state intact, nothing to do")
            return
        log("resume: tailscaled is gone, running the full start to rejoin")

    if update:
        self_update(harness.install_dir)
    log(f"running version {VERSION}")

    secrets, complete = load_secrets()
    tailscale_authkey = secrets.pop(TAILSCALE_ONLY, None)
    apps = github_apps(secrets)
    secrets.pop("GITHUB_APP_TOKENS", None)
    secrets.pop("BOTICORN_TOKENS", None)
    os.environ.update(secrets)

    set_git_identity(apps)
    name = os.environ.get("GIT_AUTHOR_NAME")
    email = os.environ.get("GIT_AUTHOR_EMAIL")
    identity = (name, email) if name and email else None
    harness.write_instructions(apps, identity, resuming=resuming)
    lines = harness.env_preamble()
    lines += [
        f"export {var}={shlex.quote(value)}"
        for var, value in secrets.items()
        if var not in SESSION_ENV_EXCLUDED
    ]
    lines += use_github_directly(
        apps,
        secrets.get("GITHUB_TOKEN"),
        bypass_proxy=harness.bypass_github_proxy,
        script=harness.script,
    )
    if resuming and not complete:
        # The session already has the env from its first start; rewriting would drop secrets
        log("env: secrets incomplete, keeping the env file from the last start")
    elif harness.write_env(lines) and complete:
        init_marker.touch()

    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [
            pool.submit(join_tailscale, tailscale_authkey, supported=harness.supports_tailscale),
            pool.submit(install_repo_tools),
        ]
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


# --- setup --------------------------------------------------------------------------------


def cmd_setup() -> int:
    signer = os.environ.get("SIGNER")
    min_version = os.environ.get("MIN_VERSION", "")
    if not signer or not min_version.isdigit():
        sys.exit("bootstrap: SIGNER or MIN_VERSION is missing, both must be set by stub.sh")
    if int(min_version) > VERSION:
        sys.exit(
            f"bootstrap: version {VERSION} is below MIN_VERSION {min_version}, refusing to run"
        )
    arch = platform.machine()
    if arch not in ("x86_64", "aarch64"):
        sys.exit(f"bootstrap: unsupported architecture {arch}")

    os.environ["PATH"] = f"{tools_path()}:{os.environ['PATH']}"
    with tempfile.TemporaryDirectory() as tmp:
        install_mise(Path(tmp), arch)
    install_global_tools()
    install_self(signer)
    HARNESS.setup()
    print(f"bootstrap: version {VERSION} complete")
    return 0


# --- cli ----------------------------------------------------------------------------------


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["setup"]:
        return cmd_setup()
    if args[:1] == ["session-start"]:
        return cmd_session_start(update="--no-update" not in args)
    if args[:1] == ["pre-tool-use"]:
        return cmd_pre_tool_use()
    if args[:1] == ["git-credential"]:
        return cmd_git_credential(args[1:])
    if args[:1] == ["gh"]:
        return cmd_gh(args[1:])
    sys.exit(
        "usage: bootstrap.py setup | session-start [--no-update] | pre-tool-use | git-credential | gh"
    )


if __name__ == "__main__":
    sys.exit(main())
