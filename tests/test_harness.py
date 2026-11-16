import contextlib
import io
import json
import os
import unittest
from unittest import mock

from helpers import FOOTER, GOLDEN, TempDirTest, bootstrap

APPS = {
    "beta": bootstrap.GitHubApp("ghs_beta", "acme-bot[bot]", "1+acme-bot[bot]@example.com"),
    "acme": bootstrap.GitHubApp("ghs_acme", "acme-bot[bot]", "1+acme-bot[bot]@example.com"),
    "corp": bootstrap.GitHubApp("ghs_corp", "corp-bot[bot]", "2+corp-bot[bot]@example.com"),
}
IDENTITY = ("Ada Lovelace", "ada@example.com")


class InstructionsTest(unittest.TestCase):
    def test_github_identity_rules_follow_the_base_instructions(self) -> None:
        renderers = [
            ("claude", bootstrap.render_claude_md, bootstrap.CLAUDE_MD),
            ("gemini", bootstrap.render_gemini_md, bootstrap.GEMINI_BASE_MD),
        ]
        cases = [
            ("apps", APPS, IDENTITY),
            ("apps_without_identity", APPS, None),
            ("without_apps", {}, None),
        ]
        for harness, render, base in renderers:
            for case, apps, identity in cases:
                with self.subTest(harness=harness, case=case):
                    want = base + (GOLDEN / f"{harness}_{case}.md").read_text()
                    self.assertEqual(render(apps, identity), want)


class ClaudeSettingsTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self.patch(CLAUDE_DIR=self.tmp)
        self.path = self.tmp / "settings.json"

    def test_merges_into_the_image_settings_and_stays_idempotent(self) -> None:
        other_hook = {"hooks": [{"type": "command", "command": "/usr/bin/other"}]}
        image = {
            "model": "opus",
            "permissions": {"allow": ["Bash(ls:*)"], "deny": ["mcp__Linear"]},
            "hooks": {"SessionStart": [other_hook]},
        }
        self.path.write_text(json.dumps(image))

        bootstrap.write_claude_settings()
        once = self.path.read_text()
        bootstrap.write_claude_settings()
        self.assertEqual(self.path.read_text(), once)

        settings = json.loads(once)
        self.assertEqual(settings["model"], "opus")
        self.assertEqual(settings["attribution"], {"commit": "", "pr": "", "sessionUrl": False})
        self.assertEqual(
            settings["permissions"]["allow"], ["Bash(ls:*)", *bootstrap.ALLOWED_COMMANDS]
        )
        deny = settings["permissions"]["deny"]
        self.assertEqual(deny.count("mcp__Linear"), 1)
        self.assertIn("mcp__github__create_pull_request", deny)
        self.assertEqual(settings["autoMode"]["allow"], ["$defaults", bootstrap.AUTO_MODE_ALLOW])
        session_start = {
            "type": "command",
            "command": bootstrap.hook_command("session-start"),
            "timeout": 600,
        }
        self.assertEqual(
            settings["hooks"]["SessionStart"], [other_hook, {"hooks": [session_start]}]
        )
        (pre_tool_use,) = settings["hooks"]["PreToolUse"]
        self.assertEqual(pre_tool_use["matcher"], bootstrap.PRE_TOOL_USE_MATCHER)
        self.assertEqual(
            pre_tool_use["hooks"][0]["command"], bootstrap.hook_command("pre-tool-use")
        )

    def test_replaces_invalid_json(self) -> None:
        self.path.write_text("{not json")
        with contextlib.redirect_stderr(io.StringIO()):
            bootstrap.write_claude_settings()
        self.assertIn("SessionStart", json.loads(self.path.read_text())["hooks"])


class AntigravityBashrcTest(TempDirTest):
    def test_replaces_the_interactive_guard_and_its_own_block(self) -> None:
        self.patch(HOME=self.tmp)
        bashrc = self.tmp / ".bashrc"
        bashrc.write_text(
            "# header\ncase $- in\n    *i*) ;;\n      *) return;;\nesac\nalias ll='ls'\n"
        )

        bootstrap.write_antigravity_bashrc()
        once = bashrc.read_text()
        bootstrap.write_antigravity_bashrc()
        self.assertEqual(bashrc.read_text(), once)

        self.assertTrue(once.startswith("# header\nalias ll='ls'\n# >>> cloud-env\n"))
        self.assertIn(f"_CLOUD_ENV_RUNNING=1 {bootstrap.hook_command('session-start')}\n", once)
        self.assertIn(f"[ ! -e {bootstrap.ATTEMPT_MARKER} ]", once)
        self.assertIn(f"  . {bootstrap.ANTIGRAVITY_ENV_FILE}\n", once)


class EnvBlockTest(TempDirTest):
    def test_claude_code_replaces_its_block_in_the_env_file(self) -> None:
        env_file = self.tmp / "env.sh"
        env_file.write_text("export KEEP=1\n")
        self.patch(ANTIGRAVITY=False)
        self.patch_env(CLAUDE_ENV_FILE=str(env_file))

        self.assertTrue(bootstrap.write_env_block(["export A=1"]))
        self.assertTrue(bootstrap.write_env_block(["export A=2"]))

        self.assertEqual(
            env_file.read_text(), "export KEEP=1\n# >>> cloud-env\nexport A=2\n# <<< cloud-env\n"
        )
        self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)

    def test_claude_code_without_env_file_reports_failure(self) -> None:
        self.patch(ANTIGRAVITY=False)
        with mock.patch.dict(os.environ):
            os.environ.pop("CLAUDE_ENV_FILE", None)
            self.assertFalse(bootstrap.write_env_block(["export A=1"]))

    def test_antigravity_writes_a_private_file(self) -> None:
        env_file = self.tmp / "cloud-env-session.sh"
        self.patch(ANTIGRAVITY=True, ANTIGRAVITY_ENV_FILE=env_file)

        self.assertTrue(bootstrap.write_env_block(["export A=1", "export B=2"]))

        self.assertEqual(env_file.read_text(), "export A=1\nexport B=2\n")
        self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)


class PreToolUseTest(unittest.TestCase):
    def run_hook(self, stdin: str) -> dict | None:
        out = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(stdin)), contextlib.redirect_stdout(out):
            self.assertEqual(bootstrap.cmd_pre_tool_use(), 0)
        return json.loads(out.getvalue())["hookSpecificOutput"] if out.getvalue() else None

    def event(self, tool: str, **tool_input: object) -> str:
        return json.dumps({"tool_name": tool, "tool_input": tool_input})

    def test_strips_the_footer_from_mcp_input(self) -> None:
        output = self.run_hook(self.event("mcp__dpn__github_create_pr", body="Done." + FOOTER))
        self.assertEqual(output, {"hookEventName": "PreToolUse", "updatedInput": {"body": "Done."}})

    def test_denies_bash_and_file_writes_with_the_footer(self) -> None:
        for event in (
            self.event("Bash", command="gh pr create --body 'Done." + FOOTER + "'"),
            self.event("Edit", old_string="x", new_string="x" + FOOTER),
        ):
            with self.subTest(event=event):
                self.assertEqual(self.run_hook(event)["permissionDecision"], "deny")

    def test_passes_everything_else_through(self) -> None:
        for stdin in (
            self.event("mcp__dpn__github_create_pr", body="Done."),
            self.event("Bash", command="ls"),
            self.event("Edit", old_string="x" + FOOTER, new_string="x"),
            "not json",
            json.dumps({"tool_name": "Bash"}),
            json.dumps({"tool_name": "Bash", "tool_input": "ls"}),
        ):
            with self.subTest(stdin=stdin):
                self.assertIsNone(self.run_hook(stdin))


if __name__ == "__main__":
    unittest.main()
