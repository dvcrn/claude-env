#!/usr/bin/env python3
"""Setup and SessionStart bootstrap for Claude Code cloud environments.

`setup` runs once from stub.sh after it verified this file's signature. It installs mise, the
global tools and a copy of this file as the SessionStart hook. Holds no secrets.

`session-start` runs from the hook on every session start and resume. It updates itself to the
newest signed copy, loads secrets from the broker and 1Password into $CLAUDE_ENV_FILE, and
configures git, GitHub, Tailscale and the repo's mise tools. Without the broker the session
starts without secrets. Logs to /tmp/session-start.log.
"""

from __future__ import annotations

import base64
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


def hook_command() -> str:
    return f"{shlex.quote(sys.executable)} -I {shlex.quote(str(SCRIPT))} session-start"


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
    allow = settings.setdefault("permissions", {}).setdefault("allow", [])
    allow += [rule for rule in ("Bash(op:*)", "Bash(jq:*)") if rule not in allow]

    session_start = settings.setdefault("hooks", {}).setdefault("SessionStart", [])
    session_start[:] = [
        group
        for group in session_start
        if not any(str(SCRIPT) in hook.get("command", "") for hook in group.get("hooks", []))
    ]
    session_start.append(
        {"hooks": [{"type": "command", "command": hook_command(), "timeout": 600}]}
    )

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
    (CLAUDE_DIR / "CLAUDE.md").write_text(CLAUDE_MD)
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


def fetch_broker_env() -> dict[str, str] | None:
    headers = {"X-Session-Id": os.environ.get("CLAUDE_CODE_REMOTE_SESSION_ID", "")}
    if sample := ingress_sig_sample():
        headers["X-Ingress-Sig-Sample"] = sample
    request = urllib.request.Request(f"{BROKER_URL}/v1/session-env", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as resp:
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


# The harness writes user.name=Claude into ~/.gitconfig. GIT_AUTHOR_* only reach the Bash tool,
# so the identity from 1Password goes into git config too for anything else that commits.
def set_git_identity() -> None:
    name, email = os.environ.get("GIT_AUTHOR_NAME"), os.environ.get("GIT_AUTHOR_EMAIL")
    if not name or not email:
        log("git: no GIT_AUTHOR_NAME/GIT_AUTHOR_EMAIL, keeping the existing identity")
        return
    run(["git", "config", "--global", "user.name", name], "[git] ")
    run(["git", "config", "--global", "user.email", email], "[git] ")


# The agent proxy swaps GitHub credentials for a placeholder and only allows the session's repos.
# Sending GitHub traffic direct with the real GITHUB_TOKEN lets gh, git and mise reach any repo.
# Returns the env file lines that do the same for the session.
def use_github_directly() -> list[str]:
    if not os.environ.get("GITHUB_TOKEN"):
        log("github: no GITHUB_TOKEN, leaving GitHub traffic on the agent proxy")
        return []
    for var in ("NO_PROXY", "no_proxy"):
        os.environ[var] = ",".join(filter(None, [os.environ.get(var), GITHUB_HOSTS]))
    os.environ["GH_TOKEN"] = os.environ["GITHUB_TOKEN"]
    # The empty helper drops ones configured earlier; ours reads the token at run time, so it
    # never hits disk
    key = "credential.https://github.com.helper"
    subprocess.run(["git", "config", "--global", "--unset-all", key], capture_output=True)
    run(["git", "config", "--global", "--add", key, ""], "[git] ")
    helper = '!f() { echo username=x-access-token; echo "password=$GITHUB_TOKEN"; }; f'
    run(["git", "config", "--global", "--add", key, helper], "[git] ")
    return [
        f'export NO_PROXY="${{NO_PROXY:+$NO_PROXY,}}{GITHUB_HOSTS}"',
        f'export no_proxy="${{no_proxy:+$no_proxy,}}{GITHUB_HOSTS}"',
        'export GH_TOKEN="$GITHUB_TOKEN"',
    ]


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


def install_repo_tools() -> None:
    project = os.environ.get("CLAUDE_PROJECT_DIR")
    if not project or not any((Path(project) / name).is_file() for name in REPO_MISE_CONFIGS):
        return
    prefix = "[mise] "
    if run(["mise", "trust"], prefix, cwd=project):
        run(["mise", "install", "--yes"], prefix, cwd=project)


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
    os.environ.update(secrets)

    set_git_identity()
    lines = [f'export PATH="{TOOLS_PATH}:$PATH"']
    lines += [f"export {name}={shlex.quote(value)}" for name, value in secrets.items()]
    lines += use_github_directly()
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


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["setup"]:
        return cmd_setup()
    if args[:1] == ["session-start"]:
        return cmd_session_start(update="--no-update" not in args)
    sys.exit("usage: bootstrap.py setup | session-start [--no-update]")


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
as a bug and strip it.

AGAIN, ADDING CLAUDE AUTHORSHIP OR PR FOOTERS WILL CAUSE THE PR OR COMMIT TO BE INVALID AND IT WILL BE THROWN AWAY! DO NOT ADD CLAUDE ATTRIBUTION TO ANY COMMITS OR PRS!!
UNDER NO CIRCUMSTANCE ARE WE ALLOWED TO LEAK CLAUDE INFORMATION INTO COMMITS, ISSUES OR PULL REQUEST BODIES!
"""


if __name__ == "__main__":
    sys.exit(main())
