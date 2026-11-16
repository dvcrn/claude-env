from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

from cloud_env.config import HOME, tools_path
from cloud_env.harness import HARNESS
from cloud_env.lib.fs import load_asset
from cloud_env.lib.http import download
from cloud_env.lib.log import log, run

MISE_VERSION = "2026.10.17"
GLOBAL_TOOLS = load_asset(__file__, "global_tools.toml")
REPO_MISE_CONFIGS = ("mise.toml", ".mise.toml", ".config/mise.toml", ".tool-versions")


def install_mise(work: Path, arch: str, home: Path = HOME) -> None:
    dest = home / ".local" / "bin" / "mise"
    if dest.is_file():
        current = subprocess.run([str(dest), "--version"], capture_output=True, text=True)
        if current.returncode == 0 and MISE_VERSION in current.stdout:
            return
    name = f"mise-v{MISE_VERSION}-linux-{'x64' if arch == 'x86_64' else 'arm64'}.tar.gz"
    tarball = work / name
    # mise.jdx.dev mirrors the GitHub release and stays up when GitHub 504s anonymous downloads
    urls = [
        f"https://mise.jdx.dev/v{MISE_VERSION}/{name}",
        f"https://github.com/jdx/mise/releases/download/v{MISE_VERSION}/{name}",
    ]
    for attempt in range(3):
        url = urls[attempt % len(urls)]
        try:
            download(url, tarball)
            break
        except OSError as e:
            log(f"mise: {url} failed ({e})")
            if attempt == 2:
                raise
            time.sleep(5)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball) as tar, open(dest, "wb") as out:
        member = tar.extractfile("mise/bin/mise")
        assert member is not None
        shutil.copyfileobj(member, out)
    dest.chmod(0o755)
    if os.access("/etc/profile.d", os.W_OK):
        Path("/etc/profile.d/mise.sh").write_text(f'export PATH="{tools_path(home)}:$PATH"\n')


def install_global_tools(home: Path = HOME) -> None:
    config = home / ".config" / "mise" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(GLOBAL_TOOLS)
    subprocess.run(["mise", "install", "--yes"], cwd=home, check=True)


def repo_mise_projects(candidates: list[Path] | None = None) -> list[Path]:
    dirs = HARNESS.repo_dirs() if candidates is None else candidates
    return [repo for repo in dirs if any((repo / name).is_file() for name in REPO_MISE_CONFIGS)]


# The hook runs inside the repo, and every mise call there (including the op and tailscale
# shims) fails until the repo's mise config is trusted, so this runs before anything else.
def trust_repo() -> None:
    for project in repo_mise_projects():
        run(["mise", "trust"], "[mise] ", cwd=str(project))


def install_repo_tools() -> None:
    for project in repo_mise_projects():
        run(["mise", "install", "--yes"], "[mise] ", cwd=str(project))
