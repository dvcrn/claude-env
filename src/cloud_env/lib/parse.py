from __future__ import annotations


def clean_text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and not any(ord(c) < 0x20 for c in value)
