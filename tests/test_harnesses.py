from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cloud_env.agy.auth import agy_broker_headers
from cloud_env.agy.harness import AgyHarness
from cloud_env.apps.github.tokens import GitHubApp
from cloud_env.apps.mise.mise import repo_mise_projects
from cloud_env.claude.auth import claude_broker_headers
from cloud_env.claude.harness import ClaudeHarness
from cloud_env.claude.pre_tool_use import cmd_pre_tool_use, has_footer, strip_footer


class HarnessTests(unittest.TestCase):
    def test_claude_harness_setup_env_and_ingress_sample(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            token_file = tmp / "ingress"
            header = base64.urlsafe_b64encode(b'{"kid":"k1"}').decode("ascii").rstrip("=")
            payload = (
                base64.urlsafe_b64encode(b'{"secret":"never-leak"}').decode("ascii").rstrip("=")
            )
            token_file.write_text(f"sk-ant-si-{header}.{payload}.sig123\n", encoding="utf-8")

            harness = ClaudeHarness(home=tmp)
            settings_path = harness.claude_dir / "settings.json"
            harness.claude_dir.mkdir(parents=True)
            settings_path.write_text(json.dumps({"customKey": True}), encoding="utf-8")

            harness.setup()
            harness.setup()  # idempotent hook & rule merge
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertTrue(settings["customKey"])
            self.assertEqual(len(settings["hooks"]["SessionStart"]), 1)
            self.assertEqual(len(settings["hooks"]["PreToolUse"]), 1)

            expected_digest = hashlib.sha256(f"{header}.{payload}".encode()).hexdigest()
            with mock.patch.dict(os.environ, {"CLAUDE_CODE_REMOTE_SESSION_ID": "cse_abc"}):
                self.assertEqual(
                    claude_broker_headers(token_file),
                    {
                        "X-Session-Id": "cse_abc",
                        "X-Ingress-Sig-Sample": f"k1.{expected_digest}.sig123",
                    },
                )

            env_file = tmp / "claude.env"
            self.assertIn(f"{tmp}/.local/bin", harness.env_preamble()[0])
            with mock.patch.dict(os.environ, {"CLAUDE_ENV_FILE": str(env_file)}):
                self.assertTrue(harness.write_env(["export FOO=bar"]))
            self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)
            self.assertIn("export FOO=bar", env_file.read_text(encoding="utf-8"))

            apps = {"dvcrn": GitHubApp("ghs_1", "boticorn[bot]", "1+bot@noreply")}
            harness.write_instructions(apps, ("David Mohl", "git@d.sh"), resuming=False)
            claude_md = (harness.claude_dir / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn("Co-authored-by: David Mohl <git@d.sh>", claude_md)
            self.assertIn("`dvcrn`", claude_md)

    def test_pre_tool_use_strips_mcp_and_denies_bash_or_writes(self) -> None:
        footer = "\n\n---\n🤖 Generated with [Claude Code](https://claude.com/claude-code)"
        self.assertEqual(strip_footer(f"Summary{footer}"), "Summary")
        self.assertEqual(strip_footer(f"Para 1{footer}\nPara 2"), "Para 1\n\nPara 2")
        self.assertFalse(has_footer({"old_string": f"x{footer}", "new_string": "clean"}))
        self.assertTrue(has_footer({"old_string": "x", "new_string": f"x{footer}"}))

        mcp_in = io.StringIO(
            json.dumps({"tool_name": "mcp__dpn__create_pr", "tool_input": {"body": f"Hi{footer}"}})
        )
        mcp_out = io.StringIO()
        with mock.patch("sys.stdin", mcp_in), mock.patch("sys.stdout", mcp_out):
            self.assertEqual(cmd_pre_tool_use(), 0)
        self.assertEqual(
            json.loads(mcp_out.getvalue())["hookSpecificOutput"]["updatedInput"],
            {"body": "Hi"},
        )

    def test_agy_harness_setup_bashrc_broker_lock_and_repos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            home = tmp / "home"
            shm = tmp / "shm"
            home.mkdir()
            shm.mkdir()

            (home / ".bashrc").write_text(
                "# header\ncase $- in\n    *i*) ;;\n      *) return;;\nesac\nalias ll='ls -l'\n",
                encoding="utf-8",
            )
            harness = AgyHarness(home=home, shm_dir=shm)
            harness.setup()

            bashrc = (home / ".bashrc").read_text(encoding="utf-8")
            self.assertNotIn("case $- in", bashrc)
            self.assertIn("alias ll='ls -l'", bashrc)
            self.assertIn(f"{home}/.local/bin", bashrc)
            self.assertIn("_CLOUD_ENV_LOADED=1", bashrc)
            self.assertTrue(harness.gemini_md.is_file())

            broker_token_file = home / ".gemini" / ".broker" / "token"
            broker_token_file.parent.mkdir(parents=True, exist_ok=True)
            broker_token_file.write_text(
                json.dumps({"CF-Access-Client-Id": "id1", "CF-Access-Client-Secret": "sec1"}),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"ANTIGRAVITY_CONVERSATION_ID": "conv-1"}):
                self.assertEqual(
                    agy_broker_headers(broker_token_file),
                    {
                        "X-Session-Id": "conv-1",
                        "CF-Access-Client-Id": "id1",
                        "CF-Access-Client-Secret": "sec1",
                    },
                )

            with self.assertRaises(RuntimeError), harness.session_context():
                raise RuntimeError("boom")
            self.assertTrue(harness.attempt_marker.exists())

            self.assertTrue(harness.write_env(["export LINEAR_API_KEY=lin"]))
            self.assertEqual(harness.env_file.stat().st_mode & 0o777, 0o600)

            repo_with_mise = home / "dvcrn" / "claude-env"
            (repo_with_mise / ".git").mkdir(parents=True)
            (repo_with_mise / "mise.toml").write_text("[tools]\n", encoding="utf-8")
            repo_without_mise = home / "dvcrn" / "plain"
            (repo_without_mise / ".git").mkdir(parents=True)

            self.assertEqual(harness.repo_dirs(), [repo_with_mise, repo_without_mise])
            self.assertEqual(repo_mise_projects(harness.repo_dirs()), [repo_with_mise])
