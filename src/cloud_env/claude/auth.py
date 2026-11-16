from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

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
