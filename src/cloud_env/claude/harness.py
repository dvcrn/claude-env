from __future__ import annotations

import contextlib
import os
from pathlib import Path

from cloud_env.apps.github.tokens import GitHubApp
from cloud_env.claude.instructions import render_claude_md
from cloud_env.claude.settings import write_claude_settings
from cloud_env.config import HOME, tools_path
from cloud_env.lib.fs import replace_marked_block
from cloud_env.lib.log import log


class ClaudeHarness:
    bypass_github_proxy = True
    supports_tailscale = True

    def __init__(self, home: Path = HOME) -> None:
        self.home = home
        self.claude_dir = home / ".claude"
        self.install_dir = self.claude_dir / "hooks" / "claude-env"
        self.script = self.install_dir / "bootstrap.py"

    def setup(self) -> None:
        self.claude_dir.mkdir(parents=True, exist_ok=True)
        write_claude_settings(self.claude_dir, self.script)
        (self.claude_dir / "CLAUDE.md").write_text(render_claude_md({}, None))

    def session_context(self) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()

    def env_preamble(self) -> list[str]:
        return [f'export PATH="{tools_path(self.home)}:$PATH"']

    def write_env(self, lines: list[str]) -> bool:
        if not (env_file := os.environ.get("CLAUDE_ENV_FILE")):
            log("env: CLAUDE_ENV_FILE is not set, the session won't see the loaded variables")
            return False
        path = Path(env_file)
        existing = path.read_text() if path.exists() else ""
        path.touch(mode=0o600)
        path.chmod(0o600)  # before writing, so secrets never sit in a readable file
        path.write_text(replace_marked_block(existing, lines))
        return True

    def write_instructions(
        self,
        apps: dict[str, GitHubApp],
        identity: tuple[str, str] | None,
        *,
        resuming: bool,
    ) -> None:
        if apps or not resuming:
            (self.claude_dir / "CLAUDE.md").write_text(render_claude_md(apps, identity))

    def repo_dirs(self) -> list[Path]:
        if project := os.environ.get("CLAUDE_PROJECT_DIR"):
            return [Path(project)]
        return []
