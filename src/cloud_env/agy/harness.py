from __future__ import annotations

import contextlib
import fcntl
from collections.abc import Iterator
from pathlib import Path

from cloud_env.agy.bashrc import write_antigravity_bashrc
from cloud_env.agy.instructions import render_gemini_md, write_gemini_md
from cloud_env.apps.github.tokens import GitHubApp
from cloud_env.config import HOME, SHM_DIR
from cloud_env.lib.fs import atomic_write


class AgyHarness:
    bypass_github_proxy = False
    supports_tailscale = False

    def __init__(self, home: Path = HOME, shm_dir: Path = SHM_DIR) -> None:
        self.home = home
        self.install_dir = home / ".local" / "share" / "cloud-env"
        self.script = self.install_dir / "bootstrap.py"
        self.env_file = shm_dir / "cloud-env-session.sh"
        self.attempt_marker = shm_dir / ".cloud-env-attempted"
        self.session_lock = shm_dir / ".cloud-env-session.lock"
        self.gemini_md = home / ".gemini" / "GEMINI.md"

    def setup(self) -> None:
        write_antigravity_bashrc(self.home, self.script, self.attempt_marker, self.env_file)
        if not self.gemini_md.exists():
            write_gemini_md(render_gemini_md({}, None), self.gemini_md)

    @contextlib.contextmanager
    def session_context(self) -> Iterator[None]:
        self.session_lock.parent.mkdir(parents=True, exist_ok=True)
        with open(self.session_lock, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                self.attempt_marker.touch()

    def env_preamble(self) -> list[str]:
        return []

    def write_env(self, lines: list[str]) -> bool:
        atomic_write(self.env_file, "\n".join(lines) + "\n", mode=0o600)
        return True

    def write_instructions(
        self,
        apps: dict[str, GitHubApp],
        identity: tuple[str, str] | None,
        *,
        resuming: bool,
    ) -> None:
        if apps or not self.gemini_md.exists():
            write_gemini_md(render_gemini_md(apps, identity), self.gemini_md)

    def repo_dirs(self) -> list[Path]:
        return [
            git_dir.parent for git_dir in sorted(self.home.glob("*/*/.git")) if git_dir.is_dir()
        ]
