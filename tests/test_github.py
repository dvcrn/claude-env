from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from cloud_env.apps.github.gh import gh_target_owner
from cloud_env.apps.github.git import cmd_git_credential, repo_owner, set_git_identity
from cloud_env.apps.github.routing import bypass_proxy_for_github, github_env_lines
from cloud_env.apps.github.tokens import (
    BOTICORN_EMAIL,
    BOTICORN_NAME,
    TOKEN_REFRESH_AFTER,
    TOKEN_RETRY_AFTER,
    GitHubApp,
    github_apps,
    github_token,
    refresh_github_tokens,
    store_github_tokens,
)


class GitHubTests(unittest.TestCase):
    def test_github_apps_parses_app_and_boticorn_formats(self) -> None:
        raw_apps = json.dumps(
            {
                "DVCRN": {"token": "ghs_1", "name": "app[bot]", "email": "1+app[bot]@noreply"},
                "bad/owner": {"token": "ghs_2", "name": "a", "email": "b"},
                "newline": {"token": "ghs_3\n", "name": "a", "email": "b"},
            }
        )
        apps = github_apps({"GITHUB_APP_TOKENS": raw_apps})
        self.assertEqual(
            apps,
            {"dvcrn": GitHubApp("ghs_1", "app[bot]", "1+app[bot]@noreply")},
        )

        boticorn = github_apps({"BOTICORN_TOKENS": json.dumps({"microfnhq": "ghs_bot"})})
        self.assertEqual(
            boticorn,
            {"microfnhq": GitHubApp("ghs_bot", BOTICORN_NAME, BOTICORN_EMAIL)},
        )

    def test_repo_owner_and_gh_target_owner(self) -> None:
        self.assertEqual(repo_owner("https://github.com/DVCRN/claude-env.git"), "dvcrn")
        self.assertEqual(repo_owner("git@github.com:microfnhq/fn.git"), "microfnhq")
        self.assertEqual(
            repo_owner("http://127.0.0.1:9999/git/digitalvibeshq/app"), "digitalvibeshq"
        )
        self.assertEqual(repo_owner("dvcrn/claude-env"), "dvcrn")
        self.assertIsNone(repo_owner("not-a-repo"))

        self.assertEqual(gh_target_owner(["pr", "view", "-R", "dvcrn/claude-env"]), "dvcrn")
        self.assertEqual(gh_target_owner(["issue", "list", "--repo=microfnhq/api"]), "microfnhq")
        self.assertEqual(
            gh_target_owner(["api", "/repos/DigitalVibesHQ/web/pulls/1/comments"]),
            "digitalvibeshq",
        )
        with mock.patch.dict(os.environ, {"GH_REPO": "dvcrn/other"}, clear=False):
            self.assertEqual(gh_target_owner(["pr", "checks"]), "dvcrn")

    def test_token_store_credential_helper_and_refresh_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            token_dir = tmp / "tokens"
            log_file = tmp / "session.log"
            apps = {"dvcrn": GitHubApp("ghs_dvcrn", BOTICORN_NAME, BOTICORN_EMAIL)}
            store_github_tokens(apps, "ghp_default", token_dir)

            self.assertEqual(github_token("dvcrn", token_dir, log_file), "ghs_dvcrn")
            self.assertEqual(github_token("other", token_dir, log_file), "ghp_default")

            stdin = io.StringIO("protocol=https\nhost=github.com\npath=dvcrn/claude-env.git\n\n")
            stdout = io.StringIO()
            with mock.patch("sys.stdin", stdin), mock.patch("sys.stdout", stdout):
                self.assertEqual(cmd_git_credential(["get"], token_dir), 0)
            self.assertEqual(stdout.getvalue(), "username=x-access-token\npassword=ghs_dvcrn\n")

            stamp = token_dir / "_stamp"
            expired = time.time() - TOKEN_REFRESH_AFTER - 10
            os.utime(stamp, (expired, expired))
            with mock.patch("cloud_env.apps.github.tokens.fetch_broker_env", return_value=None):
                refresh_github_tokens(token_dir)
            remaining = TOKEN_REFRESH_AFTER - (time.time() - stamp.stat().st_mtime)
            self.assertGreater(remaining, TOKEN_RETRY_AFTER - 30)
            self.assertEqual(github_token("dvcrn", token_dir, log_file), "ghs_dvcrn")

    def test_set_git_identity_and_routing_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            gitconfig = tmp / "gitconfig"
            identity_dir = tmp / "github-apps"
            legacy_file = tmp / "boticorn"
            env = {
                "GIT_CONFIG_GLOBAL": str(gitconfig),
                "GIT_AUTHOR_NAME": "David Mohl",
                "GIT_AUTHOR_EMAIL": "git@d.sh",
            }
            with mock.patch.dict(os.environ, env, clear=False):
                subprocess.run(
                    [
                        "git",
                        "config",
                        "--global",
                        "includeIf.hasconfig:remote.*.url:**/stale/**.path",
                        str(legacy_file),
                    ],
                    check=True,
                )
                apps = {"dvcrn": GitHubApp("ghs_1", BOTICORN_NAME, BOTICORN_EMAIL)}
                set_git_identity(apps, identity_dir, legacy_file)

                text = gitconfig.read_text(encoding="utf-8")
                self.assertIn("name = David Mohl", text)
                self.assertIn("gpgsign = false", text)
                self.assertNotIn("stale", text)
                self.assertIn(str(identity_dir / "dvcrn"), text)

            self.assertEqual(github_env_lines(bypass_proxy=False), ["unset GH_TOKEN GITHUB_TOKEN"])

            with mock.patch.dict(os.environ, {"NO_PROXY": "localhost"}, clear=False):
                bypass_proxy_for_github()
                self.assertIn("api.github.com", os.environ["NO_PROXY"])
            claude_lines = github_env_lines(bypass_proxy=True)
            self.assertEqual(len(claude_lines), 3)
            self.assertTrue(claude_lines[0].startswith("export NO_PROXY="))
            self.assertEqual(claude_lines[-1], "unset GH_TOKEN GITHUB_TOKEN")
