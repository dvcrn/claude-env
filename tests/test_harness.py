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
        harnesses = [
            ("claude", bootstrap.CLAUDE_INSTRUCTIONS),
            ("gemini", bootstrap.GEMINI_INSTRUCTIONS),
        ]
        cases = [
            ("apps", APPS, IDENTITY),
            ("apps_without_identity", APPS, None),
            ("without_apps", {}, None),
        ]
        for harness, instructions in harnesses:
            for case, apps, identity in cases:
                with self.subTest(harness=harness, case=case):
                    want = instructions.base + (GOLDEN / f"{harness}_{case}.md").read_text()
                    rendered = bootstrap.render_instructions(instructions, apps, identity)
                    self.assertEqual(rendered, want)


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


class ClaudeCodeTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self.harness = bootstrap.ClaudeCode()
        self.patch(CLAUDE_DIR=self.tmp)

    def test_replaces_its_block_in_the_env_file(self) -> None:
        env_file = self.tmp / "env.sh"
        env_file.write_text("export KEEP=1\n")
        self.patch_env(CLAUDE_ENV_FILE=str(env_file))

        self.assertTrue(self.harness.write_env(["export A=1"]))
        self.assertTrue(self.harness.write_env(["export A=2"]))

        self.assertEqual(
            env_file.read_text(), "export KEEP=1\n# >>> cloud-env\nexport A=2\n# <<< cloud-env\n"
        )
        self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)

    def test_without_env_file_reports_failure(self) -> None:
        with mock.patch.dict(os.environ):
            os.environ.pop("CLAUDE_ENV_FILE", None)
            self.assertFalse(self.harness.write_env(["export A=1"]))

    def test_resume_without_apps_keeps_the_instructions(self) -> None:
        claude_md = self.tmp / "CLAUDE.md"
        self.harness.write_instructions({}, None, resuming=False)
        self.assertEqual(
            claude_md.read_text(),
            bootstrap.render_instructions(bootstrap.CLAUDE_INSTRUCTIONS, {}, None),
        )
        self.harness.write_instructions(APPS, IDENTITY, resuming=True)
        with_apps = bootstrap.render_instructions(bootstrap.CLAUDE_INSTRUCTIONS, APPS, IDENTITY)
        self.assertEqual(claude_md.read_text(), with_apps)
        self.harness.write_instructions({}, None, resuming=True)
        self.assertEqual(claude_md.read_text(), with_apps)


class AntigravityTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self.harness = bootstrap.Antigravity()
        self.gemini_md = self.tmp / ".gemini" / "GEMINI.md"
        self.patch(
            HOME=self.tmp,
            HARNESS=self.harness,
            GEMINI_MD=self.gemini_md,
            ANTIGRAVITY_ENV_FILE=self.tmp / "cloud-env-session.sh",
            SESSION_LOCK=self.tmp / "session.lock",
            ATTEMPT_MARKER=self.tmp / "attempted",
        )

    def test_setup_writes_the_bashrc_block_and_the_first_instructions(self) -> None:
        self.harness.setup()
        self.assertIn("# >>> cloud-env\n", (self.tmp / ".bashrc").read_text())
        self.assertEqual(
            self.gemini_md.read_text(),
            bootstrap.render_instructions(bootstrap.GEMINI_INSTRUCTIONS, {}, None),
        )

        self.gemini_md.write_text("from another VM")
        self.harness.setup()
        self.assertEqual(self.gemini_md.read_text(), "from another VM")

    def test_instructions_are_only_replaced_with_apps(self) -> None:
        self.harness.write_instructions(APPS, IDENTITY, resuming=False)
        with_apps = bootstrap.render_instructions(bootstrap.GEMINI_INSTRUCTIONS, APPS, IDENTITY)
        self.assertEqual(self.gemini_md.read_text(), with_apps)
        self.harness.write_instructions({}, None, resuming=False)
        self.assertEqual(self.gemini_md.read_text(), with_apps)

    def test_writes_the_env_to_a_private_file(self) -> None:
        self.assertTrue(self.harness.write_env(["export A=1", "export B=2"]))
        env_file = bootstrap.ANTIGRAVITY_ENV_FILE
        self.assertEqual(env_file.read_text(), "export A=1\nexport B=2\n")
        self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)

    def test_session_marks_the_attempt_even_when_the_start_fails(self) -> None:
        with self.assertRaises(RuntimeError), self.harness.session():
            raise RuntimeError("boom")
        self.assertTrue(bootstrap.ATTEMPT_MARKER.exists())

    def test_sets_up_every_attached_repo_with_a_mise_config(self) -> None:
        with_mise = self.tmp / "acme" / "app"
        (with_mise / ".git").mkdir(parents=True)
        (with_mise / "mise.toml").write_text("[tools]\n")
        (self.tmp / "acme" / "plain" / ".git").mkdir(parents=True)
        with mock.patch.dict(os.environ):
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
            self.assertEqual(bootstrap.repo_mise_projects(), [with_mise])

    def test_skips_tailscale(self) -> None:
        with (
            mock.patch("shutil.which", return_value="/tools/tailscaled"),
            mock.patch.object(bootstrap, "tailscaled_running", return_value=True),
            mock.patch.object(bootstrap, "run") as run,
        ):
            bootstrap.join_tailscale("tskey-1")
        run.assert_not_called()

    def test_skips_repo_mise_install(self) -> None:
        with_mise = self.tmp / "acme" / "app"
        (with_mise / ".git").mkdir(parents=True)
        (with_mise / "mise.toml").write_text("[tools]\n")
        with mock.patch.object(bootstrap, "run") as run:
            bootstrap.install_repo_tools()
        run.assert_not_called()

    def test_writes_the_instructions_where_chmod_is_not_permitted(self) -> None:
        denied = PermissionError("EPERM")
        with (
            mock.patch("os.fchmod", side_effect=denied),
            mock.patch("os.chmod", side_effect=denied),
            mock.patch("pathlib.Path.chmod", side_effect=denied),
        ):
            bootstrap.write_gemini_md("rules")
        self.assertEqual(self.gemini_md.read_text(), "rules")


class RepoMiseProjectsTest(TempDirTest):
    def test_claude_code_sets_up_the_project_dir(self) -> None:
        (self.tmp / ".tool-versions").write_text("")
        self.patch(HARNESS=bootstrap.ClaudeCode())
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(self.tmp)}):
            self.assertEqual(bootstrap.repo_mise_projects(), [self.tmp])
            (self.tmp / ".tool-versions").unlink()
            self.assertEqual(bootstrap.repo_mise_projects(), [])
            del os.environ["CLAUDE_PROJECT_DIR"]
            self.assertEqual(bootstrap.repo_mise_projects(), [])


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
