from __future__ import annotations

import shutil
import urllib.request
from pathlib import Path


def download(url: str, dest: Path, timeout: float = 60) -> None:
    # Cloudflare (in front of mise.jdx.dev) rejects urllib's default User-Agent with 403
    request = urllib.request.Request(url, headers={"User-Agent": "claude-env-bootstrap"})
    with urllib.request.urlopen(request, timeout=timeout) as resp, open(dest, "wb") as f:
        shutil.copyfileobj(resp, f)
