from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path


def load_asset(anchor_file: str, name: str) -> str:
    return Path(anchor_file).with_name(name).read_text(encoding="utf-8")


def atomic_write(path: Path, content: str, *, mode: int | None = None) -> None:
    """Writes content to a sibling temp file and replaces path atomically.

    Leaves permissions untouched when mode is None so writes to virtiofs mounts
    (where chmod raises EPERM) still succeed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        if mode is not None:
            tmp.chmod(mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def replace_marked_block(existing: str, lines: list[str]) -> str:
    kept = re.sub(r"(?ms)^# >>> cloud-env\n.*?^# <<< cloud-env(\n|\Z)", "", existing)
    if kept and not kept.endswith("\n"):
        kept += "\n"
    return kept + "\n".join(["# >>> cloud-env", *lines, "# <<< cloud-env"]) + "\n"
