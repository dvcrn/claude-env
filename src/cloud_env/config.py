from __future__ import annotations

from pathlib import Path

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
