from __future__ import annotations

import json
from pathlib import Path

from cloud_env.lib.log import log
from cloud_env.lib.parse import clean_text


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
