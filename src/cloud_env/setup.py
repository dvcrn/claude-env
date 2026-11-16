from __future__ import annotations

import os
import platform
import sys
import tempfile
from pathlib import Path

from cloud_env.apps.mise.mise import install_global_tools, install_mise
from cloud_env.config import VERSION, tools_path
from cloud_env.harness import HARNESS
from cloud_env.update import install_self


def cmd_setup() -> int:
    signer = os.environ.get("SIGNER")
    min_version = os.environ.get("MIN_VERSION", "")
    if not signer or not min_version.isdigit():
        sys.exit("bootstrap: SIGNER or MIN_VERSION is missing, both must be set by stub.sh")
    if int(min_version) > VERSION:
        sys.exit(
            f"bootstrap: version {VERSION} is below MIN_VERSION {min_version}, refusing to run"
        )
    arch = platform.machine()
    if arch not in ("x86_64", "aarch64"):
        sys.exit(f"bootstrap: unsupported architecture {arch}")

    os.environ["PATH"] = f"{tools_path()}:{os.environ['PATH']}"
    with tempfile.TemporaryDirectory() as tmp:
        install_mise(Path(tmp), arch)
    install_global_tools()
    install_self(signer)
    HARNESS.setup()
    print(f"bootstrap: version {VERSION} complete")
    return 0
