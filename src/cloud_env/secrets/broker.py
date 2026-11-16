from __future__ import annotations

import json
import os
import re
import urllib.request
from collections.abc import Mapping

from cloud_env.config import VERSION
from cloud_env.harness import HARNESS
from cloud_env.lib.log import log

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
