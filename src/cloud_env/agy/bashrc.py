from __future__ import annotations

import re
import shlex
from pathlib import Path

from cloud_env.config import tools_path
from cloud_env.lib.fs import replace_marked_block
from cloud_env.lib.log import hook_command


# Antigravity runs every command in a non-interactive bash shell with BASH_ENV=~/.bashrc
def write_antigravity_bashrc(
    home: Path,
    script: Path,
    attempt_marker: Path,
    env_file_path: Path,
) -> None:
    path = home / ".bashrc"
    existing = path.read_text() if path.exists() else ""
    # Debian's default non-interactive early return would stop BASH_ENV before our block
    existing = re.sub(r"(?ms)^case \$- in\n.*?^esac\n?", "", existing)
    bins = tools_path(home)
    attempted = shlex.quote(str(attempt_marker))
    env_file = shlex.quote(str(env_file_path))
    lines = [
        f'case ":$PATH:" in *":{bins}:"*) ;; *) export PATH="{bins}:$PATH" ;; esac',
        f'if [ -z "${{_CLOUD_ENV_RUNNING:-}}" ] && [ ! -e {attempted} ]; then',
        f"  _CLOUD_ENV_RUNNING=1 {hook_command('session-start', script)}",
        "fi",
        # Subshells inherit the loaded env; re-sourcing would clobber per-command overrides
        f'if [ -z "${{_CLOUD_ENV_LOADED:-}}" ] && [ -r {env_file} ]; then',
        "  export _CLOUD_ENV_LOADED=1",
        f"  . {env_file}",
        "fi",
    ]
    path.write_text(replace_marked_block(existing, lines))
