from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Protocol

from cloud_env.agy.harness import AgyHarness
from cloud_env.apps.github.prompts import GitHubApp
from cloud_env.claude.harness import ClaudeHarness
from cloud_env.config import HOME


class Harness(Protocol):
    install_dir: Path
    script: Path
    bypass_github_proxy: bool
    supports_tailscale: bool

    def setup(self) -> None: ...
    def session_context(self) -> contextlib.AbstractContextManager[None]: ...
    def broker_headers(self) -> dict[str, str]: ...
    def env_preamble(self) -> list[str]: ...
    def write_env(self, lines: list[str]) -> bool: ...
    def write_instructions(
        self,
        apps: dict[str, GitHubApp],
        identity: tuple[str, str] | None,
        *,
        resuming: bool,
    ) -> None: ...
    def repo_dirs(self) -> list[Path]: ...


ANTIGRAVITY = os.environ.get("ANTIGRAVITY_AGENT") == "1" or HOME.name == "antigravity"
HARNESS: Harness = AgyHarness() if ANTIGRAVITY else ClaudeHarness()
INSTALL_DIR = HARNESS.install_dir
SCRIPT = HARNESS.script
