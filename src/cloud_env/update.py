from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from cloud_env.config import BASE_URL, SIGNING_NAMESPACE, VERSION
from cloud_env.harness import INSTALL_DIR
from cloud_env.lib.http import download
from cloud_env.lib.log import log


def install_self(
    signer: str,
    *,
    source_script: Path | None = None,
    install_dir: Path = INSTALL_DIR,
) -> None:
    install_dir.mkdir(parents=True, exist_ok=True)
    (install_dir / "allowed_signers").write_text(f"claude-env {signer}\n")
    here = (source_script or Path(__file__)).resolve()
    dest = install_dir / "bootstrap.py"
    shutil.copy(here, dest)
    shutil.copy(here.with_name("bootstrap.py.sig"), dest.with_name("bootstrap.py.sig"))


def script_version(source: bytes) -> int | None:
    match = re.search(rb"^VERSION = (\d+)$", source, re.MULTILINE)
    return int(match.group(1)) if match else None


def verify_signature(script: Path, sig: Path, allowed_signers: Path) -> bool:
    cmd = [
        "ssh-keygen", "-Y", "verify", "-f", str(allowed_signers),
        "-I", "claude-env", "-n", SIGNING_NAMESPACE, "-s", str(sig),
    ]  # fmt: skip
    try:
        with open(script, "rb") as f:
            verify_result = subprocess.run(cmd, stdin=f, capture_output=True, text=True)
    except OSError as e:
        log(f"update: can't run ssh-keygen ({e})")
        return False
    if verify_result.returncode != 0:
        log(f"update: signature check failed: {verify_result.stderr.strip()}")
    return verify_result.returncode == 0


def self_update(
    install_dir: Path = INSTALL_DIR,
    *,
    base_url: str = BASE_URL,
    current_version: int = VERSION,
) -> None:
    """Replaces the installed copy with a newer signed one from the repo and re-runs it.

    Only the installed copy runs as the hook and it is only ever replaced by an equal or newer
    version, so its VERSION is the floor: an old signed copy can't be replayed. Any failure
    keeps the installed copy.
    """
    installed = install_dir / "bootstrap.py"
    allowed_signers = install_dir / "allowed_signers"
    # Inside install_dir so os.replace stays on one filesystem and is atomic
    with tempfile.TemporaryDirectory(dir=install_dir) as tmp:
        script, sig = Path(tmp) / "bootstrap.py", Path(tmp) / "bootstrap.py.sig"
        try:
            download(f"{base_url}/bootstrap.py", script, timeout=5)
            download(f"{base_url}/bootstrap.py.sig", sig, timeout=5)
        except OSError as e:
            log(f"update: fetch failed ({e}), running the installed copy")
            return
        source = script.read_bytes()
        if source == installed.read_bytes() or not verify_signature(script, sig, allowed_signers):
            return
        version = script_version(source)
        if version is None or version < current_version:
            log(f"update: fetched version {version} is below {current_version}, refusing it")
            return
        os.replace(sig, installed.with_name("bootstrap.py.sig"))
        os.replace(script, installed)
    log(f"update: installed version {version}, re-running")
    os.execv(
        sys.executable,
        [sys.executable, "-I", str(installed), "session-start", "--no-update"],
    )
