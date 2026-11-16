from __future__ import annotations

import json

from cloud_env.lib.log import log


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
