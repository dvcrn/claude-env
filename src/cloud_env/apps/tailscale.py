from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from cloud_env.config import SHM_DIR
from cloud_env.harness import HARNESS
from cloud_env.lib.log import log, run

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
