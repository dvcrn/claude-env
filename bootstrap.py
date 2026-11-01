#!/usr/bin/env python3
"""Setup and SessionStart bootstrap for Claude Code cloud environments.

`setup` runs once from stub.sh after it verified this file's signature. It installs mise, the
global tools and a copy of this file as the SessionStart hook. Holds no secrets.

`session-start` runs from the hook on every session start and resume. It updates itself to the
newest signed copy, loads secrets from the broker and 1Password into $CLAUDE_ENV_FILE, and
configures git, GitHub, Tailscale and the repo's mise tools. Without the broker the session
starts without secrets. Logs to /tmp/session-start.log.

`pre-tool-use` runs from the PreToolUse hook. It strips the Claude Code footer from GitHub MCP
tool input, and denies Bash commands and file writes that contain it. Before Bash calls it also
refreshes the boticorn tokens when they near expiry.

`git-credential` is git's credential helper for github.com. It returns the boticorn token of the
remote's owner, or the default token.
"""

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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Raise to stop older signed copies from running. Installed copies only ever move forward.
VERSION = 1

BASE_URL = "https://raw.githubusercontent.com/dvcrn/claude-env/main"
BROKER_URL = os.environ.get("CLOUD_ENV_BROKER_URL", "https://cloud-env-broker.d.pn")
SIGNING_NAMESPACE = "claude-env"

MISE_VERSION = "2026.10.17"
GLOBAL_TOOLS = """\
[tools]
"aqua:1password/cli" = "2.41.1"
"aqua:tailscale/tailscale" = "1.104.2"
age = "1.3.2"
fnox = "1.42.0"
gh = "2.103.0"
trzsz-ssh = "0.1.26"
"""

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
INSTALL_DIR = CLAUDE_DIR / "hooks" / "claude-env"
SCRIPT = INSTALL_DIR / "bootstrap.py"
ALLOWED_SIGNERS = INSTALL_DIR / "allowed_signers"
TOOLS_PATH = f"{HOME}/.local/bin:{HOME}/.local/share/mise/shims"

LOG_FILE = Path("/tmp/session-start.log")
TAILSCALED_LOG = Path("/tmp/tailscaled.log")
TAILSCALED_SOCKET = Path("/var/run/tailscale/tailscaled.sock")
INIT_MARKER = Path("/dev/shm/.cloud-env-initialized")
TAILSCALE_MARKER = Path("/dev/shm/.cloud-env-tailscale")
INGRESS_TOKEN_FILE = Path(
    os.environ.get(
        "CLAUDE_SESSION_INGRESS_TOKEN_FILE",
        "/home/claude/.claude/remote/.session_ingress_token",
    )
)
GITHUB_HOSTS = (
    "api.github.com,github.com,codeload.github.com,objects.githubusercontent.com,"
    "raw.githubusercontent.com,uploads.github.com"
)
REPO_MISE_CONFIGS = ("mise.toml", ".mise.toml", ".config/mise.toml", ".tool-versions")

# One file per owner with that owner's boticorn token. Underscore names can't clash with
# owners: `_default` serves other owners, `_stamp` times the next refresh, `_lock` guards it.
TOKEN_DIR = Path("/dev/shm/cloud-env-github")
DEFAULT_TOKEN = "_default"
TOKEN_STAMP = TOKEN_DIR / "_stamp"
TOKEN_LOCK = TOKEN_DIR / "_lock"
# Installation tokens expire after an hour
TOKEN_REFRESH_AFTER = 50 * 60
TOKEN_RETRY_AFTER = 5 * 60
GITHUB_OWNER = re.compile(r"[a-z0-9][a-z0-9-]*")
BOTICORN_GITCONFIG = HOME / ".config" / "git" / "boticorn"
BOTICORN_NAME = "boticorn[bot]"
BOTICORN_EMAIL = "323198915+boticorn[bot]@users.noreply.github.com"
# Kept out of the secrets exported to the session: git identity variables override the per-owner
# identity in git config, and use_github_directly decides whether to export a GitHub token
SESSION_ENV_EXCLUDED = {
    "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
    "GITHUB_TOKEN", "GH_TOKEN", "BOTICORN_TOKENS",
}  # fmt: skip

ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")
# Never taken from the broker or 1Password: they would change how this script or the shell runs
RESERVED_NAMES = {
    "PATH", "HOME", "BASH_ENV", "ENV", "LD_PRELOAD", "LD_LIBRARY_PATH",
    "CLAUDE_ENV_FILE", "CLAUDE_PROJECT_DIR",
}  # fmt: skip
# Passed through to op, which needs the agent proxy to reach 1Password
OP_PASSTHROUGH = ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy", "SSL_CERT_FILE")
OP_TIMEOUT = 30
# TS_AUTHKEY only goes to `tailscale up`, never into the session env
TAILSCALE_ONLY = "TS_AUTHKEY"

# Claude Code footer on GitHub posts, including any --- separator above it
FOOTER = re.compile(
    r"\n*(?:-{3,}[ \t]*\n+)?(?:🤖 )?_?Generated (?:by|with) "
    r"\[Claude Code\]\(https://claude\.(?:ai/code|com/claude-code)[^)]*\)_?[ \t]*"
)
PRE_TOOL_USE_MATCHER = r"^(Bash|Write|Edit|MultiEdit|NotebookEdit|mcp__.*[Gg]it[Hh]ub.*)$"
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

ALLOWED_COMMANDS = (
    "Bash(op:*)",
    "Bash(jq:*)",
    "Bash(gh pr view:*)",
    "Bash(gh pr diff:*)",
    "Bash(gh pr checks:*)",
    "Bash(gh pr comment:*)",
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

_log_lock = threading.Lock()


# --- helpers --------------------------------------------------------------------------------


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
    assert proc.stdout
    for line in proc.stdout:
        log(line.rstrip("\n"), prefix)
    if code := proc.wait():
        log(f"{cmd[0]} exited with {code}", prefix)
    return code == 0


def download(url: str, dest: Path, timeout: float = 60) -> None:
    with urllib.request.urlopen(url, timeout=timeout) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)


# --- setup ----------------------------------------------------------------------------------


def install_mise(work: Path, arch: str) -> None:
    name = f"mise-v{MISE_VERSION}-linux-{'x64' if arch == 'x86_64' else 'arm64'}.tar.gz"
    tarball = work / name
    download(f"https://github.com/jdx/mise/releases/download/v{MISE_VERSION}/{name}", tarball)
    with tarfile.open(tarball) as tar:
        tar.extract("mise/bin/mise", work, filter="data")
    dest = HOME / ".local" / "bin" / "mise"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(work / "mise/bin/mise", dest)
    dest.chmod(0o755)
    Path("/etc/profile.d/mise.sh").write_text(f'export PATH="{TOOLS_PATH}:$PATH"\n')


def install_global_tools() -> None:
    config = HOME / ".config" / "mise" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(GLOBAL_TOOLS)
    subprocess.run(["mise", "install", "--yes"], cwd=HOME, check=True)


def install_self(signer: str) -> None:
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    ALLOWED_SIGNERS.write_text(f"claude-env {signer}\n")
    here = Path(__file__).resolve()
    shutil.copy(here, SCRIPT)
    shutil.copy(here.with_name("bootstrap.py.sig"), SCRIPT.with_name("bootstrap.py.sig"))


def hook_command(subcommand: str) -> str:
    return f"{shlex.quote(sys.executable)} -I {shlex.quote(str(SCRIPT))} {subcommand}"


def set_hook(hooks: dict, event: str, group: dict) -> None:
    """Replaces this script's hook group for event with group."""
    groups = hooks.setdefault(event, [])
    groups[:] = [
        existing
        for existing in groups
        if not any(str(SCRIPT) in hook.get("command", "") for hook in existing.get("hooks", []))
    ]
    groups.append(group)


# Merges into the existing settings so keys set by the image survive
def write_claude_settings() -> None:
    path = CLAUDE_DIR / "settings.json"
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
    deny += [rule for rule in PROXIED_GITHUB_WRITES if rule not in deny]

    auto_allow = settings.setdefault("autoMode", {}).setdefault("allow", [])
    auto_allow += [rule for rule in ("$defaults", AUTO_MODE_ALLOW) if rule not in auto_allow]

    hooks = settings.setdefault("hooks", {})
    session_start = {"type": "command", "command": hook_command("session-start"), "timeout": 600}
    set_hook(hooks, "SessionStart", {"hooks": [session_start]})
    pre_tool_use = {"type": "command", "command": hook_command("pre-tool-use"), "timeout": 10}
    set_hook(hooks, "PreToolUse", {"matcher": PRE_TOOL_USE_MATCHER, "hooks": [pre_tool_use]})

    path.write_text(json.dumps(settings, indent=2) + "\n")


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

    os.environ["PATH"] = f"{TOOLS_PATH}:{os.environ['PATH']}"
    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        install_mise(Path(tmp), arch)
    install_global_tools()
    install_self(signer)
    write_claude_settings()
    (CLAUDE_DIR / "CLAUDE.md").write_text(render_claude_md([], None, None))
    print(f"bootstrap: version {VERSION} complete")
    return 0


# --- session-start: self-update -------------------------------------------------------------


def script_version(source: bytes) -> int | None:
    match = re.search(rb"^VERSION = (\d+)$", source, re.MULTILINE)
    return int(match.group(1)) if match else None


def verify_signature(script: Path, sig: Path) -> bool:
    cmd = [
        "ssh-keygen", "-Y", "verify", "-f", str(ALLOWED_SIGNERS),
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


def self_update() -> None:
    """Replaces the installed copy with a newer signed one from the repo and re-runs it.

    Only the installed copy runs as the hook and it is only ever replaced by an equal or newer
    version, so its VERSION is the floor: an old signed copy can't be replayed. Any failure
    keeps the installed copy.
    """
    # Inside INSTALL_DIR so os.replace stays on one filesystem and is atomic
    with tempfile.TemporaryDirectory(dir=INSTALL_DIR) as tmp:
        script, sig = Path(tmp) / "bootstrap.py", Path(tmp) / "bootstrap.py.sig"
        try:
            download(f"{BASE_URL}/bootstrap.py", script, timeout=5)
            download(f"{BASE_URL}/bootstrap.py.sig", sig, timeout=5)
        except OSError as e:
            log(f"update: fetch failed ({e}), running the installed copy")
            return
        source = script.read_bytes()
        if source == SCRIPT.read_bytes() or not verify_signature(script, sig):
            return
        version = script_version(source)
        if version is None or version < VERSION:
            log(f"update: fetched version {version} is below {VERSION}, refusing it")
            return
        os.replace(sig, SCRIPT.with_name("bootstrap.py.sig"))
        os.replace(script, SCRIPT)
    log(f"update: installed version {version}, re-running")
    os.execv(
        sys.executable,
        [sys.executable, "-I", str(SCRIPT), "session-start", "--no-update"],
    )


# --- session-start: secrets -----------------------------------------------------------------


def ingress_sig_sample() -> str | None:
    """Returns <kid>.<sha256 of header.payload>.<signature> for the session ingress token.

    The payload itself never leaves the container, so the token can't be rebuilt from this.
    """
    try:
        token = INGRESS_TOKEN_FILE.read_text().strip().removeprefix("sk-ant-si-")
        header, payload, signature = token.split(".")
        padded = header + "=" * (-len(header) % 4)
        kid = json.loads(base64.urlsafe_b64decode(padded))["kid"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    digest = hashlib.sha256(f"{header}.{payload}".encode()).hexdigest()
    return f"{kid}.{digest}.{signature}"


def fetch_broker_env(timeout: float = 20) -> dict[str, str] | None:
    headers = {
        # Cloudflare's Browser Integrity Check rejects urllib's default User-Agent (error 1010)
        "User-Agent": f"claude-env-bootstrap/{VERSION}",
        "X-Session-Id": os.environ.get("CLAUDE_CODE_REMOTE_SESSION_ID", ""),
    }
    if sample := ingress_sig_sample():
        headers["X-Ingress-Sig-Sample"] = sample
    request = urllib.request.Request(f"{BROKER_URL}/v1/session-env", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            env = json.load(resp)["env"]
        if not isinstance(env, dict):
            raise TypeError
    except OSError as e:
        log(f"broker: request to {BROKER_URL} failed ({e})")
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


# --- session-start: git, GitHub and the session env -----------------------------------------


# The harness writes user.name=Claude into ~/.gitconfig. The user's identity from 1Password is
# the global default, and repos with a boticorn token commit as boticorn instead.
def set_git_identity(owners: list[str]) -> None:
    name, email = os.environ.get("GIT_AUTHOR_NAME"), os.environ.get("GIT_AUTHOR_EMAIL")
    if name and email:
        run(["git", "config", "--global", "user.name", name], "[git] ")
        run(["git", "config", "--global", "user.email", email], "[git] ")
    else:
        log("git: no GIT_AUTHOR_NAME/GIT_AUTHOR_EMAIL, keeping the existing identity")
    # The harness signing key is registered to noreply@anthropic.com, and while signing is on its
    # Stop hook asks for every other committer's commits to be rewritten to that address
    run(["git", "config", "--global", "commit.gpgsign", "false"], "[git] ")

    if not owners:
        log("git: no boticorn tokens, keeping the boticorn identity config from earlier starts")
        return
    BOTICORN_GITCONFIG.parent.mkdir(parents=True, exist_ok=True)
    BOTICORN_GITCONFIG.write_text(f"[user]\n\tname = {BOTICORN_NAME}\n\temail = {BOTICORN_EMAIL}\n")
    # Drops the includes of earlier starts, so an owner the broker no longer mints for stops matching
    found = subprocess.run(
        ["git", "config", "--global", "--get-regexp", r"^includeif\..*\.path$"],
        capture_output=True,
        text=True,
    )
    for line in found.stdout.splitlines():
        key, _, value = line.partition(" ")
        if value == str(BOTICORN_GITCONFIG):
            subprocess.run(["git", "config", "--global", "--unset-all", key], capture_output=True)
    # `**/<owner>/**` also matches the agent proxy's http://127.0.0.1:<port>/git/<owner>/<repo>.
    # git only supports `remote.*.url` here, so any remote on a boticorn owner counts, upstream too.
    for owner in owners:
        for pattern in (f"**/{owner}/**", f"git@github.com:{owner}/**"):
            key = f"includeIf.hasconfig:remote.*.url:{pattern}.path"
            run(["git", "config", "--global", key, str(BOTICORN_GITCONFIG)], "[git] ")


def boticorn_tokens(raw: str | None) -> dict[str, str]:
    """Parses the broker's BOTICORN_TOKENS, a JSON object of owner to installation token."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        log("github: BOTICORN_TOKENS is not JSON")
        return {}
    if not isinstance(parsed, dict):
        log("github: BOTICORN_TOKENS is not an object")
        return {}
    tokens = {}
    for owner, token in parsed.items():
        valid_owner = isinstance(owner, str) and GITHUB_OWNER.fullmatch(owner.lower())
        if valid_owner and isinstance(token, str) and token:
            tokens[owner.lower()] = token
            continue
        log(f"github: skipping invalid BOTICORN_TOKENS entry {owner!r}")
    return tokens


def repo_owner() -> str | None:
    """Returns the lowercased owner of the session repo's origin remote."""
    if not (project := os.environ.get("CLAUDE_PROJECT_DIR")):
        return None
    url = subprocess.run(
        ["git", "-C", project, "remote", "get-url", "origin"], capture_output=True, text=True
    ).stdout.strip()
    match = re.search(r"[/:]([^/:]+)/[^/]+?(?:\.git)?/?$", url)
    owner = match.group(1).lower() if match else ""
    return owner if GITHUB_OWNER.fullmatch(owner) else None


def gh_owner(tokens: dict[str, str], default: str | None) -> str | None:
    """Returns the owner whose token install_github_tokens logs gh in with."""
    owner = repo_owner()
    if owner in tokens:
        return owner
    return next((name for name, token in tokens.items() if token == default), None)


def write_token(name: str, token: str) -> None:
    tmp = TOKEN_DIR / f".{name}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    os.replace(tmp, TOKEN_DIR / name)


def install_github_tokens(
    tokens: dict[str, str], default: str | None, login_timeout: float
) -> bool:
    """Writes the token files and logs gh in with the token for the session repo's owner, or the
    default token. Returns whether gh is logged in.

    Tokens of owners missing from tokens are kept: they still work until they expire.
    """
    TOKEN_DIR.mkdir(mode=0o700, exist_ok=True)
    TOKEN_DIR.chmod(0o700)
    for owner, token in tokens.items():
        write_token(owner, token)
    if default:
        write_token(DEFAULT_TOKEN, default)
    if tokens:
        TOKEN_STAMP.touch()

    owner = repo_owner()
    token = tokens.get(owner or "") or default
    if not token:
        return False
    env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
    env["PATH"] = f"{TOOLS_PATH}:{env.get('PATH', '')}"
    cmd = ["gh", "auth", "login", "--with-token", "--insecure-storage", "--hostname", "github.com"]
    try:
        result = subprocess.run(
            cmd, input=token, env=env, capture_output=True, text=True, timeout=login_timeout
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"github: gh auth login failed ({e})")
        return False
    if result.returncode != 0:
        log(f"github: gh auth login failed: {result.stderr.strip()}")
        return False
    log(
        f"github: gh uses the {'boticorn token for ' + owner if owner in tokens else 'default token'}"
    )
    return True


def bypass_proxy_for_github() -> None:
    for var in ("NO_PROXY", "no_proxy"):
        os.environ[var] = ",".join(filter(None, [os.environ.get(var), GITHUB_HOSTS]))


# The agent proxy swaps GitHub credentials for a placeholder and only allows the session's repos.
# Sending GitHub traffic direct lets gh, git and mise reach any repo. git picks the token file
# by the repo's owner, and gh uses its login, falling back to the exported default token when the
# login fails. Returns the env file lines.
def use_github_directly(tokens: dict[str, str], default: str | None) -> list[str]:
    if not tokens and not default:
        log("github: no GitHub token, leaving GitHub traffic on the agent proxy")
        return []
    bypass_proxy_for_github()
    # The first gh call can make mise install gh, and session start isn't time sensitive
    logged_in = install_github_tokens(tokens, default, login_timeout=60)
    # The empty helper drops ones configured earlier
    key = "credential.https://github.com.helper"
    subprocess.run(["git", "config", "--global", "--unset-all", key], capture_output=True)
    run(["git", "config", "--global", "--add", key, ""], "[git] ")
    run(["git", "config", "--global", "--add", key, "!" + hook_command("git-credential")], "[git] ")
    run(
        ["git", "config", "--global", "credential.https://github.com.useHttpPath", "true"], "[git] "
    )
    lines = [
        f'export NO_PROXY="${{NO_PROXY:+$NO_PROXY,}}{GITHUB_HOSTS}"',
        f'export no_proxy="${{no_proxy:+$no_proxy,}}{GITHUB_HOSTS}"',
    ]
    if logged_in:
        # gh prefers these over its login, which the refresh keeps current
        return [*lines, "unset GH_TOKEN GITHUB_TOKEN"]
    if not default:
        return lines
    # Without a login the token can't be refreshed, so a boticorn one stops working after an hour
    return [
        *lines,
        f"export GITHUB_TOKEN={shlex.quote(default)}",
        'export GH_TOKEN="$GITHUB_TOKEN"',
    ]


def refresh_github_tokens() -> None:
    """Re-mints the boticorn tokens before they expire. Runs before every Bash call, so the stamp
    file limits it to one attempt per TOKEN_REFRESH_AFTER, or TOKEN_RETRY_AFTER after a failure.
    """
    try:
        if time.time() - TOKEN_STAMP.stat().st_mtime < TOKEN_REFRESH_AFTER:
            return
    except FileNotFoundError:
        return  # the session has no boticorn tokens
    with open(TOKEN_LOCK, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return  # a parallel Bash call is refreshing
        if time.time() - TOKEN_STAMP.stat().st_mtime < TOKEN_REFRESH_AFTER:
            return
        # Schedules the retry first: urlopen's timeout doesn't bound DNS or the whole response,
        # and if the hook gets killed at its 10s limit, the next attempt still waits
        retry_at = time.time() - TOKEN_REFRESH_AFTER + TOKEN_RETRY_AFTER
        os.utime(TOKEN_STAMP, (retry_at, retry_at))
        env = fetch_broker_env(timeout=4) or {}
        tokens = boticorn_tokens(env.get("BOTICORN_TOKENS"))
        if not tokens:
            log("github: token refresh failed, keeping the current tokens")
            return
        bypass_proxy_for_github()
        # The hook's 10s timeout bounds the broker call and the login together
        if not install_github_tokens(tokens, env.get("GITHUB_TOKEN"), login_timeout=3):
            # gh still holds the old token, so try again before it expires
            os.utime(TOKEN_STAMP, (retry_at, retry_at))
        log(f"github: refreshed tokens for {' '.join(sorted(tokens))}")


def cmd_git_credential(args: list[str]) -> int:
    """git credential helper: answers `get` with the token for the remote's owner."""
    if args[:1] != ["get"]:
        return 0
    fields = dict(line.partition("=")[::2] for line in sys.stdin.read().splitlines())
    owner = fields.get("path", "").lstrip("/").split("/", 1)[0].lower()
    names = [owner] if GITHUB_OWNER.fullmatch(owner) else []
    for name in [*names, DEFAULT_TOKEN]:
        try:
            token = (TOKEN_DIR / name).read_text().strip()
        except OSError:
            continue
        print(f"username=x-access-token\npassword={token}")
        return 0
    return 0


def write_env_block(lines: list[str]) -> bool:
    """Replaces this script's marked block in $CLAUDE_ENV_FILE."""
    if not (env_file := os.environ.get("CLAUDE_ENV_FILE")):
        log("env: CLAUDE_ENV_FILE is not set, the session won't see the loaded variables")
        return False
    path = Path(env_file)
    existing = path.read_text() if path.exists() else ""
    kept = re.sub(r"(?ms)^# >>> cloud-env\n.*?^# <<< cloud-env(\n|\Z)", "", existing)
    if kept and not kept.endswith("\n"):
        kept += "\n"
    path.touch(mode=0o600)
    path.chmod(0o600)  # before writing, so secrets never sit in a readable file
    path.write_text(kept + "\n".join(["# >>> cloud-env", *lines, "# <<< cloud-env"]) + "\n")
    return True


# --- session-start: Tailscale and repo tools ------------------------------------------------


def tailscaled_running() -> bool:
    return subprocess.run(["pgrep", "-x", "tailscaled"], capture_output=True).returncode == 0


def tailscale_needs_rejoin() -> bool:
    """Whether this VM joined Tailscale earlier and tailscaled has since died."""
    return TAILSCALE_MARKER.exists() and not tailscaled_running()


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


def join_tailscale(authkey: str | None) -> None:
    prefix = "[tailscale] "
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
    up = ["tailscale", "up", f"--auth-key={authkey}", f"--hostname={hostname}", "--timeout=60s"]
    if run(up, prefix):
        run(["tailscale", "status", "--peers=false"], prefix)
        TAILSCALE_MARKER.touch()


def repo_mise_project() -> str | None:
    project = os.environ.get("CLAUDE_PROJECT_DIR")
    if not project or not any((Path(project) / name).is_file() for name in REPO_MISE_CONFIGS):
        return None
    return project


# The hook runs inside the repo, and every mise call there (including the op and tailscale
# shims) fails until the repo's mise config is trusted, so this runs before anything else.
def trust_repo() -> None:
    if project := repo_mise_project():
        run(["mise", "trust"], "[mise] ", cwd=project)


def install_repo_tools() -> None:
    if project := repo_mise_project():
        run(["mise", "install", "--yes"], "[mise] ", cwd=project)


# --- session-start --------------------------------------------------------------------------


def session_start(update: bool) -> None:
    """Prepares the session. Every step logs its failure and the session starts regardless.

    1. On resume, stop unless Tailscale needs rejoining: the VM kept everything else.
    2. Update to the newest signed copy of this script.
    3. Load secrets from the broker and 1Password.
    4. Configure git and GitHub, and write the session env to $CLAUDE_ENV_FILE.
    5. Join Tailscale and install the repo's mise tools in parallel.
    """
    log("== session-start")
    os.environ["PATH"] = f"{TOOLS_PATH}:{os.environ.get('PATH', '')}"
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

    secrets, complete = load_secrets()
    tailscale_authkey = secrets.pop(TAILSCALE_ONLY, None)
    tokens = boticorn_tokens(secrets.pop("BOTICORN_TOKENS", None))
    os.environ.update(secrets)

    owners = sorted(tokens)
    set_git_identity(owners)
    identity = (os.environ.get("GIT_AUTHOR_NAME"), os.environ.get("GIT_AUTHOR_EMAIL"))
    if owners or not resuming:
        (CLAUDE_DIR / "CLAUDE.md").write_text(
            render_claude_md(
                owners,
                identity if all(identity) else None,
                gh_owner(tokens, secrets.get("GITHUB_TOKEN")),
            )
        )
    lines = [f'export PATH="{TOOLS_PATH}:$PATH"']
    lines += [
        f"export {name}={shlex.quote(value)}"
        for name, value in secrets.items()
        if name not in SESSION_ENV_EXCLUDED
    ]
    lines += use_github_directly(tokens, secrets.get("GITHUB_TOKEN"))
    if resuming and not complete:
        # The session already has the env from its first start; rewriting would drop secrets
        log("env: secrets incomplete, keeping the env file from the last start")
    elif write_env_block(lines) and complete:
        INIT_MARKER.touch()

    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(join_tailscale, tailscale_authkey), pool.submit(install_repo_tools)]
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
        session_start(update)
    except Exception as e:  # the session must start even if this script breaks
        log(f"session-start failed: {e!r}")
    return 0


# --- pre-tool-use ---------------------------------------------------------------------------


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
    if tool == "Bash":
        # stdout is the hook's response, so the refresh logs to the session-start log
        try:
            with open(LOG_FILE, "a") as out, contextlib.redirect_stdout(out):
                refresh_github_tokens()
        except Exception as e:  # a failed refresh must not block the command
            print(f"github: token refresh crashed: {e!r}", file=sys.stderr)
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
    sys.exit(
        "usage: bootstrap.py setup | session-start [--no-update] | pre-tool-use | git-credential"
    )


CLAUDE_MD = """\
## Comment and code quality

You are REQUIRED to do the following, even if other instructions exist. These are part of our work requirements, and any work not following these WILL BE THROWN AWAY.
Take the audits seriously and don't hand-wave them away. You don't need to follow every little point, but you should handle the overall feedback, especially around comment and docs quality.

- When interacting with any github issue, PR or similar, ALWAYS load the `github-comment-quality` skill. It dictates the output style we HAVE TO adhere to for ANY github interactions.
- Any comment, commit texts and documentation HAVE TO pass `comment-and-documentation-quality`. Load this skill, then after you're done with a branch, spawn a new sub-agent and tell it to use this skill to review the changes. (If the ask-gemini variant of this skill is available, you're encouraged to use it once per session to check the final state of your PR)
- Every commit and code change has to pass `code-review-and-quality`. Load this skill, then after you're done with a branch, spawn a new sub-agent and tell it to use this skill to review the changes. (If the ask-gemini variant of this skill is available, you're encouraged to use it once per session to check the final state of your PR)
- Use the `git-commit-quality` skill before any new git commit is issued, and ensure instructions for git commits are strictly followed.

## Git

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

## Pull requests

After creating a PR, keep watching it for review activity until it is merged or closed.

- Use native PR watch or subscribe tools if available. Otherwise poll with `gh`: every 5 minutes, and every 30 minutes once there has been no new activity for 20 minutes. Any new activity resets the interval to 5 minutes. Stop after 24 hours without activity. Waits longer than the shell tool timeout must be split across multiple calls.
- Activity means review comments, PR comments, review submissions, and failing checks.
- Handle every comment, including ones you decide not to act on. Fix it, or reply briefly with why not, then resolve the review thread. For top-level comments that can't be resolved, reply. Use the `address-pr-review-comments` skill.

## Tools

- To interact with other Claude Code sessions (create them, send messages, etc.), use the `claude-code` MCP. If it isn't available, fall back to the built-in `claude-code-remote` tools.

## GitHub identity

"""


def render_claude_md(
    owners: list[str], identity: tuple[str, str] | None, gh_token_owner: str | None
) -> str:
    """Returns the global CLAUDE.md, with the GitHub identity rules for the boticorn owners."""
    if not owners:
        return CLAUDE_MD + (
            "- Use `gh` for GitHub. To create issues, PRs or comments where its token can't "
            "write, use the `github` tools of the `dpn` MCP.\n"
        )
    names = ", ".join(f"`{owner}`" for owner in owners)
    gh_token = f"the `{gh_token_owner}` token" if gh_token_owner else "the default token"
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
    return (
        CLAUDE_MD
        + f"""\
- On repos owned by {names}, `gh` and `git` act as the `boticorn[bot]` GitHub App. Commits there are authored as `boticorn[bot]` automatically (`git config user.name` shows it). {co_author}
- `gh` is logged in with {gh_token}. For another of those owners, pass its token: `GH_TOKEN="$(cat {TOKEN_DIR}/<owner>)" gh ...`. Tokens refresh on their own.
- On repos of any other owner, create issues, PRs and comments with the `github` tools of the `dpn` MCP, which act as {user}. Commits there use {user}'s identity.
- When the user asks to act "as me" on one of the boticorn repos, post through the `dpn` MCP too, and {as_me}.
"""
    )


if __name__ == "__main__":
    sys.exit(main())
