from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

# Raise to stop older signed copies from running. Installed copies only ever move forward.
VERSION = 2

BASE_URL = "https://raw.githubusercontent.com/dvcrn/claude-env/main"
SIGNING_NAMESPACE = "claude-env"

HOME = Path.home()
SHM_DIR = Path("/dev/shm")
ANTIGRAVITY = os.environ.get("ANTIGRAVITY_AGENT") == "1" or HOME.name == "antigravity"

LOG_FILE = Path("/tmp/session-start.log")
INIT_MARKER = SHM_DIR / ".cloud-env-initialized"


def tools_path(home: Path = HOME) -> str:
    return f"{home}/.local/bin:{home}/.local/share/mise/shims"


def hook_command(subcommand: str, script: Path) -> str:
    return f"{shlex.quote(sys.executable)} -I {shlex.quote(str(script))} {subcommand}"
