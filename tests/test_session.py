import json
import os
import shlex
import sys
import unittest
from unittest import mock

from helpers import TempDirTest, bootstrap, generate_signing_key, local_http_server, signed_script

APP = {"token": "ghs_acme", "name": "acme-bot[bot]", "email": "1+acme-bot[bot]@example.com"}
BROKER_ENV = {
    "OP_SERVICE_ACCOUNT_TOKEN": "ops_1",
    "OP_ENVIRONMENT_ID": "env_1",
    "TS_AUTHKEY": "tskey-1",
    "GITHUB_TOKEN": "ghp_minted",
    "GITHUB_APP_TOKENS": json.dumps({"acme": APP}),
}
ONEPASSWORD_ENV = {
    "API_KEY": "a b",
    "GIT_AUTHOR_NAME": "Ada",
    "GIT_AUTHOR_EMAIL": "ada@example.com",
    "GITHUB_TOKEN": "ghp_long_lived",
}
UNSET = "unset GH_TOKEN GITHUB_TOKEN"


class SelfUpdateTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self.key, public_key = generate_signing_key(self.tmp)
        install_dir = self.tmp / "install"
        install_dir.mkdir()
        self.script = install_dir / "bootstrap.py"
        self.installed = f"VERSION = {bootstrap.VERSION}\n"
        self.script.write_text(self.installed)
        allowed_signers = install_dir / "allowed_signers"
        allowed_signers.write_text(f"claude-env {public_key}\n")
        self.served: dict[str, bytes] = {}

        def respond(path: str, headers: dict[str, str]) -> tuple[int, bytes]:
            body = self.served.get(path)
            return (200, body) if body else (404, b"")

        url = self.enterContext(local_http_server(respond))
        self.patch(
            INSTALL_DIR=install_dir,
            SCRIPT=self.script,
            ALLOWED_SIGNERS=allowed_signers,
            BASE_URL=url,
        )
        self.patch_env(NO_PROXY="127.0.0.1")
        self.execv = self.enterContext(mock.patch.object(os, "execv"))

    def serve(self, version: int, signature: bytes | None = None) -> str:
        source = f"VERSION = {version}\n# {len(self.served)}\n"
        script, valid_signature = signed_script(self.key, self.tmp / f"v{len(self.served)}", source)
        self.served["/bootstrap.py"] = script
        self.served["/bootstrap.py.sig"] = signature or valid_signature
        return source

    def test_keeps_the_installed_copy_unless_a_newer_signed_one_is_served(self) -> None:
        bootstrap.self_update()
        self.serve(bootstrap.VERSION + 1, signature=b"not a signature")
        bootstrap.self_update()
        self.serve(bootstrap.VERSION - 1)
        bootstrap.self_update()

        self.assertEqual(self.script.read_text(), self.installed)
        self.execv.assert_not_called()

    def test_installs_a_newer_signed_copy_and_reruns_it_without_updating(self) -> None:
        source = self.serve(bootstrap.VERSION + 1)

        bootstrap.self_update()

        self.assertEqual(self.script.read_text(), source)
        self.assertEqual(
            self.script.with_name("bootstrap.py.sig").read_bytes(), self.served["/bootstrap.py.sig"]
        )
        rerun = [sys.executable, "-I", str(self.script), "session-start", "--no-update"]
        self.execv.assert_called_once_with(sys.executable, rerun)


class SessionStartTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self.marker = self.tmp / "initialized"
        self.env_file = self.tmp / "env.sh"
        self.claude_md = self.tmp / "CLAUDE.md"
        self.patch(
            ANTIGRAVITY=False,
            CLAUDE_DIR=self.tmp,
            INIT_MARKER=self.marker,
            trust_repo=mock.Mock(),
            self_update=mock.Mock(),
            fetch_broker_env=mock.Mock(return_value=dict(BROKER_ENV)),
            load_1password=mock.Mock(return_value=dict(ONEPASSWORD_ENV)),
            set_git_identity=mock.Mock(),
            use_github_directly=mock.Mock(return_value=[UNSET]),
            tailscale_needs_rejoin=mock.Mock(return_value=False),
            join_tailscale=mock.Mock(),
            install_repo_tools=mock.Mock(),
        )
        self.patch_env(CLAUDE_ENV_FILE=str(self.env_file))
        for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "TS_AUTHKEY"):
            os.environ.pop(name, None)

    def env_lines(self) -> list[str]:
        return self.env_file.read_text().splitlines()[1:-1]

    def test_first_start_loads_secrets_and_configures_the_session(self) -> None:
        bootstrap.session_start(update=True)

        bootstrap.self_update.assert_called_once_with()
        bootstrap.load_1password.assert_called_once_with("ops_1", "env_1")
        path = f'export PATH="{bootstrap.TOOLS_PATH}:$PATH"'
        self.assertEqual(self.env_lines(), [path, f"export API_KEY={shlex.quote('a b')}", UNSET])
        apps = {"acme": bootstrap.GitHubApp(**APP)}
        bootstrap.use_github_directly.assert_called_once_with(apps, "ghp_minted")
        bootstrap.set_git_identity.assert_called_once()
        self.assertEqual(os.environ["GIT_AUTHOR_NAME"], "Ada")
        self.assertEqual(os.environ["API_KEY"], "a b")
        self.assertNotIn("TS_AUTHKEY", os.environ)
        self.assertNotIn("GITHUB_APP_TOKENS", os.environ)
        self.assertIn("Co-authored-by: Ada <ada@example.com>", self.claude_md.read_text())
        bootstrap.join_tailscale.assert_called_once_with("tskey-1")
        bootstrap.install_repo_tools.assert_called_once_with()
        self.assertTrue(self.marker.exists())

    def test_without_the_broker_the_session_starts_without_secrets(self) -> None:
        bootstrap.fetch_broker_env.return_value = None

        bootstrap.session_start(update=False)

        bootstrap.self_update.assert_not_called()
        bootstrap.load_1password.assert_not_called()
        self.assertEqual(self.env_lines(), [f'export PATH="{bootstrap.TOOLS_PATH}:$PATH"', UNSET])
        bootstrap.use_github_directly.assert_called_once_with({}, None)
        bootstrap.join_tailscale.assert_called_once_with(None)
        self.assertEqual(self.claude_md.read_text(), bootstrap.render_claude_md({}, None))
        self.assertFalse(self.marker.exists())

    def test_failed_1password_keeps_the_broker_secrets_and_retries_next_start(self) -> None:
        bootstrap.load_1password.return_value = None

        bootstrap.session_start(update=False)

        bootstrap.use_github_directly.assert_called_once_with(mock.ANY, "ghp_minted")
        bootstrap.join_tailscale.assert_called_once_with("tskey-1")
        self.assertFalse(self.marker.exists())

    def test_resume_with_intact_state_does_nothing(self) -> None:
        self.marker.touch()

        bootstrap.session_start(update=True)

        bootstrap.trust_repo.assert_called_once_with()
        bootstrap.self_update.assert_not_called()
        bootstrap.fetch_broker_env.assert_not_called()
        self.assertFalse(self.env_file.exists())

    def test_resume_without_tailscaled_rejoins_and_keeps_the_env_on_missing_secrets(self) -> None:
        self.marker.touch()
        self.env_file.write_text("export FROM_FIRST_START=1\n")
        self.claude_md.write_text("from the first start")
        bootstrap.tailscale_needs_rejoin.return_value = True
        bootstrap.fetch_broker_env.return_value = None

        bootstrap.session_start(update=True)

        self.assertEqual(self.env_file.read_text(), "export FROM_FIRST_START=1\n")
        self.assertEqual(self.claude_md.read_text(), "from the first start")
        bootstrap.join_tailscale.assert_called_once_with(None)


class SetupTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self.patch(
            ANTIGRAVITY=False,
            CLAUDE_DIR=self.tmp / ".claude",
            install_mise=mock.Mock(),
            install_global_tools=mock.Mock(),
            install_self=mock.Mock(),
        )
        self.enterContext(mock.patch("platform.machine", return_value="x86_64"))
        self.enterContext(mock.patch("builtins.print"))

    def test_installs_the_tools_the_script_and_the_claude_code_config(self) -> None:
        self.patch_env(SIGNER="ssh-ed25519 AAAA", MIN_VERSION=str(bootstrap.VERSION))

        self.assertEqual(bootstrap.cmd_setup(), 0)

        bootstrap.install_self.assert_called_once_with("ssh-ed25519 AAAA")
        settings = json.loads((bootstrap.CLAUDE_DIR / "settings.json").read_text())
        self.assertEqual(set(settings["hooks"]), {"SessionStart", "PreToolUse"})
        claude_md = (bootstrap.CLAUDE_DIR / "CLAUDE.md").read_text()
        self.assertEqual(claude_md, bootstrap.render_claude_md({}, None))

    def test_refuses_to_run_below_the_stub_min_version(self) -> None:
        self.patch_env(SIGNER="ssh-ed25519 AAAA", MIN_VERSION=str(bootstrap.VERSION + 1))
        with self.assertRaises(SystemExit):
            bootstrap.cmd_setup()
        bootstrap.install_self.assert_not_called()


if __name__ == "__main__":
    unittest.main()
