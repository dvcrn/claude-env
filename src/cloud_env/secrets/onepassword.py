from __future__ import annotations

import os
import subprocess

from cloud_env.config import HOME
from cloud_env.lib.log import log
from cloud_env.secrets.broker import ENV_NAME, RESERVED_NAMES

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
